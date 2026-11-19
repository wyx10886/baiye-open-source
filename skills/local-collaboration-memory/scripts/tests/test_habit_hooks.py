import uuid
"""Lifecycle identity, context budgets, and cross-store forgetting regressions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_memory.integration import handle_hook
from local_memory.store import Store


class HabitHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-habit-hook-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "private"
        self.store = Store(self.home)
        from local_memory.habits import HabitLearning
        self.learning = HabitLearning(self.home)
        self.learning.configure(enabled=True, use_learned=True, source="explicit synthetic test authorization")
        self.native = str(uuid.UUID(int=101, version=4))

    def hook(self, prompt, *, status="main", event="UserPromptSubmit", **changes):
        payload = {"hook_event_name":event, "session_id":self.native,
                   "turn_id":"fixture-turn", "prompt":prompt, "cwd":str(self.home)}
        payload.update(changes)
        with patch("local_memory.title_sync.native_identity", return_value={"status":status}), \
             patch("local_memory.title_sync.synchronize", return_value={"status":"ok"}):
            return handle_hook(self.home, payload)

    def test_capture_has_the_verified_identity_and_receipt_guidance(self):
        with patch("local_memory.habits.HabitLearning.capture_prompt",
                   return_value={"guidance":"synthetic receipt guidance"}) as capture:
            result = self.hook("I usually prefer concise answers.")
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(capture.call_args.kwargs["thread_id"], self.native)
        self.assertEqual(capture.call_args.kwargs["turn_id"], "fixture-turn")
        self.assertIn("synthetic receipt guidance", result["hookSpecificOutput"]["additionalContext"])

    def test_subagents_unknown_origins_and_automations_never_become_evidence(self):
        cases = [("I usually prefer concise answers.", {"status":"pending"}),
                 ("I usually prefer concise answers.", {"status":"subagent"}),
                 ("I usually prefer concise answers.", {"agent_id":"child"}),
                 ("I usually prefer concise answers.", {"session_id":None}),
                 ("I usually prefer concise answers.", {"event":"SessionStart"}),
                 ("[local-proactive-chat] I prefer concise answers.", {}),
                 ("<heartbeat><automation_id>test</automation_id>I prefer concise answers.</heartbeat>", {})]
        for prompt, changes in cases:
            with self.subTest(changes=changes, prompt=prompt), \
                 patch("local_memory.habits.HabitLearning.capture_prompt") as capture:
                self.hook(prompt, **changes)
                capture.assert_not_called()

    def test_learning_failure_never_suppresses_confirmed_context(self):
        self.store.remember(kind="preference", title="Confirmed preference", content="Use metric units.",
                            source="explicit fixture statement")
        with patch("local_memory.habits.HabitLearning.capture_prompt", side_effect=OSError("private error")):
            result = self.hook("I prefer concise replies.")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Use metric units.", context)
        self.assertNotIn("private error", context)

    def test_context_learning_uses_only_the_remaining_budget(self):
        self.store.remember(kind="preference", title="Confirmed preference", content="Explicit statement wins.",
                            source="explicit fixture statement")
        with patch("local_memory.habits.HabitLearning.context",
                   return_value={"items":[{"id":"candidate"}], "markdown":"x" * 10000}):
            result = self.store.context("", budget=700, thread_id=self.native)
        self.assertLessEqual(len(result["markdown"]), 700)
        self.assertIn("Explicit statement wins.", result["markdown"])
        self.assertEqual(result["learned_habits"], [])

    def test_thread_identity_reaches_context_policy_checks(self):
        with patch("local_memory.habits.HabitLearning.context", return_value={"items":[],"markdown":""}) as context:
            self.hook("continue")
        self.assertEqual(context.call_args.kwargs["thread_id"], self.native)

    def test_unknown_current_chat_never_bypasses_a_per_chat_usage_policy(self):
        with patch("local_memory.habits.HabitLearning.context") as context:
            self.store.context("", budget=700)
        context.assert_not_called()

    def test_partial_forgetting_is_explicit_durable_and_retryable(self):
        row = self.store.remember(kind="preference", title="Fixture", content="Erase this detail.",
                                  source="explicit fixture")
        with patch("local_memory.habits.HabitLearning.forget_linked", side_effect=OSError("private database error")):
            result = self.store.forget(row["id"], expected_revision=row["revision"])
            self.assertEqual(result["state"], "deleted")
            self.assertFalse(result["habit_cleanup"]["ok"])
            context = self.store.context("", thread_id=self.native)
            self.assertEqual(context["learned_habits"], [])
            self.assertIn("learning_error", context)
        with patch("local_memory.habits.HabitLearning.forget_linked") as cleanup:
            retried = self.store.forget(row["id"], expected_revision=row["revision"])
        cleanup.assert_called_once_with(row["root_id"])
        self.assertNotIn("habit_cleanup", retried)
        self.assertFalse(self.store._complete_habit_forgets())

    def test_direct_memory_forget_notifies_habit_store_by_stable_root(self):
        row = self.store.remember(kind="preference", title="Own fixture", content="Original preference.",
                                  source="explicit fixture")
        revised = self.store.revise(row["id"], expected_revision=row["revision"],
                                    source="user correction", content="Revised preference.")
        with patch("local_memory.habits.HabitLearning.forget_linked") as forget:
            self.store.forget(revised["id"], expected_revision=revised["revision"])
        forget.assert_called_once_with(row["root_id"])

    def test_restore_cannot_overwrite_unfinished_forgetting_intent(self):
        from local_memory.store import ConflictError
        row = self.store.remember(kind="preference", title="Fixture", content="Do not revive this.", source="user fixture")
        old_snapshot = self.store.backup()
        with patch("local_memory.habits.HabitLearning.forget_linked", side_effect=OSError("locked")):
            self.store.forget(row["id"], expected_revision=row["revision"])
            with self.assertRaises(ConflictError):
                self.store.restore(old_snapshot)
        self.assertEqual(self.store.get(row["id"])["state"], "deleted")

    def test_real_forgetting_journal_roundtrips_through_a_validated_backup(self):
        row = self.store.remember(kind="preference", title="Fixture", content="Private fixture.", source="user fixture")
        self.store.forget(row["id"], expected_revision=row["revision"])
        backup_path = self.store.backup()
        result = self.store.restore(backup_path)
        self.assertTrue(result["safety_backup"])
        self.assertEqual(self.store.list_memories(), [])
        self.assertFalse(self.store._complete_habit_forgets())

    def test_restore_respects_independent_learned_memory_tombstones(self):
        row = self.store.remember(kind="preference", title="Learned confirmation", content="Erased fixture detail.",
                                  source="user confirmed fixture")
        backup = self.store.backup()
        backup_path = backup["path"] if isinstance(backup, dict) else backup
        self.store.forget(row["id"], expected_revision=row["revision"])
        with patch("local_memory.habits.HabitLearning.suppressed_memory_roots", return_value=[row["root_id"]]):
            result = self.store.restore(backup_path)
        self.assertEqual(result["forgotten_habit_roots_preserved"], 1)
        self.assertEqual(self.store.list_memories(), [])


if __name__ == "__main__":
    unittest.main()
