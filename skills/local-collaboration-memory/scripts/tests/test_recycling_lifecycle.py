"""Cross-generation lifecycle guards, with anonymous host evidence."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store, ValidationError


def native(n):
    return f"00000000-0000-4000-8000-{n:012d}"


class RecyclingLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="local-recycling-lifecycle-")
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.home=self.root/"private";self.host=self.root/"host";self.host.mkdir()
        self.store=Store(self.home);self.registry=SessionRegistry(self.store)
        with closing(sqlite3.connect(self.host/"state_5.sqlite")) as con,con:
            con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,source TEXT,thread_source TEXT)")
            con.executemany("INSERT INTO threads VALUES(?,?,?)",[(native(i),"vscode","user") for i in range(1,8)])
        self.snapshot={"complete":True,"live_thread_ids":[],"thread_codes":{},"directory_codes":{},"errors":[],"stats":{},"workspace_roots":[str(self.root)],"host_home":str(self.host)}
        scanner=patch.object(SessionRegistry,"_scan_occupancy",side_effect=lambda:self.snapshot,create=True)
        scanner.start();self.addCleanup(scanner.stop)

    def seed(self):
        self.registry.ensure(thread_id=native(1),source="historical A00 fixture")
        return self.registry.ensure(thread_id=native(2),source="historical old owner fixture")

    def enable(self):
        (self.home/"settings.json").write_text(json.dumps({"host_title_sync":{"codex_home":str(self.host)},
            "session_recycling":{"enabled":True,"workspace_roots":[str(self.root)]}}),encoding="utf-8")
        self.registry=SessionRegistry(self.store)

    def test_v2_retire_reuse_same_instance_and_reload_keep_events_separate(self):
        old=self.seed();self.enable();self.snapshot["live_thread_ids"]=[native(3)]
        self.registry.retire(code="A01",source="authorized anonymous retirement",expected_revision=old["revision"])
        new=self.registry.ensure(thread_id=native(3),source="new owner fixture")
        self.assertEqual(new["code"],"A01")
        self.assertGreater(new["revision"],old["revision"])
        reloaded=SessionRegistry(self.store)
        history=reloaded.recycling_history(code="A01")
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]["thread_id"],native(2))
        self.assertFalse(any(event["source"]=="new owner fixture" for event in history[0]["events"]))
        with reloaded._connect() as con:
            current=[dict(row) for row in con.execute("SELECT * FROM events WHERE session_seq=2")]
        self.assertTrue(current)
        self.assertTrue(all(row["revision"]>=new["revision"] for row in current))
        self.assertTrue(any(row["source"]=="new owner fixture" for row in current))

    def test_archived_owner_still_in_host_reserves_slot_without_title_prefix(self):
        old=self.seed()
        self.registry.retire(code="A01",source="historical retirement",expected_revision=old["revision"])
        self.snapshot["live_thread_ids"]=[native(2),native(3)]
        self.enable();scan=self.registry.recycling_scan()
        self.assertEqual(scan["next_code"],"A02")
        self.assertTrue(any(item["code"]=="A01" for item in scan["blocked"]))
        new=self.registry.ensure(thread_id=native(3),source="new owner fixture")
        self.assertEqual(new["code"],"A02")

    def test_restored_historical_owner_blocks_release_of_current_generation(self):
        self.seed();self.enable();self.snapshot["live_thread_ids"]=[native(3)]
        self.registry.ensure(thread_id=native(3),source="new generation fixture")
        self.snapshot["live_thread_ids"]=[native(2)]
        scan=self.registry.recycling_scan()
        self.assertEqual(scan["next_code"],"A02")
        self.assertNotIn("A01",[row["code"] for row in scan["releasable"]])
        self.assertTrue(any(row["code"]=="A01" for row in scan["blocked"]))

    def test_new_local_identity_missing_from_complete_inventory_cannot_allocate(self):
        self.seed();self.enable()
        with self.assertRaises(ValidationError):
            self.registry.ensure(thread_id=native(3),source="stale rollout identity fixture")
        self.assertEqual(len(self.registry.list()),2)
        self.assertEqual(self.registry.recycling_history(),[])


if __name__=="__main__":
    unittest.main()
