import uuid
"""Public registry registration uses host evidence before consuming a code."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from local_memory.mcp import MCPServer
from local_memory.session_registry import SessionRegistry, dispatch_registry
from local_memory.store import Store, ValidationError
from local_memory.web import make_server

ROOT = Path(__file__).resolve().parents[1]
MAIN = str(uuid.UUID(int=101, version=4))
OTHER = str(uuid.UUID(int=102, version=4))
CHILD = str(uuid.UUID(int=103, version=4))
UNKNOWN = str(uuid.UUID(int=104, version=4))


class RegistrationGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="local-registration-gate-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "private"
        self.host = self.root / "host"
        self.host.mkdir()
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        (self.home / "settings.json").write_text(json.dumps({
            "host_title_sync": {"enabled": False, "codex_home": str(self.host)}}), encoding="utf-8")
        with closing(sqlite3.connect(self.host / "state_5.sqlite")) as con, con:
            con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, thread_source TEXT, attached_thread_parent_id TEXT, rollout_path TEXT)")
            con.executemany("INSERT INTO threads VALUES(?,?,?,?,?)", [
                (MAIN, "vscode", "user", None, ""),
                (OTHER, "vscode", "user", None, ""),
                (CHILD, json.dumps({"subagent":{"thread_spawn":{"parent_thread_id":MAIN}}}), "subagent", MAIN, "")])
        self.server = None

    def payload(self, identity):
        return {"operation":"ensure", "thread_id":identity,
                "source":"caller asserts main chat; host evidence must decide"}

    def snapshot(self):
        with self.registry._connect() as con:
            return {table:[tuple(r) for r in con.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                    for table in ("sessions", "events", "sqlite_sequence")}

    def cli(self, identity):
        return subprocess.run([sys.executable, "-B", "entry.py", "--home", str(self.home),
                "session", "update", "--input", "-"], input=json.dumps(self.payload(identity)),
                capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=20,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1"))

    def mcp(self, identity):
        response = MCPServer(self.store).handle({"jsonrpc":"2.0", "id":1, "method":"tools/call",
            "params":{"name":"local_session_update", "arguments":self.payload(identity)}})
        return response["result"]

    def http(self, identity):
        if self.server is None:
            self.server = make_server(self.home, port=0)
            worker = threading.Thread(target=self.server.serve_forever, daemon=True)
            worker.start()
            def stop():
                self.server.shutdown()
                self.server.server_close()
                worker.join(timeout=3)
            self.addCleanup(stop)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        connection.request("POST", "/api/session-update", json.dumps(self.payload(identity)),
            {"Content-Type":"application/json", "X-Local-Memory-Token":self.server.token})
        response = connection.getresponse()
        status, body = response.status, json.loads(response.read())
        connection.close()
        return status, body

    def test_cli_rejects_child_and_unknown_without_consuming_sequence(self):
        before = self.snapshot()
        for identity in (CHILD, UNKNOWN, "arbitrary-not-a-native-id"):
            with self.subTest(identity=identity):
                result = self.cli(identity)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(json.loads(result.stderr)["error"], "ValidationError")
                self.assertEqual(self.snapshot(), before)
        accepted = self.cli(MAIN)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["code"], "A00")

    def test_mcp_rejects_child_and_unknown_without_consuming_sequence(self):
        before = self.snapshot()
        for identity in (CHILD, UNKNOWN):
            result = self.mcp(identity)
            self.assertTrue(result.get("isError"), result)
            self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.mcp(MAIN).get("isError"))
        self.assertEqual(self.registry.show(thread_id=MAIN)["code"], "A00")

    def test_http_rejects_child_and_unknown_without_consuming_sequence(self):
        before = self.snapshot()
        for identity in (CHILD, UNKNOWN):
            status, body = self.http(identity)
            self.assertEqual(status, 400, body)
            self.assertFalse(body["ok"])
            self.assertEqual(self.snapshot(), before)
        status, body = self.http(MAIN)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["code"], "A00")

    def test_verified_mains_are_consecutive_and_repeat_identity_is_idempotent(self):
        first = dispatch_registry(self.store, **self.payload(MAIN))
        repeated = dispatch_registry(self.store, **self.payload(MAIN))
        second = dispatch_registry(self.store, **self.payload(OTHER))
        self.assertEqual((first["code"], repeated["code"], second["code"]), ("A00", "A00", "A01"))
        self.assertEqual(len(self.registry.list()), 2)

    def test_source_text_cannot_promote_unknown_or_child_to_main(self):
        for identity in (CHILD, UNKNOWN):
            with self.subTest(identity=identity), self.assertRaises(ValidationError):
                dispatch_registry(self.store, operation="ensure", thread_id=identity,
                    source="host verified user main session", title="[A00] Main chat")
        self.assertEqual(self.registry.list(), [])

    def test_reserved_identity_case_variants_cannot_bypass_native_gate(self):
        before = self.snapshot()
        for fields in ({"platform":"Codex"}, {"host":"LOCAL"},
                       {"platform":"CoDeX", "host":"Local"}):
            for identity in (CHILD, UNKNOWN):
                with self.subTest(fields=fields, identity=identity), self.assertRaises(ValidationError):
                    dispatch_registry(self.store, **dict(self.payload(identity), **fields))
                self.assertEqual(self.snapshot(), before)

    def test_reserved_case_variants_resolve_to_one_native_identity(self):
        first = dispatch_registry(self.store, **dict(self.payload(MAIN), platform="CODEX", host="LOCAL"))
        repeated = dispatch_registry(self.store, **self.payload(MAIN))
        self.assertEqual((first["platform"], first["host"]), ("codex", "local"))
        self.assertEqual((first["code"], repeated["code"]), ("A00", "A00"))
        self.assertEqual(len(self.registry.list()), 1)

    def test_invalid_public_platform_or_host_is_rejected_before_registration(self):
        before = self.snapshot()
        for fields in ({"platform":None}, {"platform":["codex"]},
                       {"host":False}, {"host":"local/parent"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                dispatch_registry(self.store, **dict(self.payload(MAIN), **fields))
            self.assertEqual(self.snapshot(), before)

    def test_remote_and_other_platform_registration_do_not_read_local_host(self):
        remote = dispatch_registry(self.store, operation="ensure", platform="other-platform",
            thread_id="foreign-native", source="explicit external host evidence")
        other_host = dispatch_registry(self.store, operation="ensure", host="remote-host",
            thread_id="remote-native", source="explicit remote host evidence")
        self.assertEqual((remote["code"], other_host["code"]), ("A00", "A01"))


if __name__ == "__main__":
    unittest.main()
