import asyncio
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.mcp.http_transport import BearerAuditMiddleware, build_http_app
from app.mcp.server import create_server


async def _ok(_request):
    return JSONResponse({"ok": True})


def _client(audit_path):
    downstream = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    return TestClient(BearerAuditMiddleware(downstream, audit_path))


def test_missing_and_empty_tokens_return_401(tmp_path):
    client = _client(tmp_path / "mcp.log")
    assert client.get("/mcp").status_code == 401
    assert client.get(
        "/mcp", headers={"Authorization": "Bearer "}
    ).status_code == 401


def test_wrong_or_unconfigured_token_returns_403(tmp_path):
    client = _client(tmp_path / "mcp.log")
    with patch("app.api.auth._get_token", return_value="expected"):
        assert client.get(
            "/mcp", headers={"Authorization": "Bearer wrong"}
        ).status_code == 403
    with patch("app.api.auth._get_token", return_value=""):
        assert client.get(
            "/mcp", headers={"Authorization": "Bearer supplied"}
        ).status_code == 403


def test_correct_token_uses_constant_time_check_and_reaches_app(tmp_path):
    client = _client(tmp_path / "mcp.log")
    with patch("app.api.auth._get_token", return_value="expected"), patch(
        "app.api.auth.hmac.compare_digest",
        return_value=True,
    ) as compare:
        response = client.get(
            "/mcp", headers={"Authorization": "Bearer candidate"}
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    compare.assert_called_once_with(b"expected", b"candidate")


def test_audit_contains_no_authorization_value(tmp_path):
    audit_path = tmp_path / "mcp.log"
    client = _client(audit_path)
    with patch("app.api.auth._get_token", return_value="secret-token"):
        response = client.get(
            "/mcp", headers={"Authorization": "Bearer secret-token"}
        )

    line = audit_path.read_text()
    assert response.status_code == 200
    assert " GET /mcp 200" in line
    assert "secret-token" not in line
    assert "Authorization" not in line


def test_rejected_request_is_audited(tmp_path):
    audit_path = tmp_path / "mcp.log"
    client = _client(audit_path)
    response = client.get("/mcp")
    assert response.status_code == 401
    line = audit_path.read_text()
    assert " GET /mcp 401" in line


def test_audit_failure_is_reported_off_the_audit_sink(tmp_path):
    """The warning must not go to the file that just refused a write.

    The launcher redirects the daemon's stderr into `logs/mcp.log`, which is
    `audit_path`, so stderr is the one place the operator cannot read it from.
    """
    audit_path = tmp_path / "mcp.log"
    audit_path.mkdir()  # a directory: every append raises OSError
    client = _client(audit_path)

    assert client.get("/mcp").status_code == 401

    assert "cannot write" in (tmp_path / "api.log").read_text()


def test_unauditable_requests_are_refused_not_served(tmp_path):
    """Fail closed: no audit trail, no service."""
    audit_path = tmp_path / "mcp.log"
    audit_path.mkdir()
    client = _client(audit_path)

    # The first request discovers the broken sink (only the write can), and
    # latches. Everything after it is refused before reaching the app.
    client.get("/mcp")
    with patch("app.api.auth._get_token", return_value="secret-token"):
        response = client.get(
            "/mcp", headers={"Authorization": "Bearer secret-token"}
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_unavailable"


def test_service_resumes_once_the_audit_sink_is_writable(tmp_path):
    audit_path = tmp_path / "mcp.log"
    audit_path.mkdir()
    downstream = Starlette(routes=[Route("/mcp", _ok, methods=["GET"])])
    middleware = BearerAuditMiddleware(downstream, audit_path)
    client = TestClient(middleware)

    client.get("/mcp")
    assert middleware.audit_broken is True

    audit_path.rmdir()
    with patch("app.api.auth._get_token", return_value="secret-token"):
        response = client.get(
            "/mcp", headers={"Authorization": "Bearer secret-token"}
        )

    assert response.status_code == 200
    assert middleware.audit_broken is False
    assert " GET /mcp 200" in audit_path.read_text()


def test_lifespan_scope_passes_through_unauthenticated(tmp_path):
    seen = []

    async def downstream(scope, receive, send):
        seen.append(scope["type"])

    middleware = BearerAuditMiddleware(downstream, tmp_path / "mcp.log")
    asyncio.run(middleware({"type": "lifespan"}, None, None))

    assert seen == ["lifespan"]


def test_unknown_scope_is_refused_not_forwarded(tmp_path):
    audit_path = tmp_path / "mcp.log"
    forwarded = []
    sent = []

    async def downstream(scope, receive, send):
        forwarded.append(scope["type"])

    async def send(message):
        sent.append(message)

    middleware = BearerAuditMiddleware(downstream, audit_path)
    scope = {"type": "websocket", "path": "/mcp", "headers": ()}
    asyncio.run(middleware(scope, None, send))

    assert forwarded == []
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert " /mcp 403" in audit_path.read_text()


def test_stdio_and_http_servers_share_identical_tools(api_spec_path, tmp_path):
    stdio_server = create_server(
        spec_path=api_spec_path,
        allow_destructive=True,
    )
    http_server = create_server(
        spec_path=api_spec_path,
        allow_destructive=True,
    )
    build_http_app(
        http_server,
        host="127.0.0.1",
        audit_path=tmp_path / "mcp.log",
    )

    stdio_tools = asyncio.run(stdio_server.list_tools())
    http_tools = asyncio.run(http_server.list_tools())

    assert [
        (tool.name, tool.annotations)
        for tool in http_tools
    ] == [
        (tool.name, tool.annotations)
        for tool in stdio_tools
    ]
