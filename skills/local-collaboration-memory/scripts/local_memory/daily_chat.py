"""A reusable daily-chat entry pointing to an immutable native session identity.

The A00 entry is a separate namespace from stable conversation codes. Updating
its pointer never renumbers a session, changes a host title or moves a project.
"""
from __future__ import annotations

from .session_registry import SessionRegistry, _identity, _stamp
from .store import ConflictError, ValidationError, _text


ENTRY_CODE = "A00"


class DailyChat:
    """Persist the daily entry and binding history beside the session registry."""

    def __init__(self, store):
        self.registry = SessionRegistry(store)
        with self.registry._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS daily_chat (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    session_seq INTEGER NOT NULL REFERENCES sessions(seq),
                    title TEXT NOT NULL, source TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>=1),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_chat_events (
                    revision INTEGER PRIMARY KEY CHECK(revision>=1),
                    previous_session_seq INTEGER REFERENCES sessions(seq),
                    new_session_seq INTEGER NOT NULL REFERENCES sessions(seq),
                    previous_title TEXT, new_title TEXT NOT NULL,
                    source TEXT NOT NULL, at TEXT NOT NULL
                );
            """)

    def _session(self, con, seq):
        if seq is None:
            return None
        row = con.execute(
            "SELECT seq,platform,host,thread_id,title FROM sessions WHERE seq=?",
            (seq,),
        ).fetchone()
        if row is None:
            raise ValidationError("Daily-chat entry references an unavailable registered session")
        return {
            "code": self.registry.code(row["seq"]),
            "thread_id": row["thread_id"],
            "platform": row["platform"],
            "host": row["host"],
            "observed_title": row["title"],
        }

    def _show(self, con):
        result = {
            "entry_kind": "daily-chat", "entry_code": ENTRY_CODE,
            "configured": False, "revision": 0, "current_session": None,
            "history": [],
        }
        row = con.execute("SELECT * FROM daily_chat WHERE singleton=1").fetchone()
        if row is None:
            return result
        result.update({
            "configured": True,
            "title": row["title"], "source": row["source"],
            "revision": row["revision"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "current_session": self._session(con, row["session_seq"]),
        })
        for event in con.execute("SELECT * FROM daily_chat_events ORDER BY revision"):
            result["history"].append({
                "revision": event["revision"],
                "previous_session": self._session(con, event["previous_session_seq"]),
                "new_session": self._session(con, event["new_session_seq"]),
                "previous_title": event["previous_title"],
                "new_title": event["new_title"],
                "source": event["source"], "at": event["at"],
            })
        return result

    def show(self):
        """Read a consistent entry snapshot; historical titles are evidence only."""
        with self.registry._connect() as con:
            con.execute("BEGIN")
            return self._show(con)

    def bind(self, *, thread_id, title, source, expected_revision,
             platform="codex", host="local"):
        """Associate an explicitly chosen, already registered native identity."""
        identity = tuple(_identity(value, label) for label, value in (
            ("platform", platform), ("host", host), ("thread_id", thread_id)))
        title = _text(title, "daily-chat title", 500)
        source = _text(source, "daily-chat binding source", 3000)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValidationError("expected_revision must be a nonnegative integer")
        with self.registry._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT * FROM daily_chat WHERE singleton=1").fetchone()
            revision = previous["revision"] if previous else 0
            if expected_revision != revision:
                raise ConflictError("Daily-chat entry changed; read its current revision before binding")
            target = con.execute(
                "SELECT seq FROM sessions WHERE platform=? AND host=? AND thread_id=?",
                identity,
            ).fetchone()
            if target is None:
                raise ValidationError("Register the verified native conversation before binding the daily-chat entry")
            if previous is None or previous["session_seq"] != target["seq"] or previous["title"] != title:
                stamp = _stamp()
                revision += 1
                con.execute("""
                    INSERT INTO daily_chat(singleton,session_seq,title,source,revision,created_at,updated_at)
                    VALUES(1,?,?,?,?,?,?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        session_seq=excluded.session_seq,title=excluded.title,source=excluded.source,
                        revision=excluded.revision,updated_at=excluded.updated_at
                """, (target["seq"], title, source, revision, stamp, stamp))
                con.execute("""
                    INSERT INTO daily_chat_events(revision,previous_session_seq,new_session_seq,
                                                  previous_title,new_title,source,at)
                    VALUES(?,?,?,?,?,?,?)
                """, (revision, previous["session_seq"] if previous else None, target["seq"],
                      previous["title"] if previous else None, title, source, stamp))
            result = self._show(con)
        result["sync"] = self.registry.sync()
        return result
