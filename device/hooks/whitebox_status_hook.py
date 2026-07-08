"""
whitebox_status_hook — Claude Code hook → 本机 dashboard HTTP 状态上报
PermissionRequest 会在上报状态后，长轮询 dashboard 的设备 action，再把 allow/deny
决策回写给 Claude Code。MQTT 仍由 dashboard 常驻进程维护。
"""
import json
import os
import sys
import time
import glob
import hashlib
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

HOOK_DIR = os.path.dirname(__file__)
QUEUE_DIR = os.path.join(HOOK_DIR, ".queue")
SEQ_FILE = os.path.join(HOOK_DIR, ".seq")
SEQ_LOCK = os.path.join(HOOK_DIR, ".seq.lock")
CLAUDE_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
GIF_MAP = {"cooking": 1, "thinking": 2, "idle": 3, "marinating": 4}
ACTION_WAIT_DEFAULT_TIMEOUT = 110.0
ACTION_WAIT_DEFAULT_POLL_INTERVAL = 0.3
ACTION_WAIT_DEFAULT_HTTP_TIMEOUT = 2.0
HUMAN_WAIT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
MARINATING_MESSAGE_KEYWORDS = (
    "permission",
    "needs your permission",
    "waiting for your input",
    "do you want to proceed",
    "是否继续",
    "等待确认",
    "等待用户",
)
INTERNAL_MESSAGE_PREFIXES = (
    "Caveat:",
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)
SPEECH_HOOK_EVENTS = {"PreToolUse", "PermissionRequest", "Notification", "Stop"}
SPEECH_LOOKBACK_SECONDS = 180
SPEECH_FORWARD_SECONDS = 8
SPEECH_MAX_CHARS = 120
SPEECH_MIN_CHARS = 6


def safe_text(value, limit=None):
    try:
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value
        else:
            text = str(value)
    except Exception:
        text = "<unprintable>"
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    if limit is not None:
        return text[:limit]
    return text


def is_internal_message_text(value):
    """识别 Claude 注入的内部消息，避免误判为真实用户输入。"""
    text = safe_text(value).strip()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in INTERNAL_MESSAGE_PREFIXES)


def parse_iso(value):
    """解析 Claude transcript 的 ISO 时间。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(safe_text(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def read_tail_lines(path, max_bytes=256 * 1024):
    """读取 transcript 尾部，避免 hook 处理大文件时拖慢 Claude。"""
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


def extract_visible_assistant_text(content):
    """只抽 Claude 展示给人的 text block，不抽 thinking/tool_use/tool_result。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(safe_text(item.get("text", "")))
        return "\n".join(part for part in parts if part.strip())
    return ""


def normalize_speech_text(text):
    text = safe_text(text).strip()
    if not text:
        return ""
    text = " ".join(text.replace("```", "").split())
    return text


