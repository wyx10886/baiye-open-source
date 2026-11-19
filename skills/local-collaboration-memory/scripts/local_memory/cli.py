"""Command line interface. All ordinary command results are UTF-8 JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .store import ConflictError, Store, ValidationError


KINDS = ["preference", "persona", "event", "project", "lesson"]
STATUSES = ["confirmed", "candidate", "unverified"]


def default_home() -> Path:
    return Path(os.environ.get("LOCAL_MEMORY_HOME", str(Path.home() / ".local-memory"))).expanduser()


def configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict" if stream is sys.stdout else "replace")


def _fields(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--kind", choices=KINDS, required=required)
    parser.add_argument("--title", required=required)
    parser.add_argument("--content", required=required, help="Memory text; '-' reads stdin")
    parser.add_argument("--source", required=True, help="Actual message, artifact or evidence provenance")
    parser.add_argument("--scope", default="global" if required else None)
    parser.add_argument("--status", choices=STATUSES, default="confirmed" if required else None)
    parser.add_argument("--event-date", help="YYYY-MM-DD, occurrence date rather than import date")
    parser.add_argument("--importance", type=int, choices=range(1, 6), default=3 if required else None)
    parser.add_argument("--tag", dest="tags", action="append")
    parser.add_argument("--valid-until", help="YYYY-MM-DD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-memory", description="Local collaboration memory with evidence and project isolation")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--home", type=Path, default=None, help="Private data directory; defaults to LOCAL_MEMORY_HOME or ~/.local-memory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize storage and show health")
    remember = sub.add_parser("remember", help="Save evidence-backed memory")
    _fields(remember, required=True)
    remember.add_argument("--key", help="Stable unique key within this scope")
    remember.add_argument("--idempotency-key")
    revise = sub.add_parser("revise", help="Correct a current record without losing revision history")
    revise.add_argument("memory_id")
    revise.add_argument("--expected-revision", type=int, required=True)
    _fields(revise, required=False)
    revise.add_argument("--clear-valid-until", action="store_true")
    forget = sub.add_parser("forget", help="Erase text from the complete revision chain")
    forget.add_argument("memory_id")
    forget.add_argument("--expected-revision", type=int, required=True)
    forget.add_argument("--reason", default="user request")
    get = sub.add_parser("get", help="Inspect one record including its revision metadata")
    get.add_argument("memory_id")
    listing = sub.add_parser("list", help="List memories, optionally including historical tombstones")
    listing.add_argument("--scope")
    listing.add_argument("--include-inactive", action="store_true")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--kind", choices=KINDS)
    listing.add_argument("--limit", type=int, default=200)
    search = sub.add_parser("search", help="Retrieve active matching memories")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--scope", default="global")
    search.add_argument("--no-global", action="store_true")
    search.add_argument("--limit", type=int, default=12)
    search.add_argument("--status", dest="statuses", choices=STATUSES, action="append")
    search.add_argument("--include-persona", action="store_true")
    context = sub.add_parser("context", help="Generate project handoff context with candidates separated")
    context.add_argument("query", nargs="?", default="")
    context.add_argument("--project")
    context.add_argument("--thread-id", help="Provide the actual current native chat ID to include learned candidates under its use policy")
    context.add_argument("--budget", type=int, default=6000)
    context.add_argument("--include-persona", action="store_true")
    context.add_argument("--markdown", action="store_true", help="Print readable context instead of JSON")
    backup = sub.add_parser("backup", help="Create a consistent SQLite snapshot")
    backup.add_argument("--target", type=Path)
    restore = sub.add_parser("restore", help="Validate and restore a backup after protecting current data")
    restore.add_argument("backup_path", type=Path)
    export = sub.add_parser("export", help="Regenerate a human-readable Markdown projection")
    export.add_argument("--target", type=Path)
    review = sub.add_parser("review", help="Record a truthful daily collaboration review")
    review.add_argument("--note", default="")
    review.add_argument("--day")
    audit = sub.add_parser("audit", help="Inspect content-free operation history")
    audit.add_argument("--limit", type=int, default=50)
    sub.add_parser("stats", help="Inspect storage counts and review metadata")
    serve = sub.add_parser("serve", help="Start the local memory management webpage")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", dest="open_browser", action="store_true")
    sub.add_parser("mcp", help="Run MCP JSON-RPC over standard input/output")
    importer = sub.add_parser("import", help="Import literal Markdown notes or a curated JSON array")
    importer.add_argument("path", type=Path)
    importer.add_argument("--format", choices=["markdown", "json"], default="markdown")
    importer.add_argument("--source", required=True)
    importer.add_argument("--scope", default="global")
    importer.add_argument("--status", choices=STATUSES, default="unverified")
    for name in ("install", "doctor"):
        integration = sub.add_parser(name, help="Install Codex integration" if name == "install" else "Inspect Codex integration health")
        integration.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    sub.add_parser("hook", help="Handle a Codex hook JSON event from stdin")
    notify = sub.add_parser("notify", help="Capture a completed Codex turn without stdout output")
    notify.add_argument("payload", nargs="?", help="JSON event; defaults to stdin")
    project = sub.add_parser("project", help="Portable project summary and issue history")
    projects = project.add_subparsers(dest="project_action", required=True)
    projects.add_parser("list", help="List explicitly bound project directories")
    for name in ("show", "export"):
        operation = projects.add_parser(name)
        operation.add_argument("project")
        if name == "show":
            operation.add_argument("--markdown", action="store_true")
    checkpoint = projects.add_parser("checkpoint", help="Create or revise an evidence-backed overview/issue")
    checkpoint.add_argument("project")
    checkpoint.add_argument("--input", required=True, help="UTF-8 JSON object with key/data/source; '-' reads stdin")
    session = sub.add_parser("session", help="Stable conversation codes and verified directory associations")
    sessions = session.add_subparsers(dest="session_action", required=True)
    listing = sessions.add_parser("list")
    listing.add_argument("--query", default="")
    listing.add_argument("--project")
    show = sessions.add_parser("show")
    show.add_argument("code", nargs="?")
    show.add_argument("--thread-id")
    show.add_argument("--platform", default="codex")
    show.add_argument("--host", default="local")
    update = sessions.add_parser("update", help="JSON operation: ensure/bind/observe-title/title-plan/export/backup")
    update.add_argument("--input", required=True, help="UTF-8 JSON file; '-' reads stdin")
    sessions.add_parser("backup")
    recycle = sessions.add_parser("recycle", help="Scan reusable numbers, read allocation history, or release verified vacant registrations")
    recycle.add_argument("--input", required=True, help="UTF-8 JSON operation scan/history/reclaim; reclaim requires source, expected_scan_id, confirmed=true")
    daily = sub.add_parser("daily-chat", help="Separate A00 daily-chat entry; native conversation codes remain stable")
    daily_ops = daily.add_subparsers(dest="daily_action", required=True)
    daily_ops.add_parser("show", help="Read the entry, its current native session and binding history")
    daily_bind = daily_ops.add_parser("bind", help="Point the entry to an already registered native session")
    daily_bind.add_argument("--input", required=True, help="UTF-8 JSON with thread_id/title/source/expected_revision; '-' reads stdin")
    proactive = sub.add_parser("proactive", help="Opt-in daytime conversation preparation; never sends messages itself")
    proactive_ops = proactive.add_subparsers(dest="proactive_action", required=True)
    proactive_ops.add_parser("status", help="Read the policy and preparation attempts, not delivery receipts")
    for name in ("configure", "evaluate", "claim"):
        operation = proactive_ops.add_parser(name)
        operation.add_argument("--input", required=True, help="UTF-8 JSON object; '-' reads stdin")
    habits = sub.add_parser("habits", help="Review evidence-backed learned habits and manage opt-in learning")
    habit_ops = habits.add_subparsers(dest="habit_action", required=True)
    habit_status = habit_ops.add_parser("status", help="Read effective learning policy and counts")
    habit_status.add_argument("--thread-id")
    habit_list = habit_ops.add_parser("list", help="List candidate habits; inferred habits are not confirmed preferences")
    habit_list.add_argument("--scope")
    habit_list.add_argument("--include-inactive", action="store_true")
    habit_list.add_argument("--limit", type=int, default=30)
    habit_show = habit_ops.add_parser("show", help="Read one habit and its attributable evidence")
    habit_show.add_argument("habit_id")
    for name in ("configure", "observe", "review", "clear"):
        operation = habit_ops.add_parser(name)
        operation.add_argument("--input", required=True, help="UTF-8 JSON object; '-' reads stdin. Management needs source/revision; review and clear also confirmed=true.")
    cleanup = sub.add_parser("cleanup", help="15-day review-first processing-record housekeeping")
    ops = cleanup.add_subparsers(dest="cleanup_action", required=True)
    config = ops.add_parser("configure", help="Explicit managed workspace roots")
    config.add_argument("--root", action="append", required=True)
    config.add_argument("--days", type=int, default=15)
    ops.add_parser("scan", help="Discover records and create confirmation lists; never remove")
    ops.add_parser("status")
    enroll = ops.add_parser("enroll")
    enroll.add_argument("path")
    activity = ops.add_parser("activity", help="Record actual use; scans are not use")
    activity.add_argument("path")
    activity.add_argument("--reason", required=True)
    ack = ops.add_parser("acknowledge", help="Mark only notifications actually delivered")
    ack.add_argument("proposal_ids", nargs="+")
    decide = ops.add_parser("decide")
    decide.add_argument("target_id")
    decide.add_argument("action", choices=["keep", "snooze", "resume", "protect", "recycle"])
    decide.add_argument("--proposal-id")
    decide.add_argument("--file", dest="files", action="append")
    decide.add_argument("--reason", default="")
    decide.add_argument("--confirmed", action="store_true", help="Only after the user's concrete file-list confirmation")
    recover = ops.add_parser("restore")
    recover.add_argument("job_id")
    return parser


def dispatch_session_recycling(home: Path, operation: str, **payload):
    """Use one strict contract for CLI, MCP and HTTP recycling controls."""
    from .session_registry import SessionRegistry
    allowed = {
        "scan": set(),
        "history": {"code", "thread_id"},
        "reclaim": {"source", "expected_scan_id", "confirmed"},
    }
    required = {"scan": set(), "history": set(), "reclaim": allowed["reclaim"]}
    if not isinstance(operation, str) or operation not in allowed:
        raise ValidationError("Unknown session recycling operation")
    if set(payload) - allowed[operation] or required[operation] - set(payload):
        raise ValidationError("Session recycling operation has missing or unsupported fields")
    for field in set(payload) - {"confirmed"}:
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValidationError(field + " must be nonempty text")
    if operation == "reclaim" and payload.pop("confirmed") is not True:
        raise ValidationError("Releasing registrations requires explicit confirmation of the current scan")
    registry = SessionRegistry(Store(home))
    if operation == "scan":
        return registry.recycling_scan()
    if operation == "history":
        return registry.recycling_history(**payload)
    return registry.recycling_reclaim(**payload)


def dispatch_habit_operation(home: Path, operation: str, **payload):
    """Validate each habit operation before forwarding to the learning store."""
    from .habits import HabitLearning
    allowed = {
        "status": {"thread_id"},
        "list": {"scope", "include_inactive", "limit"},
        "show": {"habit_id"},
        "configure": {"enabled", "use_learned", "allow_tool_assisted", "source", "thread_id", "expected_revision"},
        "observe": {"receipt_id", "topic", "value", "quote", "scope"},
        "review": {"habit_id", "action", "expected_revision", "source", "value", "confirmed"},
        "clear": {"source", "expected_revision", "confirmed"},
    }
    required = {
        "status": set(), "list": set(), "show": {"habit_id"},
        "configure": {"source", "expected_revision"},
        "observe": {"receipt_id", "topic", "value", "quote"},
        "review": {"habit_id", "action", "expected_revision", "source", "confirmed"},
        "clear": {"source", "expected_revision", "confirmed"},
    }
    if operation not in allowed:
        raise ValidationError("Unknown habit operation")
    if set(payload) - allowed[operation] or required[operation] - set(payload):
        raise ValidationError("Habit operation has missing or unsupported fields")
    if operation in {"configure", "review", "clear"}:
        if not isinstance(payload["source"], str) or not payload["source"].strip():
            raise ValidationError("Actual user action or confirmation source is required")
        minimum = 1 if operation == "review" else 0
        revision = payload["expected_revision"]
        if type(revision) is not int or revision < minimum:
            raise ValidationError("Invalid expected_revision; read current policy or habit first")
    if operation in {"review", "clear"}:
        if payload.pop("confirmed") is not True:
            raise ValidationError("This action requires the user's explicit confirmation")
    if operation == "configure":
        changes = set(payload) & {"enabled", "use_learned", "allow_tool_assisted"}
        if not changes or any(type(payload[k]) is not bool for k in changes):
            raise ValidationError("Provide boolean learning policy changes")
    if operation == "review":
        if payload["action"] not in {"confirm", "edit", "reject", "forget"}:
            raise ValidationError("Unsupported habit review action")
        if payload["action"] == "edit":
            if not isinstance(payload.get("value"), str) or not payload["value"].strip():
                raise ValidationError("Editing a habit requires a nonempty value")
        elif "value" in payload:
            raise ValidationError("Only habit editing accepts value")
    if operation == "list":
        if "include_inactive" in payload and type(payload["include_inactive"]) is not bool:
            raise ValidationError("include_inactive must be boolean")
        if "limit" in payload and (type(payload["limit"]) is not int or not 1 <= payload["limit"] <= 200):
            raise ValidationError("limit must be between 1 and 200")
    learning = HabitLearning(home)
    if operation == "show":
        return learning.get(payload["habit_id"])
    if operation == "review":
        habit_id = payload.pop("habit_id")
        return learning.review(habit_id, **payload)
    return getattr(learning, operation)(**payload)


def dispatch(args: argparse.Namespace, home: Path):
    command = args.command
    if command == "habits":
        if args.habit_action == "status":
            payload = {"thread_id": args.thread_id} if args.thread_id else {}
        elif args.habit_action == "list":
            payload = {"include_inactive": args.include_inactive, "limit": args.limit}
            if args.scope:
                payload["scope"] = args.scope
        elif args.habit_action == "show":
            payload = {"habit_id": args.habit_id}
        else:
            raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValidationError("Habit input must be a JSON object")
        return dispatch_habit_operation(home, args.habit_action, **payload)
    if command == "proactive":
        from .proactive import ProactiveChat
        proactive = ProactiveChat(home)
        if args.proactive_action == "status":
            return proactive.status()
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValidationError("Proactive input must be a JSON object")
        if args.proactive_action in {"evaluate", "claim"}:
            from .daily_chat import DailyChat
            daily = DailyChat(Store(home)).show()
            actual_id = (daily.get("current_session") or {}).get("thread_id")
            if "daily_thread_id" in payload and payload["daily_thread_id"] != actual_id:
                raise ValidationError("Daily-chat identity must match the current registered entry")
            payload["daily_thread_id"] = actual_id
        return getattr(proactive, args.proactive_action)(**payload)
    if command == "cleanup":
        from .retention import Retention
        cleanup = Retention(home)
        action = args.cleanup_action
        if action == "configure":
            return cleanup.configure(args.root, days=args.days)
        if action in {"scan", "status"}:
            return getattr(cleanup, action)()
        if action == "enroll":
            return cleanup.enroll(args.path)
        if action == "activity":
            return cleanup.activity(args.path, reason=args.reason)
        if action == "acknowledge":
            return cleanup.acknowledge(args.proposal_ids)
        if action == "decide":
            return cleanup.decision(args.target_id, args.action, proposal_id=args.proposal_id,
                                    files=args.files, reason=args.reason, confirmed=args.confirmed)
        if action == "restore":
            return cleanup.restore(args.job_id)
    if command == "mcp":
        from .mcp import run_stdio
        run_stdio(home)
        return None
    if command == "serve":
        from .web import serve
        serve(home, host=args.host, port=args.port, open_browser=args.open_browser)
        return None
    if command in {"install", "doctor"}:
        from . import integration
        operation = integration.install if command == "install" else integration.doctor
        return operation(home, args.codex_home)
    if command in {"hook", "notify"}:
        from . import integration
        try:
            raw = args.payload if command == "notify" and args.payload is not None else sys.stdin.read()
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValidationError("Integration event must be a JSON object")
            if command == "hook":
                return integration.handle_hook(home, payload)
            integration.notify(home, payload)
        except Exception as exc:
            try:
                integration._log_failure(home,exc)
            except Exception:
                pass
            if command == "hook":
                # Codex interprets exit 2 as a blocking hook. Memory failure must not block work.
                return {"systemMessage":"本地协作记忆暂不可用；本轮可继续工作，稍后核查本机存储。"}
        return None
    store = Store(home)
    if command == "daily-chat":
        from .daily_chat import DailyChat
        daily = DailyChat(store)
        if args.daily_action == "show":
            return daily.show()
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValidationError("Daily-chat binding input must be an object")
        return daily.bind(**payload)
    if command == "session":
        from .session_registry import SessionRegistry, dispatch_registry
        registry = SessionRegistry(store)
        if args.session_action == "list":
            return registry.list(query=args.query, project=args.project)
        if args.session_action == "show":
            return registry.show(args.code, thread_id=args.thread_id, platform=args.platform, host=args.host)
        if args.session_action == "backup":
            return registry.backup()
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValidationError("Input must be an object with operation and fields")
        if args.session_action == "recycle":
            return dispatch_session_recycling(home, **payload)
        return dispatch_registry(store, **payload)
    if command == "project":
        from .work_summary import WorkSummary
        summaries = WorkSummary(store)
        if args.project_action == "list":
            return summaries.projects()
        if args.project_action == "checkpoint":
            raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "project" in payload:
                raise ValidationError("Input must be an object; project comes from the CLI argument")
            return summaries.checkpoint(project=args.project, **payload)
        if args.project_action == "export":
            return summaries.export(args.project)
        result = summaries.read(args.project)
        if args.markdown:
            print(result["markdown"])
            return None
        return result
    if command == "import":
        from .importer import import_file
        return import_file(store, args.path, format=args.format, source=args.source,
                           scope=args.scope, status=args.status)
    if command == "init":
        return {"home": str(home.resolve()), "stats": store.stats()}
    if command == "remember":
        fields = {key: getattr(args, key) for key in (
            "kind", "title", "content", "source", "scope", "status", "key", "event_date",
            "importance", "tags", "valid_until", "idempotency_key")}
        if fields["content"] == "-":
            fields["content"] = sys.stdin.read()
        return store.remember(**fields)
    if command == "revise":
        changes = {key: getattr(args, key) for key in (
            "kind", "title", "content", "scope", "status", "event_date", "importance", "tags", "valid_until")
                   if getattr(args, key) is not None}
        if changes.get("content") == "-":
            changes["content"] = sys.stdin.read()
        if args.clear_valid_until:
            changes["valid_until"] = None
        if not changes:
            raise ValidationError("Provide at least one field to revise")
        return store.revise(args.memory_id, expected_revision=args.expected_revision, source=args.source, **changes)
    if command == "forget":
        return store.forget(args.memory_id, expected_revision=args.expected_revision, reason=args.reason)
    if command == "get":
        return store.get(args.memory_id)
    if command == "list":
        return store.list_memories(scope=args.scope, include_inactive=args.include_inactive,
                                   status=args.status, kind=args.kind, limit=args.limit)
    if command == "search":
        return store.search(args.query, scope=args.scope, include_global=not args.no_global, limit=args.limit,
                            statuses=args.statuses, include_persona=args.include_persona)
    if command == "context":
        result = store.context(args.query, project=args.project, budget=args.budget, include_persona=args.include_persona, thread_id=args.thread_id)
        if args.markdown:
            print(result["markdown"])
            return None
        return result
    if command == "backup":
        return {"backup": store.backup(args.target)}
    if command == "restore":
        return store.restore(args.backup_path)
    if command == "export":
        return {"markdown": store.export_markdown(args.target)}
    if command == "review":
        return store.review(args.note, day=args.day)
    if command == "audit":
        return store.audit(limit=args.limit)
    if command == "stats":
        return store.stats()
    raise ValidationError(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    home = (args.home or default_home()).expanduser()
    try:
        result = dispatch(args, home)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except ConflictError as exc:
        print(json.dumps({"error": "ConflictError", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (ValidationError, ValueError, TypeError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
