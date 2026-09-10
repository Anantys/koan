"""MCP settings derived from Kōan's API configuration."""

from app.config import get_api_host, get_api_port


def get_api_base_url() -> str:
    host = get_api_host().strip()
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{get_api_port()}"
