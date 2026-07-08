"""
cc-dashboard — Claude Code 轻量监控 + 常驻 MQTT 桥接 + CC 控制
状态: thinking / cooking / marinating / idle
"""

import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_file

# 读取 .env 文件（不需要 python-dotenv）
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

app = Flask(__name__)
CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude", "projects")
APP_ROLE = "local"

# Hook token 校验
HOOK_HTTP_TOKEN = os.environ.get("HOOK_HTTP_TOKEN", os.environ.get("MQTT_PASSWORD", ""))
DEVICE_ACTION_WAIT_MAX_SECONDS = 120.0
SESSION_ROUTE_LOCK = threading.Lock()
SESSION_ROUTE = {
    "mode": os.environ.get("SESSION_ROUTE_MODE", "all"),
    "session_id": os.environ.get("SESSION_ROUTE_SESSION_ID", ""),
    "project_key": os.environ.get("SESSION_ROUTE_PROJECT_KEY", ""),
}
DB_PATH = os.path.join(os.path.dirname(__file__), "dashboard.db")


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    row = conn.execute("SELECT value FROM settings WHERE key='session_route'").fetchone()
    if row:
        saved = json.loads(row[0])
        with SESSION_ROUTE_LOCK:
            SESSION_ROUTE.update(saved)
    conn.close()


def _save_session_route():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('session_route', ?)",
        (json.dumps(SESSION_ROUTE),),
    )
    conn.commit()
    conn.close()


_init_db()


# ── 会话扫描（与原版一致）──

def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type", "")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_use":
                name = b.get("name", "")
                inp = b.get("input", {})
                cmd = inp.get("command", inp.get("file_path", ""))
                parts.append(f"[{name}] {cmd}")
            elif t == "thinking":
                parts.append("_思考中..._")
        return "\n".join(parts)
    return ""


def has_thinking(content):
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "thinking" for b in content)
    return False


def has_tool_use(content):
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    return False


HUMAN_WAIT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
PERMISSION_PROMPT_TOOLS = {"Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit"}
PERMISSION_PROMPT_GRACE_SECONDS = 3
STALE_SECONDS = 120


def tool_use_ids(content, names=None):
    ids = set()
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                if names is None or b.get("name") in names:
                    tool_id = b.get("id", "")
                    if tool_id:
                        ids.add(tool_id)
    return ids


def tool_result_ids(content):
    ids = set()
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tool_id = b.get("tool_use_id", "")
                if tool_id:
                    ids.add(tool_id)
    return ids


def has_text(content):
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip() for b in content)
    return False


def is_internal_user_message(text: str) -> bool:
    text = text.strip()
    if not text:
        return True
    return (
        text.startswith("Caveat:")
        or text.startswith("<task-notification>")
        or text.startswith("<local-command-stdout>")
        or text.startswith("<local-command-stderr>")
    )


def is_interrupted_message(text: str) -> bool:
    return text.strip().startswith("[Request interrupted by user]")