def trim_speech_text(text, max_chars=SPEECH_MAX_CHARS):
    text = normalize_speech_text(text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for mark in ("。", "！", "？", "；", "，", ",", ".", "!", "?", ";"):
        pos = cut.rfind(mark)
        if pos >= max_chars // 2:
            return cut[:pos + 1]
    return cut.rstrip() + "..."


def is_speech_text(text):
    """过滤工具标签、内部标签和太短的碎片。"""
    text = normalize_speech_text(text)
    if len(text) < SPEECH_MIN_CHARS:
        return False
    if is_internal_message_text(text):
        return False
    if text.startswith("<") and text.endswith(">"):
        return False
    if text.startswith("[") and "]" in text[:24]:
        return False
    return True


def build_speech_source_id(entry, text):
    """用 assistant 消息 UUID 做稳定来源 ID，避免同一句被多个 hook 重复朗读。"""
    entry_id = safe_text(entry.get("uuid", "") or (entry.get("message") or {}).get("id", ""))
    entry_ts = safe_text(entry.get("timestamp", ""))
    text_hash = hashlib.sha1(normalize_speech_text(text).encode("utf-8", errors="ignore")).hexdigest()[:12]
    anchor = entry_id or entry_ts or text_hash
    return f"transcript:{anchor}:{text_hash}"


def extract_speech_candidate_from_transcript(path, updated_at=None):
    """提取当前 hook 附近最新的 assistant 可见文本，也就是界面红框里的内容。"""
    updated_dt = parse_iso(updated_at)
    min_dt = updated_dt - timedelta(seconds=SPEECH_LOOKBACK_SECONDS) if updated_dt else None
    max_dt = updated_dt + timedelta(seconds=SPEECH_FORWARD_SECONDS) if updated_dt else None
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
        if entry_dt and min_dt and entry_dt < min_dt:
            continue
        if entry_dt and max_dt and entry_dt > max_dt:
            continue

        text = trim_speech_text(extract_visible_assistant_text((entry.get("message") or {}).get("content", "")))
        if not is_speech_text(text):
            continue
        candidate = {
            "kind": "assistant_progress",
            "source": "transcript",
            "source_id": build_speech_source_id(entry, text),
            "text": text,
            "transcript_ts": safe_text(entry.get("timestamp", "")),
        }

    return candidate


def should_ignore_event(hook_event, hook_input):
    """忽略会污染状态的内部事件。"""
    if hook_event == "UserPromptSubmit":
        return is_internal_message_text(hook_input.get("prompt", ""))
    return False


def acquire_file_lock(lock_path, stale_seconds=30):
    now = time.time()
    stale = False
    try:
        if os.path.exists(lock_path) and now - os.path.getmtime(lock_path) > stale_seconds:
            stale = True
            os.remove(lock_path)
    except OSError:
        pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, safe_text(os.getpid()).encode("utf-8"))
        os.close(fd)
        return True
    except OSError:
        if stale:
            try:
                with open(lock_path, "w", encoding="utf-8") as f:
                    f.write(safe_text(os.getpid()))
                return True
            except OSError:
                pass
        return False


def release_file_lock(lock_path):
    try:
        os.remove(lock_path)
    except OSError:
        try:
            os.utime(lock_path, (0, 0))
        except OSError:
            pass


def append_trace(hook_event, hook_input, status="", error="", extra=None):
    try:
        trace_path = os.path.join(HOOK_DIR, ".trace.jsonl")
        tool_input = hook_input.get("tool_input") or {}
        item = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": safe_text(hook_event),
            "status": safe_text(status),
            "error": safe_text(error),
            "cwd": safe_text(hook_input.get("cwd", "")),
            "session_id": safe_text(hook_input.get("session_id", "")),
            "transcript_path": safe_text(hook_input.get("transcript_path", "")),
            "permission_mode": safe_text(hook_input.get("permission_mode", "")),
            "tool_name": safe_text(hook_input.get("tool_name", "")),
            "message": safe_text(hook_input.get("message", ""), 300),
            "prompt": safe_text(hook_input.get("prompt", ""), 300),
            "tool_input": safe_text(tool_input, 500),
        }
        if extra:
            item.update(extra)
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def summarize_tool_input(hook_input):
    tool_input = hook_input.get("tool_input") or {}
    for key in ("command", "file_path", "pattern", "question", "description"):
        value = tool_input.get(key)
        if value:
            return safe_text(value, 200)
    return ""


def build_project_key_from_work_dir(work_dir):
    """按 Claude projects 目录的命名习惯生成项目 key。"""
    text = safe_text(work_dir).strip()
    if not text:
        return ""
    return (
        text
        .replace(":", "-")
        .replace("\\", "-")
        .replace("/", "-")
        .replace("_", "-")
        .strip("-")
    )


