"""MCP settings derived from Kōan's API configuration."""

from app.config import get_api_host, get_api_port, get_mcp_host, get_mcp_port


def get_api_base_url() -> str:
    host = get_api_host().strip()
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{get_api_port()}"


def get_mcp_http_url() -> str:
    """Return the loopback HTTP URL MCP clients use to reach the daemon.

    Wildcard bind addresses are mapped to loopback only for this generated
    client URL — the daemon itself still binds whatever ``mcp.host`` names.
    IPv6 hosts get bracket-wrapped.
    """
    host = get_mcp_host()
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{get_mcp_port()}/mcp"
