import asyncio
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.mcp.http import BearerAuditMiddleware, build_http_app
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
