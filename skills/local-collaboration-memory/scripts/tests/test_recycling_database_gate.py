"""Generation protocol closes every stale-writer mutation path."""
from contextlib import closing
import sqlite3
import unittest
import test_session_recycling as fixtures

class RecyclingProtocolTests(unittest.TestCase):
    setUp=fixtures.RecyclingTests.setUp
    seed=fixtures.RecyclingTests.seed
    enable=fixtures.RecyclingTests.enable
    def db_snapshot(self):
        with closing(sqlite3.connect(self.registry.db)) as con:
            names=[x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name:con.execute('SELECT * FROM "'+name+'" ORDER BY rowid').fetchall() for name in names}
    def test_old_clients_without_generation_protocol_cannot_mutate_active_or_history(self):
        self.seed(2);self.enable();before=self.db_snapshot()
        statements=[("UPDATE sessions SET title='stale' WHERE seq=2",()),
                    ("DELETE FROM sessions WHERE seq=2",()),
                    ("INSERT INTO events(session_seq,revision,operation,source,at) VALUES(2,2,'stale','fixture','fixture')",()),
                    ("DELETE FROM bindings WHERE session_seq=2",()),
                    ("INSERT INTO daily_chat VALUES(1,2,'stale','fixture',1,'fixture','fixture')",())]
        for sql,args in statements:
            with self.subTest(sql=sql),closing(sqlite3.connect(self.registry.db)) as con,con:
                con.create_function("local_native_main",1,lambda _:1)
                with self.assertRaises(sqlite3.Error):con.execute(sql,args)
            self.assertEqual(self.db_snapshot(),before)
    def test_legacy_main_verifier_cannot_append_and_new_generation_writer_can(self):
        self.seed(2);self.enable();self.snapshot["live_thread_ids"]=[fixtures.native(3)]
        before=self.db_snapshot()
        with closing(sqlite3.connect(self.registry.db)) as con,con:
            con.create_function("local_native_main",1,lambda _:1)
            with self.assertRaises(sqlite3.Error):
                con.execute("INSERT INTO sessions(platform,host,thread_id,title,source,created_at,updated_at) VALUES('codex','local',?,'','legacy','fixture','fixture')",(fixtures.native(3),))
        self.assertEqual(self.db_snapshot(),before)
        row=self.registry.ensure(thread_id=fixtures.native(3),source="actual new writer")
        self.assertEqual(row["code"],"A01")
    def test_new_connection_requires_exact_transaction_ticket_even_with_verified_native(self):
        self.seed(1);self.enable();before=self.db_snapshot()
        with self.registry._connect() as con:
            with self.assertRaises(sqlite3.Error):
                con.execute("INSERT INTO sessions(seq,platform,host,thread_id,title,source,created_at,updated_at) VALUES(2,'codex','local',?,'','unchecked','fixture','fixture')",(fixtures.native(2),))
        self.assertEqual(self.db_snapshot(),before)

if __name__=="__main__":unittest.main()
