"""Explicit, provenance-preserving imports without interpreting document instructions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .store import ConflictError, Store, ValidationError


MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_RECORDS = 1000
WRITE_FIELDS = {"kind", "title", "content", "source", "scope", "status", "key", "event_date",
                "importance", "tags", "valid_until"}


def _markdown_records(text: str) -> list[dict]:
    """Split headings, bullets and paragraphs; all contents remain literal evidence."""
    records = []
    headings: dict[int, str] = {}
    pending: list[str] = []
    fence = None

    def flush():
        content = "\n".join(pending).strip()
        pending.clear()
        if content:
            title = " / ".join(headings[level] for level in sorted(headings)) or "Imported note"
            records.append({"kind": "event", "title": title[:195], "content": content})

    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                flush()
                fence = marker[1][0]
            elif marker[1][0] == fence:
                pending.append(line)
                fence = None
                flush()
                continue
            pending.append(line)
            continue
        if fence is not None:
            pending.append(line)
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush()
            level = len(heading[1])
            for old_level in list(headings):
                if old_level >= level:
                    del headings[old_level]
            headings[level] = heading[2]
            continue
        bullet = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$", line)
        if bullet:
            flush()
            pending.append(bullet[1])
        elif not line.strip():
            flush()
        else:
            pending.append(line)
    flush()
    return records


def import_file(store: Store, path: str | Path, *, format: str = "markdown", source: str,
                scope: str = "global", status: str = "unverified") -> dict:
    """Import explicit records, recording partial validation failures in the result.

    Markdown always inherits the caller's explicit status (unverified by default).
    A curated JSON array may choose a different status for individual records.
    Stable source slots are idempotent across edits: reimporting is deliberately
    conservative and cannot silently revise, reactivate, or resurrect a memory.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValidationError("Import source is required")
    if format not in {"markdown", "json"}:
        raise ValidationError("Import format must be markdown or json")
    if status not in {"confirmed", "candidate", "unverified"}:
        raise ValidationError("Invalid import status")
    original = Path(path).expanduser().resolve(strict=True)
    if not original.is_file() or original.stat().st_size > MAX_IMPORT_BYTES:
        raise ValidationError("Import must be a regular UTF-8 file of at most 2 MiB")
    raw = original.read_bytes()
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValidationError("Import exceeds 2 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValidationError("Import must use UTF-8 encoding") from exc
    if format == "json":
        try:
            records = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
        if not isinstance(records, list):
            raise ValidationError("JSON import must be an array of memory objects")
    else:
        records = _markdown_records(text)
    if not records or len(records) > MAX_IMPORT_RECORDS:
        raise ValidationError("Import must contain between 1 and 1000 records")

    result = {"format": format, "path": str(original), "total": len(records),
              "imported": 0, "skipped": 0, "errors": [], "records": []}
    existing = {item["id"] for item in store.list_memories(include_inactive=True, limit=10000)}
    for index, item in enumerate(records):
        try:
            if not isinstance(item, dict):
                raise ValidationError("Each JSON record must be an object")
            if set(item) - WRITE_FIELDS:
                raise ValidationError("Unknown record fields: " + ", ".join(sorted(set(item) - WRITE_FIELDS)))
            if "content" not in item:
                raise ValidationError("Record content is required")
            if item.get("key") is not None and not isinstance(item["key"], str):
                raise ValidationError("Record key must be text")
            identity = "key:" + item["key"] if item.get("key") is not None else f"slot:{index}"
            digest = hashlib.sha256((os.path.normcase(str(original)) + "\0" + format + "\0" + identity).encode("utf-8")).hexdigest()
            provenance = f"{source.strip()}; import:{original}#{index + 1}"
            if "source" in item:
                if not isinstance(item["source"], str) or not item["source"].strip():
                    raise ValidationError("Record source must be nonempty text")
                provenance = f"{item['source'].strip()}; {provenance}"
            fields = dict(kind="event", title=f"Imported note {index + 1}", scope=scope,
                          status=status, key="import:" + digest)
            fields.update(item)
            fields["source"] = provenance
            fields["idempotency_key"] = "import:" + digest
            saved = store.remember(**fields)
            was_existing = saved["id"] in existing or saved["state"] != "active"
            result["skipped" if was_existing else "imported"] += 1
            existing.add(saved["id"])
            result["records"].append({"index": index + 1, "id": saved["id"], "state": saved["state"],
                                      "revision": saved["revision"], "status": saved["status"],
                                      "action": "existing" if was_existing else "imported"})
        except (ValidationError, ConflictError, TypeError, ValueError) as exc:
            result["errors"].append({"index": index + 1, "error": type(exc).__name__, "message": str(exc)})
    result["ok"] = not result["errors"]
    return result


import_memories = import_file