def resolve_project_key(work_dir, session_id, transcript_path=""):
    """优先按 transcript_path / session_id 反查真实项目目录，保证与 Claude 一致。"""
    transcript_path = safe_text(transcript_path).strip()
    if transcript_path:
        return os.path.basename(os.path.dirname(transcript_path.rstrip("\\/")))

    session_id = safe_text(session_id).strip()
    if session_id:
        pattern = os.path.join(CLAUDE_PROJECTS_DIR, "*", f"{session_id}.jsonl")
        matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if matches:
            return os.path.basename(os.path.dirname(matches[0]))
    return build_project_key_from_work_dir(work_dir)


def load_hook_config():
    """从 cc-dashboard/.env 读取 hook 上报配置。"""
    env_path = os.path.join(os.path.dirname(HOOK_DIR), ".env")
    env_values = {}
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_values[k.strip()] = v.strip()

    dashboard_url = env_values.get(
        "DASHBOARD_HOOK_URL",
        os.environ.get("DASHBOARD_HOOK_URL", "http://127.0.0.1:5000/api/hook/state"),
    )
    action_wait_url = env_values.get(
        "DASHBOARD_ACTION_WAIT_URL",
        os.environ.get("DASHBOARD_ACTION_WAIT_URL", ""),
    )
    if not action_wait_url and dashboard_url:
        if dashboard_url.endswith("/api/hook/state"):
            action_wait_url = dashboard_url[: -len("/api/hook/state")] + "/api/device/action/wait"
        else:
            action_wait_url = dashboard_url.rstrip("/") + "/api/device/action/wait"

    return {
        "dashboard_url": dashboard_url,
        "dashboard_action_wait_url": action_wait_url,
        "http_token": env_values.get("HOOK_HTTP_TOKEN",
                                      os.environ.get("HOOK_HTTP_TOKEN", "")),
        "http_timeout": env_values.get("HOOK_HTTP_TIMEOUT",
                                        os.environ.get("HOOK_HTTP_TIMEOUT", "0.5")),
        "action_wait_timeout": env_values.get("HOOK_ACTION_WAIT_TIMEOUT",
                                               os.environ.get("HOOK_ACTION_WAIT_TIMEOUT",
                                                              str(ACTION_WAIT_DEFAULT_TIMEOUT))),
        "action_wait_poll_interval": env_values.get(
            "HOOK_ACTION_WAIT_POLL_INTERVAL",
            os.environ.get("HOOK_ACTION_WAIT_POLL_INTERVAL", str(ACTION_WAIT_DEFAULT_POLL_INTERVAL)),
        ),
        "action_wait_http_timeout": env_values.get(
            "HOOK_ACTION_WAIT_HTTP_TIMEOUT",
            os.environ.get("HOOK_ACTION_WAIT_HTTP_TIMEOUT", str(ACTION_WAIT_DEFAULT_HTTP_TIMEOUT)),
        ),
    }


def list_queue_files(include_new=False):
    """列出待发送队列文件。include_new 参数保留给测试兼容。"""
    if not os.path.isdir(QUEUE_DIR):
        return []
    files = [
        os.path.join(QUEUE_DIR, name)
        for name in os.listdir(QUEUE_DIR)
        if name.endswith(".json")
    ]
    return sorted(files)


def queue_size():
    """队列文件数量。"""
    return len(list_queue_files(include_new=True))


def get_seq():
    locked = False
    for _ in range(20):
        locked = acquire_file_lock(SEQ_LOCK, stale_seconds=10)
        if locked:
            break
        time.sleep(0.02)
    try:
        try:
            with open(SEQ_FILE, "r", encoding="utf-8") as f:
                seq = int(f.read().strip()) + 1
        except (FileNotFoundError, ValueError):
            seq = 1
        try:
            with open(SEQ_FILE, "w", encoding="utf-8") as f:
                f.write(str(seq))
        except OSError:
            pass
        return seq
    finally:
        if locked:
            release_file_lock(SEQ_LOCK)


