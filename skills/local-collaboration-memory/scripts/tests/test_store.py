"""Behavioral acceptance checks; all records and homes are synthetic and temporary."""

import concurrent.futures
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_memory.store import ConflictError, Store, ValidationError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="local-store-test-")
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, **changes):
        fields = dict(kind="event", title="测试进展", content="已完成示例数据核查。",
                      source="synthetic test fixture", event_date="2026-11-10")
        fields.update(changes)
        return self.store.remember(**fields)

    def ids(self, rows):
        return {r["id"] for r in rows}

    def test_chinese_retrieval_and_persistence(self):
        memory = self.add(title="传感器数据核查进展", content="传感器数据连续性已核查，下一步确定测量单位。",
                          scope="project:solar")
        other = Store(self.home)
        results = other.search("传感器 测量单位", scope="project:solar")
        self.assertIn(memory["id"], self.ids(results))
        self.assertEqual(other.get(memory["id"])["source"], "synthetic test fixture")

    def test_correction_hides_old_preference_and_preserves_history(self):
        old = self.add(kind="preference", key="display-name", title="称呼偏好", content="称呼为阿甲。")
        new = self.store.revise(old["id"], expected_revision=old["revision"],
                                source="synthetic explicit correction", content="称呼为阿乙，停止使用旧称呼。")
        current = self.store.list_memories()
        self.assertIn(new["id"], self.ids(current))
        self.assertNotIn(old["id"], self.ids(current))
        self.assertEqual(new["revision"], old["revision"] + 1)
        self.assertEqual(new["root_id"], old["root_id"])
        self.assertEqual(self.store.get(old["id"])["state"], "superseded")
        self.assertNotIn(old["id"], self.ids(self.store.search("称呼 阿甲")))
        self.assertIn(new["id"], self.ids(self.store.search("称呼 阿乙")))

    def test_candidate_does_not_become_confirmed_fact(self):
        confirmed = self.add(kind="project", scope="project:solar", title="当前阶段",
                             content="仍在比较研究路线，尚未定题。")
        proposed = self.add(scope="project:solar", status="candidate", title="候选方案",
                            content="考虑累计概率预测，尚未被用户选定。")
        self.assertNotIn(proposed["id"], self.ids(self.store.search("预测", scope="project:solar")))
        context = self.store.context("研究路线 预测", project="solar")
        self.assertIn(confirmed["id"], self.ids(context["confirmed"]))
        self.assertIn(proposed["id"], self.ids(context["candidates"]))
        self.assertNotIn(proposed["id"], self.ids(context["confirmed"]))

    def test_scope_and_fictional_persona_are_separate(self):
        global_pref = self.add(kind="preference", title="沟通偏好", content="偏好中文解释研究进展。")
        solar = self.add(kind="project", scope="project:solar", title="太阳能项目", content="研究进展等待单位核查。")
        other = self.add(kind="project", scope="project:finance", title="财务项目", content="研究进展等待凭证核查。")
        persona = self.add(kind="persona", title="虚构背景", content="角色研究进展：虚构的故事讲述者设定。")
        results = self.store.search("研究进展", scope="project:solar")
        self.assertIn(global_pref["id"], self.ids(results))
        self.assertIn(solar["id"], self.ids(results))
        self.assertNotIn(other["id"], self.ids(results))
        self.assertNotIn(persona["id"], self.ids(results))
        self.assertNotIn(solar["id"], self.ids(self.store.search("研究进展")))
        self.assertIn(persona["id"], self.ids(self.store.search("研究进展", include_persona=True)))

    def test_include_global_false_and_empty_query(self):
        global_record = self.add(title="全局", content="全局沟通偏好")
        project_record = self.add(scope="project:alpha", title="项目阶段", content="完成核查，下一步建模。")
        results = self.store.search("", scope="project:alpha", include_global=False)
        self.assertIn(project_record["id"], self.ids(results))
        self.assertNotIn(global_record["id"], self.ids(results))

    def test_expired_memory_is_not_current_context(self):
        old = self.add(title="过期临时决定", content="过期项目截止安排", event_date="1999-01-01", valid_until="2000-01-01")
        self.assertNotIn(old["id"], self.ids(self.store.search("项目截止")))
        self.assertIn(old["id"], self.ids(self.store.list_memories(include_inactive=True)))

    def test_idempotent_import(self):
        first = self.add(idempotency_key="fixture:line:1")
        second = self.add(idempotency_key="fixture:line:1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_duplicate_key_requires_explicit_revision(self):
        self.add(key="phase", scope="project:alpha", kind="project", content="阶段一")
        with self.assertRaises(ConflictError):
            self.add(key="phase", scope="project:alpha", kind="project", content="阶段二")
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_keys_are_scoped_per_project(self):
        a = self.add(key="phase", scope="project:alpha", kind="project", content="甲项目阶段")
        b = self.add(key="phase", scope="project:beta", kind="project", content="乙项目阶段")
        self.assertNotEqual(a["id"], b["id"])

    def test_concurrent_writes_do_not_drop_records(self):
        def write(index):
            return self.add(title=f"并发事件{index}", content=f"第{index}项并发写入成果", key=f"event-{index}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(write, range(24)))
        self.assertEqual(len(self.ids(rows)), 24)
        self.assertEqual(len(self.store.list_memories(limit=100)), 24)
        with closing(sqlite3.connect(self.home / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_simultaneous_correction_has_one_winner(self):
        old = self.add(kind="project", key="phase", scope="project:alpha")
        def correct(index):
            try:
                return self.store.revise(old["id"], expected_revision=old["revision"],
                                         source=f"synthetic correction {index}", content=f"新阶段{index}")
            except ConflictError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(correct, (1, 2)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len(self.store.list_memories(scope="project:alpha")), 1)

    def test_forget_erases_text_across_revision_chain_and_blocks_reimport(self):
        old = self.add(key="private-fixture", content="测试隐私代号原值ABCDEFG", idempotency_key="private-import")
        new = self.store.revise(old["id"], expected_revision=old["revision"],
                                source="fixture corrected", content="测试隐私代号新值HIJKLMN")
        self.store.forget(new["id"], expected_revision=new["revision"])
        self.assertEqual(self.store.list_memories(), [])
        serialized = json.dumps(self.store.list_memories(include_inactive=True), ensure_ascii=False)
        self.assertNotIn("ABCDEFG", serialized)
        self.assertNotIn("HIJKLMN", serialized)
        self.assertNotIn("ABCDEFG", json.dumps(self.store.audit(), ensure_ascii=False))
        try:
            self.add(key="private-fixture", content="测试隐私代号原值ABCDEFG", idempotency_key="private-import")
        except ConflictError:
            pass
        self.assertEqual(self.store.list_memories(), [])

    def test_stale_revision_cannot_delete_newer_version(self):
        old = self.add(key="preference")
        self.store.revise(old["id"], expected_revision=old["revision"], source="fixture correction", content="新偏好")
        with self.assertRaises(ConflictError):
            self.store.forget(old["id"], expected_revision=old["revision"])
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_backup_restore_uses_database_snapshot(self):
        kept = self.add(content="备份前已确认的成果")
        snapshot = self.store.backup()
        later = self.add(content="备份后新增，恢复后不应存在")
        self.assertTrue(Path(snapshot).is_file())
        self.store.restore(snapshot)
        current = self.ids(self.store.list_memories())
        self.assertIn(kept["id"], current)
        self.assertNotIn(later["id"], current)
        with closing(sqlite3.connect(self.home / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_markdown_export_and_context_budget(self):
        for i in range(12):
            self.add(title=f"研究成果{i}", content="来源明确的阶段成果与下一步。" * 80)
        path = Path(self.store.export_markdown())
        self.assertTrue(path.is_file())
        self.assertIn("研究成果", path.read_text(encoding="utf-8"))
        context = self.store.context("研究成果", budget=1200)
        self.assertLessEqual(len(context["markdown"]), 1200)

    def test_metadata_validation_and_required_evidence(self):
        for changes in ({"source": ""}, {"content": ""}, {"kind": "bogus"},
                        {"status": "bogus"}, {"event_date": "2026-13-50"},
                        {"importance": 99}, {"scope": "../outside"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.add(**changes)
        self.assertEqual(self.store.list_memories(), [])

    def test_secrets_are_rejected(self):
        for secret in ("sk-proj-" + "A" * 48,
                       "-----BEGIN " + "PRIVATE KEY-----\nFAKE-TEST-ONLY\n-----END PRIVATE KEY-----"):
            with self.subTest(secret_type=secret[:10]), self.assertRaises(ValidationError):
                self.add(content=secret)
        self.assertEqual(self.store.list_memories(), [])

    def test_capture_turn_is_minimal_candidate_and_idempotent(self):
        payload = {"type": "agent-turn-complete", "thread-id": "test-thread-1", "turn-id": "test-turn-1",
                   "cwd": str(self.home / "fixture-project"),
                   "input-messages": ["INPUT-TRANSCRIPT-MUST-NOT-BE-STORED"],
                   "last-assistant-message": "已完成示例数据连续性核查，单位定义仍需用户确认，下一步准备复核字段说明。"}
        self.store.capture_turn(payload)
        self.store.capture_turn(payload)
        rows = self.store.list_memories()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "candidate")
        self.assertNotIn("INPUT-TRANSCRIPT-MUST-NOT-BE-STORED", json.dumps(rows))
        self.assertEqual(self.store.search("连续性核查"), [])
        self.store.capture_turn({"type": "irrelevant-event", "last-assistant-message": "ignored"})
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_review_records_actual_day(self):
        self.store.review("已核对阶段成果和待续事项。")
        stats = json.dumps(self.store.stats(), ensure_ascii=False)
        self.assertIn(dt.date.today().isoformat(), stats)


if __name__ == "__main__":
    unittest.main()
