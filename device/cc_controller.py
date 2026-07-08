"""
cc_controller — 管理 Claude Code 托管 session + 设备 action 映射
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from claude_stream import ClaudeStreamSession

logger = logging.getLogger("cc_controller")


@dataclass
class CCController:
    """全局唯一的 Claude Code session 控制器"""
    active_session: Optional[ClaudeStreamSession] = None
    last_action: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start_session(self, work_dir: str, model: str = "", resume_session_id: str = "") -> dict:
        """启动或返回已有 session"""
        with self._lock:
            if self.active_session and self.active_session.alive():
                return {
                    "ok": True,
                    "alive": True,
                    "session_id": self.active_session.session_id,
                    "message": "session already running",
                }

            session = ClaudeStreamSession(work_dir=work_dir, model=model)
            try:
                session.start(resume_session_id=resume_session_id)
            except FileNotFoundError:
                return {"ok": False, "error": "claude CLI not found"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

            self.active_session = session
            logger.info(f"CC session started in {work_dir} (pid={session.process.pid})")
            return {
                "ok": True,
                "alive": True,
                "session_id": session.session_id or "(pending)",
            }

    def send_prompt(self, prompt: str) -> dict:
        """发送用户消息到活跃 session"""
        with self._lock:
            if not self.active_session or not self.active_session.alive():
                return {"ok": False, "error": "no active session"}
            self.active_session.send(prompt)
            return {"ok": True}

    def get_state(self) -> dict:
        """获取当前 CC session 状态"""
        with self._lock:
            s = self.active_session
            if not s:
                return {
                    "ok": True,
                    "alive": False,
                    "session_id": "",
                    "pending_request_id": "",
                    "pending_tool_use_id": "",
                    "pending_tool_name": "",
                    "last_action": self.last_action,
                }
            return {
                "ok": True,
                "alive": s.alive(),
                "session_id": s.session_id,
                "pending_request_id": s.pending_request_id,
                "pending_tool_use_id": s.pending_tool_use_id,
                "pending_tool_name": s.pending_tool_name,
                "last_action": self.last_action,
            }

    def close_session(self) -> dict:
        """关闭活跃 session"""
        with self._lock:
            if self.active_session:
                self.active_session.close()
                self.active_session = None
                logger.info("CC session closed")
            return {"ok": True}

    ACTION_CONTINUE = ("continue", "继续", "确定")
    ACTION_REJECT = ("reject", "拒绝")

    @classmethod
    def _normalize_action(cls, action: str) -> str:
        if action in cls.ACTION_CONTINUE:
            return "continue"
        if action in cls.ACTION_REJECT:
            return "reject"
        return action

    def handle_device_action(self, device_id: str, payload: dict) -> dict:
        """
        处理设备发来的 action，映射到 CC 控制。

        返回:
            {"ok": True, "handled": True/False, "reason": "..."}
        """
        action = self._normalize_action(payload.get("action", ""))
        source = payload.get("source", "")
        last_seq = int(payload.get("last_seq", 0))
        device_state = payload.get("state", "")

        if action not in ("continue", "reject"):
            return {"ok": False, "error": f"invalid action: {action}"}

        self.last_action = {
            "device_id": device_id,
            "action": action,
            "source": source,
            "last_seq": last_seq,
            "device_state": device_state,
            "ts": payload.get("ts", 0),
        }

        logger.info(f"Device action: {action} from {source} (device={device_id}, seq={last_seq})")

        with self._lock:
            s = self.active_session

            # 情况 1: 权限请求 (control_request)
            if s and s.pending_request_id:
                if action == "continue":
                    s.respond_permission(s.pending_request_id, allow=True)
                    logger.info("Permission ALLOWED via device action")
                else:
                    s.respond_permission(s.pending_request_id, allow=False,
                                         message="User rejected from Whitebox.")
                    logger.info("Permission DENIED via device action")
                return {"ok": True, "handled": True, "type": "permission"}

            # 情况 2: AskUserQuestion / ExitPlanMode
            if s and s.pending_tool_use_id:
                if action == "continue":
                    s.respond_tool_result(s.pending_tool_use_id, "继续")
                    logger.info("Tool result '继续' sent via device action")
                else:
                    s.respond_tool_result(s.pending_tool_use_id, "拒绝")
                    logger.info("Tool result '拒绝' sent via device action")
                return {"ok": True, "handled": True, "type": "tool_result"}

            # 情况 3: 无 pending
            logger.info("No pending CC request, action recorded but not handled")
            return {"ok": True, "handled": False, "reason": "no pending cc request"}


# 全局单例
controller = CCController()
