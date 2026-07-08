"""
tts_service - 进度朗读的共享 TTS 编排层

职责：
1. 从 Claude transcript 里抽取“给人看的 assistant 文本”
2. 按配置调用本地 IndexTTS 或云端 TTS
3. 缓存 MP3，并生成 ESP32 可访问的 audio_url
4. 为 Flask 层提供异步队列和同步合成入口
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("tts_service")

SPEECH_KIND_PROGRESS = "assistant_progress"
DEFAULT_TAIL_BYTES = 256 * 1024
DEFAULT_MAX_CHARS = 300
DEFAULT_MIN_CHARS = 6
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:5000"
DEFAULT_DOUBAO_API_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_SPEECH_LOOKBACK_SECONDS = 180
DEFAULT_SPEECH_FORWARD_SECONDS = 8

INTERNAL_TEXT_PREFIXES = (
    "Caveat:",
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)

HOOK_FALLBACK_EVENTS = {"Notification", "Elicitation"}

SENTENCE_END_RE = re.compile(r"[。！？；!?;]\s*$")
SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
LEADING_MARK_RE = re.compile(r"^[\s>*•·\-—]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def safe_text(value: object, limit: int = 0) -> str:
    text = str(value or "").strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit]
    return text


def load_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "1" if default else "0")
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def load_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def load_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def normalize_base_url(value: str) -> str:
    text = safe_text(value)
    return text.rstrip("/")


def guess_lan_ip() -> str:
    """尽量猜出本机在局域网内的可访问地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith(("127.", "0.", "169.254.")):
                return ip
    except OSError:
        pass

    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith(("127.", "0.", "169.254.")):
            return host_ip
    except OSError:
        pass

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
        for info in infos:
            ip = safe_text(info[4][0])
            if ip and not ip.startswith(("127.", "0.", "169.254.")):
                return ip
    except OSError:
        pass

    return "127.0.0.1"


def resolve_public_base_url(role: str = "local") -> str:
    """解析 ESP32 可访问的 TTS 公网基址。"""
    env_value = normalize_base_url(os.environ.get("TTS_PUBLIC_BASE_URL", ""))
    if env_value:
        return env_value

    host_ip = guess_lan_ip()
    if host_ip.startswith(("127.", "0.", "169.254.")):
        logger.warning("TTS_PUBLIC_BASE_URL 未配置，当前只能猜到回环地址 %s", host_ip)
    return f"http://{host_ip}:5000"


def _default_mock_audio_path() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / "spiffs" / "task_done.mp3",
        repo_root / "spiffs" / "permission_wait.mp3",
        repo_root / "spiffs" / "action_confirm.mp3",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[0])


def _infer_doubao_resource_id(speaker: str) -> str:
    speaker = safe_text(speaker)
    if speaker.startswith("S_"):
        return "seed-icl-1.0"
    if "uranus" in speaker:
        return "seed-tts-2.0"
    return "seed-tts-1.0"


