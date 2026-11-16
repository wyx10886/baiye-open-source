"""Loopback-only memory workbench; no external assets or third-party services."""

from __future__ import annotations

import hmac
import json
import secrets
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .store import ConflictError, Store, ValidationError

MAX_BODY = 131_072


class MemoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, MemoryHandler)


class MemoryHandler(BaseHTTPRequestHandler):
    server: MemoryHTTPServer
    server_version = f"LocalMemory/{__version__}"

    def log_message(self, fmt, *args):
        # Never log query text, memory contents, or the CSRF token.
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _safe_request(self, *, api=False):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "").lower()
        if host not in allowed:
            self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": "不允许的访问主机"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{v}" for v in allowed}:
            self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": "拒绝跨来源请求"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": "拒绝跨站请求"})
            return False
        if api and not hmac.compare_digest(self.headers.get("X-Local-Memory-Token", ""), self.server.token):
            self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": "工作台凭证无效，请刷新页面"})
            return False
        return True

    def _json_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValidationError("不支持分块请求")
        if self.headers.get_content_type() != "application/json":
            raise ValidationError("请求必须使用 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("无效的请求长度") from exc
        if not 0 < length <= MAX_BODY:
            raise ValidationError("请求为空或超过 128 KiB")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ValidationError("JSON 格式无效") from exc
        if not isinstance(value, dict):
            raise ValidationError("JSON 请求必须为对象")
        return value

    def _run(self, callback):
        try:
            self._send(HTTPStatus.OK, {"ok": True, "data": callback()})
        except ConflictError as exc:
            self._send(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc), "type": "conflict"})
        except (ValidationError, TypeError, ValueError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc), "type": "validation"})
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "操作失败，请检查本地服务状态"})

    def do_GET(self):
        url = urlsplit(self.path)
        if not self._safe_request(api=url.path.startswith("/api/")):
            return
        if url.path in {"/", "/index.html"}:
            template = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")
            html = template.replace("__LOCAL_TOKEN__", self.server.token)
            self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        params = parse_qs(url.query, keep_blank_values=True)
        one = lambda name, default="": params.get(name, [default])[0]
        if url.path == "/api/daily-chat":
            from .daily_chat import DailyChat
            return self._run(lambda: DailyChat(self.server.store).show())
        if url.path == "/api/sessions":
            from .session_registry import SessionRegistry
            registry = SessionRegistry(self.server.store)
            return self._run(lambda: registry.list(query=one("q"), project=one("project") or None))
        if url.path == "/api/stats":
            return self._run(self.server.store.stats)
        if url.path in {"/api/projects", "/api/project-summary"}:
            from .work_summary import WorkSummary
            summaries = WorkSummary(self.server.store)
            return self._run(summaries.projects if url.path == "/api/projects" else lambda: summaries.read(one("project")))
        if url.path == "/api/cleanup":
            from .retention import Retention
            return self._run(lambda: Retention(self.server.store.home).status())
        if url.path == "/api/audit":
            return self._run(lambda: self.server.store.audit(limit=50))
        if url.path == "/api/memories":
            def memories():
                scope = one("scope", "global")
                status = one("status") or None
                kind = one("kind") or None
                query = one("q").strip()
                history = one("history") == "1"
                if query and not history and scope != "*":
                    rows = self.server.store.search(
                        query, scope=scope, include_global=one("global", "1") == "1",
                        limit=200, statuses=[status] if status else ["confirmed", "candidate", "unverified"],
                        include_persona=True,
                    )
                    return [r for r in rows if kind is None or r["kind"] == kind]
                rows = self.server.store.list_memories(
                    scope=None if scope == "*" else scope, status=status, kind=kind,
                    include_inactive=history, limit=500,
                )
                if query:
                    needle = query.casefold()
                    rows = [r for r in rows if needle in (r["title"] + " " + r["content"] + " " + " ".join(r["tags"])).casefold()]
                return rows
            return self._run(memories)
        if url.path == "/api/context":
            project = one("project") or None
            return self._run(lambda: self.server.store.context(one("q"), project=project, include_persona=one("persona") == "1"))
        self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "页面不存在"})

    def do_POST(self):
        url = urlsplit(self.path)
        if not self._safe_request(api=True):
            return
        def action():
            body = self._json_body()
            store = self.server.store
            if url.path == "/api/daily-chat":
                from .daily_chat import DailyChat
                return DailyChat(store).bind(**body)
            if url.path == "/api/session-update":
                from .session_registry import dispatch_registry
                return dispatch_registry(store, **body)
            if url.path == "/api/project-checkpoint":
                from .work_summary import WorkSummary
                return WorkSummary(store).checkpoint(**body)
            if url.path.startswith("/api/cleanup/"):
                from .retention import Retention
                cleanup = Retention(store.home)
                operation = url.path.rsplit("/", 1)[-1]
                if operation == "scan" and not body:
                    return cleanup.scan()
                if operation == "decision":
                    return cleanup.decision(**body)
                if operation == "acknowledge" and set(body) == {"proposal_ids"}:
                    return cleanup.acknowledge(body["proposal_ids"])
                if operation == "restore" and set(body) == {"job_id"}:
                    return cleanup.restore(body["job_id"])
                raise ValidationError("不支持的处理记录操作")
            if url.path == "/api/memories":
                allowed = {"kind", "title", "content", "source", "scope", "status", "key", "event_date", "importance", "tags", "valid_until", "idempotency_key"}
                if set(body) - allowed:
                    raise ValidationError("不支持的记忆字段")
                return store.remember(**body)
            parts = url.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "memories"]:
                memory_id, operation = parts[2:]
                if operation == "revise":
                    return store.revise(memory_id, **body)
                if operation == "forget":
                    return store.forget(memory_id, **body)
            if url.path == "/api/review":
                return store.review(**body)
            if url.path == "/api/backup":
                if body:
                    raise ValidationError("备份位置由本机配置决定")
                from .session_registry import SessionRegistry
                return {"path": store.backup(), **SessionRegistry(store).backup()}
            if url.path == "/api/export":
                if body:
                    raise ValidationError("导出位置由本机配置决定")
                return {"path": store.export_markdown()}
            raise ValidationError("未知操作")
        self._run(action)

    def do_OPTIONS(self):
        self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": "不接受跨站预检"})


def make_server(home, host="127.0.0.1", port=0):
    """Create an unstarted server; token is available to local integration tests."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("工作台只允许绑定本机回环地址")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("端口必须介于 0 和 65535")
    return MemoryHTTPServer((host, port), Store(home))


def serve(home, host="127.0.0.1", port=8765, open_browser=False):
    server = make_server(home, host, port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"本地协作记忆工作台：{url}", file=sys.stderr, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
