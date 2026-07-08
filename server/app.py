"""
Whitebox Server — 单文件 Flask 全家桶
MQTT 桥接 + 健康页 + 消息记录 + Web 面板
"""

import json
import os
import glob
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, Response, request, send_file
import paho.mqtt.client as mqtt

from tts_service import (
    SETTINGS_KEYS,
    SENSITIVE_KEYS,
    create_orchestrator,
    extract_speech_text_from_payload,
    settings_to_dict,
)

app = Flask(__name__)

# ── 配置 ──

_env = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(_env):
    with open(_env, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USERNAME", "whitebox")
MQTT_PASS = os.environ.get("MQTT_PASSWORD", "change_me")
PREFIX = os.environ.get("TOPIC_PREFIX", "whitebox")
HOOK_STALE_SECONDS = int(os.environ.get("HOOK_STATE_STALE_SECONDS", "1800"))
HOOK_HTTP_TOKEN = os.environ.get("HOOK_HTTP_TOKEN", MQTT_PASS)
DEVICE_VOLUME = int(os.environ.get("DEVICE_VOLUME", "70"))
CLAUDE_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
SESSION_TAIL_BYTES = 128 * 1024
INTERRUPTED_MARKER = "[Request interrupted by user]"
APP_ROLE = "server"
INTERNAL_HOOK_PREFIXES = (
    "Caveat:",
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)

# ── 状态存储 ──

lock = threading.Lock()
state_changed = threading.Condition(lock)
state_version = 0
pc_state = {}                        # whitebox/pc/state 最新状态
pc_state_ts = None
devices = {}                         # device_id → {ack, availability, ack_ts, avail_ts, online}
history = deque(maxlen=200)          # 消息记录
mqtt_connected = False
mqtt_client = None
mqtt_topic_state = f"{PREFIX}/pc/state"
mqtt_topic_speech = f"{PREFIX}/pc/speech"
tts_service = None
_synced_sessions = {}               # session_id → {session_id, project_key, mtime, size, source}
_sessions_lock = threading.Lock()
session_route = {
    "mode": os.environ.get("SESSION_ROUTE_MODE", "all"),
    "session_id": os.environ.get("SESSION_ROUTE_SESSION_ID", ""),
    "project_key": os.environ.get("SESSION_ROUTE_PROJECT_KEY", ""),
}
DB_PATH = os.path.join(os.path.dirname(__file__), "server.db")


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    row = conn.execute("SELECT value FROM settings WHERE key='session_route'").fetchone()
    if row:
        saved = json.loads(row[0])
        session_route.update(saved)
    conn.close()


def _save_session_route():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('session_route', ?)",
        (json.dumps(session_route),),
    )
    conn.commit()
    conn.close()


_init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    """解析 ISO 时间，失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_internal_hook_message(value):
    """识别 hook 带来的内部通知，避免误显示为用户新请求。"""
    text = str(value or "").strip()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in INTERNAL_HOOK_PREFIXES)


def is_internal_hook_payload(payload):
    """识别会把已结束任务重新点亮的内部 hook payload。"""
    if not isinstance(payload, dict):
        return False
    if payload.get("source") != "claude-hook":
        return False
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return False

    latest = payload.get("latest_message") or {}
    candidates = (
        payload.get("hook_message", ""),
        latest.get("hook_message", ""),
        latest.get("text", ""),
    )
    return any(is_internal_hook_message(item) for item in candidates)


def extract_session_text(content):
    """从 Claude 会话 JSONL 的 message.content 中提取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return ""


def is_interrupted_session_message(value):
    """识别用户手动中断会话的内部标记。"""
    return str(value or "").strip().startswith(INTERRUPTED_MARKER)


def normalize_transcript_path(value):
    """清理 hook 上报的 transcript_path。"""
    text = str(value or "").strip()
    return text


def build_project_key_from_work_dir(work_dir):
    """按 Claude projects 目录的命名习惯生成项目 key。"""
    text = str(work_dir or "").strip()
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


def build_legacy_project_key_from_work_dir(work_dir):
    """兼容旧版 hook 的 project_key 生成方式。"""
    text = str(work_dir or "").strip()
    if not text:
        return ""
    return (
        text.replace("\\", "/")
        .replace(":", "")
        .replace("/", "-")
        .lstrip("-")
    )