def scan_session(path: str, chat_limit: int = 30):
    summary = ""
    chat = []
    msg_count = 0
    last_entry_type = ""
    last_assistant_thinking = False
    last_assistant_tool = False
    last_needs_confirm = False
    last_user_is_question = False
    pending_human_tool_ids = set()
    active_permission_tool_ids = set()
    status_hint = "idle"

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type", "")
                ts = entry.get("timestamp", "")

                if etype == "assistant":
                    msg_count += 1
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    stop_reason = msg.get("stop_reason", "")
                    text = extract_text(content)
                    if text and not text.startswith("Caveat:"):
                        chat.append({"role": etype, "text": text[:500], "ts": ts})
                    last_assistant_thinking = has_thinking(content)
                    last_assistant_tool = has_tool_use(content)
                    human_ids = tool_use_ids(content, HUMAN_WAIT_TOOLS)
                    permission_ids = tool_use_ids(content, PERMISSION_PROMPT_TOOLS)
                    pending_human_tool_ids.update(human_ids)
                    active_permission_tool_ids = permission_ids

                    if stop_reason == "end_turn":
                        status_hint = "idle"
                    elif human_ids:
                        status_hint = "marinating"
                    elif last_assistant_tool:
                        status_hint = "cooking"
                    elif last_assistant_thinking:
                        status_hint = "thinking"
                    elif has_text(content):
                        status_hint = "idle"

                elif etype == "user":
                    msg_count += 1
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    text = extract_text(content)
                    result_ids = tool_result_ids(content)
                    is_tool_result = bool(result_ids)

                    if is_tool_result:
                        pending_human_tool_ids.difference_update(result_ids)
                        active_permission_tool_ids.difference_update(result_ids)
                        last_user_is_question = False
                        status_hint = "cooking"
                    elif is_interrupted_message(text):
                        active_permission_tool_ids.clear()
                        last_user_is_question = False
                        status_hint = "idle"
                    elif is_internal_user_message(text):
                        last_user_is_question = False
                    else:
                        active_permission_tool_ids.clear()
                        last_user_is_question = True
                        status_hint = "thinking"

                    if text and not is_internal_user_message(text):
                        if not summary:
                            summary = re.sub(r"<[^>]+>", "", text).strip()
                            if len(summary) > 80:
                                summary = summary[:80] + "..."
                        chat.append({"role": "user", "text": text[:500], "ts": ts})

                elif etype == "result":
                    active_permission_tool_ids.clear()
                    status_hint = "idle"

                elif etype == "control_request":
                    req = entry.get("request", {})
                    if req.get("subtype") == "can_use_tool":
                        status_hint = "marinating"

                last_entry_type = etype
    except Exception:
        pass

    last_needs_confirm = len(pending_human_tool_ids) > 0
    last_pending_permission = len(active_permission_tool_ids) > 0
    mtime = os.path.getmtime(path) if os.path.isfile(path) else 0
    chat = chat[-chat_limit:]

    return {
        "summary": summary,
        "chat": chat,
        "msg_count": msg_count,
        "last_entry_type": last_entry_type,
        "last_user_is_question": last_user_is_question,
        "last_thinking": last_assistant_thinking,
        "last_tool_use": last_assistant_tool,
        "last_needs_confirm": last_needs_confirm,
        "last_pending_permission": last_pending_permission,
        "status_hint": "marinating" if last_needs_confirm else status_hint,
        "mtime": mtime,
    }


def get_status(mtime: float, info: dict) -> str:
    now = datetime.now().timestamp()
    age = now - mtime if mtime else 9999
    status_hint = info.get("status_hint", "")

    if status_hint == "marinating" or info.get("last_needs_confirm", False):
        return "marinating"

    if status_hint == "cooking" and info.get("last_pending_permission", False) and age >= PERMISSION_PROMPT_GRACE_SECONDS:
        return "marinating"

    if status_hint == "idle":
        return "idle"

    if age > STALE_SECONDS:
        return "idle"

    if status_hint in {"thinking", "cooking"}:
        return status_hint

    if info.get("last_user_is_question", False) or info.get("last_thinking", False):
        return "thinking"
    if info.get("last_tool_use", False):
        return "cooking"
    return "idle"


def session_route_snapshot():
    """返回当前硬件跟随模式。"""
    with SESSION_ROUTE_LOCK:
        mode = str(SESSION_ROUTE.get("mode") or "all")
        if mode not in {"all", "single"}:
            mode = "all"
        return {
            "mode": mode,
            "session_id": str(SESSION_ROUTE.get("session_id") or ""),
            "project_key": str(SESSION_ROUTE.get("project_key") or ""),
        }


def session_route_allows(payload: dict) -> tuple[bool, str]:
    """判断某条 hook 状态是否允许推送到硬件。"""
    route = session_route_snapshot()
    if route["mode"] != "single":
        return True, ""

    expected_sid = route["session_id"]
    expected_project = route["project_key"]
    payload_sid = str(payload.get("session_id") or "")
    payload_project = str(payload.get("project_key") or "")

    if not expected_sid:
        return False, "no_selected_session"
    if not payload_sid:
        return False, "missing_session_id"
    if payload_sid != expected_sid:
        return False, "session_mismatch"
    if expected_project and payload_project and payload_project != expected_project:
        return False, "project_mismatch"
    return True, ""


