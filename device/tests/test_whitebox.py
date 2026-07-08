"""
Whitebox 服务端测试 — 单元测试 + MQTT 集成测试 + 健康页测试
"""
import json
import os
import sys
import tempfile
import time
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

# 把 cc-dashboard 加入 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as dashboard_app
from app import scan_session, get_status, list_projects, extract_text
from mqtt_pub import status_to_gif, build_payload, MQTTPublisher
from mqtt_bridge import MQTTBridge
from tts_service import (
    TTSOrchestrator,
    TTSSettings,
    build_speech_id,
    extract_speech_candidate_from_payload,
    extract_speech_text_from_payload,
    load_settings,
)


# ════════════════════════════════════════════════════════
#  1. 单元测试
# ════════════════════════════════════════════════════════

class TestStatusToGif(unittest.TestCase):
    """status → gif 映射"""

    def test_cooking_is_1(self):
        self.assertEqual(status_to_gif("cooking"), 1)

    def test_thinking_is_2(self):
        self.assertEqual(status_to_gif("thinking"), 2)

    def test_idle_is_3(self):
        self.assertEqual(status_to_gif("idle"), 3)

    def test_offline_is_3(self):
        self.assertEqual(status_to_gif("offline"), 3)

    def test_unknown_is_3(self):
        self.assertEqual(status_to_gif("whatever"), 3)


