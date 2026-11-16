"""Daily-entry interfaces share one pointer while native session identities stay stable."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_memory.daily_chat import DailyChat
from local_memory.integration import handle_hook
from local_memory.mcp import MCPServer
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store
from local_memory.web import make_server


class DailyChatInterfaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-daily-interface-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "private"
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        self.registry.ensure(thread_id="historical", title="[A00] old", source="fixture native identity")
        self.registry.ensure(thread_id="current", title="[A01] daily", source="fixture native identity")
        self.registry.ensure(thread_id="replacement", title="[A02] next", source="fixture native identity")
        self.daily = DailyChat(self.store)
        self.server = MCPServer(self.store)

    def payload(self, target="current", revision=0):
        return {"thread_id": target, "title": "Daily conversation", "expected_revision": revision,
                "source": "explicit fixture continuation request with verified native identity"}

    def cli(self, *arguments, payload=None):
        process = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", "entry.py", "--home", str(self.home), *arguments],
            cwd=Path(__file__).resolve().parents[1],
            input=None if payload is None else json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def mcp(self, name, arguments=None):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments or {}}})
        result = response["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def start_web(self):
        server = make_server(self.home, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(3)

        self.addCleanup(close)
        return server

    def http(self, server, method, path="/api/daily-chat", body=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Local-Memory-Token"] = server.token
            connection.request(method, path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_cli_and_mcp_bind_the_same_entry_without_reassigning_native_codes(self):
        before = self.registry.list()
        self.assertFalse(self.cli("daily-chat", "show")["configured"])
        created = self.cli("daily-chat", "bind", "--input", "-", payload=self.payload())
        self.assertTrue(created["sync"]["ok"])
        self.assertEqual(created["entry_code"], "A00")
        self.assertEqual(created["current_session"]["code"], "A01")
        failed, looked_up = self.mcp("local_daily_chat_lookup")
        self.assertFalse(failed)
        self.assertEqual(looked_up, self.cli("daily-chat", "show"))
        failed, changed = self.mcp("local_daily_chat_bind", self.payload("replacement", 1))
        self.assertFalse(failed)
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.cli("daily-chat", "show")["current_session"]["code"], "A02")
        failed, old = self.mcp("local_session_lookup", {"code": "A00"})
        self.assertFalse(failed)
        self.assertEqual(old["thread_id"], "historical")
        self.assertEqual(self.registry.list(), before)
        self.assertEqual(self.registry.ensure(thread_id="later", source="fixture native identity")["code"], "A03")
        index = (self.home / "会话与目录索引.md").read_text(encoding="utf-8")
        self.assertIn("## 日常聊天专用入口", index)
        self.assertIn("**入口 A00 · Daily conversation**", index)
        self.assertIn("当前关联真实会话：A02", index)
        self.assertIn("| A00 | [A00] old |", index)

    def test_mcp_rejects_stale_unknown_and_noninteger_revisions_without_changing_pointer(self):
        failed, created = self.mcp("local_daily_chat_bind", self.payload())
        self.assertFalse(failed)
        for payload, error in (
            (self.payload("replacement", 0), "ConflictError"),
            ({**self.payload("replacement", 1), "unexpected": True}, "ValidationError"),
            ({**self.payload("replacement", 1), "expected_revision": True}, "ValidationError"),
        ):
            failed, result = self.mcp("local_daily_chat_bind", payload)
            self.assertTrue(failed)
            self.assertEqual(result["error"], error)
        current = self.daily.show()
        self.assertEqual(current["revision"], created["revision"])
        self.assertEqual(current["current_session"]["thread_id"], "current")
        self.assertEqual(len(current["history"]), 1)

    def test_http_requires_auth_and_shares_cli_mcp_state_with_conflict_detection(self):
        server = self.start_web()
        self.assertEqual(self.http(server, "GET", token=False)[0], 403)
        self.assertEqual(self.http(server, "POST", body=self.payload(), token=False)[0], 403)
        status, result = self.http(server, "POST", body=self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["current_session"]["thread_id"], "current")
        status, result = self.http(server, "GET")
        self.assertEqual(status, 200)
        self.assertEqual(result["data"], self.cli("daily-chat", "show"))
        failed, lookup = self.mcp("local_daily_chat_lookup")
        self.assertFalse(failed)
        self.assertEqual(lookup, result["data"])
        self.assertEqual(self.http(server, "POST", body=self.payload("replacement", 0))[0], 409)
        self.assertEqual(self.http(server, "POST", body={**self.payload("replacement", 1), "unknown": True})[0], 400)
        status, result = self.http(server, "POST", body=self.payload("replacement", 1))
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["current_session"]["code"], "A02")
        self.assertEqual(self.registry.show("A00")["thread_id"], "historical")

    def test_hook_annotations_follow_the_bound_native_id_and_never_auto_adopt_another_chat(self):
        self.daily.bind(**self.payload())
        before = self.daily.show()
        with patch("local_memory.title_sync.synchronize", return_value={"status": "ok"}) as sync:
            matched = handle_hook(self.home, {"hook_event_name": "UserPromptSubmit",
                                              "session_id": "current", "prompt": "continue"})
            context = matched["hookSpecificOutput"]["additionalContext"]
            self.assertIn("本会话固定编号：A01", context)
            self.assertIn("本会话关联日常聊天专用入口：A00", context)
            other = handle_hook(self.home, {"hook_event_name": "SessionStart", "session_id": "replacement"})
            self.assertIn("本会话固定编号：A02", other["hookSpecificOutput"]["additionalContext"])
            self.assertNotIn("本会话关联日常聊天专用入口", other["hookSpecificOutput"]["additionalContext"])
            count = len(self.registry.list())
            child = handle_hook(self.home, {"hook_event_name": "SessionStart", "session_id": "child",
                                            "agent_id": "child-agent"})
            self.assertNotIn("本会话关联日常聊天专用入口", child["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(len(self.registry.list()), count)
            self.assertEqual(sync.call_count, 2)
        self.assertEqual(self.daily.show(), before)


if __name__ == "__main__":
    unittest.main()
