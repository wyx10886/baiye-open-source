"""Daily entry reuse preserves native identities, evidence and registry backups."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_memory.daily_chat import DailyChat
from local_memory.session_registry import SessionRegistry
from local_memory.store import ConflictError, Store, ValidationError


class DailyChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-daily-chat-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "private"
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        self.daily = DailyChat(self.store)

    def register(self, identity, *, platform="codex", host="local"):
        return self.registry.ensure(thread_id=identity, title="Observed native title",
                                    source="fixture host readback", platform=platform, host=host)

    def bind(self, identity, revision=0, title="Daily conversation", **kwargs):
        return self.daily.bind(thread_id=identity, title=title, expected_revision=revision,
                               source="fixture explicit user selection", **kwargs)

    def test_empty_entry_does_not_allocate_or_recycle_native_codes(self):
        entry = self.daily.show()
        self.assertFalse(entry["configured"])
        self.assertEqual(entry["entry_kind"], "daily-chat")
        self.assertEqual(entry["entry_code"], "A00")
        self.assertEqual(entry["revision"], 0)
        self.assertIsNone(entry["current_session"])
        self.assertEqual(entry["history"], [])
        self.assertEqual(self.registry.list(), [])

    def test_create_and_rebind_preserve_native_codes_and_provenance(self):
        self.register("old-native")
        for index in range(1, 9):
            self.register(f"native-{index}")
        sessions_before = self.registry.list()
        self.assertEqual(self.registry.show(thread_id="native-8")["code"], "A08")
        first = self.bind("native-8")
        self.assertTrue(first["configured"])
        self.assertTrue(first["sync"]["ok"])
        self.assertEqual(first["entry_code"], "A00")
        self.assertEqual(first["current_session"]["code"], "A08")
        self.assertEqual(first["current_session"]["thread_id"], "native-8")
        self.assertEqual(first["current_session"]["observed_title"], "Observed native title")
        self.assertIsNone(first["history"][0]["previous_session"])
        self.assertEqual(first["history"][0]["source"], "fixture explicit user selection")
        self.assertEqual(self.registry.list(), sessions_before)
        self.assertEqual(self.registry.show("A00"), sessions_before[0])
        self.assertEqual(self.registry.show("A00")["thread_id"], "old-native")
        self.register("replacement-native")
        second = self.bind("replacement-native", revision=1, title="Daily continuation")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["current_session"]["code"], "A09")
        self.assertEqual(second["history"][1]["previous_session"]["code"], "A08")
        self.assertEqual(second["history"][1]["new_session"]["code"], "A09")
        self.assertEqual(second["history"][1]["previous_title"], "Daily conversation")
        self.assertEqual(second["history"][1]["new_title"], "Daily continuation")
        self.assertEqual(self.registry.show("A08")["thread_id"], "native-8")
        self.assertEqual(self.registry.show("A00")["thread_id"], "old-native")

    def test_idempotent_binding_does_not_duplicate_history_or_source(self):
        self.register("native")
        first = self.bind("native")
        again = self.daily.bind(thread_id="native", title="Daily conversation", expected_revision=1,
                                source="fixture repeated selection")
        self.assertEqual(again["revision"], 1)
        self.assertEqual(again["history"], first["history"])
        self.assertEqual(again["source"], first["source"])
        self.assertEqual(again["updated_at"], first["updated_at"])

    def test_unknown_target_stale_revision_and_invalid_inputs_leave_entry_unchanged(self):
        with self.assertRaises(ValidationError):
            self.bind("not-registered")
        self.assertFalse(self.daily.show()["configured"])
        self.register("native")
        first = self.bind("native")
        for invalid in (True, False, -1, "1", None):
            with self.subTest(revision=invalid), self.assertRaises(ValidationError):
                self.bind("native", revision=invalid)
        with self.assertRaises(ConflictError):
            self.bind("native", revision=0)
        with self.assertRaises(ValidationError):
            self.bind("not-registered", revision=1)
        with self.assertRaises(ValidationError):
            self.bind("native", revision=1, title=" ")
        with self.assertRaises(ValidationError):
            self.bind("../unsafe", revision=1)
        unchanged = self.daily.show()
        self.assertEqual(unchanged, {key: value for key, value in first.items() if key != "sync"})
        self.assertEqual(len(self.registry.list()), 1)

    def test_native_platform_and_host_are_part_of_explicit_selection(self):
        self.register("same-id")
        alternate = self.register("same-id", platform="other", host="remote")
        result = self.bind("same-id", platform="other", host="remote")
        self.assertEqual(result["current_session"]["code"], alternate["code"])
        self.assertEqual(result["current_session"]["platform"], "other")
        self.assertEqual(result["current_session"]["host"], "remote")

    def test_parallel_compare_and_swap_allows_only_one_rebinding(self):
        self.register("initial")
        self.register("first-replacement")
        self.register("second-replacement")
        self.bind("initial")

        def attempt(identity):
            try:
                return self.bind(identity, revision=1)["revision"]
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["first-replacement", "second-replacement"]))
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(self.daily.show()["revision"], 2)
        self.assertEqual(len(self.daily.show()["history"]), 2)
        self.assertEqual(len(self.registry.list()), 3)

    def test_memory_restore_does_not_rollback_daily_pointer_and_registry_backup_contains_it(self):
        self.register("initial")
        self.bind("initial")
        memory_backup = self.store.backup()
        self.register("replacement")
        self.bind("replacement", revision=1)
        self.store.restore(memory_backup)
        restored = DailyChat(Store(self.home)).show()
        self.assertEqual(restored["revision"], 2)
        self.assertEqual(restored["current_session"]["thread_id"], "replacement")
        registry_backup = self.registry.backup()
        with closing(sqlite3.connect(registry_backup["registry_backup"])) as con:
            self.assertEqual(con.execute("SELECT revision FROM daily_chat").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM daily_chat_events").fetchone()[0], 2)
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