def list_sessions(limit: int = 300):
    """列出本机 Claude sessions，供手动选择硬件跟随对象。"""
    if not os.path.isdir(CLAUDE_HOME):
        return []

    sessions = []
    for project_name in sorted(os.listdir(CLAUDE_HOME)):
        project_dir = os.path.join(CLAUDE_HOME, project_name)
        if not os.path.isdir(project_dir):
            continue
        for fname in os.listdir(project_dir):
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(project_dir, fname)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            session_id = fname[:-6]
            sessions.append({
                "session_id": session_id,
                "project_key": project_name,
                "mtime": stat.st_mtime,
                "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size": stat.st_size,
            })

    sessions.sort(key=lambda item: item["mtime"], reverse=True)
    return sessions[:max(1, limit)]


def read_session_payload(project_key: str, session_id: str):
    """读取指定 session，构造一次可直接推送到硬件的状态快照。"""
    session_path = os.path.join(CLAUDE_HOME, project_key, f"{session_id}.jsonl")
    if not os.path.isfile(session_path):
        return None

    info = scan_session(session_path, chat_limit=50)
    status = get_status(info["mtime"], info)
    latest_message = info["chat"][-1] if info.get("chat") else {}
    seq_seed = int(info.get("mtime", 0) or 0)
    payload = {
        "source": "session-push",
        "status": status,
        "gif": {"cooking": 1, "thinking": 2, "idle": 3, "marinating": 4}.get(status, 3),
        "seq": seq_seed if seq_seed > 0 else int(datetime.now().timestamp()),
        "session_id": session_id,
        "project_key": project_key,
        "transcript_path": session_path,
        "msg_count": info.get("msg_count", 0),
        "updated_at": datetime.now().isoformat(),
        "latest_message": {
            "role": latest_message.get("role", "assistant"),
            "text": str(latest_message.get("text", "") or "")[:500],
            "type": "session-snapshot",
        },
    }
    return payload


def list_projects():
    if not os.path.isdir(CLAUDE_HOME):
        return []

    projects = []
    for name in sorted(os.listdir(CLAUDE_HOME)):
        full = os.path.join(CLAUDE_HOME, name)
        if not os.path.isdir(full):
            continue
        jsonls = glob.glob(os.path.join(full, "*.jsonl"))
        if not jsonls:
            continue

        latest_mtime = 0
        latest_sid = ""
        latest_info = None

        for fpath in jsonls:
            mtime = os.path.getmtime(fpath)
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_sid = os.path.splitext(os.path.basename(fpath))[0]
                latest_info = scan_session(fpath, chat_limit=30)

        status = get_status(latest_mtime, latest_info or {})
        summary = latest_info["summary"] if latest_info else ""

        projects.append({
            "key": name,
            "work_dir": name.replace("-", "/"),
            "summary": summary,
            "session_count": len(jsonls),
            "active_sid": latest_sid,
            "status": status,
            "mtime": latest_mtime,
            "mtime_iso": datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else None,
        })

    projects.sort(key=lambda p: p["mtime"], reverse=True)
    return projects


