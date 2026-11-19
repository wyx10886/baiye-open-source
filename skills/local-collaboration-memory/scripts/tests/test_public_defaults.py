"""Public defaults are empty, opt-in, and independent of any publisher's host."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_memory.daily_chat import DailyChat
from local_memory.habits import HabitLearning
from local_memory.integration import handle_hook, install
from local_memory.proactive import ProactiveChat
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store, ValidationError


class PublicDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-public-defaults-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "private"
        self.store = Store(self.home)

    def hook(self):
        with patch("local_memory.title_sync.native_identity", return_value={"status": "main"}), \
             patch("local_memory.title_sync.synchronize", return_value={"status": "disabled"}):
            return handle_hook(self.home, {"hook_event_name": "UserPromptSubmit",
                "session_id": "synthetic-daily", "prompt": "I usually prefer concise replies."})

    def bind_daily(self):
        SessionRegistry(self.store).ensure(thread_id="synthetic-daily", source="synthetic identity")
        DailyChat(self.store).bind(thread_id="synthetic-daily", title="Daily chat",
                                 source="synthetic user choice", expected_revision=0)

    def test_empty_initialization_has_no_memories_daily_entry_or_opt_in_ledgers(self):
        self.assertEqual(self.store.list_memories(include_inactive=True), [])
        self.assertFalse(DailyChat(self.store).show()["configured"])
        self.assertFalse(SessionRegistry(self.store)._recycling_enabled())
        self.assertFalse((self.home / "habits.sqlite3").exists())
        self.assertFalse((self.home / "proactive.sqlite3").exists())

    def test_install_disables_optional_host_mutation_and_recycling(self):
        codex = self.root / "synthetic-host"
        install(self.home, codex)
        settings = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertFalse(settings["host_title_sync"]["enabled"])
        self.assertFalse(settings["session_recycling"]["enabled"])
        self.assertEqual(self.store.list_memories(include_inactive=True), [])
        self.assertFalse((self.home / "habits.sqlite3").exists())
        self.assertFalse((self.home / "proactive.sqlite3").exists())

    def test_habit_collection_and_reuse_require_independent_opt_in(self):
        learning = HabitLearning(self.home)
        policy = learning.status()["policy"]
        self.assertFalse(policy["enabled"])
        self.assertFalse(policy["use_learned"])
        self.assertFalse(policy["allow_tool_assisted"])
        self.assertIsNone(learning.capture_prompt("I usually prefer concise replies.",
                                                thread_id="synthetic-daily")["receipt_id"])
        self.assertFalse(learning.db.exists())
        learning.configure(enabled=True, source="synthetic user opted into collection")
        self.assertFalse(learning.status()["policy"]["use_learned"])

    def test_daily_hook_does_not_create_opt_in_ledgers_when_unconfigured(self):
        self.bind_daily()
        self.hook()
        self.assertFalse((self.home / "habits.sqlite3").exists())
        self.assertFalse((self.home / "proactive.sqlite3").exists())

    def test_disabled_proactive_collection_has_no_activity_or_attempt_rows(self):
        self.bind_daily()
        proactive = ProactiveChat(self.home)
        result = proactive.observe_activity(thread_id="synthetic-daily")
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "disabled")
        self.hook()
        with closing(sqlite3.connect(proactive.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM proactive_activity").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM proactive_attempts").fetchone()[0], 0)

    def test_default_schedule_is_disabled_with_generic_utc_clock(self):
        status = ProactiveChat(self.home).status()
        self.assertFalse(status["enabled"])
        policy = status["policy"]
        self.assertEqual(policy["timezone"], "UTC+00:00")
        self.assertEqual((policy["start_hour"], policy["end_hour_exclusive"]), (0, 24))
        self.assertNotIn("scheduled_hour", policy)
        self.assertEqual(policy["fallback_hour"], 24)

    def test_explicit_offset_controls_local_day_at_supported_extremes(self):
        proactive = ProactiveChat(self.home)
        for offset, day in ((-720, "2025-12-31"), (840, "2026-01-01")):
            with self.subTest(offset=offset):
                (self.home / "settings.json").write_text(json.dumps({"proactive_chat": {
                    "timezone_offset_minutes": offset}}), encoding="utf-8")
                result = proactive.evaluate(current_thread_id="synthetic-daily",
                    daily_thread_id="synthetic-daily", now="2026-01-01T01:00:00Z")
                self.assertEqual(result["local_day"], day)
                self.assertEqual(result["reason"], "disabled")

    def test_invalid_policy_cannot_silently_change_schedule(self):
        proactive = ProactiveChat(self.home)
        cases = ({"timezone_offset_minutes": -721}, {"timezone_offset_minutes": 841},
                 {"timezone_offset_minutes": True}, {"start_hour": 12, "end_hour_exclusive": 12},
                 {"scheduled_hour": 24}, {"scheduled_hour_min": 5, "scheduled_hour_max": 4},
                 {"fallback_hour": 25}, {"recent_user_minutes": -1}, {"unknown": "ignored"})
        for policy in cases:
            with self.subTest(policy=policy):
                (self.home / "settings.json").write_text(json.dumps({"proactive_chat": policy}), encoding="utf-8")
                with self.assertRaises(ValidationError):
                    proactive.status()


if __name__ == "__main__":
    unittest.main()
