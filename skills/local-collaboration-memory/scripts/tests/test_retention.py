"""Synthetic filesystem fixtures only; no real records are aged or removed."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_memory.retention import Retention
from local_memory.store import ConflictError, ValidationError
from local_memory.integration import handle_hook
from local_memory.mcp import MCPServer


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="local-retention-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.records = self.workspace / "study" / "处理记录"
        self.records.mkdir(parents=True)
        (self.records / "trace.log").write_text("synthetic log", encoding="utf-8")
        (self.records / "source.csv").write_text("original", encoding="utf-8")
        (self.records / "reuse.py").write_text("# reusable", encoding="utf-8")
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.cleanup = Retention(self.root / "private", clock=lambda: self.now)
        self.cleanup.configure([str(self.workspace)])
        self.target = self.cleanup.scan()["targets"][0]

    def due(self):
        self.now += timedelta(days=15)
        result = self.cleanup.scan()
        return result["pending"][0]

    def recycle(self, target=None, **changes):
        target = target or self.due()
        args = dict(proposal_id=target["proposal"]["id"], files=["trace.log"],
                    reason="fixture log no longer needed; dependencies checked", confirmed=True)
        args.update(changes)
        return self.cleanup.decision(target["id"], "recycle", **args)

    def test_exact_15_day_boundary_scan_never_resets_clock(self):
        start = self.target["last_used"]
        self.now += timedelta(days=15, microseconds=-1)
        self.assertEqual(self.cleanup.scan()["pending"], [])
        self.now += timedelta(microseconds=1)
        result = self.cleanup.scan()
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(result["targets"][0]["last_used"], start)
        proposal = result["pending"][0]["proposal"]["id"]
        self.now += timedelta(days=30)
        self.assertEqual(self.cleanup.scan()["pending"][0]["proposal"]["id"], proposal)
        self.assertTrue((self.records / "trace.log").exists(), "silence must never authorize removal")

    def test_old_mtime_starts_from_enrollment_not_assumed_idle(self):
        other = self.workspace / "other" / "处理记录"
        other.mkdir(parents=True)
        file = other / "old.log"
        file.write_text("old")
        os.utime(file, (1, 1))
        self.now += timedelta(days=100)
        found = next(t for t in self.cleanup.scan()["targets"] if t["path"] == str(other))
        self.assertIsNone(found["proposal"])
        self.assertEqual(found["last_used"], self.now.isoformat())

    def test_new_processing_directory_is_discovered(self):
        new = self.workspace / "new-project" / "处理记录"
        new.mkdir(parents=True)
        (new / "record.txt").write_text("new")
        self.assertEqual(len(self.cleanup.scan()["targets"]), 2)

    def test_observed_changes_invalidate_previous_confirmation(self):
        target = self.due()
        (self.records / "trace.log").write_text("changed")
        with self.assertRaises(ConflictError):
            self.recycle(target)
        self.assertEqual(self.cleanup.scan()["pending"], [])
        self.assertTrue((self.records / "trace.log").exists())

    def test_same_size_same_mtime_content_change_is_detected(self):
        target = self.due()
        file = self.records / "trace.log"
        old = file.stat()
        file.write_text("different log", encoding="utf-8")
        os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaises(ConflictError):
            self.recycle(target)

    def test_actual_activity_only_affects_exact_project(self):
        target = self.due()
        self.assertEqual(self.cleanup.activity(self.workspace)["updated"], [])
        self.assertEqual(len(self.cleanup.status()["pending"]), 1)
        self.cleanup.activity(self.records / "source.csv", reason="actually opened source")
        self.assertEqual(self.cleanup.scan()["pending"], [])
        with self.assertRaises(ConflictError):
            self.recycle(target)

    def test_snooze_keep_and_resume(self):
        target = self.due()
        self.cleanup.decision(target["id"], "snooze")
        self.now += timedelta(days=14)
        self.assertEqual(self.cleanup.scan()["pending"], [])
        self.now += timedelta(days=1)
        self.assertEqual(len(self.cleanup.scan()["pending"]), 1)
        self.cleanup.decision(target["id"], "keep")
        self.now += timedelta(days=100)
        self.assertEqual(self.cleanup.scan()["pending"], [])
        self.cleanup.decision(target["id"], "resume")
        self.assertEqual(self.cleanup.scan()["pending"], [])

    def test_protected_scripts_data_and_explicit_evidence_cannot_move(self):
        self.cleanup.decision(self.target["id"], "protect", files=["trace.log"], reason="original evidence")
        target = self.due()
        self.assertTrue(all(r["protected_reason"] for r in target["proposal"]["files"]))
        for name in ["trace.log", "reuse.py", "source.csv"]:
            with self.assertRaises(ValidationError):
                self.recycle(target, files=[name])

    def test_missing_confirmation_and_unlisted_paths_cannot_move(self):
        target = self.due()
        for changes in [dict(confirmed=False), dict(reason=""), dict(files=["../outside.log"]),
                        dict(files=[]), dict(files=["trace.log", "trace.log"])]:
            with self.assertRaises(ValidationError):
                self.recycle(target, **changes)
        self.assertTrue((self.records / "trace.log").exists())

    def test_recycle_restores_exact_bytes_and_leaves_protected_files(self):
        original = (self.records / "trace.log").read_bytes()
        job = self.recycle()
        self.assertEqual(job["status"], "recycled")
        self.assertFalse((self.records / "trace.log").exists())
        self.assertTrue((self.records / "source.csv").exists())
        self.assertTrue((self.records / "reuse.py").exists())
        self.assertEqual((Path(job["recycle_path"]) / "trace.log").read_bytes(), original)
        self.cleanup.restore(job["id"])
        self.assertEqual((self.records / "trace.log").read_bytes(), original)
        self.assertFalse(Path(job["recycle_path"]).exists())

    def test_restore_never_overwrites_new_file(self):
        job = self.recycle()
        (self.records / "trace.log").write_text("new work")
        with self.assertRaises(ConflictError):
            self.cleanup.restore(job["id"])
        self.assertEqual((self.records / "trace.log").read_text(), "new work")
        self.assertTrue((Path(job["recycle_path"]) / "trace.log").exists())

    def test_concurrent_duplicate_confirmation_only_moves_once(self):
        target = self.due()
        def attempt(_):
            try:
                return self.recycle(target)["status"]
            except ConflictError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, ["recycled", "stale"])
        self.assertEqual(len(self.cleanup.status()["jobs"]), 1)

    def test_move_failure_is_visible_and_preserves_original(self):
        target = self.due()
        with patch.object(Path, "rename", side_effect=PermissionError("locked fixture")):
            job = self.recycle(target)
        self.assertEqual(job["status"], "failed")
        self.assertTrue((self.records / "trace.log").exists())
        self.assertIn("locked", self.cleanup.status()["jobs"][0]["error"])

    def test_partial_move_can_be_restored_without_losing_either_file(self):
        (self.records / "second.log").write_text("second")
        self.cleanup.scan()
        target = self.due()
        rename = Path.rename
        def move(source, dest):
            if source.name == "second.log":
                raise PermissionError("busy second file")
            return rename(source, dest)
        with patch.object(Path, "rename", move):
            job = self.recycle(target, files=["trace.log", "second.log"])
        self.assertEqual(job["status"], "partial")
        self.assertTrue((self.records / "second.log").exists())
        self.cleanup.restore(job["id"])
        self.assertEqual((self.records / "trace.log").read_text(), "synthetic log")

    def test_notification_ack_does_not_approve_or_reset_idle(self):
        target = self.due()
        self.cleanup.acknowledge([target["proposal"]["id"]])
        status = self.cleanup.scan()
        self.assertEqual(status["new_notifications"], [])
        self.assertEqual(len(status["pending"]), 1)
        self.assertEqual(status["targets"][0]["last_used"], self.target["last_used"])
        self.assertTrue((self.records / "trace.log").exists())

    def test_outside_root_and_wrong_directory_are_rejected(self):
        for value in [self.root / "处理记录", self.workspace, self.workspace / "study"]:
            with self.assertRaises(ValidationError):
                self.cleanup.enroll(value)

    def test_symlink_or_windows_junction_cannot_be_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "important.log").write_text("must survive")
        link = self.records / "linked"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("junction creation unavailable")
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            status = self.cleanup.scan()
            self.assertTrue(status["errors"])
            self.assertEqual(status["pending"], [])
            self.assertTrue((outside / "important.log").exists())
        finally:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()

    def test_hidden_files_are_in_confirmation_manifest(self):
        (self.records / ".hidden.log").write_text("hidden")
        self.cleanup.scan()
        target = self.due()
        self.assertIn(".hidden.log", [r["path"] for r in target["proposal"]["files"]])

    @unittest.skipUnless(os.name == "nt", "Windows file sharing semantics")
    def test_real_open_windows_file_is_retained_when_busy(self):
        target = self.due()
        with (self.records / "trace.log").open("rb"):
            job = self.recycle(target)
        self.assertEqual(job["status"], "failed")
        self.assertEqual((self.records / "trace.log").read_text(), "synthetic log")
        self.assertFalse(Path(job["recycle_path"]).exists(), "failed move must not leave an empty job folder")

    def test_restore_does_not_claim_success_for_missing_recycled_file(self):
        job = self.recycle()
        (Path(job["recycle_path"]) / "trace.log").unlink()
        with self.assertRaises(ConflictError):
            self.cleanup.restore(job["id"])

    def test_scheduler_hook_does_not_mark_scans_as_usage(self):
        target = self.due()
        payload = dict(hook_event_name="UserPromptSubmit", cwd=str(self.records.parent),
                       prompt="[local-housekeeping] scheduled scan")
        handle_hook(self.cleanup.home, payload)
        self.assertIsNotNone(self.cleanup.status()["targets"][0]["proposal"])
        payload["prompt"] = "continue actual work"
        handle_hook(self.cleanup.home, payload)
        self.assertIsNone(self.cleanup.status()["targets"][0]["proposal"])

    def test_cli_and_mcp_can_scan_without_deleting(self):
        result = subprocess.run([sys.executable, "-m", "local_memory", "--home", str(self.cleanup.home),
                                 "cleanup", "scan"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["pending"])
        server = MCPServer(self.cleanup.store)
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "local_cleanup_scan", "arguments": {}}})
        self.assertFalse(response["result"]["isError"])
        self.assertTrue((self.records / "trace.log").exists())


if __name__ == "__main__":
    unittest.main()
