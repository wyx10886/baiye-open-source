import uuid
"""Persistent allocation guards also protect against stale loaded clients."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store
MAIN=str(uuid.UUID(int=201, version=4))
OTHER=str(uuid.UUID(int=202, version=4))
CHILD=str(uuid.UUID(int=203, version=4))
UNKNOWN=str(uuid.UUID(int=204, version=4))

class DatabaseGateTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="local-db-gate-")
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.home=self.root/"private";self.host=self.root/"host"
        self.host.mkdir();self.store=Store(self.home)
        self.settings={"verify_native_allocations":True,"host_title_sync":{"codex_home":str(self.host)}}
        (self.home/"settings.json").write_text(json.dumps(self.settings),encoding="utf-8")
        with closing(sqlite3.connect(self.host/"state_5.sqlite")) as con,con:
            con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,source TEXT,thread_source TEXT)")
            con.executemany("INSERT INTO threads VALUES(?,?,?)",[(MAIN,"vscode","user"),(OTHER,"vscode","user"),(CHILD,"vscode","subagent")])
        self.registry=SessionRegistry(self.store)
    def snapshot(self):
        with closing(sqlite3.connect(self.registry.db)) as con:
            return {t:con.execute("SELECT * FROM "+t+" ORDER BY rowid").fetchall() for t in ("sessions","events","sqlite_sequence")}
    def insert(self,con,native,platform="codex",host="local"):
        return con.execute("INSERT INTO sessions(platform,host,thread_id,title,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(platform,host,native,"","caller claims main","fixture","fixture"))
    def test_legacy_connection_cannot_allocate_and_new_main_codes_remain_consecutive(self):
        before=self.snapshot()
        with closing(sqlite3.connect(self.registry.db)) as con,con:
            with self.assertRaises(sqlite3.Error):self.insert(con,UNKNOWN)
        self.assertEqual(self.snapshot(),before)
        first=self.registry.ensure(thread_id=MAIN,source="fixture metadata")
        with patch("local_memory.title_sync.native_identity",side_effect=AssertionError("reuse must not revalidate")):
            again=self.registry.ensure(thread_id=MAIN,source="repeat")
        second=self.registry.ensure(thread_id=OTHER,source="fixture metadata")
        self.assertEqual((first["code"],again["code"],second["code"]),("A00","A00","A01"))
    def test_internal_primitive_cannot_bypass_child_unknown_or_reserved_case_guard(self):
        before=self.snapshot()
        for native,platform,host in [(CHILD,"codex","local"),(UNKNOWN,"codex","local"),(CHILD,"CODEX","LOCAL")]:
            with self.subTest(native=native,platform=platform):
                with self.registry._connect() as con:
                    with self.assertRaises(sqlite3.Error):self.insert(con,native,platform,host)
                self.assertEqual(self.snapshot(),before)
    def test_callback_failure_fails_closed_without_private_exception_text(self):
        with patch("local_memory.title_sync.native_identity",side_effect=OSError("private diagnostic text")):
            with self.registry._connect() as con:
                with self.assertRaises(sqlite3.Error) as error:self.insert(con,MAIN)
        self.assertNotIn("private diagnostic text",str(error.exception))
        self.assertEqual(self.registry.list(),[])
    def test_new_connection_preserves_foreign_hosts_and_false_flag_does_not_drop_guard(self):
        with patch("local_memory.title_sync.native_identity",side_effect=AssertionError("foreign allocation")):
            a=self.registry.ensure(thread_id="foreign",platform="other",source="fixture")
            b=self.registry.ensure(thread_id="remote",host="remote-host",source="fixture")
        self.assertEqual((a["code"],b["code"]),("A00","A01"))
        (self.home/"settings.json").write_text(json.dumps(dict(self.settings,verify_native_allocations=False)),encoding="utf-8")
        SessionRegistry(self.store)
        with closing(sqlite3.connect(self.registry.db)) as con,con:
            with self.assertRaises(sqlite3.Error):self.insert(con,UNKNOWN)
    def test_only_literal_boolean_true_installs_persistent_trigger(self):
        with closing(sqlite3.connect(self.registry.db)) as con:
            self.assertIsNotNone(con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='sessions_verify_local_main'").fetchone())
        for index,value in enumerate((False,"true",1,None)):
            home=self.root/("off-"+str(index));store=Store(home)
            (home/"settings.json").write_text(json.dumps({"verify_native_allocations":value}),encoding="utf-8")
            registry=SessionRegistry(store)
            with closing(sqlite3.connect(registry.db)) as con:
                self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='sessions_verify_local_main'").fetchone())
if __name__=="__main__":unittest.main()
