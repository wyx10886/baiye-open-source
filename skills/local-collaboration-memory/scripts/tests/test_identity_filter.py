"""Native metadata gates automatic numbering before any allocation or rename."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from local_memory.integration import handle_hook, notify
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store
from local_memory.title_sync import native_identity, synchronize


class IdentityFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-identity-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "private"
        self.host = self.root / "codex"
        self.host.mkdir()
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        self.native_id = str(uuid.UUID(int=42, version=4))
        (self.home / "settings.json").write_text(json.dumps({"host_title_sync": {
            "enabled": True, "binary": sys.executable, "codex_home": str(self.host)}}), encoding="utf-8")

    def metadata(self, source, native_id=None, rollout_path="", thread_source=None, parent=None):
        with closing(sqlite3.connect(self.host / "state_5.sqlite")) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, source TEXT, rollout_path TEXT, thread_source TEXT, attached_thread_parent_id TEXT)")
            con.execute("INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?)",
                        (native_id or self.native_id, source, rollout_path, thread_source, parent))

    def test_completion_without_parent_fields_rejects_authoritative_subagent(self):
        self.metadata(json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "fixture-parent"}}}))
        (self.home / "previous-notify.json").write_text('["previous-notifier"]', encoding="utf-8")
        with patch("local_memory.title_sync.TitleClient") as client, \
             patch("local_memory.store.Store.capture_turn") as capture, \
             patch("local_memory.integration.subprocess.run") as previous:
            notify(self.home, {"type": "agent-turn-complete", "thread-id": self.native_id})
        self.assertEqual(self.registry.list(), [])
        client.assert_not_called()
        capture.assert_called_once()
        previous.assert_called_once()

    def test_hook_without_parent_fields_rejects_authoritative_subagent(self):
        self.metadata(json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "fixture-parent"}}}))
        with patch("local_memory.title_sync.synchronize") as sync:
            result = handle_hook(self.home, {"hook_event_name": "SessionStart", "session_id": self.native_id})
        self.assertEqual(self.registry.list(), [])
        self.assertNotIn("本会话固定编号", result["hookSpecificOutput"]["additionalContext"])
        sync.assert_not_called()

    def test_unknown_identity_is_pending_and_does_not_consume_a_number(self):
        with patch("local_memory.title_sync.synchronize") as sync:
            result = handle_hook(self.home, {"hook_event_name": "SessionStart", "session_id": self.native_id})
        self.assertEqual(self.registry.list(), [])
        self.assertIn("身份待核验", result["hookSpecificOutput"]["additionalContext"])
        status = json.loads((self.home / "identity-sync" / (self.native_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "pending")
        sync.assert_not_called()
        self.metadata("vscode", thread_source="user")
        with patch("local_memory.title_sync.synchronize", return_value={"status": "ok"}):
            result = handle_hook(self.home, {"hook_event_name": "UserPromptSubmit", "session_id": self.native_id})
        self.assertIn("固定编号：A00", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.registry.list()[0]["code"], "A00")

    def test_unknown_source_string_never_defaults_to_main(self):
        self.metadata("a-future-origin")
        self.assertEqual(native_identity(self.home, self.native_id)["status"], "pending")

    def test_explicit_runtime_child_type_overrides_a_main_source(self):
        self.metadata("vscode", thread_source="subagent")
        self.assertEqual(native_identity(self.home, self.native_id)["status"], "subagent")

    def test_direct_title_sync_does_not_rename_a_previously_misallocated_child(self):
        self.metadata(json.dumps({"subagent": {"thread_spawn": {"depth": 1}}}))
        row = self.registry.ensure(thread_id=self.native_id, source="historical fixture pollution")
        with patch("local_memory.title_sync.TitleClient") as client:
            result = synchronize(self.home, row, source="fixture")
        self.assertEqual(result["status"], "skipped_subagent")
        client.assert_not_called()

    def test_rollout_fallback_requires_matching_session_meta_id(self):
        path = self.host / "sessions" / "2026" / "11" / "16" / ("rollout-fixture-" + self.native_id + ".jsonl")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.native_id, "source": "vscode"}}) + "\n", encoding="utf-8")
        self.assertEqual(native_identity(self.home, self.native_id)["status"], "main")
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": str(uuid.UUID(int=99, version=4)), "source": "vscode"}}) + "\n", encoding="utf-8")
        self.assertEqual(native_identity(self.home, self.native_id)["status"], "pending")

    def test_other_runtime_thread_id_cannot_authorize_the_payload(self):
        self.metadata("vscode", native_id=str(uuid.UUID(int=99, version=4)))
        self.assertEqual(native_identity(self.home, self.native_id)["status"], "pending")

    def test_confirmed_main_completion_allocates_once(self):
        self.metadata("vscode", thread_source="user")
        with patch("local_memory.title_sync.synchronize", return_value={"status": "ok"}), \
             patch("local_memory.store.Store.capture_turn"):
            notify(self.home, {"thread-id": self.native_id})
            notify(self.home, {"thread-id": self.native_id})
        self.assertEqual([r["code"] for r in self.registry.list()], ["A00"])

    def test_identity_lookup_does_not_modify_the_host_index(self):
        self.metadata("vscode", thread_source="user")
        path = self.host / "state_5.sqlite"
        before = path.read_bytes()
        self.assertEqual(native_identity(self.home, self.native_id)["status"], "main")
        self.assertEqual(path.read_bytes(), before)

    def test_noncanonical_identity_is_rejected_before_host_access(self):
        for value in (self.native_id.replace("-", ""), "urn:uuid:" + self.native_id, "../fixture"):
            with self.subTest(value=value), patch("local_memory.integration._read_json") as read:
                result = native_identity(self.home, value)
            self.assertEqual(result["evidence"], "unsupported_identity")
            read.assert_not_called()

    def test_identity_diagnostics_exclude_host_paths_and_raw_metadata(self):
        self.metadata("vscode", thread_source="user", rollout_path="synthetic-sensitive-path")
        result = native_identity(self.home, self.native_id)
        self.assertEqual(result["status"], "main")
        status = (self.home / "identity-sync" / (self.native_id + ".json")).read_text(encoding="utf-8")
        self.assertNotIn("synthetic-sensitive-path", status)
        self.assertNotIn(str(self.host), status)
        self.assertEqual(set(json.loads(status)), {"thread_id", "status", "evidence", "checked_at"})

    def test_indexed_rollout_path_cannot_read_unrelated_host_files(self):
        candidates = (
            self.host / "auth.json",
            self.host / ("rollout-fixture-" + self.native_id + ".jsonl"),
            self.host / "sessions" / "unrelated.jsonl",
            self.host / "sessions" / ("fixture-" + self.native_id + ".txt"),
        )
        original_open = Path.open
        for path in candidates:
            with self.subTest(filename=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"type": "session_meta", "payload": {
                    "id": self.native_id, "source": "vscode"}}) + "\n", encoding="utf-8")
                self.metadata("a-future-origin", rollout_path=str(path))
                def guarded_open(target, *args, **kwargs):
                    if target == path:
                        raise AssertionError("Unrelated host file must not be opened")
                    return original_open(target, *args, **kwargs)
                with patch.object(Path, "open", guarded_open):
                    self.assertEqual(native_identity(self.home, self.native_id)["status"], "pending")

    def test_rollout_fallback_survives_an_unreadable_runtime_index(self):
        (self.host / "state_5.sqlite").write_bytes(b"not a sqlite database")
        path = self.host / "sessions" / "2026" / "11" / "16" / ("rollout-fixture-" + self.native_id + ".jsonl")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.native_id, "source": "vscode"}}) + "\n", encoding="utf-8")
        self.assertEqual(native_identity(self.home, self.native_id)["status"], "main")


if __name__ == "__main__":
    unittest.main()
