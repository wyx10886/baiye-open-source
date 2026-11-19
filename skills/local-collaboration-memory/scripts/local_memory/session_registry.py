"""Stable local conversation codes, independent of memory snapshot rollbacks.

Native identities, not editable titles, own codes. Bindings refer only to verified
project roots. This module never renames application chats or existing folders.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import hashlib
import uuid
from pathlib import Path
import re
import sqlite3

from .store import ConflictError, ValidationError, _text
from .retention import safe_path

MARKER_FILE = "会话与目录对应.md"
CODE_PATTERN = r"[A-Z]+[0-9]{2}"


def _identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,199}", value):
        raise ValidationError(label + " must be a verified native identifier")
    return value


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _md(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


class SessionRegistry:
    def __init__(self, store):
        self.store = store
        self.db = safe_path(store.home / "session-registry.sqlite3")
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL, host TEXT NOT NULL, thread_id TEXT NOT NULL,
                    title TEXT NOT NULL, source TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(platform, host, thread_id)
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    session_seq INTEGER NOT NULL REFERENCES sessions(seq),
                    slot INTEGER NOT NULL, project TEXT NOT NULL, root TEXT NOT NULL,
                    source TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(session_seq, project), UNIQUE(session_seq, slot)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_seq INTEGER NOT NULL, revision INTEGER NOT NULL,
                    operation TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retired_sessions (
                    seq INTEGER PRIMARY KEY,
                    platform TEXT NOT NULL, host TEXT NOT NULL, thread_id TEXT NOT NULL,
                    title TEXT NOT NULL, source TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    retirement_source TEXT NOT NULL, retired_at TEXT NOT NULL,
                    retirement_revision INTEGER NOT NULL,
                    UNIQUE(platform, host, thread_id)
                );
                CREATE TRIGGER IF NOT EXISTS sessions_reject_retired_identity
                BEFORE INSERT ON sessions
                WHEN EXISTS (
                    SELECT 1 FROM retired_sessions WHERE seq=NEW.seq OR
                    (platform=NEW.platform AND host=NEW.host AND thread_id=NEW.thread_id)
                )
                BEGIN
                    SELECT RAISE(ABORT, 'Retired conversation identity or code cannot be reused');
                END;
            """)
            settings_path = safe_path(store.home / "settings.json")
            if settings_path.exists():
                try:
                    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    raise ValidationError("Cannot read native allocation guard settings") from None
                if not isinstance(settings, dict):
                    raise ValidationError("Native allocation guard settings must be an object")
                if settings.get("verify_native_allocations") is True:
                    # Persist this guard so clients still running pre-fix Python
                    # cannot keep allocating unchecked codes. It is deliberately
                    # not dropped if a later settings file omits the opt-in flag.
                    con.executescript("""
                        CREATE TRIGGER IF NOT EXISTS sessions_verify_local_main
                        BEFORE INSERT ON sessions
                        WHEN lower(NEW.platform)='codex' AND lower(NEW.host)='local'
                        BEGIN
                            SELECT CASE WHEN local_native_main(NEW.thread_id)!=1
                                THEN RAISE(ABORT, 'Verified local main conversation required') END;
                        END;
                    """)

            if self._recycling_enabled() or self._has_recycling_schema(con):
                self._initialize_recycling(con)

    def _native_main(self, thread_id):
        """SQLite callback: only authoritative local main-chat evidence passes."""
        try:
            from .title_sync import native_identity
            return int(native_identity(self.store.home, thread_id)["status"] == "main")
        except Exception:
            # SQLite errors must not carry credentials, host data or exception text.
            return 0

    @contextmanager
    def _connect(self):
        safe_path(self.db)
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        con.create_function("local_native_main", 1, self._native_main)
        con.create_function("local_registry_protocol", 0, lambda: 2)
        con.create_function("local_registry_allocation", 4, self._allocation_allowed)
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _recycling_settings(self):
        path = safe_path(self.store.home / "settings.json")
        if not path.exists():
            return {}
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise ValidationError("Cannot read session recycling settings") from None
        if not isinstance(settings, dict):
            raise ValidationError("Session settings must be an object")
        policy = settings.get("session_recycling", {})
        if not isinstance(policy, dict):
            raise ValidationError("session_recycling must be an object")
        roots = policy.get("workspace_roots")
        if roots is not None and (not isinstance(roots, list) or not all(isinstance(x, str) and x for x in roots)):
            raise ValidationError("workspace_roots must contain paths")
        return policy

    def _recycling_enabled(self):
        return self._recycling_settings().get("enabled") is True

    @staticmethod
    def _has_recycling_schema(con):
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_slot_state'").fetchone() is not None

    def _allocation_allowed(self, seq, platform, host, native_id):
        return int(getattr(self, "_allocation_ticket", None) == (seq, platform, host, native_id))

    def _initialize_recycling(self, con):
        """Migrate history without renumbering occupied slots; old writers fail closed."""
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS session_slot_state (seq INTEGER PRIMARY KEY, generation INTEGER NOT NULL, last_revision INTEGER NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS session_archives (
            archive_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, generation INTEGER NOT NULL,
            platform TEXT NOT NULL, host TEXT NOT NULL, thread_id TEXT NOT NULL,
            row_data TEXT NOT NULL, events_data TEXT NOT NULL, release_source TEXT NOT NULL,
            released_at TEXT NOT NULL, scan_id TEXT NOT NULL,
            UNIQUE(seq,generation), UNIQUE(platform,host,thread_id))""")
        # Create the exact existing daily schema before installing write guards;
        # subsequent DailyChat initialisation remains CREATE IF NOT EXISTS only.
        con.execute("""CREATE TABLE IF NOT EXISTS daily_chat (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            session_seq INTEGER NOT NULL REFERENCES sessions(seq), title TEXT NOT NULL, source TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS daily_chat_events (
            revision INTEGER PRIMARY KEY CHECK(revision>=1), previous_session_seq INTEGER REFERENCES sessions(seq),
            new_session_seq INTEGER NOT NULL REFERENCES sessions(seq), previous_title TEXT,
            new_title TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL)""")
        for table in ("sessions", "retired_sessions"):
            for row in con.execute("SELECT seq,revision FROM " + table).fetchall():
                con.execute("INSERT OR IGNORE INTO session_slot_state VALUES(?,1,?)", (row["seq"], row["revision"]))
        # Legacy retired rows have already passed zero-dependency checks. Move
        # their complete row and event generation out of ambiguous seq-only views.
        for row in con.execute("SELECT * FROM retired_sessions ORDER BY seq").fetchall():
            self._archive_row(con, row, "retired_sessions", source="Preserved legacy retirement during generation migration", scan_id="schema-v2-migration")
        con.execute("DROP TRIGGER IF EXISTS sessions_reject_retired_identity")
        con.execute("""CREATE TRIGGER sessions_reject_retired_identity BEFORE INSERT ON sessions
            WHEN EXISTS(SELECT 1 FROM retired_sessions WHERE platform=NEW.platform AND host=NEW.host AND thread_id=NEW.thread_id)
              OR EXISTS(SELECT 1 FROM session_archives WHERE platform=NEW.platform AND host=NEW.host AND thread_id=NEW.thread_id)
            BEGIN SELECT RAISE(ABORT,'Historical conversation identity cannot re-enter a reused slot'); END""")
        for table in ("sessions", "events", "bindings", "retired_sessions", "daily_chat", "daily_chat_events", "session_slot_state", "session_archives"):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = "registry_v2_" + table + "_" + operation.lower()
                con.execute("CREATE TRIGGER IF NOT EXISTS " + name + " BEFORE " + operation + " ON " + table +
                    " BEGIN SELECT CASE WHEN local_registry_protocol()!=2 THEN RAISE(ABORT,'Registry writer reload required') END; END")
        con.execute("""CREATE TRIGGER IF NOT EXISTS sessions_recycling_allocation BEFORE INSERT ON sessions
            BEGIN SELECT CASE WHEN local_registry_allocation(NEW.seq,NEW.platform,NEW.host,NEW.thread_id)!=1
                THEN RAISE(ABORT,'Allocation requires a verified occupancy scan transaction') END; END""")
        con.execute("""CREATE TRIGGER IF NOT EXISTS sessions_verify_local_main BEFORE INSERT ON sessions
            WHEN lower(NEW.platform)='codex' AND lower(NEW.host)='local'
            BEGIN SELECT CASE WHEN local_native_main(NEW.thread_id)!=1
                THEN RAISE(ABORT,'Verified local main conversation required') END; END""")
        con.commit()

    def _archive_row(self, con, row, table, *, source, scan_id):
        if table not in ("sessions", "retired_sessions"):
            raise ValidationError("Unsupported archive source")
        seq = row["seq"]
        state = con.execute("SELECT * FROM session_slot_state WHERE seq=?", (seq,)).fetchone()
        generation = state["generation"] if state else 1
        events = [dict(x) for x in con.execute("SELECT * FROM events WHERE session_seq=? ORDER BY id", (seq,))]
        con.execute("INSERT INTO session_archives VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            str(uuid.uuid4()), seq, generation, row["platform"], row["host"], row["thread_id"],
            json.dumps(dict(row),ensure_ascii=False), json.dumps(events,ensure_ascii=False), source, _stamp(), scan_id))
        con.execute("INSERT INTO session_slot_state VALUES(?,?,?) ON CONFLICT(seq) DO UPDATE SET last_revision=MAX(last_revision,excluded.last_revision)",
                    (seq, generation, max([row["revision"], *[x["revision"] for x in events]])))
        con.execute("DELETE FROM events WHERE session_seq=?", (seq,))
        con.execute("DELETE FROM " + table + " WHERE seq=?", (seq,))

    def _scan_occupancy(self):
        from .occupancy import scan_occupancy
        return scan_occupancy(self.store.home, workspace_roots=self._recycling_settings().get("workspace_roots"))

    def _slot_dependencies(self, con, seq):
        reasons = []
        if con.execute("SELECT 1 FROM bindings WHERE session_seq=? LIMIT 1", (seq,)).fetchone():
            reasons.append("project_binding")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "daily_chat" in tables and con.execute("SELECT 1 FROM daily_chat WHERE session_seq=?",(seq,)).fetchone():
            reasons.append("daily_chat_current")
        if "daily_chat_events" in tables and con.execute("SELECT 1 FROM daily_chat_events WHERE previous_session_seq=? OR new_session_seq=? LIMIT 1",(seq,seq)).fetchone():
            reasons.append("daily_chat_history")
        return reasons

    def _recycling_plan(self, con, occupancy):
        if not isinstance(occupancy, dict):
            raise ValidationError("Invalid occupancy scan report")
        complete = occupancy.get("complete") is True
        live = occupancy.get("live_thread_ids", [])
        thread_codes, directory_codes = occupancy.get("thread_codes", {}), occupancy.get("directory_codes", {})
        if not isinstance(live,list) or not all(isinstance(x,str) for x in live) or not all(isinstance(x,dict) for x in (thread_codes,directory_codes)):
            raise ValidationError("Invalid occupancy scan fields")
        live = set(live)
        external = set()
        canonical_maps = []
        for mapping in (thread_codes,directory_codes):
            normal = {}
            for code, values in mapping.items():
                if not isinstance(values,list) or not all(isinstance(x,str) for x in values):
                    raise ValidationError("Invalid occupancy evidence")
                external.add(self._sequence(code))
                normal[code] = sorted(set(values))
            canonical_maps.append(normal)
        rows = [dict(row) for row in con.execute("SELECT * FROM sessions ORDER BY seq")]
        slots = [dict(row) for row in con.execute("SELECT * FROM session_slot_state ORDER BY seq")] if self._has_recycling_schema(con) else []
        slot_map = {row["seq"]:row for row in slots}
        releasable, blocked, identities = [], [], []
        enabled = self._recycling_enabled()
        for row in rows:
            code = self.code(row["seq"])
            reasons = self._slot_dependencies(con,row["seq"])
            if row["seq"] == 1:
                reasons.append("A00_reserved")
            if row["platform"] != "codex" or row["host"] != "local":
                reasons.append("external_host_or_platform")
            if row["thread_id"] in live:
                reasons.append("existing_main_chat")
            if thread_codes.get(code):
                reasons.append("host_title_occupancy")
            if directory_codes.get(code):
                reasons.append("directory_occupancy")
            if not complete:
                reasons.append("incomplete_scan")
            item = {"code":code,"thread_id":row["thread_id"],"revision":row["revision"],
                    "generation":slot_map.get(row["seq"],{}).get("generation",1)}
            identities.append(dict(row,dependencies=sorted(reasons),generation=item["generation"]))
            if reasons:
                blocked.append(dict(item,reasons=sorted(set(reasons))))
            elif enabled:
                releasable.append(item)
        archived_rows = [dict(row) for row in con.execute("SELECT seq,generation,platform,host,thread_id FROM session_archives ORDER BY seq,generation")] if self._has_recycling_schema(con) else []
        archived_occupied = set()
        for old in archived_rows:
            if old["platform"] != "codex" or old["host"] != "local" or old["thread_id"] in live:
                archived_occupied.add(old["seq"])
                blocked.append({"code":self.code(old["seq"]),"thread_id":old["thread_id"],
                    "generation":old["generation"],"reasons":["historical_owner_still_live_or_external"]})
        protected_releases = [item for item in releasable if self._sequence(item["code"]) in archived_occupied]
        blocked.extend(dict(item,reasons=["historical_owner_still_live_or_external"]) for item in protected_releases)
        releasable = [item for item in releasable if self._sequence(item["code"]) not in archived_occupied]
        used = {1,*external,*archived_occupied}
        release_codes = {x["code"] for x in releasable}
        used.update(row["seq"] for row in rows if self.code(row["seq"]) not in release_codes)
        next_seq = 2
        while next_seq in used:
            next_seq += 1
        stable = {"complete":complete,"enabled":enabled,"live_thread_ids":sorted(live),
                  "thread_codes":canonical_maps[0],"directory_codes":canonical_maps[1],
                  "workspace_roots":sorted(occupancy.get("workspace_roots",[])),"rows":identities,"slots":slots,"archive_identities":archived_rows}
        scan_id = hashlib.sha256(json.dumps(stable,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        return {"enabled":enabled,"complete":complete,"scan_id":scan_id,
                "next_code":self.code(next_seq) if complete and enabled else None,
                "releasable":releasable,"blocked":blocked,"occupancy":occupancy}

    def recycling_scan(self):
        occupancy = self._scan_occupancy()
        with self._connect() as con:
            con.execute("BEGIN")
            return self._recycling_plan(con,occupancy)

    @staticmethod
    def _archive_view(row):
        original = json.loads(row["row_data"])
        result = dict(original,archive_id=row["archive_id"],code=SessionRegistry.code(row["seq"]),
            generation=row["generation"],archived=True,released=True,retired=True,
            release_source=row["release_source"],released_at=row["released_at"],scan_id=row["scan_id"],
            events=json.loads(row["events_data"]),bindings=[],suggested_title=None,title_needs_sync=False)
        return result

    def recycling_history(self, *, code=None, thread_id=None):
        if code is not None:
            seq = self._sequence(code)
        if thread_id is not None:
            thread_id = _identity(thread_id,"thread_id")
        with self._connect() as con:
            if not self._has_recycling_schema(con):
                return []
            clauses,args = [],[]
            if code is not None:
                clauses.append("seq=?");args.append(seq)
            if thread_id is not None:
                clauses.append("thread_id=?");args.append(thread_id)
            sql="SELECT * FROM session_archives"+(" WHERE "+" AND ".join(clauses) if clauses else "")+" ORDER BY seq,generation"
            return [self._archive_view(row) for row in con.execute(sql,args)]

    def _release_plan(self, con, plan, *, source):
        released=[]
        for item in plan["releasable"]:
            seq=self._sequence(item["code"])
            row=con.execute("SELECT * FROM sessions WHERE seq=?",(seq,)).fetchone()
            if seq==1 or row is None or row["revision"]!=item["revision"] or row["thread_id"]!=item["thread_id"] or self._slot_dependencies(con,seq):
                raise ConflictError("Conversation occupancy changed before release")
            self._archive_row(con,row,"sessions",source=source,scan_id=plan["scan_id"])
            archived=con.execute("SELECT * FROM session_archives WHERE seq=? ORDER BY generation DESC LIMIT 1",(seq,)).fetchone()
            released.append(self._archive_view(archived))
        return released

    def recycling_reclaim(self, *, source, expected_scan_id):
        source=_text(source,"recycling source",3000)
        expected_scan_id=_text(expected_scan_id,"expected_scan_id",128)
        if not source.strip() or not self._recycling_enabled():
            raise ValidationError("Recycling must be enabled with an actual authorization source")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            occupancy=self._scan_occupancy()
            plan=self._recycling_plan(con,occupancy)
            if not plan["complete"]:
                raise ValidationError("Incomplete occupancy scan; no conversation was released")
            if plan["scan_id"]!=expected_scan_id:
                raise ConflictError("Occupancy changed; inspect a new scan before release")
            released=self._release_plan(con,plan,source=source)
            after=self._recycling_plan(con,occupancy)
        return {"released":released,"scan":after,"sync":self.sync()}

    @staticmethod
    def code(seq: int) -> str:
        if type(seq) is not int or seq < 1:
            raise ValidationError("Sequence must be a positive integer")
        letters, digits = divmod(seq - 1, 100)
        letters += 1
        prefix = ""
        while letters:
            letters, remainder = divmod(letters - 1, 26)
            prefix = chr(65 + remainder) + prefix
        return f"{prefix}{digits:02d}"

    @staticmethod
    def _sequence(code: str) -> int:
        if not isinstance(code, str) or len(code) > 14 or not re.fullmatch(CODE_PATTERN, code):
            raise ValidationError("Expected uppercase letters plus exactly two decimal digits, such as A00")
        letters = 0
        for letter in code[:-2]:
            letters = letters * 26 + ord(letter) - 64
        seq = (letters - 1) * 100 + int(code[-2:]) + 1
        if seq > 2**63 - 1:
            raise ValidationError("Code exceeds the storage sequence range")
        return seq

    @staticmethod
    def prefixed_title(code: str, title: str) -> str:
        SessionRegistry._sequence(code)
        title = _text(title, "observed title", 500)
        # Remove only our reserved leading code syntax; preserve all free text.
        title = re.sub(r"^(?:\[[A-Z]+\d{2}\]\s*)+", "", title).strip()
        return f"[{code}] {title}" if title else f"[{code}]"

    def _view(self, con, row):
        result = dict(row)
        result["code"] = self.code(row["seq"])
        if self._has_recycling_schema(con):
            slot = con.execute("SELECT generation FROM session_slot_state WHERE seq=?",(row["seq"],)).fetchone()
            result["generation"] = slot[0] if slot else 1
        result["suggested_title"] = self.prefixed_title(result["code"], row["title"]) if row["title"] else None
        result["title_needs_sync"] = bool(row["title"] and result["suggested_title"] != row["title"])
        result["bindings"] = [dict(b, directory_code=f"{result['code']}-{b['slot']}") for b in con.execute(
            "SELECT slot,project,root,source,created_at FROM bindings WHERE session_seq=? ORDER BY slot", (row["seq"],))]
        if "retired_at" in result:
            result.update(retired=True, suggested_title=None, title_needs_sync=False)
        return result

    def list(self, *, query="", project=None):
        if not isinstance(query, str) or len(query) > 1000:
            raise ValidationError("query must be short text")
        with self._connect() as con:
            rows = [self._view(con, r) for r in con.execute("SELECT * FROM sessions ORDER BY seq")]
        if project:
            rows = [r for r in rows if any(b["project"] == project for b in r["bindings"])]
        if query:
            rows = [r for r in rows if query.casefold() in json.dumps(r, ensure_ascii=False).casefold()]
        return rows

    def project_links(self, project):
        """Only this project's association; omit other projects and private paths."""
        return [{"code": row["code"], "title": row["title"], "directory_code": b["directory_code"],
                 "platform": row["platform"], "host": row["host"], "thread_id": row["thread_id"]}
                for row in self.list(project=project) for b in row["bindings"] if b["project"] == project]

    def show(self, code=None, *, platform="codex", host="local", thread_id=None):
        with self._connect() as con:
            if code is not None:
                row = con.execute("SELECT * FROM sessions WHERE seq=?", (self._sequence(code),)).fetchone()
                if row is None:
                    row = con.execute("SELECT * FROM retired_sessions WHERE seq=?", (self._sequence(code),)).fetchone()
            else:
                identity = tuple(_identity(v, k) for k, v in (("platform", platform), ("host", host), ("thread_id", thread_id)))
                row = con.execute("SELECT * FROM sessions WHERE platform=? AND host=? AND thread_id=?", identity).fetchone()
                if row is None:
                    row = con.execute("SELECT * FROM retired_sessions WHERE platform=? AND host=? AND thread_id=?", identity).fetchone()
            if row is None and self._has_recycling_schema(con):
                if code is not None:
                    archived=con.execute("SELECT * FROM session_archives WHERE seq=? ORDER BY generation DESC LIMIT 1",(self._sequence(code),)).fetchone()
                else:
                    archived=con.execute("SELECT * FROM session_archives WHERE platform=? AND host=? AND thread_id=?",identity).fetchone()
                if archived is not None:
                    return self._archive_view(archived)
            if row is None:
                raise ValidationError("Conversation has not been registered")
            return self._view(con, row)

    def ensure(self, *, thread_id, source, title="", platform="codex", host="local"):
        """Allocate a verified identity; recycling is opt-in and scans fail closed."""
        identity = tuple(_identity(v,k) for k,v in (("platform",platform),("host",host),("thread_id",thread_id)))
        platform,host,thread_id = identity
        source = _text(source,"source",3000)
        title = _text(title,"title",500) if title else ""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT * FROM sessions WHERE platform=? AND host=? AND thread_id=?",identity).fetchone()
            if row is None:
                if con.execute("SELECT 1 FROM retired_sessions WHERE platform=? AND host=? AND thread_id=?",identity).fetchone():
                    raise ValidationError("Conversation registration was retired; inspect its history before reuse")
                migrated=self._has_recycling_schema(con)
                if migrated and con.execute("SELECT 1 FROM session_archives WHERE platform=? AND host=? AND thread_id=?",identity).fetchone():
                    raise ValidationError("This historical native identity was released; do not reclaim its reused code automatically")
                stamp=_stamp()
                if self._recycling_enabled():
                    occupancy=self._scan_occupancy()
                    plan=self._recycling_plan(con,occupancy)
                    if not plan["complete"]:
                        raise ValidationError("Incomplete occupancy scan; no code was allocated")
                    if platform == "codex" and host == "local" and thread_id not in occupancy.get("live_thread_ids",[]):
                        raise ValidationError("New local main chat is absent from the complete occupancy scan; retry after host metadata catches up")
                    claims=[code for code,ids in occupancy.get("thread_codes",{}).items() if thread_id in ids]
                    if claims:
                        if len(claims)!=1:
                            raise ConflictError("Existing chat has conflicting numbered titles; inspect before registration")
                        seq=self._sequence(claims[0])
                        ids=occupancy["thread_codes"][claims[0]]
                        if (seq==1 or any(x!=thread_id for x in ids)
                                or con.execute("SELECT 1 FROM sessions WHERE seq=?",(seq,)).fetchone()
                                or con.execute("SELECT 1 FROM session_archives WHERE seq=?",(seq,)).fetchone()
                                or self._slot_dependencies(con,seq)):
                            raise ConflictError("Existing numbered chat conflicts with another owner or protected history")
                    else:
                        seq=self._sequence(plan["next_code"])
                    self._release_plan(con,plan,source="Automatic zero-occupancy release under user's recycling policy; "+source)
                    slot=con.execute("SELECT * FROM session_slot_state WHERE seq=?",(seq,)).fetchone()
                    generation=slot["generation"]+1 if slot else 1
                    revision=slot["last_revision"]+1 if slot else 1
                elif migrated:
                    # Disabling reuse keeps the protocol protection but returns to
                    # append-only allocation; it never resets SQLite's high-water.
                    high=con.execute("SELECT seq FROM sqlite_sequence WHERE name='sessions'").fetchone()
                    maximum=con.execute("SELECT COALESCE(MAX(seq),0) FROM session_slot_state").fetchone()[0]
                    seq=max(high[0] if high else 0,maximum,1)+1
                    generation,revision=1,1
                else:
                    seq=con.execute("INSERT INTO sessions(platform,host,thread_id,title,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (*identity,title,source,stamp,stamp)).lastrowid
                    revision=1
                if migrated:
                    self._allocation_ticket=(seq,platform,host,thread_id)
                    try:
                        con.execute("INSERT INTO sessions(seq,platform,host,thread_id,title,source,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (seq,*identity,title,source,revision,stamp,stamp))
                    finally:
                        self._allocation_ticket=None
                    con.execute("INSERT INTO session_slot_state VALUES(?,?,?) ON CONFLICT(seq) DO UPDATE SET generation=excluded.generation,last_revision=excluded.last_revision",
                        (seq,generation,revision))
                con.execute("INSERT INTO events(session_seq,revision,operation,source,at) VALUES(?,?,'allocate',?,?)",(seq,revision,source,stamp))
                row=con.execute("SELECT * FROM sessions WHERE seq=?",(seq,)).fetchone()
            result=self._view(con,row)
        result["sync"]=self.sync()
        return result

    def retire(self, *, code, source, expected_revision):
        """Legacy retirement preserves numbers; opt-in v2 archives a verified empty slot."""
        seq=self._sequence(code)
        source=_text(source,"retirement source",3000)
        if not source.strip():
            raise ValidationError("Retirement requires its verified authorization source")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT * FROM sessions WHERE seq=?",(seq,)).fetchone()
            if row is None:
                raise ValidationError("Conversation is unavailable or already retired")
            self._revision(row,expected_revision)
            if self._slot_dependencies(con,seq):
                raise ConflictError("Conversation has project or daily-chat dependencies and cannot be retired")
            if self._has_recycling_schema(con):
                if seq==1:
                    raise ConflictError("A00 is permanently reserved")
                plan=self._recycling_plan(con,self._scan_occupancy())
                if not plan["complete"] or not any(x["code"]==code for x in plan["releasable"]):
                    raise ConflictError("Conversation must have no live chat or directory occupancy before retirement")
                con.execute("INSERT INTO events(session_seq,revision,operation,source,at) VALUES(?,?,'retire',?,?)",
                    (seq,row["revision"]+1,source,_stamp()))
                self._archive_row(con,row,"sessions",source=source,scan_id=plan["scan_id"])
            else:
                stamp=_stamp()
                con.execute("""INSERT INTO retired_sessions(seq,platform,host,thread_id,title,source,revision,
                    created_at,updated_at,retirement_source,retired_at,retirement_revision)
                    SELECT seq,platform,host,thread_id,title,source,revision,created_at,updated_at,?,?,revision+1
                    FROM sessions WHERE seq=?""",(source,stamp,seq))
                con.execute("DELETE FROM sessions WHERE seq=?",(seq,))
                con.execute("INSERT INTO events(session_seq,revision,operation,source,at) VALUES(?,?,'retire',?,?)",
                    (seq,row["revision"]+1,source,stamp))
        result=self.show(code)
        result["sync"]=self.sync()
        return result

    @staticmethod
    def _revision(row, expected_revision):
        if type(expected_revision) is not int or expected_revision != row["revision"]:
            raise ConflictError("Conversation changed; read its current revision before updating")

    def observe_title(self, *, code, title, source, expected_revision):
        """Record a title actually read from the host; does not claim an app rename."""
        title = _text(title, "observed title", 500)
        source = _text(source, "source", 3000)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM sessions WHERE seq=?", (self._sequence(code),)).fetchone()
            if row is None:
                raise ValidationError("Unknown conversation code")
            self._revision(row, expected_revision)
            if row["title"] != title:
                stamp = _stamp()
                con.execute("UPDATE sessions SET title=?,source=?,revision=revision+1,updated_at=? WHERE seq=?", (title, source, stamp, row["seq"]))
                con.execute("INSERT INTO events(session_seq,revision,operation,source,at) VALUES(?,?,'observe-title',?,?)", (row["seq"], row["revision"]+1, source, stamp))
        result = self.show(code)
        result["sync"] = self.sync()
        return result

    def bind(self, *, code, project, source, expected_revision):
        from .work_summary import WorkSummary
        # The overview provides independently verified identity and directory.
        bound = next((p for p in WorkSummary(self.store).projects() if p["project"] == project), None)
        if not bound:
            raise ValidationError("Initialize a verified project overview before binding a conversation")
        root = safe_path(Path(bound["root"]))
        if not root.is_dir() or root == self.store.home or self.store.home in root.parents:
            raise ValidationError("Project root is unavailable or private")
        source = _text(source, "binding source", 3000)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM sessions WHERE seq=?", (self._sequence(code),)).fetchone()
            if row is None:
                raise ValidationError("Unknown conversation code")
            self._revision(row, expected_revision)
            old = con.execute("SELECT * FROM bindings WHERE session_seq=? AND project=?", (row["seq"], project)).fetchone()
            if old and old["root"] != str(root):
                raise ConflictError("Bound directory moved; review dependencies before rebinding")
            if old is None:
                slot = con.execute("SELECT COALESCE(MAX(slot),0)+1 FROM bindings WHERE session_seq=?", (row["seq"],)).fetchone()[0]
                stamp = _stamp()
                con.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?)", (row["seq"], slot, project, str(root), source, stamp))
                con.execute("UPDATE sessions SET revision=revision+1,updated_at=? WHERE seq=?", (stamp, row["seq"]))
                con.execute("INSERT INTO events(session_seq,revision,operation,source,at) VALUES(?,?,'bind-project',?,?)", (row["seq"], row["revision"]+1, source, stamp))
        result = self.show(code)
        result["sync"] = self.sync(project=project)
        return result

    def title_plan(self, *, code, title):
        row = self.show(code)
        if row.get("retired"):
            raise ValidationError("Retired conversation cannot be renamed")
        return {"code": code, "platform": row["platform"], "host": row["host"], "thread_id": row["thread_id"],
                "observed_title": title, "suggested_title": self.prefixed_title(code, title),
                "application_renamed": False, "revision": row["revision"]}

    def directory_plan(self, *, code, parent, name):
        """Preview a new task name; caller creates/initializes/binds, checking revision."""
        row = self.show(code)
        if row.get("retired"):
            raise ValidationError("Retired conversation cannot receive directory tasks")
        name = _text(name, "directory name", 150)
        if (re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " "))
                or name in {".", ".."} or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name)):
            raise ValidationError("Use a plain descriptive directory name, without reserved path characters")
        if not isinstance(parent, str) or not Path(parent).expanduser().is_absolute():
            raise ValidationError("Parent must be an absolute path")
        base = safe_path(Path(parent))
        if not base.is_absolute() or not base.is_dir() or base == self.store.home or self.store.home in base.parents:
            raise ValidationError("Parent must be a verified existing workspace directory")
        slot = max((b["slot"] for b in row["bindings"]), default=0) + 1
        label = f"{code}-{slot}"
        target = safe_path(base / (label + "_" + name))
        if target.exists():
            raise ConflictError("Planned directory already exists; inspect and reuse the existing task")
        return {"code": code, "directory_code": label, "path": str(target), "planned_only": True,
                "expected_revision": row["revision"], "next_step": "Verify task identity, create this directory, initialize its project overview, then bind using expected_revision. On a revision conflict, recheck the plan; do not duplicate or move files automatically."}

    def export(self, *, project=None):
        """Hash-check generated files and preserve their manual notes."""
        from .work_summary import WorkSummary
        from .integration import _atomic
        changed, files = [], []
        with self.store._exclusive():
            rows = self.list()
            index = self.store.home / "会话与目录索引.md"
            header = "# 会话与目录索引\n\n编号不随标题变化；目录子编号固定。此表含本机私有路径。\n\n"
            from .daily_chat import DailyChat
            daily = DailyChat(self.store).show()
            lines = [header, "## 日常聊天专用入口\n"]
            if daily["configured"]:
                target = daily["current_session"]
                lines += [f"**入口 {daily['entry_code']} · {_md(daily['title'])}**\n",
                          f"当前关联真实会话：{target['code']}；平台：{target['platform']}；主机：{target['host']}\n",
                          f"真实会话ID：{target['thread_id']}\n",
                          f"入口修订：{daily['revision']}；关联依据：{_md(daily['source'])}\n",
                          "此入口可明确换绑；不改写会话固定编号。下方A00仍是历史原会话。入口记录不代表宿主会话仍存在。\n"]
            else:
                lines.append("尚未关联日常聊天；不会根据标题自动接管会话。\n")
            lines += ["## 固定会话编号台账\n", "| 编号 | 当前已观察标题 | 平台 / 主机 | 目录编号与实际路径 |", "| --- | --- | --- | --- |"]
            for row in rows:
                paths = "; ".join(f"{b['directory_code']} → {b['root']}" for b in row["bindings"]) or "待核实绑定"
                lines.append(f"| {row['code']} | {_md(row['title'] or '尚未读取标题')} | {_md(row['platform'])} / {_md(row['host'])} | {_md(paths)} |")
            prepared = [(index, WorkSummary._prepare(index, "\n".join(lines)+"\n", "session-registry", "sessions"))]
            bound_projects = WorkSummary(self.store).projects()
            for binding in bound_projects:
                if project and binding["project"] != project:
                    continue
                related = [(r, b) for r in rows for b in r["bindings"] if b["project"] == binding["project"]]
                if not related:
                    continue
                root = safe_path(Path(binding["root"]))
                if any(b["root"] != str(root) for _, b in related):
                    raise ConflictError("Directory differs from registered root")
                if not root.is_dir():
                    raise ValidationError("Registered project directory is unavailable")
                marker = safe_path(root / "处理记录" / MARKER_FILE)
                body = [f"# {binding['title']} · 会话与目录对应\n", "固定编号跟随会话真实ID；以下为已核实关联，标题可以修改。\n"]
                for row, item in related:
                    body += [f"## {item['directory_code']}（会话 {row['code']}）\n", f"当前已观察标题：{row['title'] or '尚未读取'}\n",
                             f"平台：{row['platform']}；主机：{row['host']}；会话ID：{row['thread_id']}\n",
                             f"实际工作目录：{root}\n", f"关联依据：{item['source']}\n"]
                body.append("已有目录保留原路径。新建任务目录建议以目录编号加下划线开头；旧目录改名需先核实路径依赖。此文件长期保留。\n")
                prepared.append((marker, WorkSummary._prepare(marker, "\n".join(body), binding["project"], "sessions")))
            for path, content in prepared:
                files.append(str(path))
                if path.exists() and path.read_text(encoding="utf-8") == content:
                    continue
                safe_path(path.parent).mkdir(exist_ok=True)
                _atomic(path, content)
                changed.append(str(path))
        return {"ok": True, "files": files, "changed": changed}

    def sync(self, *, project=None):
        from .integration import _atomic
        try:
            result = self.export(project=project)
        except (OSError, ValueError, TypeError) as exc:
            result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        # Summary refresh takes place only after registry files are safely exported.
        if result["ok"]:
            from .work_summary import WorkSummary
            outcomes = []
            for item in WorkSummary(self.store).projects():
                if project and item["project"] != project:
                    continue
                try:
                    WorkSummary(self.store).export(item["project"])
                except (OSError, ValueError, TypeError) as exc:
                    outcomes.append({"project": item["project"], "error": str(exc)})
            result["summary_errors"] = outcomes
        _atomic(self.store.home / "session-registry-sync.json", json.dumps(result, ensure_ascii=False, indent=2))
        return result

    def backup(self):
        folder = safe_path(self.store.home / "backups")
        folder.mkdir(exist_ok=True)
        path = folder / ("session-registry-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
        with self._connect() as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
        return {"registry_backup": str(path), "memory_restore_affects_registry": False}


def dispatch_registry(store, *, operation, **args):
    """Public CLI/MCP/HTTP boundary; validate native identity before allocation."""
    if operation == "ensure":
        platform = _identity(args.get("platform", "codex"), "platform")
        host = _identity(args.get("host", "local"), "host")
        # Reserved names are case-insensitive; otherwise a casing typo could
        # both bypass local verification and allocate a second namespace.
        args["platform"] = "codex" if platform.casefold() == "codex" else platform
        args["host"] = "local" if host.casefold() == "local" else host
        if args["platform"] == "codex" and args["host"] == "local":
            from .title_sync import native_identity
            identity = native_identity(store.home, args.get("thread_id"))
            if identity["status"] == "subagent":
                raise ValidationError("Subagent conversations cannot register main conversation codes")
            if identity["status"] != "main":
                raise ValidationError("Local Codex conversation identity is not verified; registration remains pending and no code was allocated")
    registry = SessionRegistry(store)
    methods = {"ensure": registry.ensure, "bind": registry.bind, "observe-title": registry.observe_title,
               "title-plan": registry.title_plan, "directory-plan": registry.directory_plan,
               "export": registry.export, "backup": registry.backup, "retire": registry.retire}
    if operation not in methods:
        raise ValidationError("Unknown session registry operation")
    return methods[operation](**args)