def iter_session_path_candidates(payload):
    """枚举当前 payload 可能对应的 Claude 会话路径。"""
    if not isinstance(payload, dict):
        return

    transcript_path = normalize_transcript_path(payload.get("transcript_path"))
    if transcript_path:
        yield transcript_path

    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return

    project_key = str(payload.get("project_key") or "").strip()
    work_dir = str(payload.get("work_dir") or "").strip()
    candidates = []
    seen = set()

    for key in (
        project_key,
        build_project_key_from_work_dir(work_dir),
        build_legacy_project_key_from_work_dir(work_dir),
    ):
        if key and key not in seen:
            seen.add(key)
            candidates.append(os.path.join(CLAUDE_PROJECTS_DIR, key, f"{session_id}.jsonl"))

    generic_key = re.sub(r"[^A-Za-z0-9]+", "-", work_dir).strip("-")
    if generic_key and generic_key not in seen:
        seen.add(generic_key)
        candidates.append(os.path.join(CLAUDE_PROJECTS_DIR, generic_key, f"{session_id}.jsonl"))

    for path in candidates:
        yield path

    # 兜底：旧 payload 的 project_key 可能不准，直接按 session_id 反查。
    pattern = os.path.join(CLAUDE_PROJECTS_DIR, "*", f"{session_id}.jsonl")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for path in matches:
        if path not in candidates:
            yield path


def get_session_jsonl_path(payload):
    """根据 hook payload 定位当前会话的 jsonl 文件。"""
    for path in iter_session_path_candidates(payload):
        if os.path.isfile(path):
            return path
    return ""


def read_session_tail_lines(path, max_bytes=SESSION_TAIL_BYTES):
    """仅读取会话文件尾部，避免每次刷新都全量扫描。"""
    if not path or not os.path.isfile(path):
        return []

    try:
        file_size = os.path.getsize(path)
        with open(path, "rb") as f:
            if file_size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            data = f.read()
    except OSError:
        return []

    return data.decode("utf-8", errors="ignore").splitlines()


def session_interrupted_after_payload(payload):
    """判断当前 hook 状态之后，会话里是否出现了用户中断。"""
    if not isinstance(payload, dict):
        return False

    updated_at = parse_iso(payload.get("updated_at")) or parse_iso(pc_state_ts)
    path = get_session_jsonl_path(payload)
    if not path:
        return False

    for line in reversed(read_session_tail_lines(path)):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_ts = parse_iso(entry.get("timestamp"))
        if updated_at and entry_ts and entry_ts <= updated_at:
            break

        if entry.get("type") != "user":
            continue

        text = extract_session_text((entry.get("message") or {}).get("content", ""))
        if is_interrupted_session_message(text):
            return True
        if text and not is_internal_hook_message(text):
            # 更晚的真实用户输入说明已经进入下一轮，不要把它误判为 idle。
            return False

    return False


def should_check_session_interrupt(payload):
    """仅对 hook 驱动的工作态执行中断补偿。"""
    if not isinstance(payload, dict):
        return False
    if payload.get("source") != "claude-hook":
        return False
    if payload.get("interrupted"):
        return False
    return payload.get("status") in {"thinking", "cooking", "marinating"}


def build_interrupted_idle_payload(payload):
    """基于现有状态合成一个“已中断”的 idle payload。"""
    new_payload = dict(payload)
    latest = dict(new_payload.get("latest_message") or {})
    latest["role"] = latest.get("role", "hook")
    latest["type"] = "interrupted"
    latest["text"] = "任务已取消"
    latest["hook_message"] = INTERRUPTED_MARKER

    new_payload["status"] = "idle"
    new_payload["gif"] = 3
    new_payload["interrupted"] = True
    new_payload["hook_event_name"] = "Interrupted"
    new_payload["hook_message"] = INTERRUPTED_MARKER
    new_payload["latest_message"] = latest
    new_payload["updated_at"] = now_iso()
    return new_payload


