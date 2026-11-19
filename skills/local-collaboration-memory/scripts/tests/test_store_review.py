"""Regression tests from independent storage/privacy review."""

import concurrent.futures
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from local_memory.store import Store, ValidationError


class StorageReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-store-review-")
        self.home = Path(self.temp.name) / "home"
        self.store = Store(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def remember(self, **changes):
        values = dict(kind="event", title="核验记录", content="PRIVATE_OLD_SENTENCE", source="用户确认的测试事实")
        values.update(changes)
        return self.store.remember(**values)

    def edit_database(self, path, sql, params=()):
        con = sqlite3.connect(path)
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def test_partial_schema_rejected_before_live_data_or_backup_changes(self):
        item = self.remember()
        fake = Path(self.temp.name) / "partial.sqlite3"
        con = sqlite3.connect(fake)
        try:
            con.executescript("""
                CREATE TABLE memories(id TEXT, root_id TEXT, revision INTEGER, content TEXT, state TEXT);
                CREATE TABLE requests(key TEXT,memory_id TEXT);
                CREATE TABLE audit(seq INTEGER PRIMARY KEY,action TEXT,memory_id TEXT,at TEXT);
                CREATE TABLE reviews(day TEXT,note TEXT,at TEXT);
                PRAGMA user_version=1;
            """)
        finally:
            con.close()
        with self.assertRaises(ValidationError):
            self.store.restore(fake)
        self.assertEqual(self.store.get(item["id"])["content"], "PRIVATE_OLD_SENTENCE")
        self.assertFalse((self.home / "backups").exists())

    def test_backup_trigger_and_broken_revision_graph_are_rejected(self):
        item = self.remember()
        triggered = self.store.backup()
        self.edit_database(triggered, "CREATE TRIGGER unexpected AFTER INSERT ON audit BEGIN DELETE FROM memories; END")
        with self.assertRaises(ValidationError):
            self.store.restore(triggered)
        broken = self.store.backup()
        self.edit_database(broken, "UPDATE memories SET superseded_by=? WHERE id=?", (item["id"], item["id"]))
        with self.assertRaises(ValidationError):
            self.store.restore(broken)
        self.assertEqual(self.store.get(item["id"])["state"], "active")

    def test_valid_backup_restores_deleted_chains_and_reviews(self):
        old = self.remember(key="state.current")
        new = self.store.revise(old["id"], expected_revision=1, source="最新核验", content="PRIVATE_NEW_SENTENCE")
        self.store.forget(new["id"], expected_revision=2)
        self.store.review("仅记录实际复盘")
        backup = self.store.backup()
        extra = self.remember(content="SNAPSHOT_LATER_SENTENCE")
        self.store.restore(backup)
        with self.assertRaises(ValidationError):
            self.store.get(extra["id"])
        self.assertTrue(all(r["state"] == "deleted" for r in self.store.list_memories(include_inactive=True)))
        self.assertEqual(self.store.stats()["last_review"]["note"], "仅记录实际复盘")

    def test_default_projection_updates_on_change_and_delete(self):
        item = self.remember()
        projection = self.home / "overview.md"
        self.assertIn("PRIVATE_OLD_SENTENCE", projection.read_text(encoding="utf-8"))
        replacement = self.store.revise(item["id"], expected_revision=1, source="更正", content="PRIVATE_NEW_SENTENCE")
        self.assertNotIn("PRIVATE_OLD_SENTENCE", projection.read_text(encoding="utf-8"))
        self.assertIn("PRIVATE_NEW_SENTENCE", projection.read_text(encoding="utf-8"))
        self.store.forget(replacement["id"], expected_revision=2)
        text = projection.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_OLD_SENTENCE", text)
        self.assertNotIn("PRIVATE_NEW_SENTENCE", text)

    def test_inflight_export_cannot_republish_deleted_text_after_forget_returns(self):
        item = self.remember()
        reached_publish = threading.Event()
        allow_publish = threading.Event()
        import local_memory.store as module
        original_replace = module.os.replace

        def delayed_replace(source, target):
            if threading.current_thread().name == "old-export":
                reached_publish.set()
                if not allow_publish.wait(5):
                    raise RuntimeError("test synchronization timed out")
            return original_replace(source, target)

        def exporter():
            threading.current_thread().name = "old-export"
            self.store.export_markdown()

        with patch.object(module.os, "replace", side_effect=delayed_replace):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                export_future = pool.submit(exporter)
                self.assertTrue(reached_publish.wait(5))
                delete_future = pool.submit(self.store.forget, item["id"], expected_revision=1)
                allow_publish.set()
                export_future.result(timeout=5)
                delete_future.result(timeout=5)
        self.assertNotIn("PRIVATE_OLD_SENTENCE", (self.home / "overview.md").read_text(encoding="utf-8"))

    def test_restore_is_exclusive_between_safety_backup_and_replacement(self):
        self.remember()
        snapshot = self.store.backup()
        reached_safety = threading.Event()
        allow_restore = threading.Event()
        writer_finished = threading.Event()
        original_backup = self.store.backup

        def paused_backup(*args, **kwargs):
            result = original_backup(*args, **kwargs)
            reached_safety.set()
            if not allow_restore.wait(5):
                raise RuntimeError("test synchronization timed out")
            return result

        def writer():
            item = self.remember(content="ACKNOWLEDGED_AFTER_RESTORE")
            writer_finished.set()
            return item

        with patch.object(self.store, "backup", side_effect=paused_backup):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                restore_future = pool.submit(self.store.restore, snapshot)
                self.assertTrue(reached_safety.wait(5))
                writer_future = pool.submit(writer)
                try:
                    self.assertFalse(writer_finished.wait(0.1))
                finally:
                    allow_restore.set()
                restore_future.result(timeout=5)
                acknowledged = writer_future.result(timeout=5)
        self.assertEqual(self.store.get(acknowledged["id"])["content"], "ACKNOWLEDGED_AFTER_RESTORE")

    def test_lock_coordinates_a_different_python_process(self):
        code = "from local_memory.store import Store; import sys; print('started',flush=True); s=Store(sys.argv[1]); s.review('child process'); print('finished',flush=True)"
        with self.store._exclusive():
            child = subprocess.Popen([sys.executable, "-c", code, str(self.home)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(child.stdout.readline().strip(), "started")
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.15)
            except BaseException:
                child.kill()
                child.communicate()
                raise
        try:
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("finished", out)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()


if __name__ == "__main__":
    unittest.main()
