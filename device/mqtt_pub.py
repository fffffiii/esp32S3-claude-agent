"""
MQTT Publisher — 从 cc-dashboard 采集状态并发布到 whitebox/pc/state
复用 app.py 中的 list_projects() / scan_session() / get_status()
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


def status_to_gif(status: str) -> int:
    """状态 → GIF 编号映射"""
    return {"cooking": 1, "thinking": 2, "marinating": 4}.get(status, 3)


def build_payload(projects: list) -> dict:
    """从项目列表中提取最新活跃会话，构建 MQTT payload"""
    if not projects:
        return {
            "status": "offline",
            "gif": 3,
            "project_key": "",
            "work_dir": "",
            "session_id": "",
            "msg_count": 0,
            "latest_message": {
                "role": "",
                "type": "",
                "text": "",
                "ts": "",
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "seq": 0,
        }

    # 最新活跃项目 = 排序第一位
    top = projects[0]
    status = top.get("status", "idle")

    # 取最新会话的最后一条消息
    latest_chat = top.get("_latest_chat", [])
    last_msg = latest_chat[-1] if latest_chat else {}

    return {
        "status": status,
        "gif": status_to_gif(status),
        "project_key": top.get("key", ""),
        "work_dir": top.get("work_dir", ""),
        "session_id": top.get("active_sid", ""),
        "msg_count": top.get("_msg_count", 0),
        "latest_message": {
            "role": last_msg.get("role", ""),
            "type": "text",
            "text": last_msg.get("text", "")[:200],
            "ts": last_msg.get("ts", ""),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "seq": 0,
    }


class MQTTPublisher:
    """后台线程：每 5 秒发布状态到 MQTT"""

    def __init__(self, broker_host="127.0.0.1", broker_port=1883,
                 username="", password="", topic_prefix="whitebox"):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password
        self.topic = f"{topic_prefix}/pc/state"
        self.client = None
        self._thread = None
        self._running = False
        self._seq = 0

    def start(self, get_projects_fn):
        """启动发布线程，传入获取项目数据的函数"""
        self._get_projects = get_projects_fn
        self._running = True

        self.client = mqtt.Client(client_id="whitebox-pc-publisher")
        if self.username:
            self.client.username_pw_set(self.username, self.password)

        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            print(f"[mqtt] 已连接 {self.broker_host}:{self.broker_port}")
        except Exception as e:
            print(f"[mqtt] 连接失败: {e}，将在后台重试")
            # 仍然启动线程，paho 会自动重连
            try:
                self.client.loop_start()
            except Exception:
                pass

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        import glob as _glob
        from app import scan_session

        CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude", "projects")
        last_top_mtime = 0
        last_publish = 0

        while self._running:
            try:
                # 检测最新会话文件的 mtime 是否变化
                top_mtime = 0
                if os.path.isdir(CLAUDE_HOME):
                    for name in os.listdir(CLAUDE_HOME):
                        full_dir = os.path.join(CLAUDE_HOME, name)
                        if not os.path.isdir(full_dir):
                            continue
                        jsonls = _glob.glob(os.path.join(full_dir, "*.jsonl"))
                        for f in jsonls:
                            m = os.path.getmtime(f)
                            if m > top_mtime:
                                top_mtime = m

                now = time.time()
                # mtime 变了 → 立刻发布；没变 → 每 5 秒心跳一次
                if top_mtime != last_top_mtime or (now - last_publish) >= 5:
                    last_top_mtime = top_mtime
                    last_publish = now

                    projects = self._get_projects()
                    for p in projects:
                        full = os.path.join(CLAUDE_HOME, p["key"])
                        jsonls = _glob.glob(os.path.join(full, "*.jsonl"))
                        if jsonls:
                            latest = max(jsonls, key=os.path.getmtime)
                            info = scan_session(latest, chat_limit=1)
                            p["_latest_chat"] = info.get("chat", [])
                            p["_msg_count"] = info.get("msg_count", 0)
                        else:
                            p["_latest_chat"] = []
                            p["_msg_count"] = 0

                    self._seq += 1
                    payload = build_payload(projects)
                    payload["seq"] = self._seq

                    data = json.dumps(payload, ensure_ascii=False)
                    result = self.client.publish(self.topic, data, qos=1, retain=True)
                    if result.rc != mqtt.MQTT_ERR_SUCCESS:
                        print(f"[mqtt] 发布失败 rc={result.rc}")

            except Exception as e:
                print(f"[mqtt] 发布异常: {e}")

            time.sleep(1)  # 每秒检测一次文件变化

    def stop(self):
        self._running = False
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()


def create_publisher():
    """从环境变量创建 publisher"""
    host = os.environ.get("MQTT_HOST", "127.0.0.1")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    user = os.environ.get("MQTT_USERNAME", "")
    pwd = os.environ.get("MQTT_PASSWORD", "")
    prefix = os.environ.get("TOPIC_PREFIX", "whitebox")

    return MQTTPublisher(
        broker_host=host,
        broker_port=port,
        username=user,
        password=pwd,
        topic_prefix=prefix,
    )