def reconcile_interrupted_session():
    """发现用户已中断会话时，立即把展示状态和 MQTT 状态收敛回 idle。"""
    global pc_state, pc_state_ts, state_version

    with state_changed:
        current = dict(pc_state) if isinstance(pc_state, dict) else {}

    if not should_check_session_interrupt(current):
        return False
    if not session_interrupted_after_payload(current):
        return False

    new_payload = build_interrupted_idle_payload(current)

    with state_changed:
        latest_current = dict(pc_state) if isinstance(pc_state, dict) else {}
        if not should_check_session_interrupt(latest_current):
            return False
        if not session_interrupted_after_payload(latest_current):
            return False

        pc_state = new_payload
        pc_state_ts = now_iso()
        state_version += 1
        state_changed.notify_all()

    add_history("hook", "session/interrupted", new_payload)
    publish_pc_state(new_payload)
    return True


def effective_pc_state():
    """hook 工作态超时后展示为 idle，避免 retained cooking 长时间卡住。"""
    pc = dict(pc_state)
    if is_internal_hook_payload(pc):
        pc["status"] = "idle"
        pc["gif"] = 3
        pc["internal_ignored"] = True
        latest = dict(pc.get("latest_message") or {})
        latest["role"] = latest.get("role", "hook")
        latest["type"] = "internal-notification"
        latest["text"] = "内部任务通知已忽略"
        pc["latest_message"] = latest
        return pc

    if pc.get("source") != "claude-hook":
        return pc
    if pc.get("status") not in {"cooking", "thinking"}:
        return pc

    updated_at = parse_iso(pc.get("updated_at"))
    if not updated_at:
        updated_at = parse_iso(pc_state_ts)
    if not updated_at:
        return pc

    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if age <= HOOK_STALE_SECONDS:
        return pc

    pc["status"] = "idle"
    pc["gif"] = 3
    pc["stale"] = True
    pc["stale_after_seconds"] = HOOK_STALE_SECONDS
    latest = dict(pc.get("latest_message") or {})
    latest["role"] = latest.get("role", "hook")
    latest["type"] = "stale-idle"
    latest["text"] = "hook 状态超时，自动回到 idle"
    pc["latest_message"] = latest
    return pc


def add_history(direction, topic, payload, hardware=False):
    """追加消息记录（同 topic 相同内容不重复记录）"""
    # 去重：与同 topic 的最后一条比较，忽略时间戳字段
    skip_keys = {"ts", "updated_at", "seq"}
    for item in reversed(history):
        if item["topic"] == topic:
            old_data = {k: v for k, v in item["data"].items() if k not in skip_keys}
            new_data = {k: v for k, v in payload.items() if k not in skip_keys}
            if old_data == new_data:
                return
            break

    history.append({
        "ts": now_iso(),
        "dir": direction,
        "topic": topic,
        "data": payload,
        "hardware": bool(hardware),
    })


def session_route_snapshot():
    """返回当前硬件推送的 session 过滤配置。"""
    mode = session_route.get("mode") or "all"
    if mode not in {"all", "single"}:
        mode = "all"
    return {
        "mode": mode,
        "session_id": session_route.get("session_id", ""),
        "project_key": session_route.get("project_key", ""),
    }


def is_payload_allowed_by_session_route(payload):
    """判断 hook payload 是否允许更新硬件状态。"""
    route = session_route_snapshot()
    if route["mode"] != "single":
        return True, ""

    expected_sid = route.get("session_id", "")
    expected_project = route.get("project_key", "")
    if not expected_sid:
        return False, "no_selected_session"

    payload_sid = str(payload.get("session_id") or "")
    payload_project = str(payload.get("project_key") or "")
    if payload_sid != expected_sid:
        return False, "session_mismatch"
    if expected_project and payload_project and payload_project != expected_project:
        return False, "project_mismatch"
    return True, ""


def apply_pc_state(payload, history_topic=None, history_dir="recv", record_history=True, history_hardware=False):
    """应用 PC 状态 payload。"""
    global pc_state, pc_state_ts, state_version
    if is_internal_hook_payload(payload):
        return False
    if record_history:
        add_history(history_dir, history_topic or f"{PREFIX}/pc/state", payload, hardware=history_hardware)
    with state_changed:
        pc_state = payload
        pc_state_ts = now_iso()
        state_version += 1
        state_changed.notify_all()
    return True


