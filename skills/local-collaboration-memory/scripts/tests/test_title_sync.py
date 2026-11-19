import uuid
"""Regression coverage for automatic titles, not just registry suggestions."""
import io
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_memory.integration import handle_hook, notify
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store
from local_memory.title_sync import HostTitleError, TitleClient, synchronize


class FakeClient:
    def __init__(self, title="工作主题", readback=None):
        self.title = title
        self.readback = readback
        self.writes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read_title(self, thread_id):
        return self.readback if self.writes and self.readback is not None else self.title

    def set_title(self, thread_id, title):
        self.writes.append((thread_id, title))
        self.title = title


class TitleSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-title-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "private"
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        self.native_id = str(uuid.UUID(int=1, version=4))
        self.row = self.registry.ensure(thread_id=self.native_id, source="fixture native identity")
        self.settings = {"host_title_sync": {"enabled": True, "binary": sys.executable,
                                              "codex_home": str(Path(self.temp.name) / "codex")}}
        (self.home / "settings.json").write_text(json.dumps(self.settings), encoding="utf-8")
        host_home = Path(self.settings["host_title_sync"]["codex_home"])
        host_home.mkdir()
        with closing(sqlite3.connect(host_home / "state_5.sqlite")) as con, con:
            con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT)")
            con.execute("INSERT INTO threads VALUES(?,?)", (self.native_id, "vscode"))

    def sync(self, client):
        with patch("local_memory.title_sync.TitleClient", return_value=client):
            return synchronize(self.home, self.row, source="test event")

    def test_new_title_is_actually_written_then_observed(self):
        client = FakeClient("整理示例文档")
        result = self.sync(client)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.writes, [(self.native_id, "[A00] 整理示例文档")])
        self.assertEqual(self.registry.show("A00")["title"], "[A00] 整理示例文档")
        self.assertTrue(result["registry_sync_ok"])

    def test_no_visible_title_receives_a_numbered_placeholder(self):
        client = FakeClient("")
        self.sync(client)
        self.assertEqual(client.title, "[A00] 新会话")

    def test_manual_free_title_and_existing_prefix_are_preserved(self):
        client = FakeClient("[A00] 我自己改的名字")
        result = self.sync(client)
        self.assertEqual(client.writes, [])
        self.assertFalse(result["changed"])
        self.assertEqual(self.registry.show("A00")["title"], client.title)

    def test_renaming_does_not_allocate_a_new_code(self):
        client = FakeClient("[Z99] [A06] 新名字")
        self.sync(client)
        self.assertEqual(client.title, "[A00] 新名字")
        self.assertEqual(len(self.registry.list()), 1)

    def test_retired_registration_is_never_sent_to_the_host_for_renaming(self):
        self.registry.retire(code="A00", source="User approved exact erroneous fixture", expected_revision=1)
        with patch("local_memory.title_sync.TitleClient") as factory:
            result = synchronize(self.home, self.row, source="historical fixture retry")
        self.assertEqual(result["status"], "retired")
        factory.assert_not_called()

    def test_title_changed_between_reads_keeps_latest_free_name(self):
        client = FakeClient("旧名字")
        count = 0
        def read(thread_id):
            nonlocal count
            count += 1
            if count == 2:
                client.title = "刚刚手改的新名字"
            return client.title
        client.read_title = read
        self.sync(client)
        self.assertEqual(client.title, "[A00] 刚刚手改的新名字")

    def test_bad_readback_does_not_claim_a_success_or_observe(self):
        client = FakeClient("原主题", readback="外部覆盖的名字")
        result = self.sync(client)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.registry.show("A00")["title"], "")
        status_file = self.home / "title-sync" / (self.native_id + ".json")
        text = status_file.read_text(encoding="utf-8")
        self.assertNotIn("原主题", text)
        self.assertNotIn("外部覆盖", text)

    def test_latest_registry_revision_used_after_concurrent_observation(self):
        self.registry.observe_title(code="A00", title="中间标题", source="other verified read", expected_revision=1)
        result = self.sync(FakeClient("最后标题"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.registry.show("A00")["title"], "[A00] 最后标题")

    def test_host_unavailable_is_pending_without_changing_registry(self):
        with patch("local_memory.title_sync.TitleClient", side_effect=TimeoutError("private raw error")):
            result = synchronize(self.home, self.row, source="test")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertEqual(self.registry.show("A00")["title"], "")
        self.assertNotIn("private raw error", json.dumps(result))

    def test_foreign_hosts_and_non_native_ids_are_not_sent_to_local_host(self):
        for changes, expected in [({"host": "another-machine"}, "unsupported_host"),
                                  ({"platform": "other"}, "unsupported_host"),
                                  ({"thread_id": "fixture"}, "unsupported_identity")]:
            with patch("local_memory.title_sync.TitleClient") as factory:
                result = synchronize(self.home, dict(self.row, **changes), source="test")
            self.assertEqual(result["status"], expected)
            factory.assert_not_called()

    def test_hook_direct_sync_precedes_large_context_and_child_is_skipped(self):
        with patch("local_memory.title_sync.TitleClient", return_value=FakeClient("测试标题")):
            result = handle_hook(self.home, {"hook_event_name": "SessionStart", "session_id": self.native_id})
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("本会话固定编号：A00"))
        self.assertIn("自动标题同步已回读核验", text)
        self.assertEqual(self.registry.show("A00")["title"], "[A00] 测试标题")
        with patch("local_memory.title_sync.TitleClient") as factory:
            handle_hook(self.home, {"hook_event_name": "SessionStart", "session_id": self.native_id,
                                    "agent_id": "child-agent"})
        factory.assert_not_called()

    def test_completion_repairs_an_overwritten_title_and_preserves_notifier(self):
        (self.home / "previous-notify.json").write_text('["old-notifier"]', encoding="utf-8")
        client = FakeClient("宿主自动生成的新主题")
        payload = {"type": "agent-turn-complete", "thread-id": self.native_id, "turn-id": "fixture-turn",
                   "last-assistant-message": "Fixture completed and verified."}
        with patch("local_memory.title_sync.TitleClient", return_value=client), \
             patch("local_memory.integration.subprocess.run") as previous:
            notify(self.home, payload)
        self.assertEqual(client.title, "[A00] 宿主自动生成的新主题")
        self.assertEqual(previous.call_args.args[0][0], "old-notifier")

    def test_disabled_integration_does_not_start_a_host_process(self):
        (self.home / "settings.json").write_text('{}', encoding="utf-8")
        with patch("local_memory.title_sync.TitleClient") as factory:
            result = synchronize(self.home, self.row, source="test")
        self.assertEqual(result, {"status": "disabled"})
        factory.assert_not_called()


