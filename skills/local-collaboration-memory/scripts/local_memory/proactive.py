"""Local gates for an explicitly requested daily proactive chat.

The host scheduler produces the message. This ledger records only a prepared
attempt, never proof that an application notification was delivered or read.
Ordinary user messages and assistant replies do not create attempt records.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import uuid

from .retention import safe_path
from .session_registry import _identity
from .store import ValidationError, _text


_UNCHANGED = object()
POLICY = {
    "timezone": "UTC+00:00",
    "timezone_offset_minutes": 0,
    "start_hour": 0,
    "end_hour_exclusive": 24,
    "scheduled_hour_min": 0,
    "scheduled_hour_max": 23,
    "recent_user_minutes": 45,
    "fallback_hour": 24,
    "daily_target": 1,
    "attempt_limit_per_day": 1,
    "delivery_receipt_available": False,
}


def _time(value, label, *, default_now=False):
    if value is None and default_now:
        return datetime.now(timezone.utc)
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ValidationError(label + " must be an ISO timestamp with timezone") from None


class ProactiveChat:
    """Keep consent, pause state and one preparation claim per local day."""

    def __init__(self, home):
        self.home = safe_path(Path(home))
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = safe_path(self.home / "proactive.sqlite3")
        with self._connect() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValidationError("Unsupported proactive ledger version; do not downgrade")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS proactive_config (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    paused_until TEXT, source TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_attempts (
                    local_day TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL UNIQUE,
                    thread_id TEXT NOT NULL,
                    prepared_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status='prepared')
                );
                CREATE TABLE IF NOT EXISTS proactive_activity (
                    thread_id TEXT PRIMARY KEY, last_user_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connect(self):
        safe_path(self.db)
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _config(con):
        row = con.execute("SELECT * FROM proactive_config WHERE singleton=1").fetchone()
        if row is None:
            return {"enabled": False, "paused_until": None, "source": None, "updated_at": None}
        return {"enabled": bool(row["enabled"]), "paused_until": row["paused_until"],
                "source": row["source"], "updated_at": row["updated_at"]}

    def _policy(self):
        """Read explicit clock/window choices without changing the preparation ledger."""
        path = safe_path(self.home / "settings.json")
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        except (OSError, ValueError):
            raise ValidationError("Cannot read proactive schedule settings") from None
        if not isinstance(settings, dict):
            raise ValidationError("Proactive schedule settings must be an object")
        schedule = settings.get("proactive_chat", {})
        if not isinstance(schedule, dict):
            raise ValidationError("proactive_chat must be an object")
        policy = dict(POLICY)
        allowed = {"timezone_offset_minutes", "start_hour", "end_hour_exclusive",
                   "scheduled_hour_min", "scheduled_hour_max", "scheduled_hour",
                   "recent_user_minutes", "fallback_hour"}
        if set(schedule) - allowed:
            raise ValidationError("proactive_chat contains unsupported policy fields")
        def integer(name, default, lower, upper):
            value = schedule.get(name, default)
            if type(value) is not int or not lower <= value <= upper:
                raise ValidationError("proactive_chat." + name + " must be an integer from "
                                      + str(lower) + " to " + str(upper))
            return value
        offset = integer("timezone_offset_minutes", 0, -720, 840)
        start = integer("start_hour", 0, 0, 23)
        end = integer("end_hour_exclusive", 24, 1, 24)
        if start >= end:
            raise ValidationError("proactive_chat.start_hour must precede end_hour_exclusive")
        slot_min = integer("scheduled_hour_min", start, start, end - 1)
        slot_max = integer("scheduled_hour_max", end - 1, slot_min, end - 1)
        fallback = integer("fallback_hour", end, start, end)
        recent = integer("recent_user_minutes", 45, 0, 1440)
        sign = "+" if offset >= 0 else "-"
        hours, minutes = divmod(abs(offset), 60)
        policy.update(timezone=f"UTC{sign}{hours:02d}:{minutes:02d}",
                      timezone_offset_minutes=offset, start_hour=start, end_hour_exclusive=end,
                      scheduled_hour_min=slot_min, scheduled_hour_max=slot_max,
                      fallback_hour=fallback, recent_user_minutes=recent)
        if "scheduled_hour" in schedule:
            hour = integer("scheduled_hour", start, start, end - 1)
            policy.update(scheduled_hour=hour, scheduled_hour_min=hour, scheduled_hour_max=hour)
        return policy

    def _status(self, con):
        latest = con.execute(
            "SELECT * FROM proactive_attempts ORDER BY local_day DESC LIMIT 1").fetchone()
        return {**self._config(con), "policy": self._policy(),
                "latest_attempt": dict(latest) if latest else None,
                "attempt_count": con.execute("SELECT COUNT(*) FROM proactive_attempts").fetchone()[0]}

    def status(self):
        with self._connect() as con:
            con.execute("BEGIN")
            return self._status(con)

    def observe_activity(self, *, thread_id, at=None):
        """Save a verified ordinary user's timestamp, never their message text."""
        thread_id = _identity(thread_id, "thread_id")
        stamp = _time(at, "at", default_now=True)
        if stamp > datetime.now(timezone.utc):
            raise ValidationError("at cannot be a future user activity timestamp")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not self._config(con)["enabled"]:
                return {"thread_id": thread_id, "last_user_at": None,
                        "changed": False, "reason": "disabled"}
            row = con.execute("SELECT last_user_at FROM proactive_activity WHERE thread_id=?", (thread_id,)).fetchone()
            changed = row is None or stamp > _time(row["last_user_at"], "last_user_at")
            if changed:
                con.execute("INSERT INTO proactive_activity(thread_id,last_user_at) VALUES(?,?) "
                            "ON CONFLICT(thread_id) DO UPDATE SET last_user_at=excluded.last_user_at",
                            (thread_id, stamp.isoformat()))
            return {"thread_id": thread_id, "last_user_at": stamp.isoformat() if changed else row["last_user_at"],
                    "changed": changed}

    def configure(self, *, source, enabled=None, paused_until=_UNCHANGED):
        """Apply explicit consent/pause changes; omitted fields remain unchanged."""
        source = _text(source, "proactive configuration source", 3000)
        if enabled is not None and type(enabled) is not bool:
            raise ValidationError("enabled must be a boolean")
        if paused_until is not _UNCHANGED and paused_until is not None:
            paused_until = _time(paused_until, "paused_until").isoformat()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = self._config(con)
            consent = current["enabled"] if enabled is None else enabled
            pause = current["paused_until"] if paused_until is _UNCHANGED else paused_until
            con.execute("""
                INSERT INTO proactive_config(singleton,enabled,paused_until,source,updated_at)
                VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
                    enabled=excluded.enabled,paused_until=excluded.paused_until,
                    source=excluded.source,updated_at=excluded.updated_at
            """, (int(consent), pause, source, datetime.now(timezone.utc).isoformat()))
            return self._status(con)

    @staticmethod
    def _inputs(*, now, current_thread_id, daily_thread_id, last_user_at, busy):
        stamp = _time(now, "now", default_now=True)
        if type(busy) is not bool:
            raise ValidationError("busy must be a boolean")
        current = None if current_thread_id is None or current_thread_id == "" else _identity(current_thread_id, "current_thread_id")
        daily = None if daily_thread_id is None or daily_thread_id == "" else _identity(daily_thread_id, "daily_thread_id")
        user_stamp = _time(last_user_at, "last_user_at") if last_user_at is not None else None
        return stamp, current, daily, user_stamp, busy

    def _evaluate(self, con, stamp, current, daily, user_stamp, busy):
        policy = self._policy()
        local = stamp.astimezone(timezone(timedelta(minutes=policy["timezone_offset_minutes"])))
        day = local.date().isoformat()
        scheduled_hour = policy.get("scheduled_hour", policy["scheduled_hour_min"]
            + local.date().toordinal() % (policy["scheduled_hour_max"] - policy["scheduled_hour_min"] + 1))
        config = self._config(con)
        if current:
            row = con.execute("SELECT last_user_at FROM proactive_activity WHERE thread_id=?", (current,)).fetchone()
            if row is not None:
                observed_stamp = _time(row["last_user_at"], "last_user_at")
                user_stamp = observed_stamp if user_stamp is None else max(user_stamp, observed_stamp)
        result = {"eligible": False, "prepared": False, "reason": "disabled",
                  "local_day": day, "local_time": local.isoformat(),
                  "scheduled_hour": scheduled_hour, "policy": policy, "attempt": None}
        if not config["enabled"]:
            return result
        if not current or not daily:
            result["reason"] = "missing_daily_entry"
        elif current != daily:
            result["reason"] = "daily_entry_mismatch"
        elif config["paused_until"] and stamp < _time(config["paused_until"], "paused_until"):
            result["reason"] = "paused"
        elif not policy["start_hour"] <= local.hour < policy["end_hour_exclusive"]:
            result["reason"] = "outside_window"
        else:
            previous = con.execute("SELECT * FROM proactive_attempts WHERE local_day=?", (day,)).fetchone()
            if previous is not None:
                result.update(reason="already_prepared", attempt=dict(previous))
            elif busy:
                result["reason"] = "busy"
            elif local.hour < scheduled_hour:
                result["reason"] = "before_daily_slot"
            elif user_stamp is not None and user_stamp > stamp:
                result["reason"] = "future_user_activity"
            elif (local.hour < policy["fallback_hour"] and user_stamp is not None
                    and stamp - user_stamp < timedelta(minutes=policy["recent_user_minutes"])):
                result["reason"] = "recent_user_activity"
            else:
                result.update(eligible=True, reason="daily_target_due")
        return result

    def evaluate(self, *, current_thread_id, daily_thread_id, now=None, last_user_at=None, busy=False):
        """Read gates without counting ordinary replies or reserving today's turn."""
        inputs = self._inputs(now=now, current_thread_id=current_thread_id,
                              daily_thread_id=daily_thread_id, last_user_at=last_user_at, busy=busy)
        with self._connect() as con:
            con.execute("BEGIN")
            return self._evaluate(con, *inputs)

    def claim(self, *, current_thread_id, daily_thread_id, now=None, last_user_at=None, busy=False):
        """Atomically reserve one prepared output, without asserting delivery."""
        inputs = self._inputs(now=now, current_thread_id=current_thread_id,
                              daily_thread_id=daily_thread_id, last_user_at=last_user_at, busy=busy)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            result = self._evaluate(con, *inputs)
            if not result["eligible"]:
                return result
            attempt = {"local_day": result["local_day"], "attempt_id": str(uuid.uuid4()),
                       "thread_id": inputs[1], "prepared_at": inputs[0].isoformat(), "status": "prepared"}
            con.execute("INSERT INTO proactive_attempts(local_day,attempt_id,thread_id,prepared_at,status) VALUES(?,?,?,?,?)",
                        tuple(attempt[name] for name in ("local_day", "attempt_id", "thread_id", "prepared_at", "status")))
            result.update(prepared=True, reason="prepared", attempt=attempt)
            return result