def publish_pc_state(payload):
    """发布 PC 状态到 MQTT broker，供硬件端订阅。"""
    if not isinstance(payload, dict):
        return False

    # 注入设备音量
    payload.setdefault("volume", DEVICE_VOLUME)

    if not mqtt_client or not mqtt_connected:
        print("[mqtt] 跳过发布 pc/state：MQTT 未连接")
        return False

    data = json.dumps(payload, ensure_ascii=False)
    result = mqtt_client.publish(mqtt_topic_state, data, qos=1, retain=False)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        print(f"[mqtt] 发布 pc/state 失败 rc={result.rc}")
        return False
    add_history("send", mqtt_topic_state, payload, hardware=True)
    return True


def publish_speech(speech, source_payload=None):
    """单独发布 TTS 语音消息，不影响 pc/state 状态流。"""
    if not isinstance(speech, dict):
        return False

    source_payload = source_payload if isinstance(source_payload, dict) else {}
    msg = {
        "type": "speech",
        "seq": int(source_payload.get("seq", speech.get("seq", 0)) or 0),
        "status": str(source_payload.get("status") or ""),
        "session_id": str(source_payload.get("session_id") or speech.get("session_id") or ""),
        "project_key": str(source_payload.get("project_key") or ""),
        "speech": dict(speech),
        "volume": DEVICE_VOLUME,
    }
    msg["speech"]["seq"] = int(msg["seq"] or msg["speech"].get("seq", 0) or 0)
    msg["speech"]["session_id"] = msg["session_id"]

    if not mqtt_client or not mqtt_connected:
        print("[mqtt] 跳过发布 pc/speech：MQTT 未连接")
        return False

    data = json.dumps(msg, ensure_ascii=False)
    result = mqtt_client.publish(mqtt_topic_speech, data, qos=1, retain=False)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        print(f"[mqtt] 发布 pc/speech 失败 rc={result.rc}")
        return False
    add_history("tts", mqtt_topic_speech, msg, hardware=True)
    return True


def status_to_gif_value(status):
    """把状态映射成设备兼容的 gif 编号。"""
    return {"cooking": 1, "thinking": 2, "idle": 3, "offline": 3, "marinating": 4}.get(status, 3)


def make_tts_test_payload(text, base_payload=None):
    """构造一次手动 TTS 测试用状态，保证每次点击都会产生新的 speech_id。"""
    base = dict(base_payload) if isinstance(base_payload, dict) else {}
    seq = int(time.time() * 1000) % 2147483647
    status = str(base.get("status") or "idle")
    if status not in {"idle", "offline", "thinking", "cooking", "marinating"}:
        status = "idle"
    source_id = f"tts-test:{seq}"
    return {
        "source": "tts-test",
        "hook_event_name": "TtsTest",
        "hook_message": text,
        "status": status,
        "gif": status_to_gif_value(status),
        "seq": seq,
        "session_id": str(base.get("session_id") or "tts-test"),
        "project_key": str(base.get("project_key") or ""),
        "msg_count": int(base.get("msg_count", 0) or 0),
        "latest_message": {
            "role": "assistant",
            "type": "tts-test",
            "text": text[:500],
            "hook_message": text[:500],
            "ts": now_iso(),
        },
        "speech_candidate": {
            "kind": "assistant_progress",
            "source": "manual",
            "source_id": source_id,
            "text": text,
            "transcript_ts": now_iso(),
        },
        "speech_source_id": source_id,
        "updated_at": now_iso(),
    }


def _on_tts_ready(payload, speech):
    """TTS 合成完成后单独发布语音，不阻塞也不覆盖状态流。"""
    if not isinstance(payload, dict) or not isinstance(speech, dict):
        return
    publish_speech(speech, payload)


def init_tts_service():
    """启动 TTS 编排器。"""
    global tts_service
    if tts_service is not None:
        return

    try:
        cache_dir = os.path.join(os.path.dirname(__file__), "data")
        tts_service = create_orchestrator(APP_ROLE, cache_dir, on_ready=_on_tts_ready)
        print(f"[tts] Service 已启动 → role={APP_ROLE}")
    except Exception as e:
        print(f"[tts] Service 启动失败: {e}")


