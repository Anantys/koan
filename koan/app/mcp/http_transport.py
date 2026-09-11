"""Authenticated Streamable HTTP adapter for Kōan's MCP server.

Deliberately *not* named ``http``: the daemon is launched as a script
(``app/mcp/__main__.py``), which puts this directory first on ``sys.path``, so
a module named ``http`` here would shadow the stdlib package that uvicorn and
starlette import.
"""

import sys
import time
from pathlib import Path

import uvicorn
from starlette.responses import JSONResponse

from app.api import auth as api_auth


class BearerAuditMiddleware:
    """Authenticate every HTTP request and write an audit line per response.

    Missing or empty credentials return 401; invalid (or unconfigured-token)
    credentials return 403 — matching the REST API's ``require_token``
    contract. Valid requests are delegated to the wrapped ASGI app unchanged.
    """

    def __init__(self, app, audit_path: Path):
        self.app = app
        self.audit_path = audit_path
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # Latched by the first failed audit write; cleared once the sink
        # accepts a write again. While it is set the middleware serves nothing.
        self.audit_broken = False

    def _warn_off_audit_sink(self, message: str) -> None:
        """Report an audit failure through a channel that is not the audit sink.

        The launcher redirects this daemon's stderr into ``logs/mcp.log`` — the
        same file ``audit_path`` points at — so a warning printed there lands in
        the exact file that just refused a write. Try the REST API's log first,
        and only fall back to stderr when that fails too.
        """
        fallback = self.audit_path.parent / "api.log"
        if fallback != self.audit_path:
            try:
                with open(fallback, "a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
                return
            except OSError:
                pass
        print(message, file=sys.stderr)

    def _audit_sink_usable(self) -> bool:
        """Re-probe a latched-broken audit sink, clearing the latch on success."""
        try:
            with open(self.audit_path, "a", encoding="utf-8"):
                pass
        except OSError:
            return False
        self.audit_broken = False
        self._warn_off_audit_sink(
            f"Kōan MCP audit recovered: {self.audit_path} is writable again"
        )
        return True

    def _write_audit(self, scope: dict, status: int) -> None:
        client = scope.get("client")
        peer = client[0] if client else "-"
        method = scope.get("method", "-")
        path = scope.get("path", "-")
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
            f"{peer} {method} {path} {status}\n"
        )
        try:
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            # An unwritable audit path is a security-observability gap. Warn
            # somewhere that is still readable, then latch: the next request is
            # refused rather than served with no record of it.
            if not self.audit_broken:
                self.audit_broken = True
                self._warn_off_audit_sink(
                    f"Kōan MCP audit warning: cannot write {self.audit_path}: "
                    f"{exc}; refusing further requests until it is writable"
                )

    async def _reject_unsupported(self, scope, send) -> None:
        """Fail closed on a scope type this middleware cannot authenticate."""
        self._write_audit(scope, 403)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # Startup/shutdown carries no credentials and no request to audit.
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Allow-list, not "anything but HTTP": a transport added later must
            # not inherit an unauthenticated, unaudited path by default.
            await self._reject_unsupported(scope, send)
            return

        if self.audit_broken and not self._audit_sink_usable():
            # Fail closed: the spec promises every HTTP request is audited, so
            # serve nothing while the trail cannot be written.
            response = JSONResponse(
                {
                    "error": {
                        "code": "audit_unavailable",
                        "message": "Audit log is not writable",
                    }
                },
                status_code=503,
            )
            await response(scope, receive, send)
            return

        logged = False

        async def audited_send(message):
            nonlocal logged
            # Log on response.start, not completion: a successful MCP GET
            # stream can stay open for a long time.
            if message["type"] == "http.response.start" and not logged:
                self._write_audit(scope, message["status"])
                logged = True
            await send(message)

        headers = {
            key.lower(): value
            for key, value in scope.get("headers", ())
        }
        value = headers.get(b"authorization", b"").decode("latin-1")
        if not value.startswith("Bearer "):
            response = JSONResponse(
                {
                    "error": {
                        "code": "missing_token",
                        "message": "Authorization header required",
                    }
                },
                status_code=401,
            )
            await response(scope, receive, audited_send)
            return

        token = value[len("Bearer "):]
        if not token:
            response = JSONResponse(
                {
                    "error": {
                        "code": "missing_token",
                        "message": "Token is empty",
                    }
                },
                status_code=401,
            )
            await response(scope, receive, audited_send)
            return

        if not api_auth.check_token(token):
            response = JSONResponse(
                {
                    "error": {
                        "code": "invalid_token",
                        "message": "Invalid token",
                    }
                },
                status_code=403,
            )
            await response(scope, receive, audited_send)
            return

        try:
            await self.app(scope, receive, audited_send)
        except Exception:
            if not logged:
                self._write_audit(scope, 500)
            raise


def build_http_app(server, *, host: str, audit_path: Path):
    """Wrap the SDK-generated Streamable HTTP app with Kōan auth and audit."""
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        host=host,
    )
    return BearerAuditMiddleware(app, audit_path)


def serve_http(server, *, host: str, port: int, audit_path: Path) -> None:
    uvicorn.run(
        build_http_app(server, host=host, audit_path=audit_path),
        host=host,
        port=port,
        access_log=False,
    )
