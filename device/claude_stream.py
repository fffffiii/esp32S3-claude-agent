"""
Claude Code stream-json 协议交互演示

cc-connect 通过 --input-format stream-json 和 --output-format stream-json
与 Claude Code 建立双向 JSON 流通信。

协议格式: 每行一个 JSON 对象 (newline-delimited JSON)

输出方向 (Claude → cc-connect):
  {"type": "system", "subtype": "init", "session_id": "...", ...}
  {"type": "assistant", "message": {"role": "assistant", "content": [
      {"type": "thinking", "thinking": "..."},
      {"type": "text", "text": "..."},
      {"type": "tool_use", "id": "...", "name": "Bash", "input": {...}}
  ]}}
  {"type": "user", "message": {"role": "user", "content": [
      {"type": "tool_result", "tool_use_id": "...", "content": "..."}
  ]}}
  {"type": "result", "result": "...", "session_id": "...",
   "usage": {"input_tokens": N, "output_tokens": N}}
  {"type": "control_request", "request_id": "...",
   "request": {"subtype": "can_use_tool", "tool_name": "...", "input": {...}}}

输入方向 (cc-connect → Claude):
  {"type": "user", "message": {"role": "user", "content": "prompt text"}}
  {"type": "user", "message": {"role": "user", "content": [
      {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
      {"type": "text", "text": "describe this"}
  ]}}
  {"type": "control_response", "response": {
      "subtype": "success", "request_id": "...",
      "response": {"behavior": "allow", "updatedInput": {}}}}
"""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StreamEvent:
    """Claude Code stream-json 事件"""
    type: str
    content: str = ""
    session_id: str = ""
    tool_name: str = ""
    tool_input: str = ""
    thinking: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str = ""
    raw: dict = field(default_factory=dict)


