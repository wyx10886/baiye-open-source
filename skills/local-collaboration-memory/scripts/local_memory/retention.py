"""Review-first housekeeping. Scanning never grants permission to remove files.

The private ledger is separate from watched folders and memory backups. Approved
files are renamed into a local recycle area; no automatic permanent purge exists.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .store import Store, ValidationError, ConflictError, _sqlite


DAY = 86400
SCRIPT_SUFFIXES = {".py", ".ps1", ".sh", ".bat", ".cmd", ".r", ".ipynb"}
DATA_SUFFIXES = {".csv", ".xlsx", ".xls", ".parquet", ".pdf", ".docx", ".sqlite3"}
SKIP = {".git", ".local", "node_modules", "__pycache__", ".venv"}


def _stamp(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _linked(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def safe_path(value):
    """Reject every existing reparse component before resolving a path."""
    path = Path(os.path.abspath(Path(value).expanduser()))
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise ValidationError("不处理符号链接或目录联接：" + str(component))
    return path


def _inside(path, root):
    return path == root or root in path.parents


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prune_empty_job(path):
    """Only the operation-owned job directory; retain any hidden/unknown files."""
    for current, _, _ in os.walk(path, topdown=False, followlinks=False):
        try:
            safe_path(current).rmdir()
        except OSError:
            pass


class Retention:
    def __init__(self, home, *, clock=None):
        self.store = Store(home)
        self.home = safe_path(self.store.home)
        self.db = self.home / "housekeeping.sqlite3"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self.store._exclusive(), _sqlite(self.db) as con:
            con.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")

    def _load(self):
        with _sqlite(self.db) as con:
            row = con.execute("SELECT body FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {"version": 1, "days": 15, "roots": [], "targets": {}, "jobs": {}}

    def _save(self, state):
        with _sqlite(self.db) as con:
            con.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(state, ensure_ascii=False),))

    @contextmanager
    def _edit(self):
        with self.store._exclusive():
            state = self._load()
            yield state
            self._save(state)

    def configure(self, roots, *, days=15):
        if type(days) is not int or not 1 <= days <= 365:
            raise ValidationError("闲置天数应为 1 到 365 的整数")
        if not isinstance(roots, list) or not roots:
            raise ValidationError("需明确指定管理范围")
        paths = [safe_path(root) for root in roots]
        if any(not p.is_dir() or p == Path(p.anchor) or _inside(p, self.home) for p in paths):
            raise ValidationError("管理范围须为现有工作目录，不能是磁盘根目录或系统私有目录")
        with self._edit() as state:
            changed = state["roots"] != [str(p) for p in paths] or state["days"] != days
            state.update(roots=[str(p) for p in paths], days=days)
            if changed:
                for target in state["targets"].values():
                    target["proposal"] = None
            return {"roots": state["roots"], "days": days, "confirmation_required": True, "removal": "local_recycle"}

    def _target_path(self, state, value):
        path = safe_path(value)
        if (path.name != "处理记录" or _inside(path, self.home)
                or not any(_inside(path, safe_path(root)) and path != Path(root) for root in state["roots"])):
            raise ValidationError("目标必须是管理范围内的‘处理记录’子目录")
        return path

    def _snapshot(self, path):
        safe_path(path)
        if not path.is_dir():
            raise ValidationError("处理记录目录不存在")
        entries, total = [], 0
        for current, dirs, files in os.walk(path, followlinks=False):
            safe_path(current)
            for name in sorted(dirs + files):
                item = safe_path(Path(current) / name)
                info = item.lstat()
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise ValidationError("发现特殊文件，需人工处理")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ValidationError("发现共享硬链接，保留原文件")
                total += info.st_size if item.is_file() else 0
                if len(entries) >= 5000 or total > 2 * 1024 ** 3:
                    raise ValidationError("目录超过单次核查上限（5000 项或 2 GiB），请人工复核")
                row = {"path": item.relative_to(path).as_posix(), "type": "dir" if item.is_dir() else "file",
                       "size": info.st_size if item.is_file() else 0, "mtime": info.st_mtime_ns,
                       "ctime": info.st_ctime_ns, "inode": info.st_ino, "device": info.st_dev}
                if row["type"] == "file":
                    row["sha256"] = _hash(item)
                    after = item.lstat()
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino) != (
                            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino):
                        raise ConflictError("文件在检查中发生变化，请稍后重试")
                entries.append(row)
        root = path.stat()
        body = {"root": [root.st_dev, root.st_ino, root.st_mtime_ns, root.st_ctime_ns],
                "entries": sorted(entries, key=lambda x: x["path"])}
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return body, fingerprint

    def _enroll(self, state, path):
        for target in state["targets"].values():
            if os.path.normcase(target["path"]) == os.path.normcase(str(path)):
                return target
        body, fingerprint = self._snapshot(path)
        now = _stamp(self.clock())
        target = {"id": str(uuid.uuid4()), "path": str(path), "enrolled_at": now, "last_used": now,
                  "activity_source": "首次纳管，从今天开始观察", "fingerprint": fingerprint,
                  "protected": {}, "keep": False, "snooze_until": None, "proposal": None, "error": None}
        state["targets"][target["id"]] = target
        return target

    def enroll(self, path):
        with self._edit() as state:
            target = self._enroll(state, self._target_path(state, path))
            return dict(target)

    def _discover(self, state):
        errors, count = [], 0
        for root in state["roots"]:
            safe_path(root)
            for current, dirs, _ in os.walk(root, followlinks=False):
                count += 1
                if count > 30000:
                    errors.append({"path": root, "error": "发现目录达到上限，请缩小管理范围"})
                    break
                allowed = []
                for name in dirs:
                    path = Path(current) / name
                    if name in SKIP or _inside(path, self.home):
                        continue
                    try:
                        if _linked(path.lstat()):
                            continue  # Discovery skips unrelated junctions; never traverse them.
                        safe_path(path)
                        if name == "处理记录":
                            self._enroll(state, self._target_path(state, path))
                        else:
                            allowed.append(name)
                    except (OSError, ValidationError) as exc:
                        errors.append({"path": str(path), "error": str(exc)})
                dirs[:] = allowed
        return errors

    def _touch(self, target, reason):
        target.update(last_used=_stamp(self.clock()), activity_source=reason, proposal=None, snooze_until=None)

    def activity(self, path, *, reason="系统记录的实际使用"):
        reason = str(reason).strip()
        if not reason or len(reason) > 300:
            raise ValidationError("请提供简短的实际使用来源")
        path = safe_path(path)
        touched = []
        with self._edit() as state:
            for target in state["targets"].values():
                base = Path(target["path"])
                # Exact project directory or a path inside its processing records.
                # A shared workspace ancestor must never refresh every project.
                if path == base.parent or _inside(path, base):
                    self._target_path(state, base)
                    self._touch(target, reason)
                    touched.append(target["id"])
        return {"updated": touched}

    def _protection(self, target, row):
        rel = row["path"]
        if rel in {"项目工作总结.md", "项目问题与解决记录.md", "会话与目录对应.md"}:
            return "项目持续接续资料，长期保留，不纳入闲置回收"
        explicit = target["protected"]
        for prefix, reason in explicit.items():
            if rel == prefix or rel.startswith(prefix.rstrip("/") + "/") or prefix == "*":
                return reason
        suffix = Path(rel).suffix.casefold()
        if suffix in SCRIPT_SUFFIXES:
            return "可能是可复用脚本，默认保留"
        if suffix in DATA_SUFFIXES:
            return "可能是原始资料、数据或正式文档，默认保留"
        return None

    def scan(self, *, discover=True):
        with self._edit() as state:
            errors = self._discover(state) if discover else []
            now = self.clock()
            for target in state["targets"].values():
                try:
                    path = self._target_path(state, target["path"])
                    if not path.exists():
                        target.update(proposal=None, missing=True, error=None)
                        continue
                    target["missing"] = False
                    body, fingerprint = self._snapshot(path)
                    if target["fingerprint"] != fingerprint:
                        self._touch(target, "检测到目录或文件变化")
                        target["fingerprint"] = fingerprint
                    target["error"] = None
                    due = datetime.fromisoformat(target["last_used"]) + timedelta(days=state["days"])
                    if target["snooze_until"]:
                        due = max(due, datetime.fromisoformat(target["snooze_until"]))
                    if not target["keep"] and now >= due and not target["proposal"]:
                        files = [{**r, "protected_reason": self._protection(target, r)}
                                 for r in body["entries"] if r["type"] == "file"]
                        if files:
                            target["proposal"] = {"id": str(uuid.uuid4()), "created_at": _stamp(now),
                                                  "fingerprint": fingerprint, "files": files,
                                                  "notified_at": None}
                except (OSError, ValidationError) as exc:
                    target.update(proposal=None, error=str(exc))
                    errors.append({"path": target["path"], "error": str(exc)})
            return self._view(state, errors)

    def _view(self, state, errors=None):
        targets = []
        for target in state["targets"].values():
            row = dict(target)
            due = datetime.fromisoformat(row["last_used"]) + timedelta(days=state["days"])
            if row["snooze_until"]:
                due = max(due, datetime.fromisoformat(row["snooze_until"]))
            row["due_at"] = _stamp(due)
            targets.append(row)
        return {"days": state["days"], "roots": state["roots"], "targets": targets,
                "pending": [t for t in targets if t["proposal"]],
                "new_notifications": [t for t in targets if t["proposal"] and not t["proposal"]["notified_at"]],
                "jobs": list(state["jobs"].values()), "errors": errors or []}

    def status(self):
        with self.store._exclusive():
            return self._view(self._load())

    def acknowledge(self, proposal_ids):
        with self._edit() as state:
            found = []
            for target in state["targets"].values():
                proposal = target["proposal"]
                if proposal and proposal["id"] in proposal_ids:
                    proposal["notified_at"] = _stamp(self.clock())
                    found.append(proposal["id"])
            return {"acknowledged": found}

    def decision(self, target_id, action, *, proposal_id=None, files=None, reason="", confirmed=False):
        if action not in {"keep", "snooze", "resume", "protect", "recycle"}:
            raise ValidationError("不支持的清理操作")
        with self._edit() as state:
            target = state["targets"].get(target_id)
            if not target:
                raise ValidationError("处理记录未纳管")
            if action == "recycle":
                return self._recycle(state, target, proposal_id, files, reason, confirmed)
            if action == "keep":
                target.update(keep=True, proposal=None)
            elif action == "snooze":
                target.update(snooze_until=_stamp(self.clock() + timedelta(days=state["days"])), proposal=None)
            elif action == "resume":
                target["keep"] = False
                self._touch(target, "用户重新开启观察")
            else:
                if not isinstance(files, list) or not files or not reason.strip():
                    raise ValidationError("保护操作需列出相对路径及理由，整夹可用 *")
                body, _ = self._snapshot(self._target_path(state, target["path"]))
                names = {r["path"] for r in body["entries"]} | {"*"}
                if any(f not in names for f in files):
                    raise ValidationError("保护路径不在目录清单中")
                target["protected"].update({f: reason.strip()[:300] for f in files})
                target["proposal"] = None
            return dict(target)

    def _recycle(self, state, target, proposal_id, files, reason, confirmed):
        proposal = target["proposal"]
        if not proposal or proposal["id"] != proposal_id:
            raise ConflictError("确认清单已经过期，请重新扫描并确认")
        if confirmed is not True or not isinstance(reason, str) or not reason.strip():
            raise ValidationError("须明确确认已核对用途和依赖，并提供清理理由")
        if not isinstance(files, list) or not files or len(set(files)) != len(files):
            raise ValidationError("须明确选择待回收文件")
        by_name = {r["path"]: r for r in proposal["files"]}
        if any(f not in by_name or by_name[f]["protected_reason"] for f in files):
            raise ValidationError("不可回收受保护文件或清单之外的路径")
        path = self._target_path(state, target["path"])
        body, fingerprint = self._snapshot(path)
        if fingerprint != proposal["fingerprint"] or self.clock() < datetime.fromisoformat(target["last_used"]) + timedelta(days=state["days"]):
            target["proposal"] = None
            self._save(state)
            raise ConflictError("确认前目录发生变化或重新使用，旧确认失效")
        job_id = str(uuid.uuid4())
        recycle = safe_path(self.home / "recycle" / job_id)
        job = {"id": job_id, "target": str(path), "at": _stamp(self.clock()), "reason": reason.strip()[:500],
               "status": "moving", "files": [by_name[f] for f in files], "moved": [], "error": None}
        state["jobs"][job_id] = job
        target["proposal"] = None
        # Persist intent before touching the filesystem; interrupted moves remain inspectable.
        self._save(state)
        try:
            for name in files:
                source = safe_path(path / name)
                dest = safe_path(recycle / name)
                if not _inside(source, path) or not _inside(dest, recycle) or dest.exists():
                    raise ConflictError("回收位置冲突，保留原文件")
                if _hash(source) != by_name[name]["sha256"]:
                    raise ConflictError("文件在操作前已变化，停止回收")
                dest.parent.mkdir(parents=True, exist_ok=True)
                # rename fails safely for cross-volume moves and Windows deny-delete locks.
                source.rename(dest)
                job["moved"].append(name)
                self._save(state)
                if _hash(dest) != by_name[name]["sha256"]:
                    raise ConflictError("文件在回收时变化，需核查回收区")
            job["status"] = "recycled"
            # Only known snapshot directories; rmdir retains any unobserved/hidden contents.
            for row in sorted(body["entries"], key=lambda r: r["path"].count("/"), reverse=True):
                if row["type"] == "dir":
                    try:
                        safe_path(path / row["path"]).rmdir()
                    except OSError:
                        pass
            try:
                safe_path(path).rmdir()
            except OSError:
                pass
        except (OSError, ValidationError) as exc:
            job.update(status="partial" if job["moved"] else "failed", error=str(exc))
            _prune_empty_job(recycle)
        self._touch(target, "用户确认回收" if job["status"] == "recycled" else "回收中止，保留未移动文件")
        self._save(state)
        return dict(job, recycle_path=str(recycle))

    def restore(self, job_id):
        with self._edit() as state:
            job = state["jobs"].get(job_id)
            if not job or job["status"] not in {"recycled", "partial", "moving", "restoring"}:
                raise ValidationError("没有可恢复的回收记录")
            target = self._target_path(state, job["target"])
            recycle = safe_path(self.home / "recycle" / job_id)
            # Reconcile interrupted moves using the persisted complete manifest.
            candidates = [r for r in job["files"] if (recycle / r["path"]).exists()]
            for row in job["files"]:
                if not (recycle / row["path"]).exists():
                    original = safe_path(target / row["path"])
                    if not original.is_file() or _hash(original) != row["sha256"]:
                        raise ConflictError("原位置和回收区未找到一致文件，需人工核查：" + row["path"])
            for row in candidates:
                dest = safe_path(target / row["path"])
                source = safe_path(recycle / row["path"])
                if not _inside(source, recycle) or not _inside(dest, target) or dest.exists():
                    raise ConflictError("原位置已有文件，恢复不会覆盖：" + row["path"])
                if _hash(source) != row["sha256"]:
                    raise ConflictError("回收文件已变化，需人工核查")
            job["status"] = "restoring"
            self._save(state)
            for row in candidates:
                dest = safe_path(target / row["path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                safe_path(recycle / row["path"]).rename(dest)
            job["status"] = "restored"
            _prune_empty_job(recycle)
            for entry in state["targets"].values():
                if entry["path"] == str(target):
                    self._touch(entry, "已从本地回收区恢复")
            return dict(job)