def decode_mqtt_payload(topic, payload_bytes):
    """按 topic 解析 MQTT payload。"""
    text = payload_bytes.decode("utf-8", errors="replace")

    # 设备 availability 使用纯文本 online/offline，不是 JSON。
    if topic.endswith("/availability"):
        return {"status": text.strip()}

    return json.loads(text)


# ── MQTT ──

def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    mqtt_connected = True
    client.subscribe(f"{PREFIX}/pc/state", qos=1)
    client.subscribe(f"{PREFIX}/pc/speech", qos=1)
    client.subscribe(f"{PREFIX}/device/+/ack", qos=1)
    client.subscribe(f"{PREFIX}/device/+/availability", qos=1)
    client.subscribe(f"{PREFIX}/device/+/action", qos=1)
    client.subscribe(f"{PREFIX}/tts/response", qos=1)
    print(f"[mqtt] 已连接，订阅 {PREFIX}/#")

    # 注入 mqtt_client 到 TTS 服务
    if tts_service:
        tts_service.set_mqtt_client(client, PREFIX)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    global mqtt_connected
    mqtt_connected = False
    print(f"[mqtt] 断开连接 reason={reason_code}")


def on_message(client, userdata, msg):
    global pc_state, pc_state_ts, state_version

    topic = msg.topic

    try:
        payload = decode_mqtt_payload(topic, msg.payload)
    except Exception:
        raw = msg.payload.decode("utf-8", errors="replace")
        print(f"[mqtt] 无法解析 {topic}: {raw[:200]}")
        return

    if topic == f"{PREFIX}/pc/state":
        apply_pc_state(payload, history_hardware=True)
        return

    if topic == f"{PREFIX}/pc/speech":
        if isinstance(payload, dict):
            add_history("tts", topic, payload, hardware=True)
        return

    if topic == f"{PREFIX}/tts/response":
        if tts_service and isinstance(payload, dict):
            tts_service.handle_mqtt_response(
                request_id=str(payload.get("request_id", "")),
                ok=bool(payload.get("ok", False)),
                audio_base64=str(payload.get("audio_base64", "")),
                error=str(payload.get("error", "")),
            )
        return

    add_history("recv", topic, payload)

    changed = False
    with state_changed:
        if "/ack" in topic:
            dev_id = topic.split("/")[2] if len(topic.split("/")) > 2 else "unknown"
            if dev_id not in devices:
                devices[dev_id] = {}
            devices[dev_id]["ack"] = payload
            devices[dev_id]["ack_ts"] = now_iso()
            devices[dev_id]["online"] = True
            changed = True

        elif "/availability" in topic:
            dev_id = topic.split("/")[2] if len(topic.split("/")) > 2 else "unknown"
            if dev_id not in devices:
                devices[dev_id] = {}
            devices[dev_id]["availability"] = payload
            devices[dev_id]["avail_ts"] = now_iso()
            devices[dev_id]["online"] = payload.get("status") == "online"
            changed = True

        elif "/action" in topic:
            dev_id = topic.split("/")[2] if len(topic.split("/")) > 2 else "unknown"
            if dev_id not in devices:
                devices[dev_id] = {}
            devices[dev_id]["last_action"] = payload
            devices[dev_id]["action_ts"] = now_iso()
            changed = True

        if changed:
            state_version += 1
            state_changed.notify_all()


def mqtt_loop():
    global mqtt_client
    client = mqtt.Client(
        client_id="whitebox-server",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    mqtt_client = client
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            print(f"[mqtt] 连接失败: {e}，5 秒后重试")
            time.sleep(5)


def session_reconcile_loop():
    """后台轮询会话中断信号，减少从中断到 idle 的等待时间。"""
    while True:
        try:
            reconcile_interrupted_session()
        except Exception as e:
            print(f"[session] reconcile failed: {e}")
        time.sleep(1)


# ── 路由 ──

@app.after_request
def no_store_api(response):
    """API 响应不缓存，避免浏览器显示旧状态。"""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "mqtt": mqtt_connected, "ts": now_iso()})


@app.route("/api/state")
def api_state():
    reconcile_interrupted_session()
    with lock:
        return jsonify({
            "ok": True,
            "mqtt": mqtt_connected,
            "pc": effective_pc_state(),
            "pc_updated_at": pc_state_ts,
            "devices": {k: dict(v) for k, v in devices.items()},
            "session_route": session_route_snapshot(),
        })