class TitleClientTests(unittest.TestCase):
    def process(self, replies):
        from unittest.mock import MagicMock
        process = MagicMock()
        process.stdout = io.StringIO("".join(json.dumps(r) + "\n" for r in replies))
        process.stdin = io.StringIO()
        process.poll.return_value = None
        return process

    def test_wire_handshake_metadata_read_and_set_only(self):
        process = self.process([{"id": 1, "result": {}},
                                {"id": 2, "result": {"thread": {"id": "native", "name": "Title"}}},
                                {"id": 3, "result": {}}])
        with patch("local_memory.title_sync.subprocess.Popen", return_value=process):
            with TitleClient("codex", "/test/codex") as client:
                self.assertEqual(client.read_title("native"), "Title")
                client.set_title("native", "[A00] Title")
                sent = [json.loads(s) for s in process.stdin.getvalue().splitlines()]
        self.assertEqual([s["method"] for s in sent],
                         ["initialize", "initialized", "thread/read", "thread/name/set"])
        self.assertFalse(sent[2]["params"]["includeTurns"])
        process.terminate.assert_called_once()

    def test_failed_handshake_closes_child_process(self):
        process = self.process([{"id": 1, "error": {"message": "private details"}}])
        with patch("local_memory.title_sync.subprocess.Popen", return_value=process):
            with self.assertRaises(HostTitleError):
                with TitleClient("codex", "/test/codex"):
                    self.fail("failed handshake must not enter")
        process.terminate.assert_called_once()

    def test_wrong_native_thread_is_rejected(self):
        process = self.process([{"id": 1, "result": {}},
                                {"id": 2, "result": {"thread": {"id": "other", "name": "Title"}}}])
        with patch("local_memory.title_sync.subprocess.Popen", return_value=process):
            with TitleClient("codex", "/test/codex") as client:
                with self.assertRaises(HostTitleError):
                    client.read_title("native")


if __name__ == "__main__":
    unittest.main()
