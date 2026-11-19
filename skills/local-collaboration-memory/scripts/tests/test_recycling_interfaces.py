"""Number recycling through the shipped CLI, stdio MCP and private HTTP API."""
from __future__ import annotations

from contextlib import closing
import http.client
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from local_memory.cli import build_parser
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store
from local_memory.web import make_server

ROOT = Path(__file__).resolve().parents[1]


def native(index):
    return f"00000000-0000-4000-8000-{index:012d}"


class RecyclingInterfaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="local-recycling-interfaces-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / "private"
        self.host = self.base / "anonymous-host"
        self.workspace = self.base / "workspace"
        self.host.mkdir()
        self.workspace.mkdir()
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        with closing(sqlite3.connect(self.host / "state_5.sqlite")) as con, con:
            con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,name TEXT,source TEXT,thread_source TEXT,attached_thread_parent_id TEXT,archived INTEGER)")
            con.executemany("INSERT INTO threads VALUES(?,?,?,?,?,?)", [
                (native(1), "[A00] reserved fixture", "vscode", "user", None, 0),
                (native(3), "[A02] live fixture", "vscode", "user", None, 0),
                (native(4), "[A03] archived fixture", "vscode", "user", None, 1),
                (native(5), "new anonymous fixture", "vscode", "user", None, 0),
            ])
        # Legacy fixture allocation precedes the explicit opt-in. Native(2) is
        # deliberately absent from the anonymous host, simulating a deleted chat.
        for index in range(1, 5):
            self.registry.ensure(thread_id=native(index), source="anonymous historical fixture")
        (self.workspace / "A04-1_fixture").mkdir()
        (self.home / "settings.json").write_text(json.dumps({
            "host_title_sync": {"codex_home": str(self.host)},
            "session_recycling": {"enabled": True, "workspace_roots": [str(self.workspace)]},
        }), encoding="utf-8")
        self.registry = SessionRegistry(self.store)
        self.server = None

    def cli(self, payload, *, operation="recycle"):
        return subprocess.run(
            [sys.executable, "-B", "-m", "local_memory", "--home", str(self.home),
             "session", operation, "--input", "-"],
            input=json.dumps(payload, ensure_ascii=False), text=True, encoding="utf-8",
            capture_output=True, cwd=ROOT, timeout=20,
        )

    def mcp(self, calls):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "anonymous-recycling-interface-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ] + calls
        result = subprocess.run(
            [sys.executable, "-B", "-m", "local_memory", "--home", str(self.home), "mcp"],
            input="\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n",
            text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=25,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return {response["id"]: response for response in map(json.loads, result.stdout.splitlines())}

    @staticmethod
    def call(request_id, args):
        return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
            "name": "local_session_recycle", "arguments": args}}

    def unpack(self, response):
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def start_http(self):
        self.server = make_server(self.home, port=0)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            worker.join(timeout=3)
        self.addCleanup(close)

    def request(self, method, path, payload=None, *, token=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["X-Local-Memory-Token"] = self.server.token
        request_headers.update(headers or {})
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = {"status": response.status, "raw": response.read()}
        if response.getheader("Content-Type", "").startswith("application/json"):
            result["body"] = json.loads(result["raw"])
        connection.close()
        return result

    def identities(self):
        return [(row["code"], row["thread_id"], row["revision"]) for row in self.registry.list()]

    def test_cli_scan_and_history_do_not_release_or_renumber(self):
        before = self.identities()
        result = self.cli({"operation": "scan"})
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan["enabled"])
        self.assertTrue(plan["complete"], plan)
        self.assertEqual(plan["next_code"], "A01")
        self.assertEqual([row["code"] for row in plan["releasable"]], ["A01"])
        history = self.cli({"operation": "history", "code": "A01", "thread_id": native(2)})
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertEqual(json.loads(history.stdout), [])
        self.assertEqual(self.identities(), before)
        parsed = build_parser().parse_args(["session", "recycle", "--input", "-"])
        self.assertEqual(parsed.session_action, "recycle")

    def test_cli_confirmation_release_history_and_reallocation_roundtrip(self):
        plan = json.loads(self.cli({"operation": "scan"}).stdout)
        body = {"operation": "reclaim", "source": "anonymous user confirmed displayed fixture list",
                "expected_scan_id": plan["scan_id"]}
        self.assertEqual(self.cli(body).returncode, 2)
        self.assertEqual(len(self.identities()), 4)
        result = self.cli(dict(body, confirmed=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        released = json.loads(result.stdout)
        self.assertEqual([row["code"] for row in released["released"]], ["A01"])
        history = self.cli({"operation": "history", "code": "A01"})
        self.assertEqual(history.returncode, 0, history.stderr)
        previous = json.loads(history.stdout)
        self.assertEqual(previous[0]["thread_id"], native(2))
        self.assertTrue(previous[0]["events"])
        new = self.cli({"operation": "ensure", "thread_id": native(5),
                        "title": "new anonymous fixture", "source": "verified anonymous host fixture"}, operation="update")
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(json.loads(new.stdout)["code"], "A01")
        self.assertEqual(self.registry.show("A00")["thread_id"], native(1))
        self.assertEqual(self.registry.show("A02")["thread_id"], native(3))
        self.assertEqual(self.registry.show("A03")["thread_id"], native(4))
        self.assertEqual(self.registry.recycling_history(thread_id=native(2))[0]["code"], "A01")

    def test_cli_rejects_unrecognized_fields_and_nonliteral_confirmation(self):
        plan = json.loads(self.cli({"operation": "scan"}).stdout)
        reclaim = {"operation": "reclaim", "source": "anonymous explicit action",
                   "expected_scan_id": plan["scan_id"], "confirmed": True}
        cases = [
            {"operation": "scan", "confirmed": True},
            {"operation": "history", "source": "unsupported"},
            {"operation": "history", "thread_id": ""},
            {"operation": "reclaim"},
            dict(reclaim, confirmed=1), dict(reclaim, confirmed="true"),
            dict(reclaim, source=" "), dict(reclaim, expected_scan_id=17),
            dict(reclaim, code="A01"), {"operation": "unknown"}, {}, [],
        ]
        before = self.identities()
        for case in cases:
            with self.subTest(payload=case):
                result = self.cli(case)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.identities(), before)

    def test_incomplete_scan_disables_next_number_and_refuses_reclaim(self):
        before = self.identities()
        (self.host / "state_5.sqlite").rename(self.host / "unavailable.sqlite")
        scan = self.cli({"operation": "scan"})
        self.assertEqual(scan.returncode, 0, scan.stderr)
        plan = json.loads(scan.stdout)
        self.assertFalse(plan["complete"])
        self.assertIsNone(plan["next_code"])
        result = self.cli({"operation": "reclaim", "source": "anonymous explicit action",
                           "expected_scan_id": plan["scan_id"], "confirmed": True})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.identities(), before)

    def test_mcp_advertises_controls_and_uses_same_validation(self):
        results = self.mcp([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            self.call(3, {"operation": "scan"}),
            self.call(4, {"operation": "scan", "code": "A01"}),
            self.call(5, {"operation": "reclaim", "source": "fixture"}),
            self.call(6, {"operation": "history", "code": "A01"}),
        ])
        by_name = {tool["name"]: tool for tool in results[2]["result"]["tools"]}
        tool = by_name["local_session_recycle"]
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertTrue(tool["annotations"]["destructiveHint"])
        self.assertIn("expected_scan_id", tool["inputSchema"]["properties"])
        self.assertNotIn("never recycle codes", by_name["local_session_update"]["description"])
        plan = self.unpack(results[3])
        self.assertEqual(plan["next_code"], "A01")
        for request_id in (4, 5):
            self.assertTrue(results[request_id]["result"]["isError"])
        self.assertEqual(self.unpack(results[6]), [])
        saved = self.mcp([
            self.call(2, {"operation": "reclaim", "source": "anonymous user confirmed fixture list",
                          "expected_scan_id": plan["scan_id"], "confirmed": True}),
            self.call(3, {"operation": "history", "thread_id": native(2)}),
        ])
        self.assertEqual(self.unpack(saved[2])["released"][0]["code"], "A01")
        self.assertEqual(self.unpack(saved[3])[0]["thread_id"], native(2))

    def test_http_csrf_strict_fields_and_stale_scan(self):
        self.start_http()
        self.assertEqual(self.request("GET", "/api/session-recycling", token=False)["status"], 403)
        self.assertEqual(self.request("GET", "/api/session-recycling", headers={"Origin": "https://example.invalid"})["status"], 403)
        scan = self.request("GET", "/api/session-recycling")
        self.assertEqual(scan["status"], 200, scan)
        plan = scan["body"]["data"]
        self.assertEqual(plan["next_code"], "A01")
        body = {"source": "anonymous user confirmed displayed fixture list", "expected_scan_id": plan["scan_id"], "confirmed": True}
        self.assertEqual(self.request("POST", "/api/session-recycling/reclaim", body, token=False)["status"], 403)
        self.assertEqual(self.request("POST", "/api/session-recycling/reclaim", dict(body, confirmed=False))["status"], 400)
        self.assertEqual(self.request("POST", "/api/session-recycling/reclaim", dict(body, code="A01"))["status"], 400)
        self.assertEqual(self.request("GET", "/api/session-recycling?code=A01")["status"], 400)
        self.assertEqual(self.request("GET", "/api/session-recycling/history?code=A01&code=A02")["status"], 400)
        blocker = self.workspace / "A01-1_new-occupancy"
        blocker.mkdir()
        stale = self.request("POST", "/api/session-recycling/reclaim", body)
        self.assertEqual(stale["status"], 409, stale)
        self.assertEqual(len(self.identities()), 4)
        blocker.rmdir()
        current = self.request("GET", "/api/session-recycling")["body"]["data"]
        saved = self.request("POST", "/api/session-recycling/reclaim", dict(body, expected_scan_id=current["scan_id"]))
        self.assertEqual(saved["status"], 200, saved)
        self.assertEqual(saved["body"]["data"]["released"][0]["code"], "A01")
        history = self.request("GET", "/api/session-recycling/history?code=A01&thread_id=" + native(2))
        self.assertEqual(history["status"], 200, history)
        self.assertEqual(history["body"]["data"][0]["thread_id"], native(2))
        self.assertEqual(self.registry.show("A00")["thread_id"], native(1))

    def test_workbench_reclaim_gate_requires_complete_enabled_current_plan(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable for the workbench behavior check")
        html = (ROOT / "local_memory/static/index.html").read_text(encoding="utf-8")
        start = html.index("  function recyclingCanReclaim(")
        end = html.index("  function recyclingRender(", start)
        source = html[start:end]
        plan = {"enabled": True, "complete": True, "scan_id": "fixture-scan", "releasable": [{"code": "A01"}]}
        cases = [None, {}, dict(plan, enabled=False), dict(plan, complete=False),
                 dict(plan, scan_id=""), dict(plan, releasable=[]), plan]
        script = "const fs=require('fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));const f=new Function(x.source+';return arguments[0].map(recyclingCanReclaim);');process.stdout.write(JSON.stringify(f(x.cases)));"
        result = subprocess.run([node, "-e", script], input=json.dumps({"source": source, "cases": cases}),
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [False] * 6 + [True])

    def test_workbench_has_visible_safe_recycling_controls(self):
        self.start_http()
        response = self.request("GET", "/", token=False)
        self.assertEqual(response["status"], 200)
        html = response["raw"].decode("utf-8")
        for widget in ("session-recycling-heading", "session-recycling-scan", "session-recycling-reclaim",
                       "session-recycling-status", "session-recycling-results", "session-recycling-history-code"):
            self.assertIn('id="' + widget + '"', html)
        self.assertIn("不会删除会话或文件", html)
        self.assertIn("expected_scan_id:plan.scan_id", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn('<script src=', html)


if __name__ == "__main__":
    unittest.main()