def extract_text_blocks(content) -> str:
    """从 Claude JSONL 的 message.content 里提取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return ""


def normalize_speech_text(text: str) -> str:
    text = safe_text(text)
    if not text:
        return ""

    text = TAG_RE.sub("", text)
    text = LEADING_MARK_RE.sub("", text)
    text = SPACE_RE.sub(" ", text).strip()
    text = text.replace("```", "")

    return text


def is_internal_speech_text(text: str) -> bool:
    if not text:
        return True
    for prefix in INTERNAL_TEXT_PREFIXES:
        if text.startswith(prefix):
            return True
    if text.startswith("<") and text.endswith(">"):
        return True
    if text.startswith("[") and "]" in text[:24]:
        # 大概率是工具名或内部标签，不适合读出来。
        return True
    return False


def extract_hook_fallback_text(payload: dict, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """在 transcript 不可用时，从 hook 的可读文案里兜底抽取一句话。"""
    if not isinstance(payload, dict):
        return ""

    hook_event = safe_text(payload.get("hook_event_name", ""))
    if hook_event not in HOOK_FALLBACK_EVENTS:
        return ""

    latest = payload.get("latest_message") or {}
    candidates = [
        payload.get("hook_message", ""),
        latest.get("text", "") if isinstance(latest, dict) else "",
        latest.get("hook_message", "") if isinstance(latest, dict) else "",
    ]

    for candidate in candidates:
        text = normalize_speech_text(candidate)
        if not text or len(text) < DEFAULT_MIN_CHARS:
            continue
        if is_internal_speech_text(text):
            continue
        return trim_speech_text(text, max_chars=max_chars)

    return ""


def trim_speech_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    text = normalize_speech_text(text)
    if not text:
        return ""

    if len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    for mark in ("。", "！", "？", "；", "，", ",", ".", "!", "?", ";"):
        pos = cut.rfind(mark)
        if pos >= max_chars // 2:
            return cut[: pos + 1]
    return cut.rstrip() + "..."


def read_tail_lines(path: str, max_bytes: int = DEFAULT_TAIL_BYTES) -> list[str]:
    if not path or not os.path.isfile(path):
        return []

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            data = f.read()
    except OSError:
        return []

    return data.decode("utf-8", errors="ignore").splitlines()


def build_transcript_source_id(entry: dict, text: str) -> str:
    """用 assistant 消息 UUID 构造稳定来源 ID，避免重复朗读同一红框文本。"""
    msg = entry.get("message") or {}
    entry_id = safe_text(entry.get("uuid", "") or msg.get("id", ""))
    entry_ts = safe_text(entry.get("timestamp", ""))
    text_hash = hashlib.sha1(normalize_speech_text(text).encode("utf-8", errors="ignore")).hexdigest()[:12]
    anchor = entry_id or entry_ts or text_hash
    return f"transcript:{anchor}:{text_hash}"


def extract_speech_candidate_from_transcript(
    path: str,
    updated_at=None,
    since_seq=None,
    max_chars: int = DEFAULT_MAX_CHARS,
    lookback_seconds: int = DEFAULT_SPEECH_LOOKBACK_SECONDS,
) -> Optional[dict]:
    """从 transcript 里找 hook 附近最新一条可朗读的 assistant 可见文本。"""
    updated_dt = parse_iso(updated_at)
    min_dt = updated_dt - timedelta(seconds=lookback_seconds) if updated_dt else None
    max_dt = updated_dt + timedelta(seconds=DEFAULT_SPEECH_FORWARD_SECONDS) if updated_dt else None
    try:
        since_seq_int = int(since_seq) if since_seq is not None and safe_text(since_seq) != "" else None
    except (TypeError, ValueError):
        since_seq_int = None

    candidate = None
    for line in read_tail_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("type") != "assistant":
            continue

        entry_dt = parse_iso(entry.get("timestamp") or entry.get("updated_at") or entry.get("created_at"))
        entry_seq = entry.get("seq")
        try:
            entry_seq_int = int(entry_seq) if entry_seq is not None and safe_text(entry_seq) != "" else None
        except (TypeError, ValueError):
            entry_seq_int = None

        if entry_dt and min_dt and entry_dt < min_dt:
            continue
        if entry_dt and max_dt and entry_dt > max_dt:
            continue
        if since_seq_int is not None and entry_seq_int is not None and entry_seq_int < since_seq_int:
            continue

        msg = entry.get("message") or {}
        text = normalize_speech_text(extract_text_blocks(msg.get("content", "")))
        if not text or len(text) < DEFAULT_MIN_CHARS:
            continue
        if is_internal_speech_text(text):
            continue
        text = trim_speech_text(text, max_chars=max_chars)
        candidate = {
            "kind": SPEECH_KIND_PROGRESS,
            "source": "transcript",
            "source_id": build_transcript_source_id(entry, text),
            "text": text,
            "transcript_ts": safe_text(entry.get("timestamp", "")),
        }

    return candidate


def extract_speech_text_from_transcript(
    path: str,
    updated_at=None,
    since_seq=None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """从 transcript 里找最近一条适合朗读的 assistant 文本。"""
    candidate = extract_speech_candidate_from_transcript(
        path,
        updated_at=updated_at,
        since_seq=since_seq,
        max_chars=max_chars,
    )
    return safe_text((candidate or {}).get("text", ""))


def extract_speech_candidate_from_payload(payload: dict, max_chars: int = DEFAULT_MAX_CHARS) -> Optional[dict]:
    """从 hook payload 中提取唯一允许 TTS 的红框文本候选。"""
    if not isinstance(payload, dict):
        return None

    candidate = payload.get("speech_candidate")
    if isinstance(candidate, dict):
        text = trim_speech_text(candidate.get("text", ""), max_chars=max_chars)
        if text and len(text) >= DEFAULT_MIN_CHARS and not is_internal_speech_text(text):
            return {
                "kind": safe_text(candidate.get("kind", SPEECH_KIND_PROGRESS)) or SPEECH_KIND_PROGRESS,
                "source": safe_text(candidate.get("source", "payload")) or "payload",
                "source_id": safe_text(candidate.get("source_id", "")),
                "text": text,
                "transcript_ts": safe_text(candidate.get("transcript_ts", "")),
            }

    if payload.get("source") != "claude-hook":
        return None

    if payload.get("status") not in {"thinking", "cooking", "marinating"}:
        return None

    transcript_path = safe_text(payload.get("transcript_path", ""))
    if not transcript_path:
        fallback = extract_hook_fallback_text(payload, max_chars=max_chars)
        if fallback:
            return {"kind": SPEECH_KIND_PROGRESS, "source": "hook_fallback", "source_id": "", "text": fallback}
        return None

    transcript_candidate = extract_speech_candidate_from_transcript(
        transcript_path,
        updated_at=payload.get("updated_at"),
        since_seq=payload.get("seq"),
        max_chars=max_chars,
    )
    if transcript_candidate:
        return transcript_candidate

    fallback = extract_hook_fallback_text(payload, max_chars=max_chars)
    if fallback:
        return {"kind": SPEECH_KIND_PROGRESS, "source": "hook_fallback", "source_id": "", "text": fallback}
    return None


def extract_speech_text_from_payload(payload: dict, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """优先依赖 speech_candidate / transcript，只返回允许朗读的文本。"""
    candidate = extract_speech_candidate_from_payload(payload, max_chars=max_chars)
    return safe_text((candidate or {}).get("text", ""))


def build_speech_id(payload: dict, text: str, provider: str, voice: str, sample_rate: int) -> str:
    """用会话、序号和文本做稳定去重。"""
    session_id = safe_text((payload or {}).get("session_id", ""))
    seq = safe_text((payload or {}).get("seq", 0))
    candidate = (payload or {}).get("speech_candidate") if isinstance(payload, dict) else {}
    source_id = safe_text((payload or {}).get("speech_source_id", ""))
    if not source_id and isinstance(candidate, dict):
        source_id = safe_text(candidate.get("source_id", ""))
    if source_id:
        material = "\0".join([
            session_id,
            source_id,
            normalize_speech_text(text),
        ])
        return hashlib.sha1(material.encode("utf-8", errors="ignore")).hexdigest()[:20]

    material = "\0".join([
        session_id,
        seq,
        normalize_speech_text(text),
    ])
    return hashlib.sha1(material.encode("utf-8", errors="ignore")).hexdigest()[:20]


def build_speech_payload(
    payload: dict,
    text: str,
    public_base_url: str,
    provider: str,
    voice: str,
    sample_rate: int,
    audio_format: str = "mp3",
) -> dict:
    speech_id = build_speech_id(payload, text, provider, voice, sample_rate)
    base_url = normalize_base_url(public_base_url or resolve_public_base_url())
    audio_url = f"{base_url}/api/tts/audio/{speech_id}.{audio_format}"
    candidate = (payload or {}).get("speech_candidate") if isinstance(payload, dict) else {}
    source_id = safe_text((payload or {}).get("speech_source_id", ""))
    if not source_id and isinstance(candidate, dict):
        source_id = safe_text(candidate.get("source_id", ""))

    return {
        "id": speech_id,
        "kind": SPEECH_KIND_PROGRESS,
        "text": trim_speech_text(text),
        "audio_url": audio_url,
        "format": audio_format,
        "sample_rate": int(sample_rate),
        "created_at": now_iso(),
        "provider": provider,
        "voice": voice,
        "seq": int((payload or {}).get("seq", 0) or 0),
        "session_id": safe_text((payload or {}).get("session_id", "")),
        "source_id": source_id,
    }


def _read_response_audio(resp) -> bytes:
    body = resp.read()
    content_type = safe_text(resp.headers.get("Content-Type", "")).lower()

    if "application/json" in content_type or body.lstrip().startswith((b"{", b"[")):
        try:
            data = json.loads(body.decode("utf-8", errors="ignore"))
        except Exception as exc:
            raise RuntimeError(f"tts json decode failed: {exc}") from exc

        if isinstance(data, dict):
            for key in ("audio_base64", "audio", "data"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    try:
                        return base64.b64decode(value)
                    except Exception:
                        pass

            audio_url = safe_text(data.get("audio_url", ""))
            if audio_url:
                with urllib.request.urlopen(audio_url, timeout=30) as audio_resp:
                    return audio_resp.read()

            nested = data.get("result") or data.get("response")
            if isinstance(nested, dict):
                for key in ("audio_base64", "audio", "data"):
                    value = nested.get(key)
                    if isinstance(value, str) and value.strip():
                        try:
                            return base64.b64decode(value)
                        except Exception:
                            pass

        raise RuntimeError("tts response does not contain audio")

    return body


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 30.0) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "whitebox-tts/1",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not (200 <= getattr(resp, "status", 200) < 300):
            body = resp.read(256)
            raise RuntimeError(f"tts http {resp.status}: {safe_text(body, 120)}")
        return _read_response_audio(resp)


def _http_post_text_or_audio(url: str, payload: dict, headers: dict | None = None, timeout: float = 30.0) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "whitebox-tts/1",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not (200 <= getattr(resp, "status", 200) < 300):
            body = resp.read(256)
            raise RuntimeError(f"tts http {resp.status}: {safe_text(body, 120)}")
        return _read_response_audio(resp)


def _http_post_doubao(url: str, payload: dict, headers: dict | None = None, timeout: float = 60.0) -> bytes:
    """调用豆包流式 TTS 接口并收集音频分片。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "whitebox-tts/1",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    audio_chunks: list[bytes] = []

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not (200 <= getattr(resp, "status", 200) < 300):
            body = resp.read(256)
            raise RuntimeError(f"doubao tts http {resp.status}: {safe_text(body, 120)}")

        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            try:
                data_obj = json.loads(line.decode("utf-8", errors="ignore"))
            except Exception as exc:
                logger.debug("Doubao TTS line decode failed: %s", exc)
                continue

            code = int(data_obj.get("code", 0) or 0)
            message = safe_text(data_obj.get("message", ""))

            if code == 20000000:
                break

            if code not in {0, 20000000}:
                raise RuntimeError(f"doubao tts code {code}: {message or 'unknown error'}")

            audio_data = data_obj.get("data")
            if isinstance(audio_data, str) and audio_data.strip():
                try:
                    audio_chunks.append(base64.b64decode(audio_data))
                except Exception as exc:
                    logger.debug("Doubao TTS audio decode failed: %s", exc)

    if not audio_chunks:
        raise RuntimeError("doubao tts response does not contain audio")

    return b"".join(audio_chunks)