class ClaudeStreamSession:
    """
    直接与 Claude Code CLI 通过 stream-json 协议交互。
    复现了 cc-connect 的核心 session 管理逻辑。
    """

    def __init__(self, work_dir: str = ".", cli_bin: str = "claude", model: str = ""):
        self.work_dir = work_dir
        self.cli_bin = cli_bin
        self.model = model
        self.session_id = ""
        self.process: Optional[subprocess.Popen] = None
        self._alive = False
        self._events: list[StreamEvent] = []
        self._lock = threading.Lock()

        # pending 状态跟踪（供 CC controller 使用）
        self.pending_request_id: str = ""
        self.pending_tool_use_id: str = ""
        self.pending_tool_name: str = ""

    def start(self, resume_session_id: str = ""):
        """启动 Claude Code 子进程，使用 stream-json 协议"""
        cmd = [
            self.cli_bin,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--permission-prompt-tool", "stdio",
            "--verbose",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # 防止嵌套检测

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.work_dir,
            env=env,
            text=True,
            bufsize=1,  # line buffered
        )
        self._alive = True

        # 启动读取线程
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        """读取 stdout 中的每一行 JSON"""
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = self._parse_event(raw)
            with self._lock:
                self._events.append(event)

    def _parse_event(self, raw: dict) -> StreamEvent:
        """解析 stream-json 事件，跟踪 pending 状态"""
        event_type = raw.get("type", "")
        event = StreamEvent(type=event_type, raw=raw)

        if event_type == "system":
            sid = raw.get("session_id", "")
            if sid:
                self.session_id = sid
                event.session_id = sid

        elif event_type == "assistant":
            msg = raw.get("message", {})
            content_arr = msg.get("content", [])
            for item in content_arr:
                if not isinstance(item, dict):
                    continue
                ct = item.get("type", "")
                if ct == "text":
                    event.content += item.get("text", "")
                elif ct == "thinking":
                    event.thinking += item.get("thinking", "")
                elif ct == "tool_use":
                    event.tool_name = item.get("name", "")
                    event.tool_input = json.dumps(item.get("input", {}), ensure_ascii=False)
                    # 跟踪需要人类确认的 tool_use
                    if item.get("name") in {"AskUserQuestion", "ExitPlanMode"}:
                        self.pending_tool_use_id = item.get("id", "")
                        self.pending_tool_name = item.get("name", "")

        elif event_type == "result":
            event.content = raw.get("result", "")
            if sid := raw.get("session_id"):
                self.session_id = sid
                event.session_id = sid
            usage = raw.get("usage", {})
            event.input_tokens = int(usage.get("input_tokens", 0))
            event.output_tokens = int(usage.get("output_tokens", 0))
            # result 清空 pending
            self.pending_request_id = ""
            self.pending_tool_use_id = ""
            self.pending_tool_name = ""

        elif event_type == "control_request":
            req = raw.get("request", {})
            event.request_id = raw.get("request_id", "")
            event.tool_name = req.get("tool_name", "")
            # 保存 pending permission request
            if req.get("subtype") == "can_use_tool":
                self.pending_request_id = event.request_id

        elif event_type == "tool_result":
            # tool_result 清空对应的 pending tool_use
            tuid = raw.get("tool_use_id", "")
            if tuid and tuid == self.pending_tool_use_id:
                self.pending_tool_use_id = ""
                self.pending_tool_name = ""

        return event

    def send(self, prompt: str):
        """发送用户消息，复现 cc-connect 的 Send 方法"""
        msg = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        self._write_json(msg)

    def respond_permission(self, request_id: str, allow: bool, message: str = ""):
        """回复权限请求"""
        if allow:
            response = {"behavior": "allow", "updatedInput": {}}
        else:
            response = {"behavior": "deny", "message": message or "Permission denied."}

        msg = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        }
        self._write_json(msg)

    def respond_tool_result(self, tool_use_id: str, content: str):
        """回复 tool_use（如 AskUserQuestion / ExitPlanMode）"""
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                    }
                ],
            },
        }
        self._write_json(msg)
        # 清空 pending
        if tool_use_id == self.pending_tool_use_id:
            self.pending_tool_use_id = ""
            self.pending_tool_name = ""

    def _write_json(self, data: dict):
        """写入 JSON 到 stdin"""
        if not self.process or not self.process.stdin:
            return
        line = json.dumps(data, ensure_ascii=False) + "\n"
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def get_events(self) -> list[StreamEvent]:
        """获取所有已接收的事件"""
        with self._lock:
            return list(self._events)

    def alive(self) -> bool:
        return self._alive and self.process is not None and self.process.poll() is None

    def close(self):
        """关闭会话，复现 cc-connect 的三阶段优雅停止"""
        if self.process:
            # Phase 1: 关闭 stdin
            try:
                self.process.stdin.close()
            except Exception:
                pass
            # Phase 2: 等待退出
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Phase 3: 强制终止
                self.process.kill()
                self.process.wait(timeout=5)
        self._alive = False


def demo_stream_protocol():
    """
    演示 stream-json 协议的基本用法。
    需要安装 claude CLI: npm install -g @anthropic-ai/claude-code
    """
    print("=== Claude Code stream-json 协议演示 ===\n")

    session = ClaudeStreamSession(work_dir=".")
    try:
        session.start()
        print(f"[启动] Claude Code 进程已启动, PID: {session.process.pid}")

        # 发送消息
        session.send("Say hello in one sentence.")
        print("[发送] 用户消息已发送")

        # 等待并收集事件
        timeout = 30
        start = time.time()
        while time.time() - start < timeout:
            events = session.get_events()
            for evt in events:
                if evt.type == "system" and evt.session_id:
                    print(f"[系统] session_id: {evt.session_id}")
                elif evt.type == "assistant":
                    if evt.thinking:
                        print(f"[思考] {evt.thinking[:100]}...")
                    if evt.content:
                        print(f"[回复] {evt.content}")
                    if evt.tool_name:
                        print(f"[工具] {evt.tool_name}: {evt.tool_input[:80]}")
                elif evt.type == "control_request":
                    print(f"[权限] {evt.tool_name}, request_id: {evt.request_id}")
                    session.respond_permission(evt.request_id, allow=True)
                elif evt.type == "result":
                    print(f"[完成] tokens: {evt.input_tokens} in / {evt.output_tokens} out")
                    return
            time.sleep(0.5)

        print("[超时] 未在时限内收到完整回复")

    except FileNotFoundError:
        print("[错误] claude CLI 未找到，请先安装: npm install -g @anthropic-ai/claude-code")
    finally:
        session.close()
        print("[关闭] 会话已关闭")


if __name__ == "__main__":
    demo_stream_protocol()