@app.route("/api/session/route", methods=["GET", "POST"])
def api_session_route():
    """读取或设置发往硬件的 session 过滤模式。"""
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
        return jsonify({"ok": False, "error": "session_id required for single mode"}), 400

    with lock:
        session_route["mode"] = mode
        session_route["session_id"] = session_id if mode == "single" else ""
        session_route["project_key"] = project_key if mode == "single" else ""
    _save_session_route()

    add_history("route", "session/route", session_route_snapshot())
    return jsonify({"ok": True, "route": session_route_snapshot()})


@app.route("/api/hook/state", methods=["POST"])
def api_hook_state():
    """HTTP hook 状态入口，同时刷新页面状态并同步到 MQTT。"""
    if HOOK_HTTP_TOKEN:
        token = request.headers.get("X-Whitebox-Token", "")
        if token != HOOK_HTTP_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    if is_internal_hook_payload(payload):
        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": "internal_user_prompt_submit",
        })

    add_history("hook", "http/hook/state", payload)

    # forward=1 表示 cc-dashboard 转发的
    is_forward = str(request.args.get("forward", "")).strip() in {"1", "true", "yes"}

    # 所有 hook 都检查 session route（包括转发的）
    allowed, blocked_reason = is_payload_allowed_by_session_route(payload)

    # 始终更新 pc_state（dashboard 页面展示最新状态）
    applied = apply_pc_state(payload, history_topic="http/hook/state", history_dir="hook", record_history=False)

    # 只有 session route 允许时才发布到 MQTT 和触发 TTS
    mqtt_published = False
    if applied and allowed:
        mqtt_published = publish_pc_state(payload)
        if tts_service:
            tts_service.schedule_from_payload(payload)

    if not allowed:
        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": blocked_reason,
            "status": payload.get("status"),
            "seq": payload.get("seq", 0),
            "session_route": session_route_snapshot(),
        })

    return jsonify({
        "ok": True,
        "status": payload.get("status"),
        "seq": payload.get("seq", 0),
        "ts": pc_state_ts,
        "mqtt": mqtt_connected,
        "mqtt_published": mqtt_published,
    })


@app.route("/api/tts/speak", methods=["POST"])
def api_tts_speak():
    """手动触发 TTS 合成，用于调试本地 IndexTTS / 云端 TTS。"""
    if not tts_service or not tts_service.enabled():
        return jsonify({"ok": False, "error": "tts disabled"}), 503

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    push_to_device = str(body.get("push_to_device", "")).lower() in {"1", "true", "yes", "on"}
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    text = str(body.get("text", "") or "").strip()
    if not text:
        text = extract_speech_text_from_payload(payload, max_chars=tts_service.settings.max_chars)
    if not text:
        return jsonify({"ok": False, "error": "empty text"}), 400

    if push_to_device:
        with lock:
            current = dict(pc_state) if isinstance(pc_state, dict) else {}
        payload = make_tts_test_payload(text, base_payload=current)

    try:
        speech = tts_service.speak_now(payload, text)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    pushed = False
    if push_to_device:
        pushed = publish_speech(speech, payload)
    elif isinstance(payload, dict) and payload.get("seq", 0):
        pushed = publish_speech(speech, payload)

    return jsonify({"ok": True, "speech": speech, "pushed": pushed, "payload": payload if push_to_device else None})


@app.route("/api/tts/audio/<speech_id>.mp3")
def api_tts_audio(speech_id):
    """返回缓存的 TTS MP3，供 ESP32 直接拉取。"""
    if not tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503

    path = tts_service.audio_path(speech_id)
    if not path.is_file():
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, mimetype="audio/mpeg", conditional=True, download_name=path.name)


@app.route("/api/tts/audio/<speech_id>.wav")
def api_tts_audio_wav(speech_id):
    """返回缓存的 TTS WAV。"""
    if not tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503

    cache_dir = tts_service.settings.cache_path
    path = cache_dir / f"{speech_id}.wav"
    if not path.is_file():
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, mimetype="audio/wav", conditional=True, download_name=path.name)


@app.route("/api/tts/health")
def api_tts_health():
    """查看 TTS 服务状态。"""
    if not tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503
    return jsonify(tts_service.health())


