"""Consent, local-day windows and atomic deduplication for proactive chats."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from local_memory.proactive import ProactiveChat
from local_memory.store import ValidationError


class ProactiveChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-proactive-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "private"
        self.chat = ProactiveChat(self.home)
        (self.home / "settings.json").write_text(json.dumps({"proactive_chat": {
            "timezone_offset_minutes": 480, "start_hour": 9, "end_hour_exclusive": 18,
            "scheduled_hour_min": 9, "scheduled_hour_max": 15, "fallback_hour": 17}}), encoding="utf-8")

    def enable(self):
        return self.chat.configure(enabled=True, source="fixture explicit user request")

    def call(self, operation="evaluate", **changes):
        args = {"current_thread_id": "synthetic-daily", "daily_thread_id": "synthetic-daily",
                "now": "2026-11-17T17:00:00+08:00"}
        args.update(changes)
        return getattr(self.chat, operation)(**args)

    def test_disabled_until_explicitly_configured_and_no_ordinary_usage_records(self):
        self.assertFalse(self.chat.status()["enabled"])
        self.assertEqual(self.call("claim")["reason"], "disabled")
        self.enable()
        for _ in range(3):
            self.assertTrue(self.call()["eligible"])
        self.assertEqual(self.chat.status()["attempt_count"], 0)
        self.assertFalse((self.home / "memory.sqlite3").exists())
        self.assertFalse((self.home / "session-registry.sqlite3").exists())

    def test_window_start_inclusive_end_exclusive_and_no_late_catchup(self):
        self.enable()
        # Pick a day whose varying slot is 09:00 to test the opening boundary.
        day = date(2026, 11, 17)
        while day.toordinal() % 7:
            day += timedelta(days=1)
        self.assertEqual(self.call(now=f"{day}T08:59:59+08:00")["reason"], "outside_window")
        self.assertTrue(self.call(now=f"{day}T09:00:00+08:00")["eligible"])
        self.assertTrue(self.call(now=f"{day}T17:59:59+08:00")["eligible"])
        self.assertEqual(self.call(now=f"{day}T18:00:00+08:00")["reason"], "outside_window")
        self.assertEqual(self.call(now=f"{day}T23:59:59+08:00")["reason"], "outside_window")

    def test_slots_vary_by_day_and_before_slot_does_not_claim(self):
        self.enable()
        hours = set()
        for offset in range(7):
            day = date(2026, 11, 17) + timedelta(days=offset)
            result = self.call(now=f"{day}T09:00:00+08:00")
            hours.add(result["scheduled_hour"])
            if result["scheduled_hour"] > 9:
                self.assertEqual(result["reason"], "before_daily_slot")
                self.assertFalse(result["prepared"])
        self.assertEqual(hours, set(range(9, 16)))
        self.assertEqual(self.chat.status()["attempt_count"], 0)

    def test_recent_user_activity_defers_without_fulfilling_daily_target(self):
        self.enable()
        recent = self.call("claim", now="2026-11-17T16:00:00+08:00",
                           last_user_at="2026-11-17T15:30:00+08:00")
        self.assertEqual(recent["reason"], "recent_user_activity")
        self.assertEqual(self.chat.status()["attempt_count"], 0)
        self.assertTrue(self.call("claim", now="2026-11-17T16:15:00+08:00",
                                 last_user_at="2026-11-17T15:30:00+08:00")["prepared"])

    def test_fallback_after_17_allows_recent_chat_but_never_overrides_busy(self):
        self.enable()
        changes = {"last_user_at": "2026-11-17T16:59:59+08:00"}
        self.assertEqual(self.call("claim", busy=True, **changes)["reason"], "busy")
        self.assertEqual(self.chat.status()["attempt_count"], 0)
        self.assertTrue(self.call("claim", **changes)["prepared"])

    def test_missing_or_mismatched_daily_entry_never_claims(self):
        self.enable()
        for changes, reason in (({"daily_thread_id": None}, "missing_daily_entry"),
                                ({"current_thread_id": None}, "missing_daily_entry"),
                                ({"daily_thread_id": "replacement"}, "daily_entry_mismatch")):
            with self.subTest(changes=changes):
                self.assertEqual(self.call("claim", **changes)["reason"], reason)
        self.assertEqual(self.chat.status()["attempt_count"], 0)

    def test_pause_disable_and_reenable_preserve_pause_and_attempt_history(self):
        self.enable()
        self.chat.configure(paused_until="2026-11-18T17:00:00+08:00", source="fixture pause")
        self.assertEqual(self.call("claim")["reason"], "paused")
        self.chat.configure(enabled=False, source="fixture disable")
        self.chat.configure(enabled=True, source="fixture reenable")
        self.assertEqual(self.call("claim")["reason"], "paused")
        self.chat.configure(paused_until=None, source="fixture unpause")
        self.assertTrue(self.call("claim")["prepared"])
        self.chat.configure(enabled=False, source="fixture disable after prepare")
        self.assertEqual(self.call("claim")["reason"], "disabled")
        self.chat.configure(enabled=True, source="fixture reenable after prepare")
        self.assertEqual(self.call("claim")["reason"], "already_prepared")

    def test_pause_exact_expiry_is_allowed(self):
        self.enable()
        self.chat.configure(paused_until="2026-11-17T17:00:00+08:00", source="fixture finite pause")
        self.assertTrue(self.call()["eligible"])

    def test_utc_input_and_local_day_deduplication_across_days(self):
        self.enable()
        first = self.call("claim", now="2026-11-17T09:00:00Z")
        self.assertEqual(first["local_day"], "2026-11-17")
        self.assertTrue(first["prepared"])
        self.assertEqual(self.call(now="2026-11-17T09:30:00Z")["reason"], "already_prepared")
        midnight = self.call(now="2026-11-17T16:00:00Z")
        self.assertEqual(midnight["local_day"], "2026-11-18")
        self.assertEqual(midnight["reason"], "outside_window")
        self.assertTrue(self.call("claim", now="2026-11-18T09:00:00Z")["prepared"])
        self.assertEqual(self.chat.status()["attempt_count"], 2)

    def test_duplicate_claim_and_rebinding_do_not_repeat_same_day(self):
        self.enable()
        first = self.call("claim")
        second = self.call("claim")
        self.assertTrue(first["prepared"])
        self.assertFalse(second["prepared"])
        self.assertEqual(second["attempt"], first["attempt"])
        rebound = self.call("claim", current_thread_id="replacement", daily_thread_id="replacement")
        self.assertEqual(rebound["reason"], "already_prepared")
        self.assertEqual(self.chat.status()["latest_attempt"]["status"], "prepared")
        self.assertFalse(self.chat.status()["policy"]["delivery_receipt_available"])

    def test_concurrent_claims_prepare_exactly_once(self):
        self.enable()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.call("claim"), range(16)))
        self.assertEqual(sum(result["prepared"] for result in results), 1)
        self.assertEqual(self.chat.status()["attempt_count"], 1)

    def test_invalid_inputs_leave_ledger_unchanged(self):
        self.enable()
        for changes in ({"now": "2026-11-17T17:00:00"}, {"now": "invalid"},
                        {"last_user_at": "invalid"}, {"busy": 1},
                        {"current_thread_id": "../invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.call("claim", **changes)
        for changes in ({"enabled": 1}, {"paused_until": "2026-11-18"}, {"source": " "}):
            payload = {"source": "fixture invalid configuration", **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.chat.configure(**payload)
        self.assertEqual(self.chat.status()["attempt_count"], 0)

    def test_future_activity_does_not_allow_an_unverified_time_assumption(self):
        self.enable()
        self.assertEqual(self.call("claim", last_user_at="2026-11-17T17:01:00+08:00")["reason"],
                         "future_user_activity")
        self.assertEqual(self.chat.status()["attempt_count"], 0)

    def test_observed_user_activity_defers_but_never_counts_as_proactive(self):
        self.enable()
        stamp = datetime.now(timezone.utc) - timedelta(minutes=5)
        observed = self.chat.observe_activity(thread_id="synthetic-daily", at=stamp.isoformat())
        self.assertTrue(observed["changed"])
        # Use a local-day time earlier than the fallback and choose its preceding
        # recent activity independently of the machine's wall-clock hour.
        self.chat.observe_activity(thread_id="historical", at="2026-01-01T07:30:00Z")
        result = self.call("claim", current_thread_id="historical", daily_thread_id="historical",
                           now="2026-01-01T08:00:00Z")
        self.assertEqual(result["reason"], "recent_user_activity")
        self.assertEqual(self.chat.status()["attempt_count"], 0)
        older = self.chat.observe_activity(thread_id="synthetic-daily", at=(stamp - timedelta(minutes=1)).isoformat())
        self.assertFalse(older["changed"])
        self.assertEqual(older["last_user_at"], observed["last_user_at"])

    def test_observed_activity_is_scoped_to_native_identity(self):
        self.enable()
        self.chat.observe_activity(thread_id="other", at="2026-01-01T07:59:00Z")
        self.assertTrue(self.call(now="2026-01-01T08:00:00Z")["eligible"])

    def test_external_activity_cannot_roll_back_a_newer_hook_timestamp(self):
        self.enable()
        self.chat.observe_activity(thread_id="synthetic-daily", at="2026-01-01T07:59:00Z")
        result = self.call("claim", now="2026-01-01T08:00:00Z", last_user_at="2026-01-01T07:00:00Z")
        self.assertEqual(result["reason"], "recent_user_activity")
        self.assertEqual(self.chat.status()["attempt_count"], 0)
        # A newer externally verified activity must be respected as well.
        newer = self.call("claim", now="2026-01-01T08:44:00Z", last_user_at="2026-01-01T08:43:00Z")
        self.assertEqual(newer["reason"], "recent_user_activity")

    def test_observe_rejects_invalid_identity_time_and_future_time(self):
        for changes in ({"thread_id": False}, {"thread_id": "../invalid"}, {"at": "invalid"},
                        {"at": "2026-01-01T09:00:00"},
                        {"at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.chat.observe_activity(**{"thread_id": "synthetic-daily", **changes})
        self.assertEqual(self.chat.status()["attempt_count"], 0)

    def test_unknown_schema_is_rejected(self):
        with closing(sqlite3.connect(self.chat.db)) as con:
            con.execute("PRAGMA user_version=2")
        with self.assertRaises(ValidationError):
            ProactiveChat(self.home)


if __name__ == "__main__":
    unittest.main()
