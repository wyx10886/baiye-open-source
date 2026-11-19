"""Read-only, fail-closed inventories for recycling local conversation codes.

The host state index includes archived threads. Only code prefixes are returned;
full chat titles and conversation bodies are never retained. Workspace links are
not traversed: only aliases of already enumerated entities can be complete.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from .store import ValidationError

# A code must end before a separator, not in an ASCII word or cache hash.
_PREFIX=re.compile(r"^\s*(?:\[|【)?([A-Z]+[0-9]{2})(?![A-Za-z0-9])",re.I)
_INTERNAL={"subagent","guardian_review","memory_consolidation","thread_title"}
_REPARSE=getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",1024)


def _code(value):
    match=_PREFIX.match(value or "") if isinstance(value,str) else None
    return match.group(1).upper() if match else None


def _linked(path):
    meta=path.lstat()
    return stat.S_ISLNK(meta.st_mode) or bool(getattr(meta,"st_file_attributes",0)&_REPARSE)


def _source_kind(source):
    if isinstance(source,str):
        try: parsed=json.loads(source)
        except (ValueError,TypeError): parsed=source
        if parsed!=source:return _source_kind(parsed)
        label=source.replace("_","").replace("-","").casefold()
        if label.startswith("subagent"):return "subagent"
        return "main" if label in {"vscode","cli","exec","appserver"} else "unknown"
    if isinstance(source,dict) and any(str(k).replace("_","").casefold()=="subagent" for k in source):
        return "subagent"
    return "unknown"


def scan_occupancy(home, workspace_roots=None):
    home=Path(home).expanduser().resolve()
    errors=[];live=[];thread_codes={};directory_codes={}
    stats={"main_threads":0,"archived_threads":0,"non_user_threads":0,"directories":0,"files":0,"links":0}
    def error(kind,path=None):
        item={"kind":kind}
        if path is not None:item["path"]=str(path)
        errors.append(item)
    try:
        settings_path=home/"settings.json"
        settings=json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path.exists() else {}
        if not isinstance(settings,dict):raise ValueError()
    except (OSError,ValueError,TypeError):
        settings={};error("settings_unavailable")
    recycle=settings.get("session_recycling",{})
    if not isinstance(recycle,dict):recycle={};error("invalid_recycling_settings")
    roots=workspace_roots if workspace_roots is not None else recycle.get("workspace_roots")
    if not isinstance(roots,list) or not roots or any(not isinstance(r,str) or not r for r in roots):
        roots=[];error("workspace_roots_not_configured")
    host=None
    try:
        host_cfg=settings.get("host_title_sync",{})
        if not isinstance(host_cfg,dict):raise ValueError()
        host_value=host_cfg.get("codex_home",str(Path.home()/".codex"))
        if not isinstance(host_value,str) or not host_value:raise ValueError()
        candidate=Path(host_value).expanduser()
        if not candidate.is_absolute():raise ValueError()
        host=candidate.resolve()
    except (OSError,ValueError,TypeError):error("invalid_host_configuration")
    try:
        if host is None:raise ValueError()
        dbs=sorted((p for p in host.glob("state_*.sqlite") if p.stem[6:].isdigit()),
                   key=lambda p:int(p.stem[6:]),reverse=True)
        if not dbs:raise FileNotFoundError()
        with closing(sqlite3.connect(dbs[0].as_uri()+"?mode=ro",uri=True,timeout=.5)) as con:
            con.row_factory=sqlite3.Row
            con.execute("BEGIN")
            columns={r[1] for r in con.execute("PRAGMA table_info(threads)")}
            if not {"id","source","archived"}.issubset(columns) or not ({"name","title"}&columns):
                raise ValidationError("Unsupported host index schema")
            fields=[f for f in ("id","name","title","source","thread_source","attached_thread_parent_id","archived") if f in columns]
            rows=con.execute("SELECT "+",".join(fields)+" FROM threads").fetchall()
            for row in rows:
                data=dict(row);kind=_source_kind(data.get("source"));thread_type=data.get("thread_source")
                if data.get("attached_thread_parent_id") or kind=="subagent" or thread_type in _INTERNAL:
                    stats["non_user_threads"]+=1;continue
                if thread_type in {"user","agent_created_thread"}:kind="main"
                elif thread_type not in (None,""):
                    error("unknown_thread_class");continue
                if kind!="main":error("unknown_thread_origin");continue
                try:uuid.UUID(data["id"])
                except (ValueError,TypeError,AttributeError):error("invalid_host_thread_id");continue
                live.append(data["id"]);stats["main_threads"]+=1
                stats["archived_threads"]+=int(bool(data.get("archived")))
                # name is the user-visible label; title is retained only as a conservative reservation.
                for code in {_code(data.get("name")),_code(data.get("title"))}-{None}:
                    thread_codes.setdefault(code,[]).append(data["id"])
    except (OSError,sqlite3.Error,ValueError,TypeError,ValidationError):error("host_inventory_unavailable")
    visited=set();entities=set();links=[];normalized=[]
    for raw in roots:
        try:
            base=Path(raw).expanduser()
            if not base.is_absolute() or _linked(base) or not base.is_dir():
                error("workspace_root_unavailable",base);continue
            base=base.resolve();normalized.append(str(base));stack=[base]
            while stack:
                folder=stack.pop()
                if folder in visited:continue
                try:
                    if _linked(folder):
                        error("workspace_directory_became_link",folder);continue
                except OSError:
                    error("workspace_directory_unreadable",folder);continue
                visited.add(folder);entities.add(folder);stats["directories"]+=1
                own_code=_code(folder.name)
                if own_code:directory_codes.setdefault(own_code,[]).append(str(folder))
                try:
                    first=folder.stat().st_mtime_ns
                    with os.scandir(folder) as iterator:
                        entries=list(iterator)
                    for entry in entries:
                        path=Path(entry.path)
                        code=_code(entry.name)
                        if code:directory_codes.setdefault(code,[]).append(str(path))
                        try:
                            attrs=entry.stat(follow_symlinks=False)
                            if entry.is_symlink() or bool(getattr(attrs,"st_file_attributes",0)&_REPARSE):
                                links.append(path);stats["links"]+=1;continue
                            if entry.is_dir(follow_symlinks=False):stack.append(path)
                            elif entry.is_file(follow_symlinks=False):
                                entities.add(path);stats["files"]+=1
                        except OSError:error("workspace_entry_unreadable",path)
                    if first!=folder.stat().st_mtime_ns:error("workspace_changed_during_scan",folder)
                except OSError:error("workspace_directory_unreadable",folder)
        except (OSError,ValueError,TypeError):error("workspace_root_unavailable",raw)
    for link in links:
        try:
            target=link.resolve(strict=True)
            if target not in entities:error("workspace_link_target_unverified",link)
        except (OSError,RuntimeError,ValueError):error("workspace_link_unavailable",link)
    return {"complete":not errors,"scanned_at":datetime.now(timezone.utc).isoformat(),
        "live_thread_ids":sorted(set(live)),
        "thread_codes":{k:sorted(set(v)) for k,v in sorted(thread_codes.items())},
        "directory_codes":{k:sorted(set(v)) for k,v in sorted(directory_codes.items())},
        "errors":errors,"stats":stats,"workspace_roots":sorted(set(normalized)),"host_home":str(host) if host is not None else None}