def handle_event(hook_event, hook_input):
    tool_name = safe_text(hook_input.get("tool_name", ""))
    prompt = safe_text(hook_input.get("prompt", ""))
    hook_message = safe_text(hook_input.get("message", ""))
    tool_summary = summarize_tool_input(hook_input)
    now = datetime.now(timezone.utc).isoformat()

    if hook_event == "UserPromptSubmit":
        return "thinking", {
            "role": "hook",
            "type": "UserPromptSubmit",
            "text": prompt[:200] if prompt else "用户提交消息",
            "hook_message": prompt[:500],
            "ts": now,
        }

    elif hook_event == "PreToolUse":
        if tool_name in HUMAN_WAIT_TOOLS:
            return "marinating", {
                "role": "hook",
                "type": "PreToolUse",
                "text": f"[{tool_name}] 等待用户回复",
                "hook_message": tool_summary or f"PreToolUse {tool_name}".strip(),
                "ts": now,
            }
        return "cooking", {
            "role": "hook",
            "type": "PreToolUse",
            "text": f"[{tool_name}]",
            "hook_message": tool_summary or f"PreToolUse {tool_name}".strip(),
            "ts": now,
        }

    elif hook_event == "PermissionRequest":
        return "marinating", {
            "role": "hook",
            "type": "PermissionRequest",
            "text": f"[{tool_name}] 等待确认",
            "hook_message": tool_summary or f"PermissionRequest {tool_name}".strip(),
            "ts": now,
        }

    elif hook_event == "Elicitation":
        return "marinating", {
            "role": "hook",
            "type": "Elicitation",
            "text": "等待用户输入",
            "hook_message": hook_message[:500] or tool_summary or "Elicitation",
            "ts": now,
        }

    elif hook_event == "Notification":
        message_lower = hook_message.lower()
        if any(keyword in message_lower for keyword in MARINATING_MESSAGE_KEYWORDS):
            return "marinating", {
                "role": "hook",
                "type": "Notification",
                "text": hook_message[:200] or "等待确认",
                "hook_message": hook_message[:500],
                "ts": now,
            }
        return "thinking", {
            "role": "hook",
            "type": "Notification",
            "text": hook_message[:200] or "Notification",
            "hook_message": hook_message[:500],
            "ts": now,
        }

    elif hook_event in (
        "PostToolUse", "PostToolUseFailure", "PostToolBatch",
    ):
        text = f"[{tool_name}] 完成" if tool_name else f"{hook_event} 完成"
        return "cooking", {
            "role": "hook",
            "type": hook_event,
            "text": text,
            "hook_message": f"{hook_event} {tool_name}".strip(),
            "ts": now,
        }

    elif hook_event in ("PermissionDenied", "ElicitationResult"):
        text = f"[{tool_name}] 完成" if tool_name else f"{hook_event} 完成"
        return "thinking", {
            "role": "hook",
            "type": hook_event,
            "text": text,
            "hook_message": f"{hook_event} {tool_name}".strip(),
            "ts": now,
        }

    elif hook_event in ("Stop", "StopFailure", "SessionEnd", "SessionStart"):
        if hook_event == "SessionStart":
            text = "会话开始"
        elif hook_event == "StopFailure":
            text = "会话异常结束"
        else:
            text = "会话结束"
        return "idle", {
            "role": "hook",
            "type": hook_event,
            "text": text,
            "hook_message": hook_input.get("reason", "") or hook_input.get("source", "") or hook_event,
            "ts": now,
        }

    return "thinking", {
        "role": "hook",
        "type": hook_event,
        "text": hook_event,
        "hook_message": hook_message[:500],
        "ts": now,
    }


