"""Apply stable title prefixes through Codex's local, supported app-server RPC.

No model turn, thread creation/resume, network listener, or direct host DB writes.
Only metadata for the native thread being handled is read.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from .session_registry import SessionRegistry
from .store import Store
from . import __version__


class HostTitleError(RuntimeError):
    """A metadata RPC failed; raw server output is deliberately not retained."""


class TitleClient:
    """Short lived stdio connection with a total deadline, including shutdown."""

    def __init__(self, binary: str, codex_home: str, timeout: float = 4):
        self.binary = binary
        self.codex_home = codex_home
        self.timeout = timeout
        self.process = None
        self.reader = None
        self.lines = queue.SimpleQueue()
        self.sequence = 0

    def __enter__(self):
        self.deadline = time.monotonic() + self.timeout
        environment = dict(os.environ, CODEX_HOME=self.codex_home)
        self.process = subprocess.Popen(
            [self.binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {
                "name": "local_title_sync", "title": "Local memory title sync", "version": __version__,
            }})
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def _read(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self.lines.put(None)

    def _send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        if method not in {"initialize", "thread/read", "thread/name/set"}:
            raise HostTitleError("Unsupported title operation")
        self.sequence += 1
        self._send({"id": self.sequence, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Title RPC timed out")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Title RPC timed out") from exc
            if line is None:
                raise HostTitleError("Title RPC closed")
            reply = json.loads(line)
            if reply.get("id") != self.sequence:
                continue
            if "error" in reply or not isinstance(reply.get("result"), dict):
                raise HostTitleError("Title RPC was rejected")
            return reply["result"]

    def read_title(self, thread_id):
        thread = self.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread", {})
        if thread.get("id") != thread_id:
            raise HostTitleError("Host returned a different thread")
        title = thread.get("name")
        if title is not None and not isinstance(title, str):
            raise HostTitleError("Host returned an invalid title")
        return title or ""

    def set_title(self, thread_id, title):
        self.call("thread/name/set", {"threadId": thread_id, "name": title})

    def __exit__(self, *unused):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.3)
        if self.reader:
            self.reader.join(timeout=0.3)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()


def synchronize(home, row, *, source):
    """Read, prefix, read back, then record. Failure never claims a host rename."""
    from .integration import _atomic, _best_effort_log, _read_json

    config = _read_json(Path(home) / "settings.json", {}).get("host_title_sync", {})
    if not config.get("enabled"):
        return {"status": "disabled"}
    if row.get("platform") != "codex" or row.get("host") != "local":
        return {"status": "unsupported_host"}
    native_id = row["thread_id"]
    try:
        uuid.UUID(native_id)
    except (ValueError, TypeError, AttributeError):
        return {"status": "unsupported_identity"}
    status = {"code": row["code"], "thread_id": native_id,
              "checked_at": datetime.now(timezone.utc).isoformat(), "source": source,
              "status": "pending", "changed": False}
    try:
        configured = config.get("binary", "")
        binary = configured if configured and Path(configured).is_file() else shutil.which("codex")
        if not binary:
            raise FileNotFoundError("Local Codex executable unavailable")
        codex_home = config.get("codex_home") or str(Path.home() / ".codex")
        timeout = min(5.0, max(0.5, float(config.get("timeout_seconds", 4))))
        with TitleClient(binary, codex_home, timeout) as client:
            title = client.read_title(native_id)
            desired = SessionRegistry.prefixed_title(row["code"], title or "新会话")
            if title != desired:
                # Re-read just before the write to preserve a recent manual rename.
                title = client.read_title(native_id)
                desired = SessionRegistry.prefixed_title(row["code"], title or "新会话")
                if title != desired:
                    client.set_title(native_id, desired)
                    status["changed"] = True
            actual = client.read_title(native_id)
            if not actual or SessionRegistry.prefixed_title(row["code"], actual) != actual:
                raise HostTitleError("Title readback did not verify its prefix")
        registry = SessionRegistry(Store(home))
        latest = registry.show(row["code"])
        if latest["thread_id"] != native_id:
            raise HostTitleError("Registry identity changed")
        observed = registry.observe_title(
            code=row["code"], title=actual, expected_revision=latest["revision"],
            source="Local Codex app-server readback; " + source,
        )
        sync = observed.get("sync", {})
        status.update(status="ok", registry_revision=observed["revision"],
                      registry_sync_ok=bool(sync.get("ok") and not sync.get("summary_errors")))
    except Exception as exc:
        status.update(status="pending", error_type=type(exc).__name__)
        _best_effort_log(home, exc)
    try:
        _atomic(Path(home) / "title-sync" / (native_id + ".json"),
                json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        _best_effort_log(home, exc)
    return status