def version_hash(projects):
    raw = json.dumps([(p["key"], p["mtime"], p["status"]) for p in projects], sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── 路由: Dashboard 页面 ──

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    projects = list_projects()
    return jsonify({
        "ok": True,
        "ver": version_hash(projects),
        "projects": projects,
        "session_route": session_route_snapshot(),
    })


@app.route("/api/project/<key>")
def api_project(key):
    full = os.path.join(CLAUDE_HOME, key)
    if not os.path.isdir(full):
        return jsonify({"ok": False}), 404

    jsonls = glob.glob(os.path.join(full, "*.jsonl"))
    if not jsonls:
        return jsonify({"ok": False}), 404

    latest = max(jsonls, key=os.path.getmtime)
    sid = os.path.splitext(os.path.basename(latest))[0]
    info = scan_session(latest, chat_limit=50)
    status = get_status(info["mtime"], info)

    return jsonify({
        "ok": True,
        "session_id": sid,
        "status": status,
        "summary": info["summary"],
        "msg_count": info["msg_count"],
        "chat": info["chat"],
    })


@app.route("/api/sessions")
def api_sessions():
    """列出本机所有 Claude session。"""
    try:
        limit = int(request.args.get("limit", "300"))
    except (TypeError, ValueError):
        limit = 300
    limit = max(1, min(limit, 1000))

    sessions = list_sessions(limit=limit)
    project_count = len({item["project_key"] for item in sessions})
    return jsonify({
        "ok": True,
        "sessions": sessions,
        "project_count": project_count,
        "session_count": len(sessions),
        "session_route": session_route_snapshot(),
    })


@app.route("/api/session/route", methods=["GET", "POST"])
def api_session_route():
    """读取或设置硬件跟随的 session 模式。"""
    if request.method == "GET":
        return jsonify({"ok": True, "route": session_route_snapshot()})

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    mode = str(body.get("mode") or "all").strip()
    if mode not in {"all", "single"}:
        return jsonify({"ok": False, "error": "mode must be all or single"}), 400

    session_id = str(body.get("session_id") or "").strip()
    project_key = str(body.get("project_key") or "").strip()
    if mode == "single" and not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400

    with SESSION_ROUTE_LOCK:
        SESSION_ROUTE["mode"] = mode
        SESSION_ROUTE["session_id"] = session_id if mode == "single" else ""
        SESSION_ROUTE["project_key"] = project_key if mode == "single" else ""
    _save_session_route()

    return jsonify({"ok": True, "route": session_route_snapshot()})


@app.route("/api/session/push", methods=["POST"])
def api_session_push():
    """手动把某个本机 session 当前快照推送到硬件。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    session_id = str(body.get("session_id") or "").strip()
    project_key = str(body.get("project_key") or "").strip()
    if not session_id or not project_key:
        return jsonify({"ok": False, "error": "session_id and project_key required"}), 400

    payload = read_session_payload(project_key, session_id)
    if not payload:
        return jsonify({"ok": False, "error": "session file not found"}), 404

    with _latest_hook_lock:
        global _latest_hook_payload
        _latest_hook_payload = dict(payload)

    if _mqtt_bridge:
        _mqtt_bridge.publish_state(payload)

    if _tts_service:
        _tts_service.schedule_from_payload(payload)

    return jsonify({"ok": True, "status": payload["status"], "route": session_route_snapshot()})


# ── 路由: Hook 状态上报（低延迟）──

def _forward_to_cloud(payload):
    """后台转发 hook 状态到云端 server。"""
    import urllib.request
    cloud_url = os.environ.get("CLOUD_HOOK_URL", "")
    if not cloud_url:
        return
    cloud_token = os.environ.get("CLOUD_HOOK_TOKEN", "")
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(cloud_url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if cloud_token:
            req.add_header("X-Whitebox-Token", cloud_token)
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[cloud-forward] 转发失败: {e}")


@app.route("/api/hook/state", methods=["POST"])
def api_hook_state():
    """接收 Claude Code hook 的状态上报，立即通过 MQTT bridge 发布。"""
    if HOOK_HTTP_TOKEN:
        token = request.headers.get("X-Whitebox-Token", "")
        if token != HOOK_HTTP_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    allowed, blocked_reason = session_route_allows(payload)
    if not allowed:
        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": blocked_reason,
            "session_route": session_route_snapshot(),
        })

    # 通过 MQTT bridge 发布
    if _mqtt_bridge:
        _mqtt_bridge.publish_state(payload)

    with _latest_hook_lock:
        global _latest_hook_payload
        _latest_hook_payload = dict(payload)

    if _tts_service:
        _tts_service.schedule_from_payload(payload)

    # 转发到云端 server（后台线程，不阻塞响应）
    threading.Thread(target=_forward_to_cloud, args=(dict(payload),), daemon=True).start()

    status = payload.get("status", "unknown")
    seq = payload.get("seq", 0)
    return jsonify({"ok": True, "status": status, "seq": seq, "session_route": session_route_snapshot()})


@app.route("/api/tts/speak", methods=["POST"])
def api_tts_speak():
    """手动触发 TTS，便于调试本地和云端后端。"""
    if not _tts_service or not _tts_service.enabled():
        return jsonify({"ok": False, "error": "tts disabled"}), 503

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    text = str(body.get("text", "") or "").strip()
    if not text:
        from tts_service import extract_speech_text_from_payload

        text = extract_speech_text_from_payload(payload, max_chars=_tts_service.settings.max_chars)
    if not text:
        return jsonify({"ok": False, "error": "empty text"}), 400

    try:
        speech = _tts_service.speak_now(payload, text)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if isinstance(payload, dict) and payload.get("seq", 0) and _mqtt_bridge:
        _mqtt_bridge.publish_speech(speech, payload)

    return jsonify({"ok": True, "speech": speech})


@app.route("/api/tts/audio/<speech_id>.mp3")
def api_tts_audio(speech_id):
    """供 ESP32 或 server 拉取缓存的 MP3。"""
    if not _tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503

    path = _tts_service.audio_path(speech_id)
    if not path.is_file():
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, mimetype="audio/mpeg", conditional=True, download_name=path.name)


@app.route("/api/tts/proxy", methods=["POST"])
def api_tts_proxy():
    """TTS 代理：供远程 server 调用，转发到本地 IndexTTS2 异步 API。
    接收 JSON: {"text": "...", "voice": "voice_01.wav"}
    返回 JSON: {"ok": true, "audio_base64": "...", "format": "wav"}
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    text = str(body.get("text", "") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty text"}), 400

    voice = str(body.get("voice", "") or "").strip()
    base_url = str(body.get("base_url", "") or "").strip()

    # 优先用 _tts_service 配置的地址，否则用请求里带的
    if not base_url and _tts_service:
        base_url = _tts_service.settings.local_base_url
    if not base_url:
        return jsonify({"ok": False, "error": "no TTS base_url configured"}), 400

    if not voice and _tts_service:
        voice = _tts_service.settings.local_voice or "test_voice.wav"
    if not voice:
        voice = "test_voice.wav"

    try:
        from tts_service import _indextts_async_synth
        audio_bytes = _indextts_async_synth(base_url.rstrip("/"), text, voice)
        import base64 as _b64
        return jsonify({
            "ok": True,
            "audio_base64": _b64.b64encode(audio_bytes).decode("ascii"),
            "format": "wav",
            "size": len(audio_bytes),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tts/health")
def api_tts_health():
    """查看 TTS 后端状态。"""
    if not _tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503
    return jsonify(_tts_service.health())


@app.route("/api/device/action/wait")
def api_device_action_wait():
    """等待设备 action，供 PermissionRequest hook 长轮询使用。"""
    if not _mqtt_bridge:
        return jsonify({"ok": False, "error": "mqtt bridge unavailable"}), 503

    since_seq_raw = request.args.get("since_seq", "0")
    timeout_raw = request.args.get("timeout", "0")
    device_id = request.args.get("device_id", "").strip()

    try:
        since_seq = int(since_seq_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid since_seq"}), 400

    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid timeout"}), 400

    timeout = max(0.0, min(timeout, DEVICE_ACTION_WAIT_MAX_SECONDS))
    action = _mqtt_bridge.wait_for_action(since_seq=since_seq, timeout=timeout, device_id=device_id)

    if action:
        return jsonify({
            "ok": True,
            "matched": True,
            "action": action,
        })

    return jsonify({
        "ok": True,
        "matched": False,
        "since_seq": since_seq,
    })


# ── 路由: CC 控制 API ──

@app.route("/api/cc/start", methods=["POST"])
def api_cc_start():
    """启动或获取托管 Claude Code session"""
    from cc_controller import controller
    data = request.get_json(silent=True) or {}
    work_dir = data.get("work_dir", os.getcwd())
    model = data.get("model", "")
    resume = data.get("resume_session_id", "")
    result = controller.start_session(work_dir, model=model, resume_session_id=resume)
    return jsonify(result)


@app.route("/api/cc/send", methods=["POST"])
def api_cc_send():
    """发送 prompt 到托管 session"""
    from cc_controller import controller
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"ok": False, "error": "empty prompt"}), 400
    result = controller.send_prompt(prompt)
    return jsonify(result)


@app.route("/api/cc/state")
def api_cc_state():
    """获取托管 session 状态"""
    from cc_controller import controller
    return jsonify(controller.get_state())


@app.route("/api/cc/close", methods=["POST"])
def api_cc_close():
    """关闭托管 session"""
    from cc_controller import controller
    result = controller.close_session()
    return jsonify(result)


# ── MQTT Bridge 初始化 ──

_mqtt_bridge = None
_tts_service = None
_queue_drainer_started = False
_latest_hook_lock = threading.Lock()
_latest_hook_payload = {}


def _on_tts_ready(payload, speech):
    """TTS 合成完成后单独发布语音，不阻塞也不覆盖状态流。"""
    if not isinstance(payload, dict) or not isinstance(speech, dict):
        return

    allowed, _ = session_route_allows(payload)
    if _mqtt_bridge and allowed:
        _mqtt_bridge.publish_speech(speech, payload)


def _handle_mqtt_tts(request_id: str, text: str, voice: str):
    """处理 MQTT TTS 请求：调用 IndexTTS2 → 上传到 server → 发布响应。"""
    import base64 as _b64
    import urllib.request
    import urllib.parse

    # 确定 TTS 参数
    base_url = ""
    voice_prompt = voice
    if _tts_service:
        base_url = _tts_service.settings.local_base_url or ""
        if not voice_prompt:
            voice_prompt = _tts_service.settings.local_voice or "test_voice.wav"
    if not base_url:
        base_url = os.environ.get("TTS_LOCAL_BASE_URL", "http://127.0.0.1:9877")
    if not voice_prompt:
        voice_prompt = "test_voice.wav"

    topic_prefix = os.environ.get("TOPIC_PREFIX", "whitebox")
    response_topic = f"{topic_prefix}/tts/response"

    try:
        # 1. 调用 IndexTTS2 合成
        from tts_service import _indextts_async_synth
        audio_bytes = _indextts_async_synth(base_url.rstrip("/"), text, voice_prompt)
        print(f"[tts-mqtt] 合成完成: id={request_id} size={len(audio_bytes)} bytes")

        # 2. 上传到 server
        mqtt_host = os.environ.get("MQTT_HOST", "")
        server_port = os.environ.get("SERVER_PORT", "8080")
        server_url = os.environ.get("TTS_UPLOAD_URL", f"http://{mqtt_host}:{server_port}")
        upload_url = f"{server_url.rstrip('/')}/api/tts/upload"

        boundary = "----TTSMqttUploadBoundary"
        body_parts = []
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio_id\"\r\n\r\n{request_id}\r\n".encode("utf-8"))
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"{request_id}.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode("utf-8"))
        body_parts.append(audio_bytes)
        body_parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        upload_data = b"".join(body_parts)

        req = urllib.request.Request(upload_url, data=upload_data, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        resp = urllib.request.urlopen(req, timeout=30)
        resp_body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        print(f"[tts-mqtt] 上传完成: {resp_body}")

        # 3. 发布 MQTT 响应（不含音频，server 已收到文件）
        if _mqtt_bridge and _mqtt_bridge.connected:
            response = json.dumps({
                "request_id": request_id,
                "ok": True,
                "audio_id": request_id,
                "format": "wav",
                "size": len(audio_bytes),
            }, ensure_ascii=False)
            _mqtt_bridge.client.publish(response_topic, response, qos=1)
            print(f"[tts-mqtt] 响应已发布: id={request_id}")

    except Exception as e:
        print(f"[tts-mqtt] 失败: id={request_id} error={e}")
        if _mqtt_bridge and _mqtt_bridge.connected:
            response = json.dumps({
                "request_id": request_id,
                "ok": False,
                "error": str(e),
            }, ensure_ascii=False)
            _mqtt_bridge.client.publish(response_topic, response, qos=1)


def init_mqtt_bridge():
    """启动常驻 MQTT bridge"""
    global _mqtt_bridge
    mqtt_host = os.environ.get("MQTT_HOST", "")
    if not mqtt_host:
        print("[mqtt] MQTT_HOST 未配置，跳过 bridge")
        return

    try:
        from mqtt_bridge import MQTTBridge
        from cc_controller import controller

        _mqtt_bridge = MQTTBridge(
            broker_host=mqtt_host,
            broker_port=int(os.environ.get("MQTT_PORT", "1883")),
            username=os.environ.get("MQTT_USERNAME", ""),
            password=os.environ.get("MQTT_PASSWORD", ""),
            topic_prefix=os.environ.get("TOPIC_PREFIX", "whitebox"),
        )
        # 设备 action 回调 → CC controller
        _mqtt_bridge.set_action_callback(controller.handle_device_action)
        # TTS 请求回调
        _mqtt_bridge.set_tts_handler(_handle_mqtt_tts)
        _mqtt_bridge.start()
        print(f"[mqtt] Bridge 已启动 → {mqtt_host}")
    except ImportError as e:
        print(f"[mqtt] 依赖缺失: {e}")
    except Exception as e:
        print(f"[mqtt] Bridge 启动失败: {e}")


def init_tts_service():
    """启动 TTS 编排器。"""
    global _tts_service
    if _tts_service is not None:
        return

    try:
        from tts_service import create_orchestrator

        cache_dir = os.path.join(os.path.dirname(__file__), "data")
        _tts_service = create_orchestrator(APP_ROLE, cache_dir, on_ready=_on_tts_ready)
        print(f"[tts] Service 已启动 → role={APP_ROLE}")
    except Exception as e:
        print(f"[tts] Service 启动失败: {e}")


# ── Queue Drainer ──

def start_queue_drainer():
    """后台队列清理：将 hooks/.queue 中缓存的状态通过 bridge 发布"""
    global _queue_drainer_started
    if _queue_drainer_started:
        return
    _queue_drainer_started = True

    def _drain():
        import time
        queue_dir = os.path.join(os.path.dirname(__file__), "hooks", ".queue")
        while True:
            try:
                if _mqtt_bridge and _mqtt_bridge.connected and os.path.isdir(queue_dir):
                    files = sorted(
                        [f for f in os.listdir(queue_dir) if f.endswith(".json")],
                        reverse=True,  # 最新的优先
                    )
                    if files:
                        # 只取最新的，丢弃旧的
                        latest = files[0]
                        path = os.path.join(queue_dir, latest)
                        try:
                            with open(path, "r", encoding="utf-8") as f:
                                payload = json.load(f)
                            if isinstance(payload, dict):
                                allowed, _ = session_route_allows(payload)
                                if allowed:
                                    _mqtt_bridge.publish_state(payload)
                        except Exception:
                            pass
                        # 清理所有队列文件
                        for fname in files:
                            try:
                                os.remove(os.path.join(queue_dir, fname))
                            except OSError:
                                pass
            except Exception:
                pass
            time.sleep(2)

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    print("[queue] Drainer 已启动")


def start_session_sync():
    """定期把本机 session 列表同步到云端 server。"""
    import time
    import urllib.request

    cloud_host = os.environ.get("MQTT_HOST", "")
    cloud_port = os.environ.get("SERVER_PORT", "8080")
    if not cloud_host:
        print("[session-sync] MQTT_HOST 未配置，跳过同步")
        return

    sync_url = f"http://{cloud_host}:{cloud_port}/api/sessions/sync"
    sync_interval = int(os.environ.get("SESSION_SYNC_INTERVAL", "30"))

    def _sync():
        while True:
            try:
                sessions = list_sessions(limit=300)
                body = json.dumps({"sessions": sessions}, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(sync_url, data=body, method="POST")
                req.add_header("Content-Type", "application/json")
                resp = urllib.request.urlopen(req, timeout=10)
                result = json.loads(resp.read().decode("utf-8", errors="ignore"))
                print(f"[session-sync] 已同步 {result.get('received', 0)} 个 session 到云端")
            except Exception as e:
                print(f"[session-sync] 同步失败: {e}")
            time.sleep(sync_interval)

    t = threading.Thread(target=_sync, daemon=True)
    t.start()
    print(f"[session-sync] 已启动，每 {sync_interval}s 同步到 {sync_url}")


def start_background_services():
    """确保 MQTT / 队列清理 / TTS 都只启动一次。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    init_mqtt_bridge()
    start_queue_drainer()
    init_tts_service()
    start_session_sync()


# Flask debug reloader 的真正运行进程里尽早启动一次。
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" and "pytest" not in sys.modules:
    start_background_services()


@app.before_request
def _bootstrap_background_services():
    start_background_services()


if __name__ == "__main__":
    print(f"cc-dashboard: http://localhost:5000")
    print(f"本地会话: {CLAUDE_HOME}")
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_background_services()
    app.run(host="0.0.0.0", port=5000, debug=True)