def build_payload(status, message, cfg, hook_event, hook_input):
    work_dir = safe_text(hook_input.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    session_id = safe_text(hook_input.get("session_id", "") or os.environ.get("CLAUDE_SESSION_ID", ""))
    transcript_path = safe_text(hook_input.get("transcript_path", "") or os.environ.get("CLAUDE_TRANSCRIPT_PATH", ""))
    permission_mode = safe_text(hook_input.get("permission_mode", "") or os.environ.get("CLAUDE_PERMISSION_MODE", ""))
    project_key = resolve_project_key(work_dir, session_id, transcript_path)
    updated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "source": "claude-hook",
        "hook_event_name": safe_text(hook_event),
        "hook_message": safe_text(message.get("hook_message", "")),
        "status": safe_text(status),
        "gif": GIF_MAP.get(status, 3),
        "project_key": project_key,
        "work_dir": work_dir,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "permission_mode": permission_mode,
        "msg_count": 0,
        "latest_message": message,
        "updated_at": updated_at,
        "seq": get_seq(),
    }
    if hook_event in SPEECH_HOOK_EVENTS and transcript_path:
        candidate = extract_speech_candidate_from_transcript(transcript_path, updated_at=updated_at)
        if candidate:
            payload["speech_candidate"] = candidate
    return payload


def post_to_dashboard(payload, cfg):
    """HTTP POST 到本机 dashboard。"""
    url = safe_text(cfg.get("dashboard_url", ""))
    if not url:
        return False, "no dashboard url"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "whitebox-hook/1",
    }
    token = safe_text(cfg.get("http_token", ""))
    if token:
        headers["X-Whitebox-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    timeout = float(cfg.get("http_timeout", "0.5") or "0.5")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512)
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"http {resp.status}: {safe_text(body, 120)}"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}: {safe_text(e.read(256), 120)}"
    except Exception as e:
        return False, safe_text(e)


def enqueue_payload(payload):
    """写入 .queue 文件，供 dashboard drainer 后续处理。"""
    os.makedirs(QUEUE_DIR, exist_ok=True)
    seq = int(payload.get("seq", 0) or 0)
    stamp = int(time.time() * 1000)
    path = os.path.join(QUEUE_DIR, f"{seq:012d}-{stamp}-{os.getpid()}.json")
    data = json.dumps(payload, ensure_ascii=False)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
        return True
    except OSError:
        return False