@app.route("/api/tts/upload", methods=["POST"])
def api_tts_upload():
    """接收 cc-dashboard 上传的 TTS 音频文件。"""
    if not tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503

    audio_id = request.form.get("audio_id", "").strip()
    if not audio_id:
        return jsonify({"ok": False, "error": "missing audio_id"}), 400

    audio_file = request.files.get("audio_file")
    if not audio_file:
        return jsonify({"ok": False, "error": "missing audio_file"}), 400

    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"ok": False, "error": "empty audio"}), 400

    cache_dir = tts_service.settings.cache_path
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{audio_id}.wav"
    tmp_path = path.with_suffix(".wav.tmp")
    with open(tmp_path, "wb") as f:
        f.write(audio_bytes)
    os.replace(tmp_path, path)

    print(f"[tts] 音频已上传: {audio_id}.wav ({len(audio_bytes)} bytes)")
    return jsonify({"ok": True, "audio_id": audio_id, "size": len(audio_bytes)})


def update_env_file(settings_dict):
    """更新 .env 文件中的配置，保留已有非覆盖项。"""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()

    existing = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k not in existing:
                existing[k] = i

    for k, v in settings_dict.items():
        entry = f"{k}={v}\n"
        if k in existing:
            lines[existing[k]] = entry
        else:
            lines.append(entry)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@app.route("/api/tts/config", methods=["GET"])
def api_tts_config_get():
    """返回当前 TTS 配置。"""
    if not tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503
    return jsonify({
        "ok": True,
        "settings": settings_to_dict(tts_service.settings),
        "enabled": tts_service.enabled(),
    })


@app.route("/api/tts/config", methods=["POST"])
def api_tts_config_post():
    """更新 TTS 配置并热加载。"""
    if not tts_service:
        return jsonify({"ok": False, "error": "tts service unavailable"}), 503

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    incoming = body.get("settings") or body
    if not isinstance(incoming, dict):
        return jsonify({"ok": False, "error": "invalid settings"}), 400

    # 只接受已知的 key
    to_update = {}
    for k, v in incoming.items():
        if k in SETTINGS_KEYS:
            # 敏感字段：空字符串表示不修改
            if k in SENSITIVE_KEYS and not str(v).strip():
                continue
            to_update[k] = str(v).strip()

    if not to_update:
        return jsonify({"ok": False, "error": "no valid settings provided"}), 400

    # 更新环境变量
    for k, v in to_update.items():
        os.environ[k] = v

    # 写入 .env 文件
    update_env_file(to_update)

    # 热加载
    cache_dir = os.path.join(os.path.dirname(__file__), "data")
    try:
        tts_service.reload_settings(APP_ROLE, cache_dir)
    except Exception as e:
        return jsonify({"ok": False, "error": f"reload failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "settings": settings_to_dict(tts_service.settings),
        "enabled": tts_service.enabled(),
    })


@app.route("/api/sessions")
def api_sessions():
    """返回 cc-dashboard 同步过来的 session 列表 + hook 捕获的 session。"""
    with _sessions_lock:
        merged = dict(_synced_sessions)
    # 也把 hook 里见过的 session 加进来
    with state_changed:
        hook_sid = str((pc_state or {}).get("session_id") or "").strip()
        hook_proj = str((pc_state or {}).get("project_key") or "").strip()
    if hook_sid and hook_sid not in merged:
        merged[hook_sid] = {
            "session_id": hook_sid,
            "project_key": hook_proj,
            "source": "hook",
            "mtime": pc_state_ts or now_iso(),
        }
    sessions = sorted(merged.values(), key=lambda s: s.get("mtime", ""), reverse=True)
    return jsonify({"ok": True, "sessions": sessions, "count": len(sessions)})


