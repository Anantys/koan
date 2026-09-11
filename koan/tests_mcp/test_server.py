import asyncio
import builtins
import json

import pytest

from app.apiclient import ApiClientError
from app.cli.config import DEFAULT_TIMEOUT
from app.mcp.server import create_server


def _tools(server):
    return asyncio.run(server.list_tools())


def test_sdk_registers_curated_tools(api_spec_path):
    server = create_server(spec_path=api_spec_path, allow_destructive=False)
    tools = {tool.name: tool for tool in _tools(server)}

    assert "koan_status" in tools
    assert "koan_missions_delete" not in tools
    assert tools["koan_status"].annotations.read_only_hint is True
    assert tools["koan_missions_create"].annotations.read_only_hint is False


def test_sdk_registers_destructive_annotation(api_spec_path):
    server = create_server(spec_path=api_spec_path, allow_destructive=True)
    tools = {tool.name: tool for tool in _tools(server)}

    assert tools["koan_missions_delete"].annotations.destructive_hint is True


def test_all_fifteen_named_tools_dispatch(api_spec_path):
    calls = []

    class Client:
        def execute_operation(self, operation_id, **kwargs):
            calls.append(operation_id)
            return {"operation_id": operation_id}

    server = create_server(
        spec_path=api_spec_path,
        allow_destructive=True,
        client=Client(),
    )
    arguments = {
        "koan_health": {},
        "koan_status": {},
        "koan_missions_list": {},
        "koan_missions_get": {"mission_id": "mission-1"},
        "koan_missions_result": {"mission_id": "mission-1"},
        "koan_projects_list": {},
        "koan_usage": {},
        "koan_metrics": {},
        "koan_logs": {},
        "koan_config": {},
        "koan_missions_create": {"command": "/status"},
        "koan_missions_reorder": {
            "mission_id": "mission-1",
            "target_position": 1,
        },
        "koan_pause": {},
        "koan_resume": {},
        "koan_missions_delete": {"mission_id": "mission-1"},
    }

    for name, tool_arguments in arguments.items():
        result = asyncio.run(server.call_tool(name, tool_arguments))
        assert result.is_error is False

    assert len(calls) == 15


def test_exec_operation_reaches_unnamed_operation(api_spec_path):
    calls = []

    class Client:
        def execute_operation(self, operation_id, **kwargs):
            calls.append((operation_id, kwargs))
            return {"ok": True}

    server = create_server(
        spec_path=api_spec_path,
        allow_destructive=False,
        client=Client(),
    )
    result = asyncio.run(
        server.call_tool(
            "exec_operation",
            {
                "operation_id": "admin_shutdown_post",
                "path": {},
                "query": {},
                "body": {},
            },
        )
    )

    assert calls == [("admin_shutdown_post", {"path": {}, "query": {}, "body": {}})]
    assert json.loads(result.content[0].text) == {"ok": True}


def test_default_client_uses_the_cli_request_budget(monkeypatch, api_spec_path):
    """exec_operation reaches slow synchronous handlers; 10s would mis-report them."""
    captured = {}

    class Recorder:
        def __init__(self, spec_path, base_url, token, **kwargs):
            captured.update(kwargs)

        def execute_operation(self, operation_id, **kwargs):
            return {"ok": True}

    monkeypatch.setattr("app.mcp.server.RestApiClient", Recorder)
    monkeypatch.setattr("app.config.get_api_token", lambda: "secret")

    server = create_server(spec_path=api_spec_path)
    assert asyncio.run(server.call_tool("koan_status", {})).is_error is False
    assert captured["timeout"] == DEFAULT_TIMEOUT


def test_api_failure_becomes_actionable_tool_error(api_spec_path):
    class Client:
        def execute_operation(self, operation_id, **kwargs):
            raise ApiClientError.connection(
                "http://127.0.0.1:8420/v1/status",
                "connection refused",
            )

    server = create_server(spec_path=api_spec_path, client=Client())
    with pytest.raises(Exception, match="api.enabled.*make api-token.*make api"):
        asyncio.run(server.call_tool("koan_status", {}))


def test_disabled_entrypoint_refuses_to_run(monkeypatch, capsys):
    from app.mcp import __main__ as entrypoint

    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: False)

    assert entrypoint.main() == 1
    assert "mcp.enabled: true" in capsys.readouterr().err


def test_missing_sdk_prints_setup_command(monkeypatch, capsys):
    from app.mcp import __main__ as entrypoint

    real_import = builtins.__import__

    def missing_mcp(name, *args, **kwargs):
        if name == "app.mcp.server":
            error = ModuleNotFoundError("No module named 'mcp'")
            error.name = "mcp"
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: True)
    monkeypatch.setattr(builtins, "__import__", missing_mcp)

    assert entrypoint.main() == 1
    assert "make mcp-setup" in capsys.readouterr().err


def test_unreachable_startup_probe_warns_but_runs(monkeypatch, capsys):
    from app.mcp import __main__ as entrypoint

    ran = []

    class Server:
        def run(self, transport):
            ran.append(transport)

    class Probe:
        def __init__(self, *args, **kwargs):
            pass

        def execute_operation(self, operation_id):
            raise ApiClientError.connection("http://127.0.0.1:8420/v1/health", "refused")

    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: True)
    monkeypatch.setattr(entrypoint, "get_mcp_tools_allow_destructive", lambda: False)
    monkeypatch.setattr("app.mcp.server.create_server", lambda **kwargs: Server())
    monkeypatch.setattr("app.apiclient.RestApiClient", Probe)

    assert entrypoint.main() == 0
    assert ran == ["stdio"]
    warning = capsys.readouterr().err
    assert "warning" in warning
    assert "/v1/health" in warning


