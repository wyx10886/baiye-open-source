"""Verify a reviewed source manifest without reading any deployed memory home.

Private deny strings are supplied from OUTSIDE the release tree. Findings never
echo their matched text. A manifest records review scope; it is not a signature.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "release-manifest.json"
ALLOWED_SUFFIXES = {".py", ".md", ".html", ".yaml", ".toml", ".in"}
ALLOWED_NAMES = {"LICENSE", ".gitignore"}
PATTERNS = {
    "credential-token": re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "profile-path": re.compile(r"(?:[A-Za-z]:[/\\]Users[/\\]|/(?:Users|home)/)[^\s\"'`/\\]+", re.I),
    "email-literal": re.compile(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "credential-assignment": re.compile(r'''(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*["'][A-Za-z0-9_+/=-]{20,}["']'''),
}
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def private_strings(path):
    if path is None:
        return []
    path = path.resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("Private deny list must be outside the release tree")
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, list) or not all(isinstance(x, str) and x for x in values):
        raise ValueError("Deny list must be a JSON array of nonempty strings")
    return [value.casefold() for value in values]


def inventory():
    files = {}
    for directory, dirs, names in os.walk(ROOT, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(directory) / name
            meta = path.lstat()
            if path.is_symlink() or getattr(meta, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked/reparse path rejected: " + path.relative_to(ROOT).as_posix())
        for name in names:
            path = Path(directory) / name
            rel = path.relative_to(ROOT).as_posix()
            if rel == MANIFEST:
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Non-regular path rejected: " + rel)
            if path.suffix not in ALLOWED_SUFFIXES and name not in ALLOWED_NAMES:
                raise ValueError("Unreviewed file type: " + rel)
            if any(part.startswith(".") for part in path.relative_to(ROOT).parts) and rel != ".gitignore":
                raise ValueError("Unreviewed hidden path: " + rel)
            raw = path.read_bytes()
            if b"\x00" in raw:
                raise ValueError("Binary content rejected: " + rel)
            raw.decode("utf-8")
            files[rel] = raw
    return files


def scan(files, forbidden):
    findings = []
    for rel, raw in files.items():
        content = raw.decode("utf-8")
        folded = (rel + "\n" + content).casefold()
        if any(value in folded for value in forbidden):
            findings.append({"path": rel, "category": "private-marker"})
        for category, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append({"path": rel, "category": category})
        # Only explicitly synthetic all-zero-prefix UUID fixtures are allowed.
        for match in UUID.findall(content):
            if match != "00000000-0000-4000-8000-000000000001":
                findings.append({"path": rel, "category": "native-identity-literal"})
                break
    return findings


def manifest_for(files):
    return {"schema": 1, "skill": "local-collaboration-memory", "version": "1.0.1",
            "scope": "Reviewed UTF-8 source only; manifest excluded from its own hashes",
            "files": [{"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                      for path, raw in sorted(files.items())]}


def verify_manifest(files, manifest):
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ValueError("Unsupported manifest")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ValueError("Missing manifest entries")
    names = [row["path"] for row in rows]
    if len(set(names)) != len(names) or set(names) != set(files):
        raise ValueError("Manifest paths differ from the complete release tree")
    for row in rows:
        raw = files[row["path"]]
        if row["bytes"] != len(raw) or row["sha256"] != hashlib.sha256(raw).hexdigest():
            raise ValueError("Manifest content mismatch: " + row["path"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / MANIFEST)
    parser.add_argument("--deny-list", type=Path)
    parser.add_argument("--write-manifest", action="store_true", help="Only after independently reviewing the exact source tree")
    parser.add_argument("--archive", type=Path, help="Create a deterministic source-only ZIP after successful verification")
    args = parser.parse_args(argv)
    try:
        manifest_path = args.manifest.resolve()
        if manifest_path != ROOT / MANIFEST:
            raise ValueError("Manifest must be the release root's release-manifest.json")
        files = inventory()
        findings = scan(files, private_strings(args.deny_list))
        if findings:
            print(json.dumps({"ok": False, "findings": findings}, ensure_ascii=False))
            return 1
        if args.write_manifest:
            manifest_path.write_text(json.dumps(manifest_for(files), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        manifest_raw = manifest_path.read_bytes()
        verify_manifest(files, json.loads(manifest_raw))
        if scan({MANIFEST: manifest_raw}, private_strings(args.deny_list)):
            raise ValueError("Manifest text failed the privacy scan")
        if args.archive:
            archive = args.archive.resolve()
            if archive == ROOT or ROOT in archive.parents or archive.exists():
                raise ValueError("Archive must be a new file outside the release tree")
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
                for rel, raw in sorted({**files, MANIFEST: manifest_raw}.items()):
                    parts = PurePosixPath(rel).parts
                    if ".." in parts or PurePosixPath(rel).is_absolute():
                        raise ValueError("Unsafe archive member")
                    info = zipfile.ZipInfo("local-collaboration-memory/" + rel, (2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    bundle.writestr(info, raw)
        print(json.dumps({"ok": True, "source_files": len(files), "published_files": len(files) + 1,
                          "source_bytes": sum(map(len, files.values())), "privacy_findings": 0,
                          "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest()}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, UnicodeError) as exc:
        # Avoid printing exception text: it may embed a private absolute path.
        print(json.dumps({"ok": False, "error": type(exc).__name__, "reason": "Release verification failed; inspect the local source/manifest privately"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
