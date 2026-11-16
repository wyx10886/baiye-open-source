"""Stable local conversation codes, independent of memory snapshot rollbacks.

Native identities, not editable titles, own codes. Bindings refer only to verified
project roots. This module never renames application chats or existing folders.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
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
            """)

    @contextmanager
    def _connect(self):
        safe_path(self.db)
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

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
        result["suggested_title"] = self.prefixed_title(result["code"], row["title"]) if row["title"] else None
        result["title_needs_sync"] = bool(row["title"] and result["suggested_title"] != row["title"])
        result["bindings"] = [dict(b, directory_code=f"{result['code']}-{b['slot']}") for b in con.execute(
            "SELECT slot,project,root,source,created_at FROM bindings WHERE session_seq=? ORDER BY slot", (row["seq"],))]
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
            else:
                identity = tuple(_identity(v, k) for k, v in (("platform", platform), ("host", host), ("thread_id", thread_id)))
                row = con.execute("SELECT * FROM sessions WHERE platform=? AND host=? AND thread_id=?", identity).fetchone()
            if row is None:
                raise ValidationError("Conversation has not been registered")
            return self._view(con, row)

    def ensure(self, *, thread_id, source, title="", platform="codex", host="local"):
        identity = tuple(_identity(v, k) for k, v in (("platform", platform), ("host", host), ("thread_id", thread_id)))
        source = _text(source, "source", 3000)
        title = _text(title, "title", 500) if title else ""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM sessions WHERE platform=? AND host=? AND thread_id=?", identity).fetchone()
            if row is None:
                stamp = _stamp()
                seq = con.execute("INSERT INTO sessions(platform,host,thread_id,title,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                  (*identity, title, source, stamp, stamp)).lastrowid
                con.execute("INSERT INTO events(session_seq,revision,operation,source,at) VALUES(?,1,'allocate',?,?)", (seq, source, stamp))
                row = con.execute("SELECT * FROM sessions WHERE seq=?", (seq,)).fetchone()
            result = self._view(con, row)
        result["sync"] = self.sync()
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
        return {"code": code, "platform": row["platform"], "host": row["host"], "thread_id": row["thread_id"],
                "observed_title": title, "suggested_title": self.prefixed_title(code, title),
                "application_renamed": False, "revision": row["revision"]}

    def directory_plan(self, *, code, parent, name):
        """Preview a new task name; caller creates/initializes/binds, checking revision."""
        row = self.show(code)
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
    registry = SessionRegistry(store)
    methods = {"ensure": registry.ensure, "bind": registry.bind, "observe-title": registry.observe_title,
               "title-plan": registry.title_plan, "directory-plan": registry.directory_plan,
               "export": registry.export, "backup": registry.backup}
    if operation not in methods:
        raise ValidationError("Unknown session registry operation")
    return methods[operation](**args)