def _http_post_mimo(url: str, payload: dict, headers: dict | None = None, timeout: float = 60.0) -> bytes:
    """调用小米 MiMo TTS（通过 chat completions 接口），返回 WAV 音频。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "whitebox-tts/1",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not (200 <= getattr(resp, "status", 200) < 300):
            body = resp.read(256)
            raise RuntimeError(f"mimo tts http {resp.status}: {safe_text(body, 120)}")
        body = resp.read()

    result = json.loads(body.decode("utf-8", errors="ignore"))
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("mimo tts: empty choices")

    message = choices[0].get("message") or {}
    audio_obj = message.get("audio") or {}
    audio_data = safe_text(audio_obj.get("data", ""))
    if not audio_data:
        raise RuntimeError("mimo tts: no audio data in response")

    return base64.b64decode(audio_data)


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    """将 WAV 音频转换为 MP3，减小体积以便下发给 ESP32。"""
    import lameenc

    # 解析 WAV header
    if len(wav_bytes) < 44 or wav_bytes[:4] != b'RIFF':
        return wav_bytes

    import struct
    fmt_pos = wav_bytes.find(b'fmt ')
    if fmt_pos < 0:
        return wav_bytes

    channels = struct.unpack('<H', wav_bytes[fmt_pos+10:fmt_pos+12])[0]
    sample_rate = struct.unpack('<I', wav_bytes[fmt_pos+12:fmt_pos+16])[0]
    bits_per_sample = struct.unpack('<H', wav_bytes[fmt_pos+22:fmt_pos+24])[0]

    # 找到 data chunk
    data_pos = wav_bytes.find(b'data', fmt_pos)
    if data_pos < 0:
        return wav_bytes
    data_size = struct.unpack('<I', wav_bytes[data_pos+4:data_pos+8])[0]
    pcm_data = wav_bytes[data_pos+8:data_pos+8+data_size]

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(128)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(channels)
    encoder.set_quality(5)

    mp3_data = encoder.encode(pcm_data)
    mp3_data += encoder.flush()
    return mp3_data


def _http_get_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "whitebox-tts/1")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8", errors="ignore"))


def _http_get_binary(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "whitebox-tts/1")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_post_form(url: str, fields: dict, timeout: float = 30.0) -> dict:
    """以 multipart/form-data 提交表单。"""
    boundary = "----WhiteboxTTSFormBoundary"
    body_parts = []
    for key, value in fields.items():
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8"))
    body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    data = b"".join(body_parts)

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("User-Agent", "whitebox-tts/1")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8", errors="ignore"))


def _indextts_async_synth(base_url: str, text: str, voice: str, timeout: float = 120.0) -> bytes:
    """异步 IndexTTS2 合成：提交 → 轮询 → 下载 WAV。"""
    submit_url = f"{base_url}/tts"
    submit_data = _http_post_form(submit_url, {"text": text, "voice": voice}, timeout=30.0)

    task_id = safe_text(submit_data.get("task_id", ""))
    if not task_id:
        raise RuntimeError(f"indextts submit failed: {submit_data}")

    poll_url = f"{base_url}/tasks/{task_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        status_data = _http_get_json(poll_url, timeout=10.0)
        task_status = safe_text(status_data.get("status", ""))
        if task_status == "completed":
            download_url = f"{base_url}/tasks/{task_id}/result"
            return _http_get_binary(download_url, timeout=30.0)
        if task_status == "failed":
            err = safe_text(status_data.get("error", "unknown"))
            raise RuntimeError(f"indextts task failed: {err}")
        time.sleep(0.5)

    raise RuntimeError(f"indextts task {task_id} timed out after {timeout}s")


def _proxy_tts_synth(proxy_url: str, text: str, voice: str, base_url: str, timeout: float = 120.0) -> bytes:
    """通过 cc-dashboard 代理合成 TTS。代理端点返回 base64 编码的音频。"""
    payload = {"text": text, "voice": voice}
    if base_url:
        payload["base_url"] = base_url
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(proxy_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", "whitebox-tts/1")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    result = json.loads(body.decode("utf-8", errors="ignore"))
    if not result.get("ok"):
        raise RuntimeError(f"proxy tts failed: {result.get('error', 'unknown')}")
    audio_b64 = result.get("audio_base64", "")
    if not audio_b64:
        raise RuntimeError("proxy tts returned empty audio")
    return base64.b64decode(audio_b64)


@dataclass
class TTSSettings:
    role: str = "local"
    orchestrator: str = "off"
    provider: str = "local_indextts"
    enabled: bool = False
    public_base_url: str = ""
    cache_dir: str = ""
    voice: str = ""
    sample_rate: int = DEFAULT_SAMPLE_RATE
    max_chars: int = DEFAULT_MAX_CHARS
    min_chars: int = DEFAULT_MIN_CHARS
    http_timeout: float = 30.0
    local_base_url: str = "http://127.0.0.1:9877"
    local_path: str = "/tts"
    local_voice: str = "test_voice.wav"
    proxy_url: str = ""
    cloud_base_url: str = "https://api.openai.com"
    cloud_path: str = "/v1/audio/speech"
    cloud_model: str = ""
    cloud_api_key: str = ""
    cloud_voice: str = ""
    cloud_response_format: str = "mp3"
    cloud_speed: float = 1.0
    doubao_api_url: str = DEFAULT_DOUBAO_API_URL
    doubao_app_id: str = ""
    doubao_access_key: str = ""
    doubao_speaker: str = ""
    doubao_resource_id: str = ""
    doubao_speech_rate: int = 0
    doubao_loudness_rate: int = 0
    doubao_pitch: int = 0
    doubao_emotion: str = ""
    doubao_emotion_scale: int = 0
    mimo_base_url: str = "https://api.xiaomimimo.com"
    mimo_api_key: str = ""
    mimo_model: str = "mimo-v2.5-tts"
    mimo_voice: str = ""
    mock_audio_path: str = ""

    @property
    def cache_path(self) -> Path:
        return Path(self.cache_dir or ".") / "tts_cache"


def load_settings(role: str, cache_dir: str) -> TTSSettings:
    orchestrator = safe_text(os.environ.get("TTS_ORCHESTRATOR", "off")).lower()
    provider = safe_text(os.environ.get("TTS_PROVIDER", "local_indextts")).lower()
    enabled = load_bool("TTS_ENABLED", default=True)

    return TTSSettings(
        role=safe_text(role) or "local",
        orchestrator=orchestrator or "off",
        provider=provider or "local_indextts",
        enabled=enabled,
        public_base_url=resolve_public_base_url(role),
        cache_dir=safe_text(cache_dir),
        voice=safe_text(os.environ.get("TTS_VOICE", "")),
        sample_rate=load_int("TTS_SAMPLE_RATE", DEFAULT_SAMPLE_RATE),
        max_chars=load_int("TTS_MAX_TEXT_CHARS", DEFAULT_MAX_CHARS),
        min_chars=load_int("TTS_MIN_TEXT_CHARS", DEFAULT_MIN_CHARS),
        http_timeout=load_float("TTS_HTTP_TIMEOUT", 30.0),
        local_base_url=normalize_base_url(os.environ.get("TTS_LOCAL_BASE_URL", "http://127.0.0.1:9877")),
        local_path=safe_text(os.environ.get("TTS_LOCAL_PATH", "/tts")) or "/tts",
        local_voice=safe_text(os.environ.get("TTS_LOCAL_VOICE", "test_voice.wav")) or "test_voice.wav",
        proxy_url=safe_text(os.environ.get("TTS_PROXY_URL", "")),
        cloud_base_url=normalize_base_url(os.environ.get("TTS_CLOUD_BASE_URL", "https://api.openai.com")),
        cloud_path=safe_text(os.environ.get("TTS_CLOUD_PATH", "/v1/audio/speech")) or "/v1/audio/speech",
        cloud_model=safe_text(os.environ.get("TTS_CLOUD_MODEL", "")),
        cloud_api_key=safe_text(os.environ.get("TTS_API_KEY", "")),
        cloud_voice=safe_text(os.environ.get("TTS_CLOUD_VOICE", "")),
        cloud_response_format=safe_text(os.environ.get("TTS_CLOUD_RESPONSE_FORMAT", "mp3")) or "mp3",
        cloud_speed=load_float("TTS_CLOUD_SPEED", 1.0),
        doubao_api_url=normalize_base_url(os.environ.get("DOUBAO_API_URL", DEFAULT_DOUBAO_API_URL)) or DEFAULT_DOUBAO_API_URL,
        doubao_app_id=safe_text(os.environ.get("DOUBAO_APP_ID", "")),
        doubao_access_key=safe_text(os.environ.get("DOUBAO_ACCESS_KEY", "")),
        doubao_speaker=safe_text(os.environ.get("DOUBAO_SPEAKER", "")),
        doubao_resource_id=safe_text(os.environ.get("DOUBAO_RESOURCE_ID", "")),
        doubao_speech_rate=load_int("DOUBAO_SPEECH_RATE", 0),
        doubao_loudness_rate=load_int("DOUBAO_LOUDNESS_RATE", 0),
        doubao_pitch=load_int("DOUBAO_PITCH", 0),
        doubao_emotion=safe_text(os.environ.get("DOUBAO_EMOTION", "")),
        doubao_emotion_scale=load_int("DOUBAO_EMOTION_SCALE", 0),
        mimo_base_url=normalize_base_url(os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com")),
        mimo_api_key=safe_text(os.environ.get("MIMO_API_KEY", "")),
        mimo_model=safe_text(os.environ.get("MIMO_MODEL", "mimo-v2.5-tts")) or "mimo-v2.5-tts",
        mimo_voice=safe_text(os.environ.get("MIMO_VOICE", "")),
        mock_audio_path=safe_text(os.environ.get("TTS_MOCK_AUDIO_PATH", _default_mock_audio_path())),
    )


# env var name → (TTSSettings field name, type)
SETTINGS_KEYS = {
    "TTS_ENABLED": ("enabled", bool),
    "TTS_ORCHESTRATOR": ("orchestrator", str),
    "TTS_PROVIDER": ("provider", str),
    "TTS_VOICE": ("voice", str),
    "TTS_SAMPLE_RATE": ("sample_rate", int),
    "TTS_MAX_TEXT_CHARS": ("max_chars", int),
    "TTS_MIN_TEXT_CHARS": ("min_chars", int),
    "TTS_HTTP_TIMEOUT": ("http_timeout", float),
    "TTS_LOCAL_BASE_URL": ("local_base_url", str),
    "TTS_LOCAL_PATH": ("local_path", str),
    "TTS_LOCAL_VOICE": ("local_voice", str),
    "TTS_PROXY_URL": ("proxy_url", str),
    "TTS_CLOUD_BASE_URL": ("cloud_base_url", str),
    "TTS_CLOUD_PATH": ("cloud_path", str),
    "TTS_CLOUD_MODEL": ("cloud_model", str),
    "TTS_API_KEY": ("cloud_api_key", str),
    "TTS_CLOUD_VOICE": ("cloud_voice", str),
    "TTS_CLOUD_RESPONSE_FORMAT": ("cloud_response_format", str),
    "TTS_CLOUD_SPEED": ("cloud_speed", float),
    "DOUBAO_API_URL": ("doubao_api_url", str),
    "DOUBAO_APP_ID": ("doubao_app_id", str),
    "DOUBAO_ACCESS_KEY": ("doubao_access_key", str),
    "DOUBAO_SPEAKER": ("doubao_speaker", str),
    "DOUBAO_RESOURCE_ID": ("doubao_resource_id", str),
    "DOUBAO_SPEECH_RATE": ("doubao_speech_rate", int),
    "DOUBAO_LOUDNESS_RATE": ("doubao_loudness_rate", int),
    "DOUBAO_PITCH": ("doubao_pitch", int),
    "DOUBAO_EMOTION": ("doubao_emotion", str),
    "DOUBAO_EMOTION_SCALE": ("doubao_emotion_scale", int),
    "MIMO_BASE_URL": ("mimo_base_url", str),
    "MIMO_API_KEY": ("mimo_api_key", str),
    "MIMO_MODEL": ("mimo_model", str),
    "MIMO_VOICE": ("mimo_voice", str),
    "TTS_PUBLIC_BASE_URL": ("public_base_url", str),
    "TTS_MOCK_AUDIO_PATH": ("mock_audio_path", str),
}

SENSITIVE_KEYS = {"TTS_API_KEY", "DOUBAO_ACCESS_KEY", "MIMO_API_KEY"}


def settings_to_dict(s: TTSSettings) -> dict:
    """将 TTSSettings 序列化为 dict，敏感字段脱敏。"""
    result = {}
    for env_key, (field, typ) in SETTINGS_KEYS.items():
        val = getattr(s, field, "")
        if env_key in SENSITIVE_KEYS and val:
            val = str(val)[:4] + "***"
        result[env_key] = val
    return result


class TTSOrchestrator:
    """负责抽取文本、调用后端和缓存音频。"""

    def __init__(
        self,
        settings: TTSSettings,
        *,
        role: str,
        on_ready: Optional[Callable[[dict, dict], None]] = None,
    ):
        self.settings = settings
        self.role = safe_text(role) or "local"
        self._on_ready = on_ready
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=32)
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._last_error = ""

        self.settings.cache_path.mkdir(parents=True, exist_ok=True)

        # MQTT TTS 支持（由 app.py 在启动后注入 mqtt_client）
        self.mqtt_client = None
        self.mqtt_topic_prefix = os.environ.get("TOPIC_PREFIX", "whitebox")
        self._mqtt_pending: dict[str, dict] = {}
        self._mqtt_lock = threading.Lock()

    def set_mqtt_client(self, client, topic_prefix: str = ""):
        """注入 MQTT client 引用，供 mqtt_indextts provider 使用。"""
        self.mqtt_client = client
        if topic_prefix:
            self.mqtt_topic_prefix = topic_prefix

    def handle_mqtt_response(self, request_id: str, ok: bool, audio_base64: str, error: str):
        """收到 cc-dashboard 的 TTS MQTT 响应，唤醒等待线程。"""
        with self._mqtt_lock:
            entry = self._mqtt_pending.get(request_id)
            if not entry:
                return
            entry["ok"] = ok
            entry["error"] = error or ""
            if ok and audio_base64:
                try:
                    entry["audio"] = base64.b64decode(audio_base64)
                except Exception as e:
                    entry["ok"] = False
                    entry["error"] = f"audio decode failed: {e}"
            entry["event"].set()

    def _mqtt_tts_request(self, text: str, voice: str, timeout: float = 120.0) -> bytes:
        """通过 MQTT 发送 TTS 请求到 cc-dashboard，等待响应返回音频。"""
        if not self.mqtt_client:
            raise RuntimeError("MQTT client not available for mqtt_indextts")

        request_id = uuid.uuid4().hex[:8]
        event = threading.Event()

        with self._mqtt_lock:
            self._mqtt_pending[request_id] = {
                "event": event,
                "audio": None,
                "ok": False,
                "error": "",
            }

        request_topic = f"{self.mqtt_topic_prefix}/tts/request"
        payload = json.dumps({
            "request_id": request_id,
            "text": text,
            "voice": voice,
        }, ensure_ascii=False)

        result = self.mqtt_client.publish(request_topic, payload, qos=1)
        if result.rc != 0:
            with self._mqtt_lock:
                self._mqtt_pending.pop(request_id, None)
            raise RuntimeError(f"MQTT publish failed rc={result.rc}")

        logger.info("MQTT TTS request sent: id=%s text=%s", request_id, text[:40])

        if not event.wait(timeout=timeout):
            with self._mqtt_lock:
                self._mqtt_pending.pop(request_id, None)
            raise RuntimeError(f"MQTT TTS request {request_id} timed out after {timeout}s")

        with self._mqtt_lock:
            entry = self._mqtt_pending.pop(request_id, {})

        if not entry.get("ok"):
            raise RuntimeError(f"MQTT TTS failed: {entry.get('error', 'unknown')}")

        audio = entry.get("audio")
        if not audio:
            raise RuntimeError("MQTT TTS returned empty audio")
        return audio

    def enabled(self) -> bool:
        if not self.settings.enabled:
            return False
        if self.settings.orchestrator not in {self.role, "both"}:
            return False
        if self.settings.provider == "off":
            return False
        return True

    def start(self):
        if not self.enabled() or self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        logger.info("TTS orchestrator started: role=%s provider=%s", self.role, self.settings.provider)

    def stop(self):
        self._running = False
        if self._worker:
            try:
                self._queue.put_nowait({"_stop": True})
            except Exception:
                pass

    def reload_settings(self, role: str, cache_dir: str):
        """重新加载设置，支持运行时热切换。"""
        was_running = self._running
        if was_running:
            self.stop()
            # 等待 worker 线程退出
            if self._worker and self._worker.is_alive():
                self._worker.join(timeout=3)
            self._running = False

        self.settings = load_settings(role, cache_dir)
        self.settings.cache_path.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._inflight.clear()
            self._last_error = ""

        if self.enabled():
            self.start()
        logger.info(
            "TTS settings reloaded: enabled=%s provider=%s",
            self.settings.enabled,
            self.settings.provider,
        )

    def health(self) -> dict:
        return {
            "ok": True,
            "enabled": self.enabled(),
            "role": self.role,
            "orchestrator": self.settings.orchestrator,
            "provider": self.settings.provider,
            "cache_dir": str(self.settings.cache_path),
            "public_base_url": self.settings.public_base_url,
            "inflight": len(self._inflight),
            "last_error": self._last_error,
        }

    def audio_path(self, speech_id: str) -> Path:
        speech_id = safe_text(speech_id)
        return self.settings.cache_path / f"{speech_id}.mp3"

    def has_audio(self, speech_id: str) -> bool:
        path = self.audio_path(speech_id)
        return path.is_file() and path.stat().st_size > 0

    def _choose_voice(self) -> str:
        if self.settings.provider == "doubao":
            return self.settings.doubao_speaker or self.settings.voice or "zh_female_xiaohe_uranus_bigtts"
        if self.settings.provider == "cloud_openai_compatible" and self.settings.cloud_voice:
            return self.settings.cloud_voice
        if self.settings.provider == "mimo":
            return self.settings.mimo_voice or self.settings.voice or "default"
        return self.settings.voice or "zh_female_xiaohe_uranus_bigtts"

    def _provider_request(self, text: str) -> bytes:
        provider = self.settings.provider
        voice = self._choose_voice()
        sample_rate = self.settings.sample_rate

        if provider == "mqtt_indextts":
            # 通过 MQTT 请求 cc-dashboard 代理合成（server 无法直连本地局域网）
            voice_prompt = self.settings.local_voice or voice
            return self._mqtt_tts_request(text, voice_prompt, timeout=self.settings.http_timeout)

        if provider == "local_indextts":
            base_url = self.settings.local_base_url.rstrip("/") if self.settings.local_base_url else ""
            voice_prompt = self.settings.local_voice or voice
            if self.settings.proxy_url:
                # 通过 cc-dashboard 代理（server 无法直连本地局域网时使用）
                return _proxy_tts_synth(
                    self.settings.proxy_url, text, voice_prompt,
                    base_url=base_url, timeout=self.settings.http_timeout,
                )
            if not base_url:
                raise RuntimeError("TTS_LOCAL_BASE_URL not configured")
            return _indextts_async_synth(base_url, text, voice_prompt, timeout=self.settings.http_timeout)

        if provider == "cloud_openai_compatible":
            if not self.settings.cloud_base_url:
                raise RuntimeError("TTS_CLOUD_BASE_URL not configured")
            url = f"{self.settings.cloud_base_url.rstrip('/')}{self.settings.cloud_path}"
            payload = {
                "model": self.settings.cloud_model or "tts-1",
                "voice": voice,
                "input": text,
                "format": self.settings.cloud_response_format or "mp3",
                "response_format": self.settings.cloud_response_format or "mp3",
                "speed": self.settings.cloud_speed,
                "sample_rate": sample_rate,
            }
            headers = {}
            if self.settings.cloud_api_key:
                headers["Authorization"] = f"Bearer {self.settings.cloud_api_key}"
            return _http_post_json(url, payload, headers=headers, timeout=self.settings.http_timeout)

        if provider == "doubao":
            if not self.settings.doubao_app_id or not self.settings.doubao_access_key:
                raise RuntimeError("DOUBAO_APP_ID / DOUBAO_ACCESS_KEY not configured")

            url = self.settings.doubao_api_url or DEFAULT_DOUBAO_API_URL
            speaker = voice
            resource_id = self.settings.doubao_resource_id or _infer_doubao_resource_id(speaker)
            audio_params = {
                "format": "mp3",
                "sample_rate": sample_rate,
                "speech_rate": self.settings.doubao_speech_rate,
                "loudness_rate": self.settings.doubao_loudness_rate,
            }
            if self.settings.doubao_emotion:
                audio_params["emotion"] = self.settings.doubao_emotion
            if self.settings.doubao_emotion_scale:
                audio_params["emotion_scale"] = self.settings.doubao_emotion_scale

            req_params = {
                "text": text,
                "speaker": speaker,
                "audio_params": audio_params,
            }
            if self.settings.doubao_pitch:
                req_params["additions"] = json.dumps({"post_process": {"pitch": self.settings.doubao_pitch}})

            payload = {
                "user": {"uid": "whitebox"},
                "req_params": req_params,
            }

            headers = {
                "X-Api-App-Id": self.settings.doubao_app_id,
                "X-Api-Access-Key": self.settings.doubao_access_key,
                "X-Api-Resource-Id": resource_id,
                "X-Api-Request-Id": str(uuid.uuid4()),
            }
            return _http_post_doubao(url, payload, headers=headers, timeout=self.settings.http_timeout)

        if provider == "mimo":
            if not self.settings.mimo_api_key:
                raise RuntimeError("MIMO_API_KEY not configured")
            url = f"{(self.settings.mimo_base_url or 'https://api.xiaomimimo.com').rstrip('/')}/v1/chat/completions"
            mimo_voice = self.settings.mimo_voice or voice
            payload = {
                "model": self.settings.mimo_model or "mimo-v2.5-tts",
                "messages": [{"role": "assistant", "content": text}],
                "max_tokens": 10,
                "voice": mimo_voice,
            }
            headers = {"Authorization": f"Bearer {self.settings.mimo_api_key}"}
            wav_bytes = _http_post_mimo(url, payload, headers=headers, timeout=self.settings.http_timeout)
            return _wav_to_mp3(wav_bytes)

        if provider == "mock":
            mock_path = safe_text(self.settings.mock_audio_path)
            if not mock_path:
                raise RuntimeError("TTS_MOCK_AUDIO_PATH not configured")
            path = Path(mock_path)
            if not path.is_file():
                raise RuntimeError(f"mock audio file not found: {mock_path}")
            audio = path.read_bytes()
            if not audio:
                raise RuntimeError(f"mock audio file is empty: {mock_path}")
            return audio

        raise RuntimeError(f"unsupported TTS provider: {provider}")

    def _write_audio(self, speech_id: str, audio: bytes) -> Path:
        path = self.audio_path(speech_id)
        tmp_path = path.with_suffix(".mp3.tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as f:
            f.write(audio)
        os.replace(tmp_path, path)
        return path

    def synthesize(self, payload: dict, text: str) -> dict:
        """同步合成一条 speech，并写入缓存。"""
        text = trim_speech_text(text, max_chars=self.settings.max_chars)
        if not text:
            raise RuntimeError("empty tts text")

        voice = self._choose_voice()
        speech = build_speech_payload(
            payload,
            text,
            self.settings.public_base_url,
            self.settings.provider,
            voice,
            self.settings.sample_rate,
        )

        audio_path = self.audio_path(speech["id"])
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            audio = self._provider_request(text)
            if not audio:
                raise RuntimeError("empty tts audio")
            self._write_audio(speech["id"], audio)

        return speech

    def _emit_ready(self, payload: dict, speech: dict):
        if self._on_ready:
            try:
                self._on_ready(payload, speech)
            except Exception as exc:
                logger.warning("TTS ready callback failed: %s", exc)

    def schedule_from_payload(self, payload: dict) -> Optional[dict]:
        """异步合成：只负责入队，不阻塞 hook / HTTP。"""
        if not self.enabled():
            return None

        candidate = extract_speech_candidate_from_payload(payload, max_chars=self.settings.max_chars)
        text = safe_text((candidate or {}).get("text", ""))
        if not text:
            return None

        job_payload = copy.deepcopy(payload)
        if candidate:
            job_payload["speech_candidate"] = candidate
            if candidate.get("source_id"):
                job_payload["speech_source_id"] = candidate.get("source_id")

        voice = self._choose_voice()
        speech_id = build_speech_id(job_payload, text, self.settings.provider, voice, self.settings.sample_rate)
        with self._lock:
            if speech_id in self._inflight:
                return {
                    "id": speech_id,
                    "kind": SPEECH_KIND_PROGRESS,
                    "text": trim_speech_text(text, self.settings.max_chars),
                    "audio_url": f"{self.settings.public_base_url.rstrip('/')}/api/tts/audio/{speech_id}.mp3",
                    "format": "mp3",
                    "sample_rate": self.settings.sample_rate,
                    "created_at": now_iso(),
                    "provider": self.settings.provider,
                    "voice": voice,
                    "seq": int(job_payload.get("seq", 0) or 0),
                    "session_id": safe_text(job_payload.get("session_id", "")),
                    "source_id": safe_text((candidate or {}).get("source_id", "")),
                    "pending": True,
                }
            self._inflight.add(speech_id)

        job = {
            "payload": job_payload,
            "text": text,
            "speech_id": speech_id,
        }
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._inflight.discard(speech_id)
            logger.warning("TTS queue full, drop speech id=%s", speech_id)
            return None

        return {
            "id": speech_id,
            "kind": SPEECH_KIND_PROGRESS,
            "text": trim_speech_text(text, self.settings.max_chars),
            "audio_url": f"{self.settings.public_base_url.rstrip('/')}/api/tts/audio/{speech_id}.mp3",
            "format": "mp3",
            "sample_rate": self.settings.sample_rate,
            "created_at": now_iso(),
            "provider": self.settings.provider,
            "voice": voice,
            "seq": int(job_payload.get("seq", 0) or 0),
            "session_id": safe_text(job_payload.get("session_id", "")),
            "source_id": safe_text((candidate or {}).get("source_id", "")),
            "pending": True,
        }

    def speak_now(self, payload: dict, text: str) -> dict:
        """同步合成，供 /api/tts/speak 手动触发。"""
        return self.synthesize(payload, text)

    def _loop(self):
        while self._running:
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if not job or job.get("_stop"):
                continue

            payload = job.get("payload") or {}
            text = safe_text(job.get("text", ""))
            speech_id = safe_text(job.get("speech_id", ""))
            try:
                speech = self.synthesize(payload, text)
                speech["pending"] = False
                with self._lock:
                    self._inflight.discard(speech_id)
                self._emit_ready(payload, speech)
            except Exception as exc:
                self._last_error = safe_text(exc, 300)
                with self._lock:
                    self._inflight.discard(speech_id)
                logger.warning("TTS synth failed id=%s err=%s", speech_id, exc)


def create_orchestrator(role: str, cache_dir: str, on_ready=None) -> TTSOrchestrator:
    settings = load_settings(role, cache_dir)
    service = TTSOrchestrator(settings, role=role, on_ready=on_ready)
    service.start()
    return service
