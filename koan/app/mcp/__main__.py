"""Launch Kōan's optional MCP server over stdio or Streamable HTTP."""

import ipaddress
import os
import sys
from pathlib import Path

from app.config import (
    get_api_token,
    get_mcp_enabled,
    get_mcp_host,
    get_mcp_port,
    get_mcp_tools_allow_destructive,
    get_mcp_transport,
)


def _load_server():
    from app.mcp.server import create_server

    return create_server(
        allow_destructive=get_mcp_tools_allow_destructive(),
    )


def _probe_api() -> None:
    from app.apiclient import ApiClientError, RestApiClient
    from app.config import get_api_token
    from app.mcp.config import get_api_base_url
    from app.mcp.server import DEFAULT_SPEC

    try:
        RestApiClient(
            DEFAULT_SPEC,
            get_api_base_url(),
            get_api_token(),
            timeout=1,
        ).execute_operation("health_get")
    except ApiClientError as exc:
        print(f"Kōan MCP warning: {exc}", file=sys.stderr)


def _warn_non_loopback(host: str) -> None:
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname, not a literal IP: we cannot prove it is loopback, so
        # treat it as non-loopback rather than silently skipping the warning.
        is_loopback = False
    if not is_loopback:
        print(
            f"WARNING: MCP HTTP bound to non-loopback address {host}.\n"
            "         Use a reverse proxy with TLS for external exposure.",
            file=sys.stderr,
        )


def main() -> int:
    if not get_mcp_enabled():
        print(
            "Kōan MCP server disabled; set mcp.enabled: true in instance/config.yaml",
            file=sys.stderr,
        )
        return 1

    transport = get_mcp_transport()
    koan_root = Path(os.environ.get("KOAN_ROOT", ""))
    if transport == "http":
        if not koan_root.is_dir():
            print("ERROR: KOAN_ROOT must be set to a valid directory", file=sys.stderr)
            return 1
        if not get_api_token():
            print(
                "ERROR: Kōan MCP HTTP refuses to start without a bearer token",
                file=sys.stderr,
            )
            return 1

    try:
        server = _load_server()
    except ModuleNotFoundError as exc:
        if exc.name == "mcp" or (exc.name or "").startswith("mcp."):
            print(
                "Kōan MCP SDK missing; run `make mcp-setup`",
                file=sys.stderr,
            )
            return 1
        raise

    _probe_api()
    if transport == "stdio":
        server.run("stdio")
        return 0

    from app.mcp.http import serve_http
    from app.pid_manager import acquire_pidfile, release_pidfile

    from app.mcp.config import get_mcp_http_url

    host = get_mcp_host()
    port = get_mcp_port()
    _warn_non_loopback(host)
    lock = acquire_pidfile(koan_root, "mcp")
    try:
        print(f"Kōan MCP HTTP listening on {get_mcp_http_url()}", flush=True)
        serve_http(
            server,
            host=host,
            port=port,
            audit_path=koan_root / "logs" / "mcp.log",
        )
    finally:
        release_pidfile(lock, koan_root, "mcp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
