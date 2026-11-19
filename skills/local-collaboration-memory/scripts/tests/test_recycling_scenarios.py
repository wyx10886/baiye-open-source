"""End-to-end number-recycling scenarios using a real anonymous host index.

No production databases or chats are used. Each fixture lives in an automatically
removed temporary directory; occupancy scanning is exercised without mocks.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_memory.daily_chat import DailyChat
from local_memory.session_registry import SessionRegistry
from local_memory.store import ConflictError, Store, ValidationError
from local_memory.work_summary import WorkSummary


def native(index):
    return f"00000000-0000-4000-8000-{index:012d}"


class RecyclingScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-recycling-scenarios-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "private"
        self.host = self.root / "host"
        self.workspace = self.root / "work"
        self.host.mkdir()
        self.workspace.mkdir()
        self.store = Store(self.home)
        self.settings = {
            "host_title_sync": {"codex_home": str(self.host)},
            "session_recycling": {
                "enabled": False, "workspace_roots": [str(self.workspace)]
            },
        }
        self.settings_path = self.home / "settings.json"
        self.write_settings()
        self.host_db = self.host / "state_5.sqlite"
        with closing(sqlite3.connect(self.host_db)) as con, con:
            con.execute("""CREATE TABLE threads (
                id TEXT PRIMARY KEY, name TEXT, title TEXT, source TEXT,
                thread_source TEXT, attached_thread_parent_id TEXT, archived INTEGER
            )""")
        self.add_host(1, "Historical original A00 fixture")
        self.registry = SessionRegistry(self.store)
        self.original_a00 = self.registry.ensure(
            thread_id=native(1), title="Historical original A00 fixture",
            source="anonymous pre-recycling A00 allocation")
        self.assertEqual(self.original_a00["code"], "A00")
        self.settings["session_recycling"]["enabled"] = True
        self.write_settings()
        self.registry = SessionRegistry(self.store)

    def write_settings(self):
        self.settings_path.write_text(
            json.dumps(self.settings), encoding="utf-8")

    def add_host(self, index, name="Anonymous main chat", *, archived=False):
        identity = native(index)
        with closing(sqlite3.connect(self.host_db)) as con, con:
            con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (
                identity, name, "Anonymous initial prompt", "vscode", "user",
                None, int(archived)))
        return identity

    def delete_host(self, index):
        with closing(sqlite3.connect(self.host_db)) as con, con:
            con.execute("DELETE FROM threads WHERE id=?", (native(index),))

    def register(self, index, *, code=None, archived=False):
        name = f"[{code}] Anonymous main chat" if code else "Anonymous main chat"
        self.add_host(index, name, archived=archived)
        return self.registry.ensure(
            thread_id=native(index), title=name,
            source="anonymous current host-index readback")

    def bind_project(self, row, project):
        target = self.workspace / f"{row['code']}-1_{project}"
        target.mkdir()
        WorkSummary(self.store).checkpoint(
            project=project, key="overview",
            data={"root": str(target), "title": project, "goal": "anonymous fixture",
                  "current_state": "verified anonymous directory",
                  "next_steps": ["continue"], "coverage": "fixture only"},
            source="anonymous verified project directory")
        return self.registry.bind(
            code=row["code"], project=project,
            expected_revision=self.registry.show(row["code"])["revision"],
            source="anonymous verified project association")

    def scan(self):
        result = self.registry.recycling_scan()
        self.assertTrue(result["complete"], result["occupancy"]["errors"])
        return result

    def test_daily_entry_switch_restore_preserves_native_codes_and_directory_labels(self):
        current = self.bind_project(self.register(4, code="A04"), "current")
        replacement = self.bind_project(self.register(5, code="A05"), "replacement")
        before = self.registry.list()
        before_a00 = self.registry.show("A00")
        daily = DailyChat(self.store)
        first = daily.bind(thread_id=native(4), title="Anonymous daily entry",
                           source="anonymous explicit initial choice", expected_revision=0)
        changed = daily.bind(thread_id=native(5), title="Anonymous daily entry",
                             source="anonymous temporary continuation", expected_revision=first["revision"])
        self.assertEqual(changed["entry_code"], "A00")
        self.assertEqual(changed["current_session"]["code"], "A05")
        self.assertEqual(changed["current_session"]["thread_id"], native(5))
        with self.assertRaises(ConflictError):
            daily.bind(thread_id=native(4), title="Anonymous daily entry",
                       source="anonymous stale restore", expected_revision=first["revision"])
        restored = daily.bind(thread_id=native(4), title=first["title"],
                              source="anonymous explicit restore", expected_revision=changed["revision"])
        self.assertEqual(restored["current_session"], first["current_session"])
        self.assertEqual([entry["new_session"]["code"] for entry in restored["history"]],
                         ["A04", "A05", "A04"])
        self.assertEqual(self.registry.list(), before)
        self.assertEqual(self.registry.show("A00"), before_a00)
        self.assertEqual(current["bindings"][0]["directory_code"], "A04-1")
        self.assertEqual(replacement["bindings"][0]["directory_code"], "A05-1")
        self.assertEqual(self.registry.directory_plan(code="A05", parent=str(self.workspace),
                                                     name="second task")["directory_code"], "A05-2")

    def test_restored_daily_entry_keeps_deleted_previous_target_reserved_by_history(self):
        original = self.register(2)
        replacement = self.register(3)
        daily = DailyChat(self.store)
        first = daily.bind(thread_id=native(2), title="Anonymous daily entry",
                           source="anonymous original selection", expected_revision=0)
        second = daily.bind(thread_id=native(3), title=first["title"],
                            source="anonymous temporary continuation", expected_revision=1)
        daily.bind(thread_id=native(2), title=first["title"],
                   source="anonymous restore", expected_revision=second["revision"])
        self.delete_host(3)
        scan = self.scan()
        protected = next(row for row in scan["blocked"] if row["code"] == replacement["code"])
        self.assertIn("daily_chat_history", protected["reasons"])
        self.assertNotIn(replacement["code"], [row["code"] for row in scan["releasable"]])
        self.assertEqual(self.register(4)["code"], "A03")
        self.assertEqual(daily.show()["current_session"]["code"], original["code"])
        self.assertEqual(self.registry.show(replacement["code"])["thread_id"], native(3))

    def deleted_chat_directory_cycle(self, *, populated):
        old = self.register(2)
        folder = self.workspace / f"{old['code']}-1_kept_anonymous_folder"
        folder.mkdir()
        content = folder / "source.txt"
        if populated:
            content.write_text("anonymous retained source", encoding="utf-8")
        self.assertEqual(self.registry.show(old["code"])["bindings"], [])
        self.delete_host(2)
        scan = self.scan()
        self.assertEqual(scan["next_code"], "A02")
        blocked = next(row for row in scan["blocked"] if row["code"] == old["code"])
        self.assertIn("directory_occupancy", blocked["reasons"])
        self.assertEqual(self.register(3)["code"], "A02")
        self.assertTrue(folder.is_dir())
        if populated:
            self.assertEqual(content.read_text(encoding="utf-8"), "anonymous retained source")
            content.unlink()
        folder.rmdir()
        reused = self.register(4)
        self.assertEqual((reused["code"], reused["generation"]), (old["code"], 2))
        self.assertTrue(self.registry.show(thread_id=native(2))["archived"])
        self.assertEqual(self.registry.show("A00")["thread_id"], native(1))

    def test_deleted_chat_but_unbound_empty_directory_blocks_then_allows_reuse(self):
        self.deleted_chat_directory_cycle(populated=False)

    def test_deleted_chat_but_unbound_nonempty_directory_blocks_then_allows_reuse(self):
        self.deleted_chat_directory_cycle(populated=True)

    def test_archived_chat_and_removed_title_prefix_still_protect_its_native_slot(self):
        archived = self.register(2, code="A01", archived=True)
        with closing(sqlite3.connect(self.host_db)) as con, con:
            con.execute("UPDATE threads SET name=? WHERE id=?", ("Renamed without code", native(2)))
        scan = self.scan()
        self.assertEqual(scan["occupancy"]["stats"]["archived_threads"], 1)
        self.assertNotIn("A01", scan["occupancy"]["thread_codes"])
        self.assertEqual(self.register(3)["code"], "A02")
        self.assertEqual(self.registry.show(archived["code"])["thread_id"], native(2))
        self.delete_host(2)
        self.assertEqual(self.register(4)["code"], "A01")

    def test_hidden_nested_directory_reserves_even_without_any_registration(self):
        folder = self.workspace / ".hidden" / "nested" / "A01-1_retained"
        folder.mkdir(parents=True)
        scan = self.scan()
        self.assertIn(str(folder), scan["occupancy"]["directory_codes"]["A01"])
        self.assertEqual(self.register(2)["code"], "A02")
        folder.rmdir()
        self.assertEqual(self.register(3)["code"], "A01")

    def test_only_a_numbered_file_is_enough_to_reserve_a_deleted_chat_number(self):
        old = self.register(2)
        nested = self.workspace / ".hidden" / "nested"
        nested.mkdir(parents=True)
        document = nested / "A01_retained_document.txt"
        document.write_text("anonymous source remains", encoding="utf-8")
        self.delete_host(2)
        self.assertEqual(self.register(3)["code"], "A02")
        self.assertEqual(document.read_text(encoding="utf-8"), "anonymous source remains")
        document.unlink()
        self.assertEqual(self.register(4)["code"], old["code"])

    def test_decimal_boundaries_and_case_normalization_do_not_consume_similar_codes(self):
        (self.workspace / "A010_unrelated_three_digits").mkdir()
        (self.workspace / "AA01_unrelated_letter_prefix").mkdir()
        (self.workspace / "a02_retained.txt").write_text("anonymous fixture", encoding="utf-8")
        self.assertEqual(self.register(2)["code"], "A01")
        self.assertEqual(self.register(3)["code"], "A03")
        codes = self.scan()["occupancy"]["directory_codes"]
        self.assertNotIn("A01", codes)
        self.assertIn("AA01", codes)
        self.assertIn("A02", codes)

    def test_alphanumeric_cache_and_accessibility_names_do_not_reserve_codes(self):
        for label in ("a01y-debugging", "a02d7f8c89e_cache", "a11y-testing", "a12de8f13-cache"):
            (self.workspace / label).mkdir()
        codes = self.scan()["occupancy"]["directory_codes"]
        for code in ("A01", "A02", "A11", "A12"):
            self.assertNotIn(code, codes)
        self.assertEqual(self.register(2)["code"], "A01")
        self.assertEqual(self.register(3)["code"], "A02")

    def test_new_directory_after_confirmation_prevents_stale_release(self):
        old = self.register(2)
        self.delete_host(2)
        first = self.scan()
        folder = self.workspace / "A01-1_new_occupancy"
        folder.mkdir()
        with self.assertRaises(ConflictError):
            self.registry.recycling_reclaim(
                source="anonymous explicit confirmation of first list",
                expected_scan_id=first["scan_id"])
        self.assertFalse(self.registry.show(thread_id=native(2)).get("archived", False))
        self.assertEqual(self.registry.recycling_history(code=old["code"]), [])
        folder.rmdir()
        current = self.scan()
        result = self.registry.recycling_reclaim(
            source="anonymous explicit confirmation after fresh scan",
            expected_scan_id=current["scan_id"])
        self.assertEqual([row["code"] for row in result["released"]], ["A01"])

    def test_missing_configured_workspace_blocks_allocation_without_releasing_old_slot(self):
        old = self.register(2)
        self.delete_host(2)
        self.add_host(3)
        self.settings["session_recycling"]["workspace_roots"] = [str(self.root / "missing")]
        self.write_settings()
        scan = self.registry.recycling_scan()
        self.assertFalse(scan["complete"])
        self.assertIsNone(scan["next_code"])
        with self.assertRaises(ValidationError):
            self.registry.ensure(thread_id=native(3), source="anonymous failed-inventory attempt")
        self.assertFalse(self.registry.show(thread_id=native(2)).get("archived", False))
        self.assertEqual(self.registry.recycling_history(code=old["code"]), [])


if __name__ == "__main__":
    unittest.main()