def test_entrypoint_reads_api_token_from_dotenv(monkeypatch, tmp_path):
    """Clients spawn this process with a bare env, so `.env` must still apply."""
    from app.mcp import __main__ as entrypoint

    (tmp_path / ".env").write_text('KOAN_API_TOKEN="from-dotenv"\n')
    monkeypatch.setenv("KOAN_API_TOKEN", "placeholder")
    monkeypatch.delenv("KOAN_API_TOKEN")
    monkeypatch.setattr("app.utils.KOAN_ROOT", tmp_path)

    tokens = []

    class Probe:
        def __init__(self, spec_path, base_url, token, **kwargs):
            tokens.append(token)

        def execute_operation(self, operation_id):
            return {"status": "ok"}

    class Server:
        def run(self, transport):
            pass

    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: True)
    monkeypatch.setattr(entrypoint, "get_mcp_transport", lambda: "stdio")
    monkeypatch.setattr(entrypoint, "get_mcp_tools_allow_destructive", lambda: False)
    monkeypatch.setattr("app.mcp.server.create_server", lambda **kwargs: Server())
    monkeypatch.setattr("app.apiclient.RestApiClient", Probe)

    assert entrypoint.main() == 0
    assert tokens == ["from-dotenv"]


def test_http_entrypoint_serves_with_pid_lock(monkeypatch, tmp_path):
    from app.mcp import __main__ as entrypoint

    calls = []
    lock = object()

    monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: True)
    monkeypatch.setattr(entrypoint, "get_mcp_transport", lambda: "http")
    monkeypatch.setattr(entrypoint, "get_mcp_host", lambda: "127.0.0.1")
    monkeypatch.setattr(entrypoint, "get_mcp_port", lambda: 8421)
    monkeypatch.setattr(entrypoint, "get_api_token", lambda: "secret")
    monkeypatch.setattr(entrypoint, "_load_server", lambda: object())
    monkeypatch.setattr(entrypoint, "_probe_api", lambda: None)
    monkeypatch.setattr(
        "app.pid_manager.acquire_pidfile",
        lambda root, name: calls.append(("acquire", root, name)) or lock,
    )
    monkeypatch.setattr(
        "app.pid_manager.release_pidfile",
        lambda value, root, name: calls.append(("release", value, root, name)),
    )
    monkeypatch.setattr(
        "app.mcp.http_transport.serve_http",
        lambda server, **kwargs: calls.append(("serve", kwargs)),
    )

    assert entrypoint.main() == 0
    assert calls[0] == ("acquire", tmp_path, "mcp")
    assert calls[1][0] == "serve"
    assert calls[1][1]["host"] == "127.0.0.1"
    assert calls[1][1]["port"] == 8421
    assert calls[2] == ("release", lock, tmp_path, "mcp")


def test_http_entrypoint_claims_pidfile_before_slow_startup(monkeypatch, tmp_path):
    """The process manager's verify timeout starts at launch, not at listen."""
    from app.mcp import __main__ as entrypoint

    order = []

    monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: True)
    monkeypatch.setattr(entrypoint, "get_mcp_transport", lambda: "http")
    monkeypatch.setattr(entrypoint, "get_mcp_host", lambda: "127.0.0.1")
    monkeypatch.setattr(entrypoint, "get_mcp_port", lambda: 8421)
    monkeypatch.setattr(entrypoint, "get_api_token", lambda: "secret")
    monkeypatch.setattr(
        entrypoint, "_load_server", lambda: order.append("load_server") or object()
    )
    monkeypatch.setattr(entrypoint, "_probe_api", lambda: order.append("probe_api"))
    monkeypatch.setattr(
        "app.pid_manager.acquire_pidfile",
        lambda root, name: order.append("acquire") or object(),
    )
    monkeypatch.setattr("app.pid_manager.release_pidfile", lambda *a: None)
    monkeypatch.setattr(
        "app.mcp.http_transport.serve_http",
        lambda server, **kwargs: order.append("serve"),
    )

    assert entrypoint.main() == 0
    assert order == ["acquire", "load_server", "probe_api", "serve"]


def test_http_entrypoint_refuses_missing_token(monkeypatch, tmp_path, capsys):
    from app.mcp import __main__ as entrypoint

    monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: True)
    monkeypatch.setattr(entrypoint, "get_mcp_transport", lambda: "http")
    monkeypatch.setattr(entrypoint, "get_api_token", lambda: "")
    monkeypatch.setattr(
        "app.mcp.http_transport.serve_http",
        lambda server, **kwargs: pytest.fail("listener must not start"),
    )

    assert entrypoint.main() == 1
    assert "refuses to start without a bearer token" in capsys.readouterr().err


def test_http_entrypoint_warns_for_non_loopback(monkeypatch, tmp_path, capsys):
    from app.mcp import __main__ as entrypoint

    monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
    monkeypatch.setattr(entrypoint, "get_mcp_enabled", lambda: True)
    monkeypatch.setattr(entrypoint, "get_mcp_transport", lambda: "http")
    monkeypatch.setattr(entrypoint, "get_mcp_host", lambda: "0.0.0.0")
    monkeypatch.setattr(entrypoint, "get_mcp_port", lambda: 8421)
    monkeypatch.setattr(entrypoint, "get_api_token", lambda: "secret")
    monkeypatch.setattr(entrypoint, "_load_server", lambda: object())
    monkeypatch.setattr(entrypoint, "_probe_api", lambda: None)
    monkeypatch.setattr("app.pid_manager.acquire_pidfile", lambda root, name: object())
    monkeypatch.setattr("app.pid_manager.release_pidfile", lambda lock, root, name: None)
    monkeypatch.setattr("app.mcp.http_transport.serve_http", lambda server, **kwargs: None)

    assert entrypoint.main() == 0
    assert "non-loopback" in capsys.readouterr().err