class TestHookStatus(unittest.TestCase):
    """Claude Code hook 状态映射"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        hook_path = os.path.join(os.path.dirname(__file__), "..", "hooks", "whitebox_status_hook.py")
        spec = importlib.util.spec_from_file_location("whitebox_status_hook", hook_path)
        cls.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hook)

    def test_notification_permission_message_is_marinating(self):
        status, message = self.hook.handle_event("Notification", {
            "message": "Claude needs your permission to use Bash",
        })
        self.assertEqual(status, "marinating")
        self.assertEqual(message["type"], "Notification")
        self.assertIn("permission", message["hook_message"].lower())

    def test_notification_waiting_message_is_marinating(self):
        status, _ = self.hook.handle_event("Notification", {
            "message": "Claude is waiting for your input",
        })
        self.assertEqual(status, "marinating")

    def test_user_prompt_submit_is_thinking(self):
        status, message = self.hook.handle_event("UserPromptSubmit", {
            "prompt": "维护一下文档",
        })
        self.assertEqual(status, "thinking")
        self.assertEqual(message["type"], "UserPromptSubmit")

    def test_internal_user_prompt_submit_is_ignored(self):
        ignored = self.hook.should_ignore_event("UserPromptSubmit", {
            "prompt": "<task-notification>done</task-notification>",
        })
        self.assertTrue(ignored)

    def test_permission_request_is_marinating(self):
        status, message = self.hook.handle_event("PermissionRequest", {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -R D:\\manbo\\white_box"},
        })
        self.assertEqual(status, "marinating")
        self.assertEqual(message["type"], "PermissionRequest")
        self.assertIn("ls -R", message["hook_message"])

    def test_ask_user_question_tool_is_marinating(self):
        status, message = self.hook.handle_event("PreToolUse", {
            "tool_name": "AskUserQuestion",
            "tool_input": {"question": "继续吗？"},
        })
        self.assertEqual(status, "marinating")
        self.assertEqual(message["type"], "PreToolUse")

    def test_stop_failure_is_idle(self):
        status, message = self.hook.handle_event("StopFailure", {
            "reason": "rate_limit",
        })
        self.assertEqual(status, "idle")
        self.assertEqual(message["type"], "StopFailure")

    def test_enqueue_payload_writes_sorted_queue_files(self):
        old_queue = self.hook.QUEUE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.hook.QUEUE_DIR = tmp
                self.hook.enqueue_payload({"seq": 2, "status": "thinking"})
                self.hook.enqueue_payload({"seq": 1, "status": "cooking"})
                names = [os.path.basename(path) for path in self.hook.list_queue_files(include_new=True)]
                self.assertTrue(names[0].startswith("000000000001-"))
                self.assertTrue(names[1].startswith("000000000002-"))
                self.assertEqual(self.hook.queue_size(), 2)
        finally:
            self.hook.QUEUE_DIR = old_queue

    def test_build_payload_marks_hook_source(self):
        payload = self.hook.build_payload(
            "cooking",
            {"role": "hook", "type": "PreToolUse", "text": "[Read]", "hook_message": "PreToolUse Read"},
            {"prefix": "whitebox"},
            "PreToolUse",
            {
                "cwd": "D:\\manbo\\white_box",
                "session_id": "sess-1",
                "transcript_path": "C:\\Users\\Administrator\\.claude\\projects\\D--manbo-white-box\\sess-1.jsonl",
                "permission_mode": "default",
            },
        )
        self.assertEqual(payload["source"], "claude-hook")
        self.assertEqual(payload["hook_event_name"], "PreToolUse")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["transcript_path"], "C:\\Users\\Administrator\\.claude\\projects\\D--manbo-white-box\\sess-1.jsonl")
        self.assertEqual(payload["permission_mode"], "default")

    def test_build_payload_prefers_transcript_path_for_project_key(self):
        old_projects_dir = self.hook.CLAUDE_PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session_dir = os.path.join(tmp, "D--manbo-white-box")
                os.makedirs(session_dir, exist_ok=True)
                transcript_path = os.path.join(session_dir, "sess-1.jsonl")
                with open(transcript_path, "w", encoding="utf-8") as f:
                    f.write("")

                self.hook.CLAUDE_PROJECTS_DIR = tmp
                payload = self.hook.build_payload(
                    "thinking",
                    {"role": "hook", "type": "UserPromptSubmit", "text": "继续", "hook_message": "继续"},
                    {"prefix": "whitebox"},
                    "UserPromptSubmit",
                    {
                        "cwd": "D:\\manbo\\white_box",
                        "session_id": "sess-1",
                        "transcript_path": transcript_path,
                    },
                )
                self.assertEqual(payload["project_key"], "D--manbo-white-box")
                self.assertEqual(payload["transcript_path"], transcript_path)
        finally:
            self.hook.CLAUDE_PROJECTS_DIR = old_projects_dir

    def test_build_payload_uses_real_claude_project_key_when_session_exists(self):
        old_projects_dir = self.hook.CLAUDE_PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session_dir = os.path.join(tmp, "D--manbo-white-box")
                os.makedirs(session_dir, exist_ok=True)
                with open(os.path.join(session_dir, "sess-1.jsonl"), "w", encoding="utf-8") as f:
                    f.write("")

                self.hook.CLAUDE_PROJECTS_DIR = tmp
                payload = self.hook.build_payload(
                    "thinking",
                    {"role": "hook", "type": "UserPromptSubmit", "text": "继续", "hook_message": "继续"},
                    {"prefix": "whitebox"},
                    "UserPromptSubmit",
                    {"cwd": "D:\\manbo\\white_box", "session_id": "sess-1"},
                )
                self.assertEqual(payload["project_key"], "D--manbo-white-box")
        finally:
            self.hook.CLAUDE_PROJECTS_DIR = old_projects_dir

    def test_build_payload_attaches_only_visible_assistant_speech_candidate(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            transcript_path = f.name
            f.write(json.dumps({
                "type": "assistant",
                "uuid": "assistant-visible-1",
                "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "验证一下改动。"},
                ]},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "uuid": "assistant-tool-1",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "python -c \"print(1)\""}},
                ]},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False) + "\n")
        try:
            payload = self.hook.build_payload(
                "cooking",
                {"role": "hook", "type": "PreToolUse", "text": "[Bash]", "hook_message": "python -c \"print(1)\""},
                {"prefix": "whitebox"},
                "PreToolUse",
                {
                    "cwd": "D:\\manbo\\white_box",
                    "session_id": "sess-1",
                    "transcript_path": transcript_path,
                },
            )
            self.assertEqual(payload["speech_candidate"]["text"], "验证一下改动。")
            self.assertIn("assistant-visible-1", payload["speech_candidate"]["source_id"])
        finally:
            os.unlink(transcript_path)

    def test_build_payload_attaches_final_assistant_text_on_stop(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            transcript_path = f.name
            f.write(json.dumps({
                "type": "assistant",
                "uuid": "assistant-final-1",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "抱歉，我无法查询实时天气信息。我的工具没有返回有效的搜索结果。"},
                ]},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False) + "\n")
        try:
            payload = self.hook.build_payload(
                "idle",
                {"role": "hook", "type": "Stop", "text": "会话结束", "hook_message": "Stop"},
                {"prefix": "whitebox"},
                "Stop",
                {
                    "cwd": "D:\\manbo\\white_box",
                    "session_id": "sess-1",
                    "transcript_path": transcript_path,
                },
            )
            self.assertEqual(
                payload["speech_candidate"]["text"],
                "抱歉，我无法查询实时天气信息。我的工具没有返回有效的搜索结果。"
            )
            self.assertIn("assistant-final-1", payload["speech_candidate"]["source_id"])
        finally:
            os.unlink(transcript_path)

    def test_permission_request_continue_outputs_allow_decision(self):
        cfg = {
            "dashboard_url": "http://127.0.0.1:5000/api/hook/state",
            "dashboard_action_wait_url": "http://127.0.0.1:5000/api/device/action/wait",
        }
        hook_input = {
            "hook_event_name": "PermissionRequest",
            "tool_input": {"command": "ls -la"},
        }
        payload = {"seq": 42}
        action = {
            "device_id": "whitebox-001",
            "action": "continue",
            "source": "button",
            "last_seq": 42,
            "device_state": "marinating",
            "received_at": "2026-05-12T00:00:00Z",
        }
        with patch.object(self.hook, "append_trace"), \
             patch.object(self.hook, "post_to_dashboard", return_value=(True, "")), \
             patch.object(self.hook, "wait_for_device_action", return_value=action):
            decision = self.hook.process_permission_request(hook_input, cfg, payload, "marinating")
        self.assertIsNotNone(decision)
        self.assertEqual(decision["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(decision["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(decision["hookSpecificOutput"]["decision"]["updatedInput"]["command"], "ls -la")

    def test_permission_request_reject_outputs_deny_decision(self):
        cfg = {
            "dashboard_url": "http://127.0.0.1:5000/api/hook/state",
            "dashboard_action_wait_url": "http://127.0.0.1:5000/api/device/action/wait",
        }
        hook_input = {
            "hook_event_name": "PermissionRequest",
            "tool_input": {"command": "rm -rf /tmp/test"},
        }
        payload = {"seq": 43}
        action = {
            "device_id": "whitebox-001",
            "action": "reject",
            "source": "voice",
            "last_seq": 43,
            "device_state": "marinating",
            "received_at": "2026-05-12T00:00:00Z",
        }
        with patch.object(self.hook, "append_trace"), \
             patch.object(self.hook, "post_to_dashboard", return_value=(True, "")), \
             patch.object(self.hook, "wait_for_device_action", return_value=action):
            decision = self.hook.process_permission_request(hook_input, cfg, payload, "marinating")
        self.assertIsNotNone(decision)
        self.assertEqual(decision["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertEqual(decision["hookSpecificOutput"]["decision"]["message"], "User rejected from Whitebox.")

    def test_permission_request_timeout_returns_none(self):
        cfg = {
            "dashboard_url": "http://127.0.0.1:5000/api/hook/state",
            "dashboard_action_wait_url": "http://127.0.0.1:5000/api/device/action/wait",
        }
        hook_input = {
            "hook_event_name": "PermissionRequest",
            "tool_input": {"command": "ls"},
        }
        payload = {"seq": 44}
        with patch.object(self.hook, "append_trace"), \
             patch.object(self.hook, "post_to_dashboard", return_value=(True, "")), \
             patch.object(self.hook, "wait_for_device_action", return_value=None):
            decision = self.hook.process_permission_request(hook_input, cfg, payload, "marinating")
        self.assertIsNone(decision)


class TestTTSReadyAttachment(unittest.TestCase):
    """TTS 完成回调与最新状态合并"""

    class DummyBridge:
        def __init__(self):
            self.published = []
            self.speeches = []

        def publish_state(self, payload):
            self.published.append(dict(payload))

        def publish_speech(self, speech, payload=None):
            self.speeches.append({"speech": dict(speech), "payload": dict(payload or {})})

    def setUp(self):
        self.old_bridge = dashboard_app._mqtt_bridge
        self.old_latest = dashboard_app._latest_hook_payload
        with dashboard_app.SESSION_ROUTE_LOCK:
            self.old_route = dict(dashboard_app.SESSION_ROUTE)
            dashboard_app.SESSION_ROUTE["mode"] = "all"
            dashboard_app.SESSION_ROUTE["session_id"] = ""
            dashboard_app.SESSION_ROUTE["project_key"] = ""

    def tearDown(self):
        dashboard_app._mqtt_bridge = self.old_bridge
        dashboard_app._latest_hook_payload = self.old_latest
        with dashboard_app.SESSION_ROUTE_LOCK:
            dashboard_app.SESSION_ROUTE.clear()
            dashboard_app.SESSION_ROUTE.update(self.old_route)

    def test_ready_speech_publishes_independent_speech_message(self):
        bridge = self.DummyBridge()
        dashboard_app._mqtt_bridge = bridge
        dashboard_app._latest_hook_payload = {
            "status": "idle",
            "seq": 101,
            "session_id": "sess-1",
            "speech_candidate": {"source_id": "assistant-1", "text": "已经完成全部测试。"},
        }

        dashboard_app._on_tts_ready(
            {
                "status": "thinking",
                "seq": 100,
                "session_id": "sess-1",
                "speech_candidate": {"source_id": "assistant-1", "text": "已经完成全部测试。"},
            },
            {
                "id": "speech-1",
                "audio_url": "http://127.0.0.1:5000/api/tts/audio/speech-1.mp3",
                "text": "已经完成全部测试。",
                "source_id": "assistant-1",
                "seq": 100,
            },
        )

        self.assertEqual(bridge.published, [])
        self.assertEqual(len(bridge.speeches), 1)
        self.assertEqual(bridge.speeches[0]["speech"]["id"], "speech-1")
        self.assertEqual(bridge.speeches[0]["payload"]["seq"], 100)

    def test_ready_speech_respects_session_route(self):
        bridge = self.DummyBridge()
        dashboard_app._mqtt_bridge = bridge
        with dashboard_app.SESSION_ROUTE_LOCK:
            dashboard_app.SESSION_ROUTE["mode"] = "single"
            dashboard_app.SESSION_ROUTE["session_id"] = "sess-2"
            dashboard_app.SESSION_ROUTE["project_key"] = ""
        dashboard_app._latest_hook_payload = {
            "status": "thinking",
            "seq": 102,
            "session_id": "sess-1",
            "speech_candidate": {"source_id": "assistant-2", "text": "新的内容。"},
        }

        dashboard_app._on_tts_ready(
            {
                "status": "thinking",
                "seq": 100,
                "session_id": "sess-1",
                "speech_candidate": {"source_id": "assistant-1", "text": "旧内容。"},
            },
            {
                "id": "speech-1",
                "audio_url": "http://127.0.0.1:5000/api/tts/audio/speech-1.mp3",
                "text": "旧内容。",
                "source_id": "assistant-1",
                "seq": 100,
            },
        )

        self.assertEqual(bridge.published, [])
        self.assertEqual(bridge.speeches, [])
        self.assertNotIn("speech", dashboard_app._latest_hook_payload)


class TestDeviceActionCache(unittest.TestCase):
    """设备 action 缓存与等待"""

    def test_action_continue_is_cached_and_waitable(self):
        bridge = MQTTBridge()
        recorded = bridge.record_action("whitebox-001", {
            "action": "continue",
            "source": "button",
            "last_seq": 42,
            "state": "marinating",
        })
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded["action"], "continue")
        action = bridge.wait_for_action(42, 0.01)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "continue")
        self.assertEqual(action["last_seq"], 42)

    def test_action_chinese_normalization(self):
        bridge = MQTTBridge()
        recorded = bridge.record_action("whitebox-001", {
            "action": "继续",
            "source": "voice",
            "last_seq": 50,
            "state": "marinating",
        })
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded["action"], "continue")

    def test_action_last_seq_must_not_go_backwards(self):
        bridge = MQTTBridge()
        bridge.record_action("whitebox-001", {
            "action": "continue",
            "source": "button",
            "last_seq": 41,
            "state": "marinating",
        })
        self.assertIsNone(bridge.wait_for_action(42, 0.01))


class TestDeviceActionWaitRoute(unittest.TestCase):
    """设备 action 等待接口"""

    def setUp(self):
        import app as dashboard_app
        self.dashboard_app = dashboard_app
        self.client = dashboard_app.app.test_client()
        self.old_bridge = dashboard_app._mqtt_bridge
        dashboard_app._mqtt_bridge = MQTTBridge()

    def tearDown(self):
        self.dashboard_app._mqtt_bridge = self.old_bridge

    def test_wait_route_returns_matched_action(self):
        self.dashboard_app._mqtt_bridge.record_action("whitebox-001", {
            "action": "continue",
            "source": "button",
            "last_seq": 99,
            "state": "marinating",
        })
        resp = self.client.get("/api/device/action/wait?since_seq=99&timeout=0.01")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["matched"])
        self.assertEqual(data["action"]["action"], "continue")

    def test_wait_route_returns_not_matched_on_timeout(self):
        resp = self.client.get("/api/device/action/wait?since_seq=101&timeout=0.01")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["matched"])


class TestExtractText(unittest.TestCase):
    """消息正文提取"""

    def test_plain_string(self):
        self.assertEqual(extract_text("hello"), "hello")

    def test_text_block(self):
        content = [{"type": "text", "text": "hi"}]
        self.assertIn("hi", extract_text(content))

    def test_tool_use_block(self):
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
        result = extract_text(content)
        self.assertIn("Bash", result)
        self.assertIn("ls", result)

    def test_thinking_block(self):
        content = [{"type": "thinking", "thinking": "deep thought"}]
        result = extract_text(content)
        self.assertIn("思考中", result)

    def test_empty_list(self):
        self.assertEqual(extract_text([]), "")

    def test_none_content(self):
        self.assertEqual(extract_text(None), "")

    def test_mixed_blocks(self):
        content = [
            {"type": "text", "text": "doing"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/f"}},
        ]
        result = extract_text(content)
        self.assertIn("doing", result)
        self.assertIn("Read", result)


class TestTTSExtraction(unittest.TestCase):
    """TTS 文本抽取"""

    def _write_jsonl(self, lines):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.close()
        return f.name

    def test_extracts_recent_assistant_text_from_transcript(self):
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "plan"},
                {"type": "text", "text": "先清理临时文件，再重新打包。"},
            ]}, "timestamp": "2025-01-01T00:00:03Z"},
        ])
        try:
            payload = {
                "source": "claude-hook",
                "status": "thinking",
                "transcript_path": path,
                "updated_at": "2025-01-01T00:00:02Z",
            }
            text = extract_speech_text_from_payload(payload, max_chars=120)
            self.assertEqual(text, "先清理临时文件，再重新打包。")
        finally:
            os.unlink(path)

    def test_transcript_prefers_newer_assistant_text_after_update(self):
        path = self._write_jsonl([
            {"type": "assistant", "seq": 8, "message": {"role": "assistant", "content": [
                {"type": "text", "text": "旧进度，不应该读。"},
            ]}, "timestamp": "2025-01-01T00:00:01Z"},
            {"type": "assistant", "seq": 10, "message": {"role": "assistant", "content": [
                {"type": "text", "text": "新的进度，应该读这一句。"},
            ]}, "timestamp": "2025-01-01T00:00:03Z"},
        ])
        try:
            payload = {
                "source": "claude-hook",
                "status": "thinking",
                "transcript_path": path,
                "seq": 10,
                "updated_at": "2025-01-01T00:00:02Z",
            }
            text = extract_speech_text_from_payload(payload, max_chars=120)
            self.assertEqual(text, "新的进度，应该读这一句。")
        finally:
            os.unlink(path)

    def test_transcript_reads_visible_text_just_before_tool_hook(self):
        path = self._write_jsonl([
            {"type": "assistant", "uuid": "msg-redbox", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "代码导入正常。现在上传到服务器。"},
            ]}, "timestamp": "2025-01-01T00:00:01Z"},
            {"type": "assistant", "uuid": "msg-tool", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "scp app.py server:/tmp/app.py"}},
            ]}, "timestamp": "2025-01-01T00:00:02Z"},
        ])
        try:
            payload = {
                "source": "claude-hook",
                "status": "cooking",
                "transcript_path": path,
                "updated_at": "2025-01-01T00:00:03Z",
            }
            candidate = extract_speech_candidate_from_payload(payload, max_chars=120)
            self.assertEqual(candidate["text"], "代码导入正常。现在上传到服务器。")
            self.assertIn("msg-redbox", candidate["source_id"])
        finally:
            os.unlink(path)

    def test_falls_back_to_notification_hook_message(self):
        payload = {
            "source": "claude-hook",
            "status": "thinking",
            "hook_event_name": "Notification",
            "hook_message": "之前生成 MP3 时残留了临时文件，SPIFFS 打包报错。清理一下。",
            "latest_message": {
                "text": "之前生成 MP3 时残留了临时文件，SPIFFS 打包报错。清理一下。",
                "hook_message": "之前生成 MP3 时残留了临时文件，SPIFFS 打包报错。清理一下。",
            },
        }
        text = extract_speech_text_from_payload(payload, max_chars=120)
        self.assertEqual(text, "之前生成 MP3 时残留了临时文件，SPIFFS 打包报错。清理一下。")

    def test_non_notification_hook_fallback_is_ignored(self):
        payload = {
            "source": "claude-hook",
            "status": "cooking",
            "hook_event_name": "PreToolUse",
            "hook_message": "[Bash] 等待确认",
            "latest_message": {
                "text": "[Bash] 等待确认",
                "hook_message": "[Bash] 等待确认",
            },
        }
        self.assertEqual(extract_speech_text_from_payload(payload, max_chars=120), "")

    def test_speech_id_is_stable(self):
        payload = {"session_id": "sess-1", "seq": 10}
        sid1 = build_speech_id(payload, "继续清理临时文件。", "local_indextts", "voice-a", 24000)
        sid2 = build_speech_id(payload, "继续清理临时文件。", "doubao", "voice-b", 16000)
        self.assertEqual(sid1, sid2)

    def test_speech_id_uses_candidate_source_id_across_hook_seq(self):
        payload1 = {"session_id": "sess-1", "seq": 10, "speech_candidate": {"source_id": "transcript:msg-1:abc"}}
        payload2 = {"session_id": "sess-1", "seq": 11, "speech_candidate": {"source_id": "transcript:msg-1:abc"}}
        sid1 = build_speech_id(payload1, "验证一下改动。", "mock", "voice-a", 24000)
        sid2 = build_speech_id(payload2, "验证一下改动。", "mock", "voice-a", 24000)
        self.assertEqual(sid1, sid2)

    def test_mock_provider_writes_cache_audio(self):
        sample_mp3 = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "spiffs", "task_done.mp3"))
        self.assertTrue(os.path.isfile(sample_mp3))

        with tempfile.TemporaryDirectory() as tmp:
            settings = TTSSettings(
                role="local",
                orchestrator="local",
                provider="mock",
                enabled=True,
                public_base_url="http://192.168.1.50:5000",
                cache_dir=tmp,
                mock_audio_path=sample_mp3,
            )
            orch = TTSOrchestrator(settings, role="local")
            payload = {"session_id": "sess-mock", "seq": 3}
            speech = orch.synthesize(payload, "任务完成了。")
            audio_path = orch.audio_path(speech["id"])
            self.assertTrue(audio_path.is_file())
            with open(sample_mp3, "rb") as f1, open(audio_path, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())

    def test_public_base_url_uses_lan_ip_when_env_missing(self):
        with patch.dict(os.environ, {"TTS_PUBLIC_BASE_URL": ""}, clear=False):
            with patch("tts_service.guess_lan_ip", return_value="192.168.1.50"):
                settings = load_settings("local", tempfile.gettempdir())
        self.assertEqual(settings.public_base_url, "http://192.168.1.50:5000")


class TestScanSession(unittest.TestCase):
    """JSONL 扫描"""

    def _write_jsonl(self, lines):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.close()
        return f.name

    def test_basic_scan(self):
        path = self._write_jsonl([
            {"type": "user", "message": {"role": "user", "content": "hello world"}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}, "timestamp": "2025-01-01T00:00:01Z"},
        ])
        try:
            info = scan_session(path)
            self.assertEqual(info["summary"], "hello world")
            self.assertEqual(info["msg_count"], 2)
            self.assertEqual(len(info["chat"]), 2)
            self.assertEqual(info["status_hint"], "idle")
        finally:
            os.unlink(path)

    def test_skips_caveat(self):
        path = self._write_jsonl([
            {"type": "user", "message": {"role": "user", "content": "Caveat: The messages below..."}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "user", "message": {"role": "user", "content": "real question"}, "timestamp": "2025-01-01T00:00:01Z"},
        ])
        try:
            info = scan_session(path)
            self.assertEqual(info["summary"], "real question")
        finally:
            os.unlink(path)

    def test_bad_json_lines_ignored(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        f.write("not valid json\n")
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "ok"}}) + "\n")
        f.write('{"broken": \n')
        f.write("")
        f.close()
        try:
            info = scan_session(f.name)
            self.assertEqual(info["msg_count"], 1)
            self.assertEqual(info["summary"], "ok")
        finally:
            os.unlink(f.name)

    def test_long_summary_truncated(self):
        long_text = "a" * 200
        path = self._write_jsonl([
            {"type": "user", "message": {"role": "user", "content": long_text}, "timestamp": "2025-01-01T00:00:00Z"},
        ])
        try:
            info = scan_session(path)
            self.assertLessEqual(len(info["summary"]), 85)  # 80 + "..."
            self.assertTrue(info["summary"].endswith("..."))
        finally:
            os.unlink(path)

    def test_empty_file(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        f.write("")
        f.close()
        try:
            info = scan_session(f.name)
            self.assertEqual(info["msg_count"], 0)
            self.assertEqual(info["summary"], "")
        finally:
            os.unlink(f.name)

    def test_thinking_detection(self):
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "let me think..."},
                {"type": "text", "text": "answer"},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
        ])
        try:
            info = scan_session(path)
            self.assertTrue(info["last_thinking"])
            self.assertFalse(info["last_tool_use"])
            self.assertEqual(info["status_hint"], "thinking")
        finally:
            os.unlink(path)

    def test_tool_use_detection(self):
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
        ])
        try:
            info = scan_session(path)
            self.assertTrue(info["last_tool_use"])
            self.assertFalse(info["last_thinking"])
            self.assertEqual(info["status_hint"], "cooking")
        finally:
            os.unlink(path)

    def test_permission_prompt_tool_tracks_pending_confirmation(self):
        tool_id = "toolu_bash"
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "ls -R D:\\manbo\\white_box"}},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
        ])
        try:
            info = scan_session(path)
            self.assertTrue(info["last_pending_permission"])
            self.assertEqual(info["status_hint"], "cooking")
        finally:
            os.unlink(path)

        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "ls"}},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"},
            ]}, "timestamp": "2025-01-01T00:00:01Z"},
        ])
        try:
            info = scan_session(path)
            self.assertFalse(info["last_pending_permission"])
            self.assertEqual(info["status_hint"], "cooking")
        finally:
            os.unlink(path)

    def test_old_unclosed_permission_tool_does_not_pollute_reading(self):
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "old_edit", "name": "Edit", "input": {"file_path": "/tmp/a"}},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "read_1", "name": "Read", "input": {"file_path": "/tmp/doc.md"}},
            ]}, "timestamp": "2025-01-01T00:00:01Z"},
        ])
        try:
            info = scan_session(path)
            self.assertFalse(info["last_pending_permission"])
            self.assertEqual(info["status_hint"], "cooking")
        finally:
            os.unlink(path)

    def test_user_message_switches_to_thinking(self):
        path = self._write_jsonl([
            {"type": "user", "message": {"role": "user", "content": "帮我改一下"}, "timestamp": "2025-01-01T00:00:00Z"},
        ])
        try:
            info = scan_session(path)
            self.assertTrue(info["last_user_is_question"])
            self.assertEqual(info["status_hint"], "thinking")
        finally:
            os.unlink(path)

    def test_tool_result_keeps_cooking(self):
        tool_id = "toolu_1"
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "/tmp/a"}},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"},
            ]}, "timestamp": "2025-01-01T00:00:01Z"},
        ])
        try:
            info = scan_session(path)
            self.assertFalse(info["last_user_is_question"])
            self.assertEqual(info["status_hint"], "cooking")
        finally:
            os.unlink(path)

    def test_human_wait_tool_is_marinating_until_answered(self):
        tool_id = "toolu_ask"
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": {"question": "继续吗？"}},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
        ])
        try:
            info = scan_session(path)
            self.assertTrue(info["last_needs_confirm"])
            self.assertEqual(info["status_hint"], "marinating")
        finally:
            os.unlink(path)

        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": {"question": "继续吗？"}},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "继续"},
            ]}, "timestamp": "2025-01-01T00:00:01Z"},
        ])
        try:
            info = scan_session(path)
            self.assertFalse(info["last_needs_confirm"])
            self.assertEqual(info["status_hint"], "cooking")
        finally:
            os.unlink(path)

    def test_task_notification_does_not_reopen_finished_task(self):
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [
                {"type": "text", "text": "完成了"},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "user", "message": {"role": "user", "content": "<task-notification>done</task-notification>"}, "timestamp": "2025-01-01T00:00:01Z"},
        ])
        try:
            info = scan_session(path)
            self.assertFalse(info["last_user_is_question"])
            self.assertEqual(info["status_hint"], "idle")
        finally:
            os.unlink(path)

    def test_end_turn_overrides_thinking_block(self):
        path = self._write_jsonl([
            {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [
                {"type": "thinking", "thinking": "done"},
            ]}, "timestamp": "2025-01-01T00:00:00Z"},
        ])
        try:
            info = scan_session(path)
            self.assertEqual(info["status_hint"], "idle")
        finally:
            os.unlink(path)

    def test_chat_limit(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": f"msg {i}"}, "timestamp": f"2025-01-01T00:00:{i:02d}Z"}
            for i in range(50)
        ]
        path = self._write_jsonl(lines)
        try:
            info = scan_session(path, chat_limit=5)
            self.assertEqual(len(info["chat"]), 5)
            self.assertEqual(info["msg_count"], 50)
        finally:
            os.unlink(path)


class TestGetStatus(unittest.TestCase):
    """状态判断"""

    @patch("app.datetime")
    def test_recent_write_without_signal_is_idle(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(get_status(mtime, {}), "idle")

    @patch("app.datetime")
    def test_thinking_with_thinking_block(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        info = {"last_thinking": True, "last_entry_type": "assistant"}
        self.assertEqual(get_status(mtime, info), "thinking")

    @patch("app.datetime")
    def test_status_hint_cooking(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(get_status(mtime, {"status_hint": "cooking"}), "cooking")

    @patch("app.datetime")
    def test_pending_permission_becomes_marinating(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        info = {"status_hint": "cooking", "last_pending_permission": True}
        self.assertEqual(get_status(mtime, info), "marinating")

    @patch("app.datetime")
    def test_old_pending_permission_is_ignored_when_not_current_tool(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        info = {"status_hint": "thinking", "last_pending_permission": True}
        self.assertEqual(get_status(mtime, info), "thinking")

    @patch("app.datetime")
    def test_pending_permission_grace_stays_cooking(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        info = {"status_hint": "cooking", "last_pending_permission": True}
        self.assertEqual(get_status(mtime, info), "cooking")

    @patch("app.datetime")
    def test_status_hint_idle_is_immediate(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(get_status(mtime, {"status_hint": "idle"}), "idle")

    @patch("app.datetime")
    def test_marinating_does_not_timeout(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(get_status(mtime, {"status_hint": "marinating"}), "marinating")

    @patch("app.datetime")
    def test_idle_after_2min(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 3, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mtime = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(get_status(mtime, {}), "idle")


class TestBuildPayload(unittest.TestCase):
    """MQTT payload 构建"""

    def test_empty_projects(self):
        payload = build_payload([])
        self.assertEqual(payload["status"], "offline")
        self.assertEqual(payload["gif"], 3)
        self.assertEqual(payload["seq"], 0)

    def test_with_project(self):
        projects = [{
            "key": "D--test",
            "work_dir": "D/test",
            "status": "cooking",
            "active_sid": "abc-123",
            "_msg_count": 42,
            "_latest_chat": [{"role": "user", "text": "hello", "ts": "2025-01-01T00:00:00Z"}],
        }]
        payload = build_payload(projects)
        self.assertEqual(payload["status"], "cooking")
        self.assertEqual(payload["gif"], 1)
        self.assertEqual(payload["project_key"], "D--test")
        self.assertEqual(payload["msg_count"], 42)
        self.assertEqual(payload["latest_message"]["role"], "user")

    def test_thinking_gif(self):
        projects = [{"key": "x", "work_dir": "x", "status": "thinking", "active_sid": "", "_msg_count": 0, "_latest_chat": []}]
        payload = build_payload(projects)
        self.assertEqual(payload["gif"], 2)

    def test_idle_gif(self):
        projects = [{"key": "x", "work_dir": "x", "status": "idle", "active_sid": "", "_msg_count": 0, "_latest_chat": []}]
        payload = build_payload(projects)
        self.assertEqual(payload["gif"], 3)

    def test_message_text_truncated(self):
        long_text = "x" * 500
        projects = [{"key": "x", "work_dir": "x", "status": "idle", "active_sid": "", "_msg_count": 0,
                      "_latest_chat": [{"role": "assistant", "text": long_text, "ts": ""}]}]
        payload = build_payload(projects)
        self.assertLessEqual(len(payload["latest_message"]["text"]), 200)


# ════════════════════════════════════════════════════════
#  2. MQTT 集成测试（需要本地 Mosquitto）
# ════════════════════════════════════════════════════════

class TestMQTTIntegration(unittest.TestCase):
    """需要本地运行 Mosquitto: docker run -d -p 1883:1883 eclipse-mosquitto:2"""

    BROKER = "127.0.0.1"
    PORT = 1883
    TOPIC = "whitebox/pc/state"

    def _broker_available(self):
        import socket
        try:
            s = socket.create_connection((self.BROKER, self.PORT), timeout=2)
            s.close()
            return True
        except Exception:
            return False

    def test_publish_retained_qos1(self):
        """发布 retained QoS 1 消息，新订阅者能立即收到"""
        if not self._broker_available():
            self.skipTest("Mosquitto 未运行")

        import paho.mqtt.client as mqtt

        # 1. 发布一条 retained 消息
        pub = mqtt.Client(client_id="test-pub")
        pub.connect(self.BROKER, self.PORT)
        pub.loop_start()

        payload = {"status": "cooking", "gif": 1, "seq": 999}
        pub.publish(self.TOPIC, json.dumps(payload), qos=1, retain=True)
        time.sleep(1)
        pub.loop_stop()
        pub.disconnect()

        # 2. 新建订阅者，应该立即收到 retained 消息
        received = {}
        def on_msg(client, userdata, msg):
            received["data"] = json.loads(msg.payload.decode())
            received["qos"] = msg.qos

        sub = mqtt.Client(client_id="test-sub-new")
        sub.on_message = on_msg
        sub.connect(self.BROKER, self.PORT)
        sub.subscribe(self.TOPIC, qos=1)
        sub.loop_start()
        time.sleep(2)
        sub.loop_stop()
        sub.disconnect()

        self.assertIn("data", received)
        self.assertEqual(received["data"]["status"], "cooking")
        self.assertEqual(received["data"]["seq"], 999)
        self.assertEqual(received["qos"], 1)

    def test_publisher_roundtrip(self):
        """用 MQTTPublisher 发布，订阅者能收到完整 payload"""
        if not self._broker_available():
            self.skipTest("Mosquitto 未运行")

        import paho.mqtt.client as mqtt

        received = []
        def on_msg(client, userdata, msg):
            received.append(json.loads(msg.payload.decode()))

        sub = mqtt.Client(client_id="test-sub-roundtrip")
        sub.connect(self.BROKER, self.PORT)
        sub.subscribe(self.TOPIC, qos=1)
        sub.on_message = on_msg
        sub.loop_start()

        # 启动 publisher
        publisher = MQTTPublisher(
            broker_host=self.BROKER, broker_port=self.PORT,
            topic_prefix="whitebox",
        )
        fake_projects = [{
            "key": "D--test",
            "work_dir": "D/test",
            "status": "cooking",
            "active_sid": "sess-001",
            "_msg_count": 5,
            "_latest_chat": [{"role": "user", "text": "test msg", "ts": "2025-01-01T00:00:00Z"}],
        }]
        publisher.start(lambda: fake_projects)

        time.sleep(7)  # 等至少一个发布周期

        publisher.stop()
        sub.loop_stop()
        sub.disconnect()

        self.assertTrue(len(received) > 0)
        last = received[-1]
        self.assertEqual(last["status"], "cooking")
        self.assertEqual(last["gif"], 1)
        self.assertEqual(last["project_key"], "D--test")
        self.assertEqual(last["session_id"], "sess-001")
        self.assertIn("updated_at", last)
        self.assertGreater(last["seq"], 0)


# ════════════════════════════════════════════════════════
#  3. 健康页测试
# ════════════════════════════════════════════════════════

class TestHealthPage(unittest.TestCase):
    """健康页 API 测试"""

    def setUp(self):
        import importlib.util

        # 直接按文件路径加载，避免和 cc-dashboard/app.py 重名冲突
        hp_path = os.path.join(os.path.dirname(__file__), "..", "..", "server", "app.py")
        spec = importlib.util.spec_from_file_location("server_health_app", hp_path)
        hp_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hp_app)
        self.hp = hp_app
        self.client = hp_app.app.test_client()

    def test_healthz_returns_200(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])

    def test_api_state_empty(self):
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("pc", data)
        self.assertIn("devices", data)
        self.assertEqual(data["devices"], {})

    def test_api_state_with_data(self):
        """模拟写入状态后，/api/state 能返回"""
        with self.hp.lock:
            self.hp.pc_state = {"status": "cooking", "gif": 1, "project_key": "test"}
            self.hp.pc_state_ts = "2025-01-01T00:00:00Z"

        resp = self.client.get("/api/state")
        data = resp.get_json()
        self.assertEqual(data["pc"]["status"], "cooking")
        self.assertEqual(data["pc"]["gif"], 1)

    def test_broker_pc_state_counts_as_hardware_history(self):
        """本地 dashboard 直发到 broker 的状态也应出现在硬件记录。"""
        with self.hp.lock:
            self.hp.history.clear()
            self.hp.pc_state = {}
            self.hp.pc_state_ts = None

        payload = {"source": "claude-hook", "status": "idle", "gif": 3, "seq": 77}
        self.hp.on_message(None, None, type("Msg", (), {
            "topic": self.hp.mqtt_topic_state,
            "payload": json.dumps(payload).encode("utf-8"),
        })())

        hardware_items = [item for item in self.hp.history if item.get("hardware")]
        self.assertTrue(hardware_items)
        self.assertEqual(hardware_items[-1]["data"]["seq"], 77)

    def test_server_tts_ready_publishes_independent_speech_message(self):
        """server 侧 TTS 完成后发布 pc/speech，不回写 pc/state。"""
        class DummyMqttClient:
            def __init__(self):
                self.messages = []

            def publish(self, topic, data, qos=0, retain=False):
                self.messages.append({
                    "topic": topic,
                    "data": json.loads(data),
                    "qos": qos,
                    "retain": retain,
                })
                return type("Result", (), {"rc": 0})()

        old_client = self.hp.mqtt_client
        old_connected = self.hp.mqtt_connected
        dummy_client = DummyMqttClient()
        try:
            self.hp.mqtt_client = dummy_client
            self.hp.mqtt_connected = True
            with self.hp.lock:
                self.hp.pc_state = {
                    "source": "claude-hook",
                    "status": "idle",
                    "seq": 89,
                    "session_id": "sess-1",
                    "speech_candidate": {"source_id": "assistant-1", "text": "接口测试完成。"},
                }
                self.hp.pc_state_ts = "2025-01-01T00:00:00Z"

            self.hp._on_tts_ready(
                {
                    "source": "claude-hook",
                    "status": "thinking",
                    "seq": 88,
                    "session_id": "sess-1",
                    "speech_candidate": {"source_id": "assistant-1", "text": "接口测试完成。"},
                },
                {
                    "id": "speech-1",
                    "audio_url": "http://127.0.0.1:8080/api/tts/audio/speech-1.mp3",
                    "text": "接口测试完成。",
                    "source_id": "assistant-1",
                    "seq": 88,
                },
            )

            with self.hp.lock:
                current = dict(self.hp.pc_state)
            self.assertNotIn("speech", current)
            self.assertEqual(len(dummy_client.messages), 1)
            self.assertEqual(dummy_client.messages[0]["topic"], self.hp.mqtt_topic_speech)
            self.assertEqual(dummy_client.messages[0]["data"]["speech"]["id"], "speech-1")
            self.assertEqual(dummy_client.messages[0]["data"]["seq"], 88)
        finally:
            self.hp.mqtt_client = old_client
            self.hp.mqtt_connected = old_connected

    def test_tts_speak_pushes_test_speech_to_hardware(self):
        """点击测试 TTS 时应发布一次独立 speech 消息。"""
        class DummyTTS:
            settings = type("Settings", (), {"max_chars": 120})()

            def enabled(self):
                return True

            def speak_now(self, payload, text):
                return {
                    "id": f"speech-{payload['seq']}",
                    "audio_url": f"http://127.0.0.1:8080/api/tts/audio/speech-{payload['seq']}.mp3",
                    "text": text,
                    "seq": payload["seq"],
                    "source_id": payload.get("speech_source_id", ""),
                }

        class DummyMqttClient:
            def __init__(self):
                self.messages = []

            def publish(self, topic, data, qos=0, retain=False):
                self.messages.append({
                    "topic": topic,
                    "data": json.loads(data),
                    "qos": qos,
                    "retain": retain,
                })
                return type("Result", (), {"rc": 0})()

        old_tts = self.hp.tts_service
        old_client = self.hp.mqtt_client
        old_connected = self.hp.mqtt_connected
        dummy_client = DummyMqttClient()
        try:
            self.hp.tts_service = DummyTTS()
            self.hp.mqtt_client = dummy_client
            self.hp.mqtt_connected = True
            with self.hp.lock:
                self.hp.history.clear()
                self.hp.pc_state = {"status": "idle", "seq": 10, "session_id": "sess-1"}
                self.hp.pc_state_ts = "2025-01-01T00:00:00Z"

            resp = self.client.post(
                "/api/tts/speak",
                json={"text": "这是一条 TTS 测试语音", "push_to_device": True},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["pushed"])
            self.assertEqual(len(dummy_client.messages), 1)
            self.assertEqual(dummy_client.messages[0]["topic"], self.hp.mqtt_topic_speech)
            sent = dummy_client.messages[0]["data"]
            self.assertEqual(sent["type"], "speech")
            self.assertEqual(sent["speech"]["text"], "这是一条 TTS 测试语音")
            self.assertTrue(sent["speech"]["id"].startswith("speech-"))
        finally:
            self.hp.tts_service = old_tts
            self.hp.mqtt_client = old_client
            self.hp.mqtt_connected = old_connected

    def test_device_ack_update(self):
        """模拟硬件 ack 后，健康页能更新"""
        # 模拟收到 ack
        self.hp.on_message(None, None, type("Msg", (), {
            "topic": "whitebox/device/whitebox-001/ack",
            "payload": json.dumps({"last_seq": 42, "ts": "2025-01-01T00:01:00Z"}).encode(),
        })())

        resp = self.client.get("/api/state")
        data = resp.get_json()
        self.assertTrue(data["devices"]["whitebox-001"]["online"])
        self.assertEqual(data["devices"]["whitebox-001"]["ack"]["last_seq"], 42)
        self.assertIsNotNone(data["devices"]["whitebox-001"]["ack_ts"])

    def test_device_availability_offline(self):
        """模拟设备离线"""
        self.hp.on_message(None, None, type("Msg", (), {
            "topic": "whitebox/device/whitebox-001/availability",
            "payload": json.dumps({"status": "offline"}).encode(),
        })())

        resp = self.client.get("/api/state")
        data = resp.get_json()
        self.assertFalse(data["devices"]["whitebox-001"]["online"])

    def test_interrupted_session_can_be_found_from_legacy_project_key(self):
        old_projects_dir = self.hp.CLAUDE_PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.hp.CLAUDE_PROJECTS_DIR = tmp
                session_dir = os.path.join(tmp, "D--manbo-white-box")
                os.makedirs(session_dir, exist_ok=True)
                session_path = os.path.join(session_dir, "sess-1.jsonl")
                lines = [
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "..." }]},
                        "timestamp": "2026-05-11T16:59:00Z",
                    },
                    {
                        "type": "user",
                        "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
                        "timestamp": "2026-05-11T16:59:16Z",
                    },
                ]
                with open(session_path, "w", encoding="utf-8") as f:
                    for line in lines:
                        f.write(json.dumps(line, ensure_ascii=False) + "\n")

                payload = {
                    "source": "claude-hook",
                    "status": "thinking",
                    "project_key": "D-manbo-white_box",
                    "work_dir": "D:\\manbo\\white_box",
                    "session_id": "sess-1",
                    "updated_at": "2026-05-11T16:59:04Z",
                }
                self.assertTrue(self.hp.session_interrupted_after_payload(payload))
        finally:
            self.hp.CLAUDE_PROJECTS_DIR = old_projects_dir

    def test_interrupted_session_prefers_transcript_path(self):
        old_projects_dir = self.hp.CLAUDE_PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.hp.CLAUDE_PROJECTS_DIR = tmp
                session_dir = os.path.join(tmp, "ignored-project")
                os.makedirs(session_dir, exist_ok=True)
                transcript_path = os.path.join(session_dir, "sess-2.jsonl")
                lines = [
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
                        "timestamp": "2026-05-11T16:59:00Z",
                    },
                    {
                        "type": "user",
                        "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
                        "timestamp": "2026-05-11T16:59:16Z",
                    },
                ]
                with open(transcript_path, "w", encoding="utf-8") as f:
                    for line in lines:
                        f.write(json.dumps(line, ensure_ascii=False) + "\n")

                payload = {
                    "source": "claude-hook",
                    "status": "thinking",
                    "project_key": "wrong-project",
                    "transcript_path": transcript_path,
                    "work_dir": "D:\\manbo\\white_box",
                    "session_id": "sess-2",
                    "updated_at": "2026-05-11T16:59:04Z",
                }
                self.assertTrue(self.hp.session_interrupted_after_payload(payload))
        finally:
            self.hp.CLAUDE_PROJECTS_DIR = old_projects_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
