"""Minimal-slot recycling preserves allocation generations and external occupancy."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store, ConflictError, ValidationError

def native(n):return "00000000-0000-4000-8000-"+f"{n:012d}"

class RecyclingTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix="local-recycle-");self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.home=self.root/"private";self.host=self.root/"host";self.host.mkdir()
        self.store=Store(self.home);self.registry=SessionRegistry(self.store)
        with closing(sqlite3.connect(self.host/"state_5.sqlite")) as con,con:
            con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,source TEXT,thread_source TEXT)")
            con.executemany("INSERT INTO threads VALUES(?,?,?)",[(native(i),"vscode","user") for i in range(1,20)])
        self.snapshot={"complete":True,"scanned_at":"2026-11-18T00:00:00+00:00","live_thread_ids":[],"thread_codes":{},"directory_codes":{},"errors":[],"stats":{},"workspace_roots":[str(self.root)],"host_home":str(self.host)}
        self.mock=patch.object(SessionRegistry,"_scan_occupancy",side_effect=lambda:self.snapshot,create=True);self.mock.start();self.addCleanup(self.mock.stop)
    def seed(self,n):
        return [self.registry.ensure(thread_id=native(i+1),source="historical fixture") for i in range(n)]
    def enable(self):
        (self.home/"settings.json").write_text(json.dumps({"host_title_sync":{"codex_home":str(self.host)},"session_recycling":{"enabled":True,"workspace_roots":[str(self.root)]}}),encoding="utf-8")
        self.registry=SessionRegistry(self.store)
    def test_reuse_smallest_unoccupied_slot_preserves_all_existing_live_codes(self):
        rows=self.seed(5);self.snapshot["live_thread_ids"]=[native(2),native(6)]
        self.snapshot["directory_codes"]={"A02":[str(self.root/"A02-1_fixture")]}
        self.snapshot["thread_codes"]={"A03":["unregistered-existing-main"]}
        self.enable();plan=self.registry.recycling_scan()
        self.assertEqual(plan["next_code"],"A04")
        self.assertEqual([r["code"] for r in plan["releasable"]],["A04"])
        new=self.registry.ensure(thread_id=native(6),source="new real fixture")
        self.assertEqual(new["code"],"A04")
        self.assertGreater(new["revision"],rows[4]["revision"])
        self.assertEqual(self.registry.show(thread_id=native(2))["code"],"A01")
        self.assertEqual(self.registry.show("A00")["thread_id"],native(1))
    def test_complete_scan_required_before_any_release_or_allocation(self):
        self.seed(2);self.enable();self.snapshot["complete"]=False
        plan=self.registry.recycling_scan();self.assertIsNone(plan["next_code"])
        with self.assertRaises(ValidationError):self.registry.ensure(thread_id=native(3),source="fixture")
        self.assertEqual(len(self.registry.list()),2)
    def test_old_identity_cannot_reenter_and_archived_events_do_not_mix_generations(self):
        rows=self.seed(2);self.enable();self.snapshot["live_thread_ids"]=[native(3)]
        new=self.registry.ensure(thread_id=native(3),source="new fixture")
        self.assertEqual(new["code"],"A01")
        history=self.registry.recycling_history(code="A01")
        self.assertEqual(len(history),1);self.assertEqual(history[0]["thread_id"],native(2))
        self.assertTrue(history[0]["events"])
        old=self.registry.show(thread_id=native(2));self.assertTrue(old["archived"]);self.assertEqual(old["bindings"],[])
        with self.assertRaises(ValidationError):self.registry.ensure(thread_id=native(2),source="restored old identity")
        with self.assertRaises(ConflictError):self.registry.observe_title(code="A01",title="stale",source="old client",expected_revision=rows[1]["revision"])
        with self.registry._connect() as con:
            events=con.execute("SELECT revision FROM events WHERE session_seq=2").fetchall()
        self.assertTrue(all(r[0]>=new["revision"] for r in events))
    def test_scan_token_ignores_clock_and_rejects_real_occupancy_changes(self):
        self.seed(2);self.enable();plan=self.registry.recycling_scan()
        self.snapshot["scanned_at"]="2026-11-18T00:00:05+00:00"
        self.assertEqual(plan["scan_id"],self.registry.recycling_scan()["scan_id"])
        self.snapshot["directory_codes"]={"A01":[str(self.root/"A01-1_new")]}
        with self.assertRaises(ConflictError):self.registry.recycling_reclaim(source="user authorized",expected_scan_id=plan["scan_id"])
        self.assertEqual(len(self.registry.list()),2)
    def test_current_or_historical_daily_reference_prevents_release(self):
        from local_memory.daily_chat import DailyChat
        self.seed(3);daily=DailyChat(self.store)
        daily.bind(thread_id=native(2),title="fixture",source="explicit fixture",expected_revision=0)
        daily.bind(thread_id=native(3),title="fixture",source="explicit fixture",expected_revision=1)
        self.enable();plan=self.registry.recycling_scan()
        self.assertEqual(plan["releasable"],[])
        self.assertEqual(plan["next_code"],"A03")
    def test_concurrent_new_main_allocations_are_distinct_and_consecutive(self):
        self.seed(1);self.enable();self.snapshot["live_thread_ids"]=[native(2),native(3)]
        def allocate(i):return SessionRegistry(self.store).ensure(thread_id=native(i),source="parallel actual main fixture")["code"]
        with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(allocate,[2,3]))
        self.assertEqual(set(codes),{"A01","A02"})
        self.assertEqual(self.registry.ensure(thread_id=native(2),source="repeat")["code"],codes[0])
    def test_unique_existing_numbered_title_is_preserved_and_conflicts_are_rejected(self):
        self.seed(1);self.enable();self.snapshot["live_thread_ids"]=[native(2),native(3)]
        self.snapshot["thread_codes"]={"A05":[native(2)]}
        registered=self.registry.ensure(thread_id=native(2),source="existing main title")
        self.assertEqual(registered["code"],"A05")
        self.snapshot["thread_codes"]={"A05":[native(2),native(3)]}
        with self.assertRaises(ConflictError):self.registry.ensure(thread_id=native(3),source="conflicting title")
        self.assertEqual(len(self.registry.list()),2)
    def test_confirmed_reclaim_archives_then_new_identity_reuses_released_slot(self):
        self.seed(2);self.enable();self.snapshot["live_thread_ids"]=[native(3)]
        plan=self.registry.recycling_scan()
        result=self.registry.recycling_reclaim(source="user authorized exact verified empty list",expected_scan_id=plan["scan_id"])
        self.assertEqual([x["code"] for x in result["released"]],["A01"])
        new=self.registry.ensure(thread_id=native(3),source="verified new main")
        self.assertEqual((new["code"],new["generation"]),("A01",2))
    def test_disabled_reuse_after_migration_keeps_guard_and_safe_append_mode(self):
        self.seed(2);self.enable()
        settings=json.loads((self.home/"settings.json").read_text(encoding="utf-8"))
        settings["session_recycling"]["enabled"]=False
        (self.home/"settings.json").write_text(json.dumps(settings),encoding="utf-8")
        with patch.object(self.registry,"_scan_occupancy",side_effect=AssertionError("disabled reuse must not scan")):
            row=self.registry.ensure(thread_id=native(3),source="append after disabling recycling")
        self.assertEqual(row["code"],"A02")
        self.assertEqual(len(self.registry.list()),3)

    def test_legacy_retired_history_migrates_and_does_not_permanently_block_slot(self):
        self.seed(3);self.registry.retire(code="A01",source="historical authorized retirement",expected_revision=1)
        self.snapshot["live_thread_ids"]=[native(3),native(4)];self.enable()
        new=self.registry.ensure(thread_id=native(4),source="new fixture")
        self.assertEqual(new["code"],"A01")
        self.assertEqual(self.registry.recycling_history(thread_id=native(2))[0]["code"],"A01")
        self.assertTrue(self.registry.show(thread_id=native(2))["archived"])

if __name__=="__main__":unittest.main()