def build_permission_decision(action_payload, hook_input):
    """把设备动作映射成 Claude Code hook 决策 JSON。"""
    if not isinstance(action_payload, dict):
        return None

    action = safe_text(action_payload.get("action", "")).strip().lower()
    tool_input = hook_input.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    if action in ("continue", "继续", "确定"):
        decision = {
            "behavior": "allow",
            "updatedInput": tool_input,
        }
    elif action in ("reject", "拒绝"):
        decision = {
            "behavior": "deny",
            "message": "User rejected from Whitebox.",
        }
    else:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def emit_json_stdout(data):
    """把 JSON 决策写到 stdout。"""
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def wait_for_device_action(cfg, since_seq):
    """按固定轮询间隔等待 dashboard 返回最新设备动作。"""
    wait_url = safe_text(cfg.get("dashboard_action_wait_url", "")).strip()
    if not wait_url:
        return None

    try:
        overall_timeout = float(cfg.get("action_wait_timeout", ACTION_WAIT_DEFAULT_TIMEOUT) or ACTION_WAIT_DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        overall_timeout = ACTION_WAIT_DEFAULT_TIMEOUT
    try:
        poll_interval = float(cfg.get("action_wait_poll_interval", ACTION_WAIT_DEFAULT_POLL_INTERVAL) or ACTION_WAIT_DEFAULT_POLL_INTERVAL)
    except (TypeError, ValueError):
        poll_interval = ACTION_WAIT_DEFAULT_POLL_INTERVAL
    try:
        http_timeout = float(cfg.get("action_wait_http_timeout", ACTION_WAIT_DEFAULT_HTTP_TIMEOUT) or ACTION_WAIT_DEFAULT_HTTP_TIMEOUT)
    except (TypeError, ValueError):
        http_timeout = ACTION_WAIT_DEFAULT_HTTP_TIMEOUT

    overall_timeout = max(0.0, overall_timeout)
    poll_interval = max(0.05, poll_interval)
    http_timeout = max(0.1, http_timeout, poll_interval + 0.5)

    deadline = time.monotonic() + overall_timeout
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        step_timeout = max(0.05, min(poll_interval, remaining))
        query = urllib.parse.urlencode({
            "since_seq": str(int(since_seq or 0)),
            "timeout": f"{step_timeout:.3f}",
        })
        url = f"{wait_url}?{query}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "whitebox-hook/1"},
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError:
            return None
        except Exception:
            return None

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return None

        if isinstance(payload, dict) and payload.get("matched"):
            action = payload.get("action")
            if isinstance(action, dict):
                return action

    return None


def process_permission_request(hook_input, cfg, payload, status):
    """处理 PermissionRequest：上报状态、等待设备动作、生成决策。"""
    ok, error = post_to_dashboard(payload, cfg)
    queued = False

    if not ok:
        queued = enqueue_payload(payload)
        append_trace(hook_input.get("hook_event_name", "PermissionRequest"), hook_input,
                     status=status, error=f"post-failed: {error}", extra={"queued": queued})
    else:
        append_trace(hook_input.get("hook_event_name", "PermissionRequest"), hook_input,
                     status=status)

    if not (ok or queued):
        return None

    action = wait_for_device_action(cfg, payload.get("seq", 0))
    if not action:
        append_trace(
            hook_input.get("hook_event_name", "PermissionRequest"),
            hook_input,
            status=status,
            extra={"permission_wait": "timeout"},
        )
        return None

    decision = build_permission_decision(action, hook_input)
    if decision:
        append_trace(
            hook_input.get("hook_event_name", "PermissionRequest"),
            hook_input,
            status=status,
            extra={
                "permission_action": safe_text(action.get("action", "")),
                "permission_seq": safe_text(action.get("last_seq", 0)),
                "permission_source": safe_text(action.get("source", "")),
            },
        )
    return decision


def main():
    # 解析事件类型
    hook_event = None
    raw = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--event" and i + 1 < len(sys.argv):
            hook_event = sys.argv[i + 1]
            break

    # 读取 stdin
    try:
        try:
            raw_bytes = sys.stdin.buffer.read()
            raw = raw_bytes.decode("utf-8-sig", errors="replace").strip().lstrip("﻿")
        except AttributeError:
            raw = sys.stdin.read().strip().lstrip("﻿")
        if not raw:
            append_trace(hook_event or "unknown", {}, error="empty-stdin")
            sys.exit(0)
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        append_trace(hook_event or "unknown", {"message": safe_text(raw, 300)}, error="json-decode")
        sys.exit(0)

    if not hook_event:
        hook_event = hook_input.get("hook_event_name", "")
    if not hook_event:
        append_trace("unknown", hook_input, error="missing-event")
        sys.exit(0)

    if should_ignore_event(hook_event, hook_input):
        append_trace(hook_event, hook_input, status="ignored", extra={"ignored": True})
        sys.exit(0)

    status, message = handle_event(hook_event, hook_input)
    cfg = load_hook_config()
    payload = build_payload(status, message, cfg, hook_event, hook_input)

    if hook_event == "PermissionRequest":
        decision = process_permission_request(hook_input, cfg, payload, status)
        if decision:
            emit_json_stdout(decision)
        sys.exit(0)

    # HTTP POST 到本机 dashboard
    ok, error = post_to_dashboard(payload, cfg)

    if not ok:
        # 失败：写入队列，dashboard drainer 后续会处理
        queued = enqueue_payload(payload)
        append_trace(hook_event, hook_input, status=status,
                     error=f"post-failed: {error}", extra={"queued": queued})
    else:
        append_trace(hook_event, hook_input, status=status)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        append_trace("fatal", {}, error=safe_text(e, 300))
        print(f"[whitebox-hook] hook 异常已忽略: {safe_text(e)}", file=sys.stderr)
        sys.exit(0)
