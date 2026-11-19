"""Habit controls exercised through the shipped CLI, stdio MCP, and private HTTP API."""
from __future__ import annotations

import http.client
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_memory.cli import build_parser
from local_memory.habits import HabitLearning
from local_memory.store import Store
from local_memory.web import make_server

ROOT = Path(__file__).resolve().parents[1]


class HabitInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-habit-interfaces-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "private"
        self.learning = HabitLearning(self.home)
        self.server = None

    def cli(self, *args, payload=None):
        return subprocess.run(
            [sys.executable, "-B", "-m", "local_memory", "--home", str(self.home), *args],
            input=None if payload is None else json.dumps(payload, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=20,
        )

    def enabled(self):
        state = self.learning.status()
        return self.learning.configure(enabled=True, use_learned=True, source="synthetic user enabled learning and reuse", expected_revision=state["policy"]["revision"])

    def seed(self, suffix="one"):
        self.enabled()
        prompt = "我喜欢先给简短结论，再解释原因。"
        receipt = self.learning.capture_prompt(prompt, thread_id="synthetic-habit-chat-" + suffix,
                                               turn_id="synthetic-turn-" + suffix, project="fixture")
        self.assertTrue(receipt["receipt_id"], receipt)
        args = dict(receipt_id=receipt["receipt_id"], topic="reply.summary-first", value="先给简短结论，再解释原因", quote=prompt)
        habit = self.learning.observe(**args)
        return habit, args

    def mcp(self, calls):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "anonymous-habit-interface-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ] + calls
        raw = "\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n"
        result = subprocess.run([sys.executable, "-B", "-m", "local_memory", "--home", str(self.home), "mcp"],
                                input=raw, text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {r["id"]: r for r in map(json.loads, result.stdout.splitlines())}

    @staticmethod
    def call(request_id, name, args):
        return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": args}}

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
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = {"status": response.status, "raw": response.read()}
        if response.getheader("Content-Type", "").startswith("application/json"):
            result["body"] = json.loads(result["raw"])
        connection.close()
        return result

    def test_cli_status_configure_and_strict_argument_validation(self):
        status = self.cli("habits", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        policy = json.loads(status.stdout)["policy"]
        self.assertFalse(policy["enabled"])
        update = self.cli("habits", "configure", "--input", "-", payload={
            "enabled": True, "source": "synthetic explicit user action", "expected_revision": policy["revision"]})
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertTrue(json.loads(update.stdout)["policy"]["enabled"])
        malformed = self.cli("habits", "configure", "--input", "-", payload={
            "enabled": "yes", "source": "fixture", "expected_revision": json.loads(update.stdout)["policy"]["revision"]})
        self.assertEqual(malformed.returncode, 2)
        no_revision = self.cli("habits", "configure", "--input", "-", payload={"enabled": False, "source": "fixture"})
        self.assertEqual(no_revision.returncode, 2)
        unknown = self.cli("habits", "list", "--limit", "0")
        self.assertEqual(unknown.returncode, 2)
        parser = build_parser()
        parsed = parser.parse_args(["context", "next", "--thread-id", "synthetic-chat"])
        self.assertEqual(parsed.thread_id, "synthetic-chat")

    def test_cli_roundtrip_inspect_confirmation_and_stale_review(self):
        habit, _ = self.seed()
        shown = self.cli("habits", "show", habit["id"])
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["id"], habit["id"])
        listed = self.cli("habits", "list", "--scope", "project:fixture")
        self.assertIn(habit["id"], listed.stdout)
        payload = dict(habit_id=habit["id"], action="confirm", source="synthetic user confirmed this displayed habit",
                       expected_revision=habit["revision"])
        refused = self.cli("habits", "review", "--input", "-", payload=payload)
        self.assertEqual(refused.returncode, 2)
        saved = self.cli("habits", "review", "--input", "-", payload=dict(payload, confirmed=True))
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertTrue(Store(self.home).list_memories(kind="preference"))
        stale = self.cli("habits", "review", "--input", "-", payload=dict(payload, action="forget", confirmed=True))
        self.assertEqual(stale.returncode, 3, stale.stderr)

    def test_mcp_advertises_learning_tools_and_validates_operation_fields(self):
        results = self.mcp([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            self.call(3, "local_habits", {"operation": "status"}),
            self.call(4, "local_habits", {"operation": "show"}),
            self.call(5, "local_habits", {"operation": "status", "scope": "global"}),
            self.call(6, "local_habit_manage", {"operation": "clear", "source": "fixture", "expected_revision": 0}),
        ])
        by_name = {tool["name"]: tool for tool in results[2]["result"]["tools"]}
        self.assertTrue({"local_habits", "local_habit_observe", "local_habit_manage"} <= set(by_name))
        self.assertTrue(by_name["local_habits"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["local_habit_manage"]["annotations"]["destructiveHint"])
        self.assertIn("thread_id", by_name["local_context"]["inputSchema"]["properties"])
        self.assertFalse(self.unpack(results[3])["policy"]["enabled"])
        for request_id in (4, 5, 6):
            self.assertTrue(results[request_id]["result"]["isError"])

    def test_mcp_observation_must_use_an_actual_receipt_quote(self):
        habit, observation = self.seed()
        results = self.mcp([
            self.call(2, "local_habit_observe", dict(observation, quote="invented user statement")),
            self.call(3, "local_habit_observe", dict(observation, source="invented provenance")),
            self.call(4, "local_habit_observe", observation),
            self.call(5, "local_habits", {"operation": "show", "habit_id": habit["id"]}),
        ])
        self.assertTrue(results[2]["result"]["isError"])
        self.assertTrue(results[3]["result"]["isError"])
        self.assertEqual(self.unpack(results[4])["id"], habit["id"])
        shown = self.unpack(results[5])
        self.assertNotEqual(shown["state"], "confirmed")
        self.assertEqual(shown["evidence_count"], 1)

    def test_http_controls_use_existing_csrf_and_revisions(self):
        self.start_http()
        self.assertEqual(self.request("GET", "/api/habits/status", token=False)["status"], 403)
        self.assertEqual(self.request("GET", "/api/habits", headers={"Origin": "https://example.invalid"})["status"], 403)
        status = self.request("GET", "/api/habits/status")
        self.assertEqual(status["status"], 200, status)
        revision = status["body"]["data"]["policy"]["revision"]
        body = {"enabled": True, "expected_revision": revision, "source": "synthetic user toggled learning"}
        self.assertEqual(self.request("POST", "/api/habits/configure", body, token=False)["status"], 403)
        updated = self.request("POST", "/api/habits/configure", body)
        self.assertEqual(updated["status"], 200, updated)
        self.assertEqual(self.request("POST", "/api/habits/configure", body)["status"], 409)
        self.assertEqual(self.request("POST", "/api/habits/configure", dict(body, capture_prompt="spoof"))["status"], 400)
        self.assertEqual(self.request("POST", "/api/habits/observe", {})["status"], 400)
        self.assertEqual(self.request("GET", "/api/habits?limit=bad")["status"], 400)

    def test_http_chat_policy_can_pause_without_changing_host_defaults(self):
        self.enabled()
        self.start_http()
        chat = "synthetic-private-habit-chat"
        before = self.request("GET", "/api/habits/status?thread_id=" + chat)["body"]["data"]
        result = self.request("POST", "/api/habits/configure", {
            "thread_id": chat, "enabled": False, "use_learned": False,
            "expected_revision": before["policy"]["revision"],
            "source": "synthetic user paused this chat only",
        })
        self.assertEqual(result["status"], 200, result)
        current = self.request("GET", "/api/habits/status?thread_id=" + chat)["body"]["data"]["policy"]
        host = self.request("GET", "/api/habits/status")["body"]["data"]["policy"]
        self.assertFalse(current["enabled"])
        self.assertFalse(current["use_learned"])
        self.assertTrue(host["enabled"])
        self.assertTrue(host["use_learned"])

    def test_http_review_edits_and_forgets_without_implicit_confirmation(self):
        habit, _ = self.seed()
        self.start_http()
        listing = self.request("GET", "/api/habits?scope=project%3Afixture")
        self.assertEqual(listing["status"], 200, listing)
        self.assertTrue(any(row["id"] == habit["id"] for row in listing["body"]["data"]))
        shown = self.request("GET", "/api/habits/show?habit_id=" + habit["id"])
        self.assertEqual(shown["status"], 200, shown)
        self.assertTrue(shown["body"]["data"]["evidence"])
        args = {"habit_id": habit["id"], "action": "edit", "value": "先说明结论，再给两个例子", "source": "synthetic user corrected this habit", "expected_revision": habit["revision"]}
        self.assertEqual(self.request("POST", "/api/habits/review", args)["status"], 400)
        edited = self.request("POST", "/api/habits/review", dict(args, confirmed=True))
        self.assertEqual(edited["status"], 200, edited)
        revision = edited["body"]["data"]["revision"]
        self.assertGreater(revision, habit["revision"])
        self.assertNotEqual(edited["body"]["data"]["state"], "confirmed")
        forgot = self.request("POST", "/api/habits/review", {"habit_id": habit["id"], "action": "forget", "source": "synthetic user chose forget", "expected_revision": revision, "confirmed": True})
        self.assertEqual(forgot["status"], 200, forgot)
        history = self.request("GET", "/api/habits?history=1")
        self.assertNotIn("再给两个例子", json.dumps(history["body"], ensure_ascii=False))
        self.assertNotIn("简短结论", json.dumps(history["body"], ensure_ascii=False))

    def test_http_clear_requires_confirmation_and_preserves_unrelated_memories(self):
        self.seed()
        independent = Store(self.home).remember(kind="preference", title="独立约定", content="保留独立资料", source="synthetic explicit user request")
        self.start_http()
        revision = self.request("GET", "/api/habits/status")["body"]["data"]["policy"]["revision"]
        body = {"source": "synthetic user explicitly chose clear", "expected_revision": revision}
        self.assertEqual(self.request("POST", "/api/habits/clear", body)["status"], 400)
        cleared = self.request("POST", "/api/habits/clear", dict(body, confirmed=True))
        self.assertEqual(cleared["status"], 200, cleared)
        self.assertEqual(Store(self.home).get(independent["id"])["content"], "保留独立资料")
        self.assertEqual(self.request("GET", "/api/habits")["body"]["data"], [])

    def test_http_memory_forget_reports_partial_and_retries_same_deleted_revision(self):
        habit, _ = self.seed()
        confirmed = self.learning.review(habit["id"], action="confirm", expected_revision=habit["revision"],
                                         source="synthetic user explicitly confirmed this habit")
        memory = Store(self.home).get(confirmed["memory_id"])
        self.start_http()
        body = {"expected_revision": memory["revision"], "reason": "synthetic user chose forget"}
        endpoint = "/api/memories/" + memory["id"] + "/forget"
        with patch.object(HabitLearning, "forget_linked", side_effect=OSError("PRIVATE_ERROR_DETAIL")):
            partial = self.request("POST", endpoint, body)
        self.assertEqual(partial["status"], 200, partial)
        result = partial["body"]["data"]
        self.assertEqual(result["state"], "deleted")
        self.assertEqual(result["content"], "")
        self.assertFalse(result["habit_cleanup"]["ok"])
        self.assertTrue(result["habit_cleanup"]["retryable"])
        self.assertNotIn("PRIVATE_ERROR_DETAIL", json.dumps(result))
        self.assertEqual(result["habit_cleanup"]["errors"][0]["error_type"], "OSError")
        retried = self.request("POST", endpoint, body)
        self.assertEqual(retried["status"], 200, retried)
        final = retried["body"]["data"]
        self.assertEqual(final["state"], "deleted")
        self.assertNotIn("habit_cleanup", final)
        cleared = self.learning.get(habit["id"])
        self.assertEqual(cleared["state"], "forgotten")
        self.assertEqual(cleared["evidence"], [])

    def test_ui_partial_forget_state_keeps_retry_identity_without_error_details(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable for the workbench function check")
        html = (ROOT / "local_memory" / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index("  function forgetCleanupState(")
        end = html.index("  function renderForgetCleanups(", start)
        source = html[start:end]
        record = {"id": "synthetic-memory", "revision": 4}
        pending = dict(record, state="deleted", habit_cleanup={"ok": False, "retryable": True, "errors": [
            {"error_type": "OSError", "message": "PRIVATE_ERROR_DETAIL"},
            {"error_type": "bad<script>"},
        ]})
        script = "const fs=require('fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));const f=new Function(x.source+'; return [forgetCleanupState(arguments[0],arguments[1]),forgetCleanupState(arguments[0],arguments[2])];');process.stdout.write(JSON.stringify(f(x.record,x.pending,x.complete)));"
        process = subprocess.run([node, "-e", script], input=json.dumps({"source": source, "record": record,
                                 "pending": pending, "complete": dict(record, state="deleted")}),
                                 capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        failure, success = json.loads(process.stdout)
        self.assertTrue(failure["pending"])
        self.assertEqual((failure["id"], failure["revision"]), (record["id"], record["revision"]))
        self.assertEqual(failure["error_types"], ["OSError"])
        self.assertNotIn("PRIVATE_ERROR_DETAIL", process.stdout)
        self.assertFalse(success["pending"])
        self.assertEqual(success["error_types"], [])
        self.assertIn('id="forget-cleanup-notice"', html)
        self.assertIn("重试清理学习证据", html)
        self.assertIn("noteForgetResult(record,result)", html)

    def test_workbench_has_visible_safe_habit_controls(self):
        self.start_http()
        result = self.request("GET", "/", token=False)
        self.assertEqual(result["status"], 200)
        html = result["raw"].decode("utf-8")
        for widget in ("habits-button", "habits-dialog", "habits-enabled", "habits-use", "habits-tools", "habits-results", "habits-clear"):
            self.assertIn('id="' + widget + '"', html)
        self.assertIn("确认为我的偏好", html)
        self.assertIn("查看依据", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn('<script src=', html)


if __name__ == "__main__":
    unittest.main()
