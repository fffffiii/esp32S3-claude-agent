"""
mqtt_bridge — cc-dashboard 常驻 MQTT 桥接
维持 MQTT 长连接，发布 pc/state，订阅 device action，转发给 CC controller。
"""

import json
import logging
import os
from collections import deque
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logger = logging.getLogger("mqtt_bridge")


class MQTTBridge:
    """常驻 MQTT 连接：发布状态 + 订阅设备 action"""

    def __init__(self, broker_host="127.0.0.1", broker_port=1883,
                 username="", password="", topic_prefix="whitebox"):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password
        self.prefix = topic_prefix
        self.topic_state = f"{topic_prefix}/pc/state"
        self.topic_speech = f"{topic_prefix}/pc/speech"
        self.topic_action_sub = f"{topic_prefix}/device/+/action"

        self.client = None
        self._connected = False
        self._lock = threading.Lock()
        self._running = False

        # 最新状态缓存（MQTT 断开时保留，重连后发布）
        self._latest_payload = None
        self._latest_seq = 0

        # 设备动作缓存：供 hook 长轮询等待
        self._action_lock = threading.Lock()
        self._action_cond = threading.Condition(self._action_lock)
        self._action_history = deque(maxlen=8)

        # action 回调
        self._action_callback = None

        # TTS 回调
        self._tts_handler = None

    def set_action_callback(self, cb):
        """设置设备 action 回调: cb(device_id, payload_dict)"""
        self._action_callback = cb

    def set_tts_handler(self, handler):
        """设置 TTS 请求处理回调: handler(request_id, text, voice) -> None
        handler 负责合成、上传、发布响应。
        """
        self._tts_handler = handler

    @staticmethod
    def normalize_action(action: str) -> str:
        """把设备动作统一映射成 continue/reject。"""
        value = str(action or "").strip().lower()
        if value in ("continue", "继续", "确定"):
            return "continue"
        if value in ("reject", "拒绝"):
            return "reject"
        return ""

    def record_action(self, device_id: str, payload: dict) -> dict | None:
        """记录设备动作并唤醒等待中的请求。"""
        action = self.normalize_action(payload.get("action", ""))
        if action not in ("continue", "reject"):
            return None

        try:
            last_seq = int(payload.get("last_seq", 0) or 0)
        except (TypeError, ValueError):
            last_seq = 0

        entry = {
            "device_id": device_id,
            "action": action,
            "source": str(payload.get("source", "") or ""),
            "last_seq": last_seq,
            "device_state": str(payload.get("state", "") or ""),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }

        with self._action_cond:
            self._action_history.append(entry)
            self._action_cond.notify_all()

        return entry

    def _find_matching_action_locked(self, since_seq: int, device_id: str = "") -> dict | None:
        """在缓存里找最新的可用动作。调用方需要先持有锁。"""
        if since_seq <= 0:
            return None

        device_id = str(device_id or "").strip()
        for entry in reversed(self._action_history):
            if device_id and entry["device_id"] != device_id:
                continue
            if entry["last_seq"] <= 0:
                continue
            if entry["last_seq"] >= since_seq:
                return dict(entry)
        return None

    def wait_for_action(self, since_seq: int, timeout: float, device_id: str = "") -> dict | None:
        """等待最近一次匹配的设备动作。"""
        try:
            since_seq = int(since_seq)
        except (TypeError, ValueError):
            since_seq = 0

        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 0.0

        deadline = time.monotonic() + max(0.0, timeout)
        with self._action_cond:
            while True:
                matched = self._find_matching_action_locked(since_seq, device_id=device_id)
                if matched:
                    return matched

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None

                self._action_cond.wait(timeout=remaining)

    def start(self):
        """启动 MQTT 长连接"""
        self._running = True
        client_id = f"whitebox-dashboard-{int(time.time() * 1000) % 100000}"
        self.client = mqtt.Client(
            client_id=client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if self.username:
            self.client.username_pw_set(self.username, self.password)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        # 在后台线程运行连接循环
        def _connect_loop():
            while self._running:
                try:
                    logger.info(f"Connecting to {self.broker_host}:{self.broker_port}")
                    self.client.connect(self.broker_host, self.broker_port, keepalive=60)
                    self.client.loop_forever()
                except Exception as e:
                    logger.warning(f"MQTT connect failed: {e}, retry in 5s")
                    time.sleep(5)

        t = threading.Thread(target=_connect_loop, daemon=True)
        t.start()
        logger.info("MQTT bridge started")

    def stop(self):
        """停止 MQTT 连接"""
        self._running = False
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass

    @property
    def connected(self):
        return self._connected

    def publish_state(self, payload: dict):
        """发布 pc/state（QoS 1, retain true）。MQTT 未连接时缓存，重连后发布。"""
        with self._lock:
            self._latest_payload = payload
            self._latest_seq = int(payload.get("seq", 0) or 0)

        if self._connected and self.client:
            data = json.dumps(payload, ensure_ascii=False)
            result = self.client.publish(self.topic_state, data, qos=1, retain=True)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning(f"publish_state failed rc={result.rc}")
            else:
                logger.debug(f"Published state: status={payload.get('status')} seq={payload.get('seq')}")

    def publish_speech(self, speech: dict, source_payload: dict | None = None):
        """发布独立 TTS 语音消息，不改变最新 pc/state。"""
        if not isinstance(speech, dict):
            return

        source_payload = source_payload if isinstance(source_payload, dict) else {}
        msg = {
            "type": "speech",
            "seq": int(source_payload.get("seq", speech.get("seq", 0)) or 0),
            "status": str(source_payload.get("status") or ""),
            "session_id": str(source_payload.get("session_id") or speech.get("session_id") or ""),
            "project_key": str(source_payload.get("project_key") or ""),
            "speech": dict(speech),
        }
        msg["speech"]["seq"] = int(msg["seq"] or msg["speech"].get("seq", 0) or 0)
        msg["speech"]["session_id"] = msg["session_id"]

        if self._connected and self.client:
            data = json.dumps(msg, ensure_ascii=False)
            result = self.client.publish(self.topic_speech, data, qos=1, retain=False)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning(f"publish_speech failed rc={result.rc}")
            else:
                logger.debug(f"Published speech: id={speech.get('id')} seq={msg.get('seq')}")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        logger.info(f"MQTT connected (rc={reason_code})")
        self._connected = True

        # 订阅设备 action
        client.subscribe(self.topic_action_sub, qos=1)
        logger.info(f"Subscribed: {self.topic_action_sub}")

        # 订阅 TTS 请求
        self.topic_tts_request = f"{self.prefix}/tts/request"
        client.subscribe(self.topic_tts_request, qos=1)
        logger.info(f"Subscribed: {self.topic_tts_request}")

        # 重连后发布最新状态
        with self._lock:
            payload = self._latest_payload
        if payload:
            data = json.dumps(payload, ensure_ascii=False)
            client.publish(self.topic_state, data, qos=1, retain=True)
            logger.info("Re-published latest state after reconnect")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        logger.warning(f"MQTT disconnected (rc={reason_code})")
        self._connected = False

    def _on_message(self, client, userdata, msg):
        topic = msg.topic

        # 匹配 {prefix}/device/{device_id}/action
        parts = topic.split("/")
        if len(parts) >= 4 and parts[-1] == "action":
            device_id = parts[-2]
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception:
                logger.warning(f"Invalid action JSON from {device_id}")
                return

            logger.info(f"Action from {device_id}: {payload.get('action')} source={payload.get('source')}")
            recorded = self.record_action(device_id, payload)
            if recorded:
                logger.info(
                    "Action cached: device=%s action=%s seq=%s state=%s",
                    recorded["device_id"],
                    recorded["action"],
                    recorded["last_seq"],
                    recorded["device_state"],
                )

            if self._action_callback:
                try:
                    self._action_callback(device_id, payload)
                except Exception as e:
                    logger.error(f"Action callback error: {e}")
            return

        # TTS 请求
        if topic == f"{self.prefix}/tts/request":
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception:
                logger.warning("Invalid TTS request JSON")
                return

            request_id = str(payload.get("request_id", ""))
            text = str(payload.get("text", "")).strip()
            voice = str(payload.get("voice", "")).strip()
            if not request_id or not text:
                logger.warning("TTS request missing request_id or text")
                return

            logger.info(f"TTS request: id={request_id} text={text[:40]}")

            if self._tts_handler:
                # 在线程中处理，避免阻塞 MQTT 消息循环
                def _run():
                    try:
                        self._tts_handler(request_id, text, voice)
                    except Exception as e:
                        logger.error(f"TTS handler error: {e}")
                t = threading.Thread(target=_run, daemon=True)
                t.start()
            return


def create_bridge_from_env() -> MQTTBridge:
    """从环境变量创建 MQTTBridge"""
    return MQTTBridge(
        broker_host=os.environ.get("MQTT_HOST", "127.0.0.1"),
        broker_port=int(os.environ.get("MQTT_PORT", "1883")),
        username=os.environ.get("MQTT_USERNAME", ""),
        password=os.environ.get("MQTT_PASSWORD", ""),
        topic_prefix=os.environ.get("TOPIC_PREFIX", "whitebox"),
    )
