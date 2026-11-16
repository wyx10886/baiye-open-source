"""Stable codes: carry boundaries, native identity, provenance and real interfaces."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_memory.integration import handle_hook
from local_memory.mcp import MCPServer
from local_memory.retention import Retention
from local_memory.session_registry import SessionRegistry, MARKER_FILE
from local_memory.store import Store, ValidationError, ConflictError
from local_memory.web import make_server
from local_memory.work_summary import WorkSummary


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="local-session-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "private"
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        self.work = WorkSummary(self.store)

    def project(self, name):
        path = self.root / name
        path.mkdir()
        self.work.checkpoint(project=name, key="overview", data={"root": str(path), "title": name,
            "goal": "fixture", "current_state": "verified fixture", "next_steps": ["continue"],
            "coverage": "fixture only"}, source="actual fixture directory")
        return path

    def ensure(self, thread_id="native-1", **kwargs):
        return self.registry.ensure(thread_id=thread_id, source="actual fixture host identity", **kwargs)

    def bind(self, code, project):
        return self.registry.bind(code=code, project=project, expected_revision=self.registry.show(code)["revision"], source="fixture inspected messages and artifact directory")

    def test_decimal_and_letter_carry_boundaries_and_roundtrip(self):
        examples = {1:"A00", 2:"A01", 100:"A99", 101:"B00", 2600:"Z99", 2601:"AA00",
                    5200:"AZ99", 5201:"BA00", 70200:"ZZ99", 70201:"AAA00"}
        for seq, code in examples.items():
            self.assertEqual(self.registry.code(seq), code)
            self.assertEqual(self.registry._sequence(code), seq)
        for seq in range(1, 10001):
            self.assertEqual(self.registry._sequence(self.registry.code(seq)), seq)

    def test_lowercase_and_noncanonical_codes_are_rejected(self):
        for invalid in ("a00", "A0", "A000", "00A", "A-1", "AA100", "Ａ00", "A００", "[A00]"):
            with self.assertRaises(ValidationError):
                self.registry.show(invalid)

    def test_native_identity_is_idempotent_and_names_do_not_own_codes(self):
        first = self.ensure(title="任意标题")
        again = self.ensure(title="改成新名字")
        self.assertEqual(first["code"], again["code"])
        self.assertEqual(again["title"], "任意标题")
        self.assertEqual(self.ensure("native-2", title="任意标题")["code"], "A01")
        self.assertEqual(self.ensure(platform="other")["code"], "A02")
        self.assertEqual(self.ensure(host="another-host")["code"], "A03")

    def test_parallel_allocation_never_duplicates_and_duplicate_identity_is_one(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda i: self.ensure(f"id-{i}")["code"], range(20)))
        self.assertEqual(len(set(codes)), 20)
        with ThreadPoolExecutor(max_workers=8) as pool:
            same = list(pool.map(lambda _: self.ensure("shared-id")["code"], range(12)))
        self.assertEqual(len(set(same)), 1)
        self.assertEqual(len(self.registry.list()), 21)

    def test_title_plan_is_reversible_and_observation_requires_revision(self):
        row = self.ensure(title="原来的自由标题")
        plan = self.registry.title_plan(code=row["code"], title="[Z99] 改后的标题")
        self.assertEqual(plan["suggested_title"], "[A00] 改后的标题")
        self.assertFalse(plan["application_renamed"])
        self.assertEqual(self.registry.show("A00")["title"], "原来的自由标题")
        updated = self.registry.observe_title(code="A00", title="[A00] 新标题", source="host readback", expected_revision=1)
        self.assertFalse(updated["title_needs_sync"])
        with self.assertRaises(ConflictError):
            self.registry.observe_title(code="A00", title="stale", source="fixture", expected_revision=1)
        self.assertEqual(self.registry.prefixed_title("AA00", "[AA00] [A01] 自由文本"), "[AA00] 自由文本")

    def test_multiple_tasks_and_shared_directory_keep_stable_subcodes(self):
        self.project("alpha")
        self.project("beta")
        self.ensure()
        self.bind("A00", "alpha")
        self.bind("A00", "beta")
        self.ensure("native-2")
        self.bind("A01", "alpha")
        row = self.registry.show("A00")
        self.assertEqual([b["directory_code"] for b in row["bindings"]], ["A00-1", "A00-2"])
        self.assertEqual(len(self.registry.project_links("alpha")), 2)
        self.bind("A00", "alpha")
        self.assertEqual(len(self.registry.show("A00")["bindings"]), 2)
        summary = self.work.read("alpha")
        self.assertIn("A00-1", summary["markdown"])
        self.assertIn("A01-1", summary["markdown"])
        self.assertNotIn("A00-2", summary["markdown"])
        self.assertNotIn(str(self.root/"beta"), json.dumps(summary, ensure_ascii=False))

    def test_unverified_project_or_changed_revision_cannot_bind(self):
        self.project("alpha")
        self.ensure()
        with self.assertRaises(ValidationError):
            self.bind("A00", "invented")
        self.bind("A00", "alpha")
        with self.assertRaises(ConflictError):
            self.registry.bind(code="A00", project="alpha", source="fixture", expected_revision=1)

    def test_memory_rollback_does_not_recycle_already_allocated_codes(self):
        self.ensure()
        backup = self.store.backup()
        self.ensure("native-2")
        self.store.restore(backup)
        self.assertEqual(self.ensure("native-3")["code"], "A02")
        self.assertEqual(self.registry.show("A01")["thread_id"], "native-2")
        backup = self.registry.backup()
        self.assertTrue(Path(backup["registry_backup"]).is_file())

    def test_marker_manual_notes_and_no_change_mtime_are_preserved(self):
        path = self.project("alpha")
        self.ensure(title="标题")
        self.bind("A00", "alpha")
        marker = path/"处理记录"/MARKER_FILE
        with marker.open("a", encoding="utf-8") as f:
            f.write("\n人工核查内容仍要保留")
        self.registry.export()
        before = marker.stat().st_mtime_ns
        self.registry.export()
        self.assertEqual(before, marker.stat().st_mtime_ns)
        self.assertIn("人工核查内容仍要保留", marker.read_text(encoding="utf-8"))

    def test_manual_generated_edits_report_sync_failure_without_overwriting(self):
        path = self.project("alpha")
        self.ensure()
        self.bind("A00", "alpha")
        marker = path/"处理记录"/MARKER_FILE
        original = marker.read_text(encoding="utf-8").replace("会话与目录对应", "手工改动")
        marker.write_text(original, encoding="utf-8")
        result = self.registry.observe_title(code="A00", title="[A00] 新标题", source="host readback", expected_revision=2)
        self.assertFalse(result["sync"]["ok"])
        self.assertEqual(marker.read_text(encoding="utf-8"), original)
        self.assertEqual(self.registry.show("A00")["title"], "[A00] 新标题")

    def test_association_marker_is_permanently_protected(self):
        path = self.project("alpha")
        self.ensure()
        self.bind("A00", "alpha")
        clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        cleanup = Retention(self.home, clock=lambda: clock[0])
        cleanup.configure([str(self.root)])
        cleanup.scan()
        clock[0] += timedelta(days=16)
        status = cleanup.scan()
        marker = next(f for t in status["targets"] for f in t.get("proposal",{}).get("files",[]) if f["path"]==MARKER_FILE)
        self.assertTrue(marker["protected_reason"])

    def test_hook_registers_identity_without_guessing_project_from_title(self):
        self.project("alpha")
        identity = patch("local_memory.title_sync.native_identity", return_value={"status": "main"})
        identity.start()
        self.addCleanup(identity.stop)
        result = handle_hook(self.home, {"hook_event_name":"UserPromptSubmit", "session_id":"actual-session", "cwd":str(self.root), "prompt":"alpha"})
        row = self.registry.show(thread_id="actual-session")
        self.assertEqual(row["code"], "A00")
        self.assertEqual(row["bindings"], [])
        self.assertIn("固定编号：A00", result["hookSpecificOutput"]["additionalContext"])
        handle_hook(self.home, {"hook_event_name":"SessionStart", "session_id":"actual-session"})
        self.assertEqual(len(self.registry.list()), 1)
        handle_hook(self.home, {"hook_event_name":"SessionStart", "session_id":"subagent", "agent_id":"agent"})
        self.assertEqual(len(self.registry.list()), 1)

    def test_new_directory_plan_is_safe_and_does_not_create_anything(self):
        self.ensure()
        plan = self.registry.directory_plan(code="A00", parent=str(self.root), name="项目_新任务_2026-11-13")
        self.assertEqual(Path(plan["path"]).name, "A00-1_项目_新任务_2026-11-13")
        self.assertFalse(Path(plan["path"]).exists())
        for invalid in ("../escape", "CON", "name/child", "bad:name"):
            with self.assertRaises(ValidationError):
                self.registry.directory_plan(code="A00", parent=str(self.root), name=invalid)
        with self.assertRaises(ValidationError):
            self.registry.directory_plan(code="A00", parent=str(self.home), name="private")

    def test_cli_and_mcp_reach_same_registry(self):
        process = subprocess.run([sys.executable, "-X", "utf8", "-B", "entry.py", "--home", str(self.home), "session", "update", "--input", "-"],
            input=json.dumps({"operation":"ensure", "thread_id":"cli", "source":"fixture"}), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["code"], "A00")
        server = MCPServer(self.store)
        r = server.handle({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"local_session_lookup", "arguments":{"code":"A00"}}})
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["thread_id"], "cli")
        bad = server.handle({"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":{"name":"local_session_update", "arguments":{"operation":"ensure", "thread_id":"test", "source":"fixture", "code":"A00"}}})
        self.assertTrue(bad["result"]["isError"])

    def test_http_authentication_session_flow_and_unknown_fields(self):
        server = make_server(self.home, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None, token=True):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers={"Content-Type":"application/json"}
            if token:
                headers["X-Local-Memory-Token"]=server.token
            connection.request(method,path,None if body is None else json.dumps(body),headers)
            response=connection.getresponse()
            status=response.status
            data=json.loads(response.read())
            connection.close()
            return status,data
        try:
            self.assertEqual(request("GET","/api/sessions",token=False)[0],403)
            code,data=request("POST","/api/session-update",{"operation":"ensure","thread_id":"web","source":"fixture"})
            self.assertEqual(code,200)
            self.assertEqual(data["data"]["code"],"A00")
            status,data=request("GET","/api/sessions?q=A00")
            self.assertEqual(status,200)
            self.assertEqual(len(data["data"]),1)
            self.assertEqual(request("POST","/api/session-update",{"operation":"ensure","thread_id":"bad","source":"fixture","unexpected":True})[0],400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