@app.route("/api/sessions/sync", methods=["POST"])
def api_sessions_sync():
    """接收 cc-dashboard 同步的 session 列表。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    sessions = body.get("sessions")
    if not isinstance(sessions, list):
        return jsonify({"ok": False, "error": "sessions must be a list"}), 400

    with _sessions_lock:
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("session_id") or "").strip()
            if not sid:
                continue
            _synced_sessions[sid] = {
                "session_id": sid,
                "project_key": str(s.get("project_key") or ""),
                "mtime": str(s.get("mtime_iso") or s.get("mtime") or ""),
                "size": s.get("size", 0),
                "source": "cc-dashboard",
            }

    return jsonify({"ok": True, "received": len(sessions), "stored": len(_synced_sessions)})


@app.route("/api/session/push", methods=["POST"])
def api_session_push():
    """选择一个 session 推送到 ESP32。构造 pc_state payload 发布到 MQTT。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    session_id = str(body.get("session_id", "")).strip()
    project_key = str(body.get("project_key", "")).strip()
    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400

    # 用 hook 里最后收到的该 session 信息构造 payload
    with state_changed:
        current = dict(pc_state) if isinstance(pc_state, dict) else {}

    payload = {
        "source": "session-push",
        "status": body.get("status", "idle"),
        "gif": 3,
        "seq": int(time.time()) % 100000,
        "session_id": session_id,
        "project_key": project_key,
        "msg_count": body.get("msg_count", 0),
        "latest_message": {
            "role": "assistant",
            "type": "session-push",
            "text": str(body.get("text", "") or "")[:500],
        },
        "updated_at": now_iso(),
    }

    apply_pc_state(payload, history_topic="session/push", history_dir="hook")
    publish_pc_state(payload)

    if tts_service:
        tts_service.schedule_from_payload(payload)

    return jsonify({"ok": True, "session_id": session_id, "status": payload["status"]})


@app.route("/api/history")
def api_history():
    n = max(1, min(200, int(request.args.get("n", 50))))
    hardware_only = str(request.args.get("hardware", "")).lower() in {"1", "true", "yes", "on"}
    direction = str(request.args.get("dir", "")).strip()
    topic = str(request.args.get("topic", "")).strip()
    source_items = list(history)
    if hardware_only:
        source_items = [item for item in source_items if item.get("hardware")]
    if direction:
        source_items = [item for item in source_items if item.get("dir") == direction]
    if topic:
        source_items = [item for item in source_items if item.get("topic") == topic]
    items = source_items[-n:]
    return jsonify({"ok": True, "history": items, "total": len(source_items)})


@app.route("/api/device/volume", methods=["GET"])
def api_device_volume_get():
    return jsonify({"ok": True, "volume": DEVICE_VOLUME})


@app.route("/api/device/volume", methods=["POST"])
def api_device_volume_set():
    global DEVICE_VOLUME
    data = request.get_json(silent=True) or {}
    vol = data.get("volume")
    if not isinstance(vol, (int, float)) or not (0 <= vol <= 100):
        return jsonify({"ok": False, "error": "volume must be 0-100"}), 400
    DEVICE_VOLUME = int(vol)
    update_env_file({"DEVICE_VOLUME": str(DEVICE_VOLUME)})
    # 立即推送含新音量的 pc/state
    with state_changed:
        current = dict(pc_state)
    current["volume"] = DEVICE_VOLUME
    pushed = publish_pc_state(current)
    add_history("send", "http/device/volume", {"volume": DEVICE_VOLUME}, hardware=True)
    return jsonify({"ok": True, "volume": DEVICE_VOLUME, "pushed": pushed})


@app.route("/api/events")
def api_events():
    """SSE 实时推送状态变化"""
    def gen():
        last_key = None
        while True:
            data = None
            reconcile_interrupted_session()
            with state_changed:
                pc = effective_pc_state()
                key = (state_version, pc.get("seq", 0), pc.get("status"), bool(pc.get("stale")))
                if key == last_key:
                    state_changed.wait(timeout=1)
                    pc = effective_pc_state()
                    key = (state_version, pc.get("seq", 0), pc.get("status"), bool(pc.get("stale")))
                if key == last_key:
                    continue
                last_key = key
                data = json.dumps({
                    "mqtt": mqtt_connected,
                    "pc": pc,
                    "pc_updated_at": pc_state_ts,
                    "devices": {k: dict(v) for k, v in devices.items()},
                })
            if data:
                yield f"data: {data}\n\n"

    return Response(gen(), mimetype="text/event-stream")


# ── 启动 ──

threading.Thread(target=mqtt_loop, daemon=True).start()
threading.Thread(target=session_reconcile_loop, daemon=True).start()
init_tts_service()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"[server] http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
