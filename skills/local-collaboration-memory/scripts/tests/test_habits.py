"""Behavioural checks for habit learning, provenance, scope, deletion and races."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_memory.habits import HabitLearning, _classify, _excerpts
from local_memory.store import ConflictError, Store, ValidationError


class HabitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="local-habit-test-")
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/"private"
        self.habits=HabitLearning(self.home)

    def enable(self):
        return self.habits.configure(enabled=True,use_learned=True,source="synthetic explicit learning and reuse authorization",expected_revision=0)

    def capture(self,text="我喜欢简洁回答，不需要每次都重述问题。",thread="chat-a",turn="turn-a",project=None):
        # The baseline deliberately abstains on negation, semantic observe handles it.
        return self.habits.capture_prompt(text,thread_id=thread,turn_id=turn,project=project)

    def seed(self,thread="chat-a",turn="turn-a",project=None,text="我喜欢简洁回答。"):
        result=self.capture(text,thread,turn,project)
        return result["observed"][0]

    def learn(self,project=None):
        self.seed(project=project)
        self.seed(thread="chat-b",turn="turn-b",project=project,text="以后回复简短一点。")
        self.seed(thread="chat-b",turn="turn-c",project=project,text="我一般喜欢简洁回答。")
        return next(x for x in self.habits.list() if x["topic"]=="response.length")

    def confirm(self,item):
        return self.habits.review(item["id"],action="confirm",expected_revision=item["revision"],
                                  source="用户明确确认这项偏好")

    def test_default_is_disabled_and_reads_are_lazy(self):
        self.assertFalse(self.habits.status()["policy"]["enabled"])
        self.assertEqual(self.habits.list(),[])
        self.assertEqual(self.capture()["receipt_id"],None)
        self.assertFalse(self.habits.db.exists())

    def test_configure_revision_and_boolean_validation(self):
        result=self.enable()
        self.assertEqual(result["policy"]["revision"],1)
        with self.assertRaises(ConflictError):
            self.habits.configure(enabled=False,source="user request",expected_revision=0)
        with self.assertRaises(ValidationError):
            self.habits.configure(enabled=1,source="user request")

    def test_automatic_baseline_and_same_turn_idempotency(self):
        self.enable()
        first=self.seed()
        again=self.seed()
        self.assertEqual(first["id"],again["id"])
        self.assertEqual(again["evidence_count"],1)
        self.assertEqual(first["revision"],again["revision"])
        self.assertEqual(again["state"],"candidate")

    def test_without_turn_id_uses_conservative_prompt_hash(self):
        self.enable()
        for _ in range(3):
            self.habits.capture_prompt("我喜欢简洁回答。",thread_id="chat-a")
        self.assertEqual(self.habits.list()[0]["evidence_count"],1)

    def test_three_independent_choices_in_two_chats_are_learned_not_confirmed(self):
        self.enable()
        item=self.learn()
        self.assertEqual(item["state"],"learned")
        self.assertEqual(item["status"],"candidate")
        self.assertEqual(item["evidence_count"],3)
        self.assertEqual(item["thread_count"],2)
        context=self.habits.context("请分析这张表",thread_id="chat-c")
        self.assertIn("尚未由用户确认",context["markdown"])
        self.assertIn("简洁回答",context["markdown"])
        self.assertEqual(Store(self.home).list_memories(),[])

    def test_same_chat_same_day_does_not_reach_cross_chat_threshold(self):
        self.enable()
        for i in range(3):
            self.seed(turn=str(i))
        self.assertEqual(self.habits.list()[0]["state"],"candidate")
        self.assertEqual(self.habits.context()["items"],[])

    def test_two_days_can_establish_repeat_in_one_chat(self):
        self.enable()
        at=datetime(2026,1,2,12,tzinfo=timezone.utc)
        for i in range(3):
            with patch("local_memory.habits._now",return_value=at+timedelta(days=i//2)):
                self.seed(turn=str(i))
        self.assertEqual(self.habits.list()[0]["state"],"learned")

    def test_semantic_observation_requires_exact_filtered_quote_and_scope(self):
        self.enable()
        quote="我通常先看关系图，再看长篇推导。"
        capture=self.capture(quote,project="fixture")
        item=self.habits.observe(receipt_id=capture["receipt_id"],topic="learning.diagram-first",
                                 value="先看关系图再读推导",quote=quote,scope="project:fixture")
        self.assertEqual(item["scope"],"project:fixture")
        with self.assertRaises(ValidationError):
            self.habits.observe(receipt_id=capture["receipt_id"],topic="invented.preference",
                                value="unrelated",quote="我总是喜欢公开所有个人记录")
        with self.assertRaises(ValidationError):
            self.habits.observe(receipt_id=capture["receipt_id"],topic="learning.diagram-first",
                                value="先看图",quote=quote,scope="global")

    def test_generated_secrets_are_rejected_even_with_valid_quote(self):
        self.enable()
        quote="我通常先看关系图，再看长篇推导。"
        capture=self.capture(quote)
        with self.assertRaises(ValidationError):
            self.habits.observe(receipt_id=capture["receipt_id"],topic="learning.example",
                                value="sk-"+"a"*26,quote=quote)

    def test_semantic_receipt_cannot_bypass_paused_global_or_thread_policy(self):
        self.enable()
        quote="我通常先看关系图，再看长篇推导。"
        capture=self.capture(quote)
        self.habits.configure(enabled=False,source="用户要求暂停",thread_id="chat-a",expected_revision=0)
        with self.assertRaises(ValidationError):
            self.habits.observe(receipt_id=capture["receipt_id"],topic="learning.diagram-first",value="先看图",quote=quote)
        self.assertIsNone(self.capture()["receipt_id"])
        self.assertIsNotNone(self.capture(thread="chat-b")["receipt_id"])

    def test_collection_pause_and_reuse_pause_are_independent(self):
        self.enable()
        self.learn()
        self.habits.configure(enabled=False,source="用户要求暂停观察")
        self.assertTrue(self.habits.context(thread_id="chat-c")["items"])
        self.habits.configure(use_learned=False,source="用户要求暂停使用推断")
        self.assertEqual(self.habits.context(thread_id="chat-c")["items"],[])

    def test_external_context_default_off_and_rechecked_on_observe(self):
        self.enable()
        quote="我通常先看关系图，再看长篇推导。"
        self.assertIsNone(self.habits.capture_prompt(quote,thread_id="chat-a",external_context=True)["receipt_id"])
        self.habits.configure(allow_tool_assisted=True,source="用户明确允许工具辅助观察")
        capture=self.habits.capture_prompt(quote,thread_id="chat-a",turn_id="external",external_context=True)
        self.habits.configure(allow_tool_assisted=False,source="用户撤回工具辅助观察")
        with self.assertRaises(ValidationError):
            self.habits.observe(receipt_id=capture["receipt_id"],topic="learning.diagram-first",value="先看图",quote=quote)

    def test_project_scopes_remain_separate(self):
        self.enable()
        self.learn(project="alpha")
        self.assertTrue(self.habits.context(project="alpha")["items"])
        self.assertEqual(self.habits.context(project="beta")["items"],[])
        self.assertEqual(self.habits.context()["items"],[])

    def test_conflicting_evidence_suppresses_both_inferences(self):
        self.enable()
        self.learn()
        self.seed(thread="chat-c",turn="turn-d",text="我喜欢详细解释。")
        values=self.habits.list()
        self.assertEqual({v["state"] for v in values},{"conflicted"})
        self.assertTrue(all(v["confidence"]<=.3 for v in values))
        self.assertEqual(self.habits.context()["items"],[])

    def test_negation_and_changes_of_mind_do_not_get_keyword_baseline(self):
        for text in ("以后不要简短回复。","以后不要用中文回答，改用英文。",
                     "I do not prefer brief answers.","我喜欢详细解释，但是不要太长。"):
            self.assertEqual(_classify(text),[],text)

    def test_quoted_third_party_persona_one_off_and_secret_material_is_not_observed(self):
        self.enable()
        bad=("> 我喜欢简洁回答。",'“我喜欢简洁回答。”',"```text\n我喜欢简洁回答。\n```",
             "我同学喜欢简洁回答。","虚构角色说我喜欢简洁回答。","这次我喜欢简洁回答。",
             "[local-proactive-chat] 我喜欢简洁回答。","<heartbeat><automation_id>x</automation_id>我喜欢简洁回答</heartbeat>",
             "我喜欢简洁回答，api_key="+"fake_secret_for_test")
        for i,text in enumerate(bad):
            self.assertIsNone(self.capture(text,turn=str(i))["receipt_id"],text)
        self.assertEqual(self.habits.list(),[])

    def test_only_relevant_short_excerpts_are_stored(self):
        self.enable()
        result=self.capture("无关内容不应该存储。\n我喜欢简洁回答。\n其他流水内容也不应存储。")
        with closing(sqlite3.connect(self.habits.db)) as con, con:
            receipt=con.execute("SELECT excerpts FROM receipts WHERE id=?",(result["receipt_id"],)).fetchone()[0]
        self.assertEqual(json.loads(receipt),["我喜欢简洁回答。"])
        self.assertNotIn("无关内容",receipt)

    def test_expired_receipt_and_old_replay_cannot_add_evidence(self):
        self.enable()
        capture=self.capture("我喜欢简洁回答。")
        with closing(sqlite3.connect(self.habits.db)) as con, con:
            con.execute("UPDATE receipts SET expires_at=?",((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        with self.assertRaises(ValidationError):
            self.habits.observe(receipt_id=capture["receipt_id"],topic="response.length",value="简洁",quote="我喜欢简洁回答。")
        self.assertIsNone(self.capture("我喜欢简洁回答。")["receipt_id"])

    def test_concurrent_duplicate_capture_adds_one_evidence(self):
        self.enable()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.seed(),range(8)))
        self.assertEqual(len({r["id"] for r in results}),1)
        self.assertEqual(self.habits.get(results[0]["id"])["evidence_count"],1)

    def test_explicit_confirmation_creates_only_own_key_and_stale_review_fails(self):
        self.enable()
        item=self.seed()
        unrelated=Store(self.home).remember(kind="preference",title="Other explicit preference",content="Keep original",
                                            source="User explicitly asked",key="unrelated.explicit")
        with self.assertRaises(ValidationError):
            self.habits.review(item["id"],action="confirm",expected_revision=item["revision"],source="automatic inference")
        confirmed=self.confirm(item)
        self.assertEqual(confirmed["state"],"confirmed")
        self.assertTrue(Store(self.home).get(confirmed["memory_id"])["key"].startswith("learned-habit."))
        self.assertEqual(Store(self.home).get(unrelated["id"])["content"],"Keep original")
        with self.assertRaises(ConflictError):
            self.confirm(item)

    def test_edit_removes_old_support_and_demotes_own_confirmation(self):
        self.enable()
        item=self.confirm(self.seed())
        edited=self.habits.review(item["id"],action="edit",expected_revision=item["revision"],
                                 source="用户要求改为详细说明",value="详细解释")
        self.assertEqual(edited["state"],"candidate")
        self.assertEqual(edited["value"],"详细解释")
        self.assertEqual(edited["evidence_count"],0)
        self.assertEqual(edited["evidence"],[])
        self.assertEqual(Store(self.home).get(edited["memory_id"])["status"],"candidate")
        self.assertEqual(self.habits.context()["items"],[])

    def test_external_revision_is_displayed_without_old_support_or_rollback(self):
        self.enable()
        item=self.confirm(self.seed())
        store=Store(self.home)
        revised=store.revise(item["memory_id"],expected_revision=1,source="User explicitly corrected",content="Prefer detailed reasoning")
        shown=self.habits.get(item["id"])
        self.assertEqual(shown["value"],"Prefer detailed reasoning")
        self.assertTrue(shown["external_revision"])
        self.assertEqual(shown["evidence"],[])
        with self.assertRaises(ConflictError):
            self.confirm(shown)
        self.assertEqual(store.get(revised["id"])["content"],"Prefer detailed reasoning")

    def test_forget_erases_quote_value_and_prevents_silent_relearning(self):
        self.enable()
        item=self.confirm(self.seed())
        removed=self.habits.review(item["id"],action="forget",expected_revision=item["revision"],source="用户明确要求遗忘")
        self.assertEqual(removed["value"],"")
        self.assertEqual(removed["evidence"],[])
        self.assertEqual(Store(self.home).get(item["memory_id"])["state"],"deleted")
        self.assertIn(item["memory_root_id"],self.habits.suppressed_memory_roots())
        self.assertEqual(self.capture("我喜欢简洁回答。",thread="chat-z",turn="new")["observed"],[])
        self.assertEqual(self.habits.list(),[])

    def test_external_store_forget_or_link_callback_cleans_habit(self):
        self.enable()
        item=self.confirm(self.seed())
        store=Store(self.home)
        store.forget(item["memory_id"],expected_revision=1,reason="User requested forget")
        self.habits.forget_linked(item["memory_root_id"])
        shown=self.habits.get(item["id"])
        self.assertEqual(shown["state"],"forgotten")
        self.assertEqual(shown["evidence"],[])
        self.assertEqual(shown["value"],"")

    def test_reject_has_no_store_side_effect_and_blocks_replay(self):
        self.enable()
        item=self.seed()
        self.habits.review(item["id"],action="reject",expected_revision=item["revision"],source="用户不认可这个推断")
        self.assertEqual(self.capture("我喜欢简洁回答。",turn="new")["observed"],[])
        self.assertEqual(Store(self.home).list_memories(),[])

    def test_clear_removes_owned_records_but_preserves_other_preferences(self):
        self.enable()
        item=self.confirm(self.seed())
        unrelated=Store(self.home).remember(kind="preference",title="Independent",content="Do not erase this",
                                           source="User requested",key="independent.preference")
        result=self.habits.clear(source="用户明确要求清除学习资料",expected_revision=1)
        self.assertEqual(result["linked_memories_forgotten"],1)
        self.assertEqual(self.habits.list(),[])
        self.assertEqual(Store(self.home).get(unrelated["id"])["state"],"active")
        self.assertEqual(self.habits.get(item["id"])["evidence"],[])
        self.assertEqual(self.capture("我喜欢简洁回答。",turn="new")["observed"],[])

    def test_confirm_in_progress_blocks_clear(self):
        self.enable()
        item=self.seed()
        entered,release=threading.Event(),threading.Event()
        original=Store.remember
        def blocked(store,**kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test coordination timed out")
            return original(store,**kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool,patch.object(Store,"remember",blocked):
            future=pool.submit(self.confirm,item)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(ConflictError):
                    self.habits.clear(source="用户要求清除")
            finally:
                release.set()
            self.assertEqual(future.result()["state"],"confirmed")

    def test_store_failure_releases_pending_review(self):
        self.enable()
        item=self.seed()
        with patch.object(Store,"remember",side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.confirm(item)
        latest=self.habits.get(item["id"])
        self.assertIsNone(latest["pending_action"])
        self.assertEqual(self.confirm(latest)["state"],"confirmed")

    def test_negative_first_person_preference_stays_available_to_semantic_observer(self):
        self.enable()
        quote="我不喜欢回复太长。"
        capture=self.capture(quote)
        self.assertIsNotNone(capture["receipt_id"])
        self.assertEqual(capture["observed"],[])
        item=self.habits.observe(receipt_id=capture["receipt_id"],topic="response.length",value="简洁",quote=quote)
        self.assertEqual(item["value"],"简洁回答")
        self.assertEqual(item["status"],"candidate")

    def test_english_plural_and_habitual_variants_share_same_value(self):
        self.enable()
        choices=("我平时喜欢简短一点的回答。","我一般喜欢简洁回答。","I usually prefer concise answers.")
        for i,text in enumerate(choices):
            self.seed(thread="chat-"+str(i%2),turn=str(i),text=text)
        item=self.habits.list()[0]
        self.assertEqual(item["evidence_count"],3)
        self.assertEqual(item["state"],"learned")
        self.assertEqual(_classify("I tend to prefer brief replies."),[("response.length","简洁回答")])

    def test_dead_process_confirmation_is_reconciled_from_owned_store_key(self):
        self.enable()
        item=self.seed()
        saved=Store(self.home).remember(kind="preference",scope="global",status="confirmed",
            title="Synthetic confirmed habit",content=item["value"],source="User explicitly confirmed",
            key="learned-habit."+item["id"])
        token=json.dumps({"pid":999999999,"id":"synthetic-dead-owner"})
        with closing(sqlite3.connect(self.habits.db)) as con,con:
            con.execute("UPDATE habits SET pending_action='confirm',pending_token=? WHERE id=?",(token,item["id"]))
        with patch("local_memory.habits._owner_alive",return_value=False):
            restored=self.habits.get(item["id"])
        self.assertEqual(restored["state"],"confirmed")
        self.assertEqual(restored["memory_root_id"],saved["root_id"])
        self.assertIsNone(restored["pending_action"])

    def test_dead_process_clear_gate_recovers_without_claiming_data_was_cleared(self):
        self.enable()
        self.seed()
        token=json.dumps({"pid":999999999,"id":"synthetic-dead-owner"})
        with closing(sqlite3.connect(self.habits.db)) as con,con:
            con.execute("UPDATE control SET clearing=? WHERE id=1",(token,))
        with patch("local_memory.habits._owner_alive",return_value=False):
            status=self.habits.status()
        self.assertFalse(status["clearing"])
        self.assertEqual(len(self.habits.list()),1)

    def test_busy_database_has_a_short_wait_bound(self):
        self.enable()
        with closing(sqlite3.connect(self.habits.db)) as con:
            con.execute("BEGIN IMMEDIATE")
            started=time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                self.seed()
            elapsed=time.monotonic()-started
            con.rollback()
        self.assertLess(elapsed,1.5)

    def test_clear_in_progress_blocks_new_capture_and_review(self):
        self.enable()
        item=self.confirm(self.seed())
        entered,release=threading.Event(),threading.Event()
        original=Store.forget
        def blocked(store,*args,**kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test coordination timed out")
            return original(store,*args,**kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool,patch.object(Store,"forget",blocked):
            future=pool.submit(self.habits.clear,source="User explicitly requested clear")
            try:
                self.assertTrue(entered.wait(2))
                self.assertIsNone(self.capture("我喜欢详细解释。",turn="during-clear")["receipt_id"])
                with self.assertRaises(ConflictError):
                    self.habits.review(item["id"],action="edit",expected_revision=item["revision"],
                                      source="User requested edit",value="详细解释")
            finally:
                release.set()
            self.assertEqual(future.result()["linked_memories_forgotten"],1)
        self.assertEqual(self.habits.list(),[])

    def test_context_respects_budget(self):
        self.enable()
        self.learn()
        self.assertEqual(self.habits.context(budget=10)["markdown"],"")
        self.assertLessEqual(len(self.habits.context(budget=350)["markdown"]),350)


if __name__=="__main__":
    unittest.main()
