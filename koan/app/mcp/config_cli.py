"""Print client configuration without modifying external files."""

import json
import sys
from pathlib import Path

from app.config import get_api_token, get_mcp_transport
from app.mcp.config import get_mcp_http_url


def main() -> int:
    repository = Path(__file__).resolve().parents[3]
    if get_mcp_transport() == "http":
        token = get_api_token()
        if not token:
            print(
                "Kōan MCP HTTP configuration requires the shared API token",
                file=sys.stderr,
            )
            return 1
        connection = {
            "type": "http",
            "url": get_mcp_http_url(),
            "headers": {"Authorization": f"Bearer {token}"},
        }
    else:
        connection = {
            "command": str(repository / ".venv" / "bin" / "python"),
            "args": [str(repository / "bin" / "koan-mcp")],
            "env": {"KOAN_ROOT": str(repository)},
        }
    snippet = {"mcpServers": {"koan": connection}}
    json.dump(snippet, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
