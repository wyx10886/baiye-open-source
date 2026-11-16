"""Exercise the local workbench over actual HTTP, not just private handlers."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from local_memory.web import make_server
from local_memory.retention import Retention


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-web-test-")
        self.server = make_server(self.temp.name, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, body=None, *, token=True, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["X-Local-Memory-Token"] = self.server.token
        request_headers.update(headers or {})
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        client.request(method, path, body=payload, headers=request_headers)
        response = client.getresponse()
        raw = response.read()
        result = dict(status=response.status, headers=dict(response.getheaders()), raw=raw)
        if "application/json" in result["headers"].get("Content-Type", ""):
            result["body"] = json.loads(raw)
        client.close()
        return result

    def create(self, **changes):
        data = dict(kind="project", title="已核验成果", content="数据核验通过；下一步比较基线模型。", source="2026-11-10 任务验收", scope="project:study", status="confirmed", key="project.current")
        data.update(changes)
        response = self.request("POST", "/api/memories", data)
        self.assertEqual(response["status"], 200, response)
        return response["body"]["data"]

    def test_html_is_private_self_contained_and_xss_safe(self):
        result = self.request("GET", "/", token=False)
        self.assertEqual(result["status"], 200)
        html = result["raw"].decode("utf-8")
        self.assertIn("本地协作记忆", html)
        self.assertIn(self.server.token, html)
        self.assertNotIn("__LOCAL_TOKEN__", html)
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertIn("frame-ancestors 'none'", result["headers"]["Content-Security-Policy"])
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", result["headers"])
        self.assertEqual(self.request("GET", "/../../etc/passwd")["status"], 404)

    def test_blocks_cross_origin_dns_rebinding_and_missing_token(self):
        self.assertEqual(self.request("GET", "/api/stats", token=False)["status"], 403)
        self.assertEqual(self.request("POST", "/api/backup", {}, token=False)["status"], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "malicious.test"})["status"], 403)
        self.assertEqual(self.request("GET", "/api/stats", headers={"Origin": "https://malicious.test"})["status"], 403)
        self.assertEqual(self.request("GET", "/api/stats", headers={"Sec-Fetch-Site": "cross-site"})["status"], 403)
        self.assertEqual(self.request("OPTIONS", "/api/memories")["status"], 403)

    def test_create_search_revision_conflict_and_forget(self):
        old = self.create()
        query = self.request("GET", "/api/memories?scope=project%3Astudy")
        self.assertEqual([r["id"] for r in query["body"]["data"]], [old["id"]])
        other_project = self.request("GET", "/api/memories?scope=project%3Aother")
        self.assertEqual(other_project["body"]["data"], [])
        new_result = self.request("POST", f"/api/memories/{old['id']}/revise", {
            "expected_revision": old["revision"], "source": "用户更正", "content": "单位待核实；下一步确认数据定义。",
        })
        self.assertEqual(new_result["status"], 200, new_result)
        new = new_result["body"]["data"]
        self.assertEqual(new["revision"], old["revision"] + 1)
        stale = self.request("POST", f"/api/memories/{old['id']}/revise", {
            "expected_revision": old["revision"], "source": "过时聊天", "content": "不应覆盖最新核验",
        })
        self.assertEqual(stale["status"], 409, stale)
        forgotten = self.request("POST", f"/api/memories/{new['id']}/forget", {
            "expected_revision": new["revision"], "reason": "用户主动删除",
        })
        self.assertEqual(forgotten["status"], 200, forgotten)
        history = self.request("GET", "/api/memories?scope=project%3Astudy&history=1")
        encoded = json.dumps(history["body"], ensure_ascii=False)
        self.assertNotIn("基线模型", encoded)
        self.assertNotIn("确认数据定义", encoded)

    def test_candidates_stay_distinct_and_backup_export_work(self):
        self.create(kind="event", status="candidate", key="proposal", title="候选路线", content="考虑在线适应，尚未决定。")
        candidates = self.request("GET", "/api/memories?scope=project%3Astudy&status=candidate")
        self.assertEqual(candidates["body"]["data"][0]["status"], "candidate")
        context = self.request("GET", "/api/context?project=study")
        self.assertEqual(context["status"], 200, context)
        data = context["body"]["data"]
        self.assertFalse(any(r["title"] == "候选路线" for r in data["confirmed"]))
        self.assertTrue(any(r["title"] == "候选路线" for r in data["candidates"]))
        for endpoint in ("backup", "export"):
            result = self.request("POST", "/api/" + endpoint, {})
            self.assertEqual(result["status"], 200, result)
            self.assertTrue(Path(result["body"]["data"]["path"]).is_file())
            bad = self.request("POST", "/api/" + endpoint, {"target": "outside.txt"})
            self.assertEqual(bad["status"], 400)
        review = self.request("POST", "/api/review", {"note": "已复核候选与确认状态"})
        self.assertEqual(review["status"], 200, review)
        self.assertEqual(self.request("GET", "/api/stats")["status"], 200)

    def test_request_validation_and_bind_restriction(self):
        self.assertEqual(self.request("POST", "/api/memories", {"unsupported": True})["status"], 400)
        self.assertEqual(self.request("POST", "/api/review", ["not an object"])["status"], 400)
        self.assertEqual(self.request("POST", "/api/review", {"note": "x"}, headers={"Content-Type": "text/plain"})["status"], 400)
        with self.assertRaises(ValueError):
            make_server(self.temp.name, host="0.0.0.0")

    def test_project_summary_checkpoint_and_preview_over_http(self):
        project_temp = tempfile.TemporaryDirectory(prefix="local-http-project-")
        self.addCleanup(project_temp.cleanup)
        root = Path(project_temp.name)
        result = self.request("POST", "/api/project-checkpoint", dict(project="http-project", key="overview",
            data=dict(root=str(root), title="HTTP项目", goal="测试HTTP接续", current_state="接口已核验",
                      next_steps=["继续问题记录"], coverage="测试资料"), source="HTTP synthetic fixture"))
        self.assertEqual(result['status'], 200, result)
        projects = self.request("GET", "/api/projects")['body']['data']
        self.assertEqual(projects[0]['project'], 'http-project')
        preview = self.request("GET", "/api/project-summary?project=http-project")
        self.assertEqual(preview['status'], 200)
        self.assertIn('接口已核验', preview['body']['data']['markdown'])
        stale = self.request("POST", "/api/project-checkpoint", dict(project="http-project", key="overview",
            data=dict(root=str(root), title="HTTP项目", goal="测试HTTP接续", current_state="stale",
                      next_steps=[], coverage="测试资料"), source="fixture", expected_revision=0))
        self.assertEqual(stale['status'], 409)

    def test_cleanup_review_recycle_and_restore_over_http(self):
        workspace_temp = tempfile.TemporaryDirectory(prefix="local-cleanup-http-fixture-")
        self.addCleanup(workspace_temp.cleanup)
        workspace = Path(workspace_temp.name)
        records = workspace / "sample" / "处理记录"
        records.mkdir(parents=True)
        (records / "trace.log").write_text("synthetic trace")
        (records / "source.csv").write_text("original data")
        clock = lambda: datetime.now(timezone.utc) - timedelta(days=16)
        cleanup = Retention(self.temp.name, clock=clock)
        cleanup.configure([str(workspace)])
        cleanup.enroll(records)
        self.assertEqual(self.request("GET", "/api/cleanup", token=False)["status"], 403)
        scan = self.request("POST", "/api/cleanup/scan", {})
        self.assertEqual(scan["status"], 200, scan)
        target = scan["body"]["data"]["pending"][0]
        args = dict(target_id=target["id"], action="recycle", proposal_id=target["proposal"]["id"],
                    files=["trace.log"], reason="用途与依赖已核对，无需保留的匿名测试日志")
        self.assertEqual(self.request("POST", "/api/cleanup/decision", args)["status"], 400)
        blocked = self.request("POST", "/api/cleanup/decision", dict(args, confirmed=True, files=["source.csv"]))
        self.assertEqual(blocked["status"], 400)
        result = self.request("POST", "/api/cleanup/decision", dict(args, confirmed=True))
        self.assertEqual(result["status"], 200, result)
        job = result["body"]["data"]
        self.assertEqual(job["status"], "recycled")
        self.assertFalse((records / "trace.log").exists())
        self.assertTrue((records / "source.csv").exists())
        restored = self.request("POST", "/api/cleanup/restore", {"job_id": job["id"]})
        self.assertEqual(restored["status"], 200, restored)
        self.assertEqual((records / "trace.log").read_text(), "synthetic trace")


if __name__ == "__main__":
    unittest.main()
