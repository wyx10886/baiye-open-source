"""User-selected fixed hours preserve consent and same-day preparation gates."""
from contextlib import closing
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_memory.proactive import ProactiveChat
from local_memory.store import ValidationError


class ProactiveFixedScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="local-proactive-fixed-")
        self.addCleanup(temporary.cleanup)
        self.home=Path(temporary.name)/"private"
        self.chat=ProactiveChat(self.home)
        self.chat.configure(enabled=True,source="anonymous explicit fixed-time preference")

    def settings(self,value):
        if isinstance(value.get("proactive_chat", {}), dict):
            fixture = {"timezone_offset_minutes":-300,"start_hour":9,"end_hour_exclusive":18,
                       "scheduled_hour_min":9,"scheduled_hour_max":15,"fallback_hour":17}
            value = dict(value, proactive_chat=dict(fixture, **value.get("proactive_chat", {})))
        (self.home/"settings.json").write_text(json.dumps(value),encoding="utf-8")

    def call(self,operation="evaluate",**changes):
        payload={"current_thread_id":"native-fixed","daily_thread_id":"native-fixed",
                 "now":"2026-11-19T13:00:00-05:00"}
        payload.update(changes)
        return getattr(self.chat,operation)(**payload)

    def test_explicit_hour_applies_every_day_at_exact_boundary(self):
        self.settings({"proactive_chat":{"scheduled_hour":13}})
        for offset in range(7):
            day=date(2026,11,19)+timedelta(days=offset)
            with self.subTest(day=day):
                early=self.call(now=f"{day}T12:59:59-05:00")
                self.assertEqual(early["reason"],"before_daily_slot")
                self.assertFalse(early["eligible"])
                opening=self.call(now=f"{day}T13:00:00-05:00")
                self.assertEqual(opening["scheduled_hour"],13)
                self.assertTrue(opening["eligible"])
        self.assertEqual(self.chat.status()["attempt_count"],0)

    def test_status_reports_fixed_hour_and_same_day_claim_remains_unique(self):
        self.settings({"unrelated":{"preserved":True},"proactive_chat":{"scheduled_hour":13}})
        policy=self.chat.status()["policy"]
        self.assertEqual(policy["scheduled_hour"],13)
        self.assertEqual(policy["scheduled_hour_min"],13)
        self.assertEqual(policy["scheduled_hour_max"],13)
        first=self.call("claim")
        self.assertTrue(first["prepared"])
        second=self.call("claim",now="2026-11-19T13:00:01-05:00")
        self.assertEqual(second["reason"],"already_prepared")
        self.assertFalse(second["prepared"])
        self.assertEqual(self.chat.status()["attempt_count"],1)

    def test_unconfigured_schedule_keeps_legacy_day_variation(self):
        self.settings({"unrelated":{"preserved":True}})
        hours={self.call(now=f"{date(2026,11,19)+timedelta(days=offset)}T17:00:00-05:00")["scheduled_hour"] for offset in range(7)}
        self.assertEqual(hours,set(range(9,16)))
        self.assertEqual(self.chat.status()["policy"]["scheduled_hour_min"],9)
        self.assertEqual(self.chat.status()["policy"]["scheduled_hour_max"],15)

    def test_fixed_hour_preserves_recent_busy_pause_identity_and_window_gates(self):
        self.settings({"proactive_chat":{"scheduled_hour":13}})
        self.assertEqual(self.call("claim",busy=True)["reason"],"busy")
        self.assertEqual(self.call("claim",last_user_at="2026-11-19T12:59:59-05:00")["reason"],"recent_user_activity")
        self.assertEqual(self.call("claim",daily_thread_id="other")["reason"],"daily_entry_mismatch")
        self.assertEqual(self.call("claim",now="2026-11-19T18:00:00-05:00")["reason"],"outside_window")
        self.chat.configure(paused_until="2026-11-20T13:00:00-05:00",source="anonymous pause")
        self.assertEqual(self.call("claim")["reason"],"paused")
        self.assertEqual(self.chat.status()["attempt_count"],0)

    def test_invalid_explicit_schedule_is_rejected_without_preparation(self):
        for value in (True,False,None,"13",13.0,8,18,[],{}):
            with self.subTest(value=value):
                self.settings({"proactive_chat":{"scheduled_hour":value}})
                with self.assertRaises(ValidationError):self.call("claim")
                with self.assertRaises(ValidationError):self.chat.status()
        with closing(sqlite3.connect(self.chat.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM proactive_attempts").fetchone()[0],0)

    def test_malformed_settings_or_policy_is_not_silently_ignored(self):
        for text in ("{invalid", "[]", '{"proactive_chat":null}', '{"proactive_chat":[]}'):
            with self.subTest(settings=text):
                (self.home/"settings.json").write_text(text,encoding="utf-8")
                with self.assertRaises(ValidationError):self.call("claim")
        with closing(sqlite3.connect(self.chat.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM proactive_attempts").fetchone()[0],0)

    def test_changed_hour_is_loaded_without_restarting_client(self):
        self.settings({"proactive_chat":{"scheduled_hour":17}})
        self.assertEqual(self.call()["reason"],"before_daily_slot")
        self.settings({"proactive_chat":{"scheduled_hour":13}})
        self.assertTrue(self.call()["eligible"])
        self.assertEqual(self.chat.status()["policy"]["scheduled_hour"],13)


if __name__=="__main__":
    unittest.main()
