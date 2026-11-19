"""Persistent handoffs: provenance, isolation, safe regeneration and real interfaces."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_memory.integration import handle_hook, notify, resolve_project
from local_memory.retention import Retention
from local_memory.store import ConflictError, Store, ValidationError
from local_memory.work_summary import WorkSummary, SUMMARY_FILES, NOTES


class WorkSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-project-summary-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / "sample-project"
        self.root.mkdir()
        (self.root / "result.txt").write_text("verified fixture", encoding="utf-8")
        self.store = Store(self.workspace / "private")
        self.summaries = WorkSummary(self.store)
        self.profile = dict(root=str(self.root), title="示例项目", goal="核验项目接续",
                            current_state="已检查原始资料；尚未训练。", next_steps=["确认单位后再实验"],
                            constraints=["不能把模拟数据称为实测"], coverage="只覆盖已核对的测试资料。",
                            artifacts=[{"path": "result.txt", "description": "核验成果"}])
        self.summaries.checkpoint(project="sample", key="overview", data=self.profile,
                                 source="fixture project identity verified", event_date="2026-01-02")

    def checkpoint(self, **changes):
        args = dict(project="sample", key="units", data=dict(title="计量单位待确认", category="problem",
                    state="open", problem="原始单位定义不明", next_step="问数据提供者"),
                    source="fixture evidence", event_date="2026-01-03")
        args.update(changes)
        return self.summaries.checkpoint(**args)

    def files(self):
        return [self.root / "处理记录" / name for name in SUMMARY_FILES]

    def test_create_resolve_revision_and_chronological_history(self):
        old = self.checkpoint()
        self.assertIn("计量单位待确认", self.files()[0].read_text(encoding="utf-8"))
        self.checkpoint(expected_revision=old["revision"], data=dict(title="单位已核实", category="problem",
            state="resolved", problem="原始单位定义不明", solution="核对仪表字段说明，确认为区间电量。",
            result="逐项与仪表文档一致", next_step="按已确认定义开展实验"))
        summary = self.summaries.read("sample")
        self.assertNotIn("计量单位待确认", summary["markdown"])
        self.assertIn("逐项与仪表文档一致", summary["history_markdown"])
        self.assertIn("修订：2", summary["history_markdown"])
        self.assertIn("过往修订", summary["history_markdown"])
        self.assertIn("计量单位待确认", summary["history_markdown"])
        self.assertLess(summary["history_markdown"].index("单位已核实"), summary["history_markdown"].index("## 候选"))
        self.assertEqual(self.store.get(old["memory_id"])["state"], "superseded")

    def test_candidates_and_persona_do_not_become_project_facts(self):
        self.checkpoint(status="candidate")
        self.store.remember(kind="persona", title="private persona", content="fictional narrator background",
                            source="fixture", scope="project:sample")
        self.store.remember(kind="project", title="other", content="other project secret material",
                            source="fixture", scope="project:another")
        self.store.remember(kind="preference", title="global", content="global private material", source="fixture")
        result = self.summaries.read("sample")
        self.assertNotIn("计量单位待确认", result["markdown"])
        self.assertIn("证据级别：candidate", result["history_markdown"])
        combined = result["markdown"] + result["history_markdown"]
        for excluded in ("narrator background", "other project secret", "global private"):
            self.assertNotIn(excluded, combined)

    def test_native_records_update_and_forget_regenerates_files(self):
        row = self.store.remember(kind="lesson", title="核查经验", content="particular verified lesson",
                                  source="fixture", scope="project:sample")
        self.assertIn(row["content"], self.files()[1].read_text(encoding="utf-8"))
        self.store.forget(row["id"], expected_revision=1)
        self.assertNotIn(row["content"], self.files()[1].read_text(encoding="utf-8"))

    def test_manual_notes_survive_and_no_change_does_not_touch_files(self):
        path = self.files()[0]
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n2026-01-04 其他平台核验：保留此条人工补充。")
        self.checkpoint()
        before = {p: p.stat().st_mtime_ns for p in self.files()}
        self.summaries.export("sample")
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.files()})
        self.assertIn("保留此条人工补充", path.read_text(encoding="utf-8"))

    def test_manual_edits_block_both_exports_but_checkpoint_is_saved(self):
        first, history = self.files()
        original = first.read_bytes()
        history.write_text(history.read_text(encoding="utf-8").replace("历史覆盖范围", "manual modification"), encoding="utf-8")
        result = self.checkpoint()
        self.assertTrue(any(not r["ok"] for r in result["sync"]))
        self.assertEqual(self.store.get(result["memory_id"])["state"], "active")
        self.assertEqual(first.read_bytes(), original)
        self.assertIn("manual modification", history.read_text(encoding="utf-8"))
        with self.assertRaises(ConflictError):
            self.summaries.export("sample")

    def test_preexisting_unowned_file_is_not_replaced(self):
        path = self.files()[0]
        path.write_text("user's original summary", encoding="utf-8")
        self.checkpoint()
        self.assertEqual(path.read_text(encoding="utf-8"), "user's original summary")

    def test_stale_and_parallel_edits_cannot_overwrite(self):
        self.checkpoint()
        def writer(index):
            try:
                return self.checkpoint(expected_revision=1, data=dict(title=f"修订{index}", category="problem",
                                        state="open", problem="待核实定义"))["revision"]
            except ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, range(2)))
        self.assertCountEqual(results, [2, "conflict"])
        with self.assertRaises(ConflictError):
            self.checkpoint(expected_revision=1)

    def test_validation_blocks_unverified_identity_and_path_escape(self):
        for path in ("../outside.txt", "/outside.txt", "C:/outside.txt", ".local/database", "dir\\file"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self.checkpoint(data=dict(title="bad", category="problem", state="open", problem="fixture",
                                         artifacts=[{"path": path, "description": "invalid"}]))
        with self.assertRaises(ValidationError):
            self.checkpoint(data=dict(title="bad", category="problem", state="resolved", problem="fixture"))
        with self.assertRaises(ValidationError):
            self.summaries.checkpoint(project="sample", key="overview", data=self.profile,
                                      expected_revision=1, status="candidate", source="fixture")
        with self.assertRaises(ConflictError):
            self.summaries.checkpoint(project="other", key="overview", data=self.profile, source="fixture")
        with self.assertRaises(ValidationError):
            self.summaries.checkpoint(project="global", key="overview", data=self.profile, source="fixture")
        with self.assertRaises(ValidationError):
            self.checkpoint(source="password=" + "a" * 12)

    def test_missing_root_never_creates_new_project_directory(self):
        moved = self.root.with_name("moved")
        self.root.rename(moved)
        with self.assertRaises(ValidationError):
            self.summaries.export("sample")
        self.assertFalse(self.root.exists())

    def test_artifact_links_portable_and_missing_paths_explicit(self):
        self.assertIn("(<../result.txt>)", self.summaries.read("sample")["markdown"])
        (self.root / "result.txt").unlink()
        self.summaries.export("sample")
        self.assertIn("当前路径缺失", self.files()[0].read_text(encoding="utf-8"))

    def test_backup_restore_covers_structured_history(self):
        self.checkpoint()
        snapshot = self.store.backup()
        self.checkpoint(expected_revision=1, data=dict(title="temporary later change", category="problem",
                        state="open", problem="fixture"))
        self.store.restore(snapshot)
        self.assertIn("计量单位待确认", self.files()[0].read_text(encoding="utf-8"))
        self.assertNotIn("temporary later change", self.files()[1].read_text(encoding="utf-8"))

    def test_expired_native_record_excluded(self):
        self.store.remember(kind="project", title="expired", content="outdated state must not be used",
                            source="fixture", scope="project:sample", valid_until="2000-01-01")
        self.assertNotIn("outdated state", self.files()[1].read_text(encoding="utf-8"))

    def test_summary_long_term_protection_and_scan_not_usage(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cleanup = Retention(self.store.home, clock=lambda: now)
        cleanup.configure([str(self.workspace)])
        first = cleanup.scan()["targets"][0]
        now += timedelta(days=15)
        target = cleanup.scan()["pending"][0]
        self.assertEqual(first["last_used"], target["last_used"])
        self.assertTrue(all(f["protected_reason"] for f in target["proposal"]["files"]))
        with self.assertRaises(ValidationError):
            cleanup.decision(target["id"], "recycle", proposal_id=target["proposal"]["id"],
                             files=[SUMMARY_FILES[0]], reason="fixture", confirmed=True)

    def test_path_binding_hooks_and_automatic_capture_stays_candidate(self):
        self.assertEqual(resolve_project(self.store.home, str(self.root)), "sample")
        hook = handle_hook(self.store.home, {"hook_event_name": "SessionStart", "cwd": str(self.root)})
        self.assertIn("项目工作总结.md", hook["hookSpecificOutput"]["additionalContext"])
        notify(self.store.home, {"type": "agent-turn-complete", "cwd": str(self.root),
               "last-assistant-message": "已完成新模型训练与核验，此摘要仅作自动捕获测试，不是真实实验结果。"})
        result = self.summaries.read("sample")
        self.assertEqual(result["unreviewed_count"], 1)
        self.assertNotIn("已完成新模型训练", result["markdown"] + result["history_markdown"])

    def test_real_cli_and_mcp_read_write(self):
        root = Path(__file__).resolve().parents[1]
        cli = subprocess.run([sys.executable, "-B", "entry.py", "--home", str(self.store.home),
                              "project", "show", "sample", "--markdown"], cwd=root,
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertIn("从这里继续", cli.stdout)
        messages = [dict(jsonrpc="2.0", id=1, method="tools/list"),
            dict(jsonrpc="2.0", id=2, method="tools/call", params=dict(name="local_project_checkpoint", arguments=dict(
                project="sample", key="protocol", data=dict(title="协议验证", category="milestone", state="resolved",
                problem="跨平台如何调用", solution="通过标准MCP工具调用", result="真实stdio写入并导出成功"), source="fixture stdio protocol"))),
            dict(jsonrpc="2.0", id=3, method="tools/call", params=dict(name="local_project_summary", arguments=dict(project="sample")))]
        proc = subprocess.run([sys.executable, "-B", "entry.py", "--home", str(self.store.home), "mcp"], cwd=root,
                              input="\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n",
                              capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertIn("local_project_checkpoint", [t["name"] for t in responses[0]["result"]["tools"]])
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertIn("真实stdio写入", json.loads(responses[2]["result"]["content"][0]["text"])["history_markdown"])


if __name__ == "__main__":
    unittest.main()
