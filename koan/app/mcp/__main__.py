"""Launch Kōan's optional MCP stdio server."""

import sys

from app.config import get_mcp_enabled, get_mcp_tools_allow_destructive


def main() -> int:
    if not get_mcp_enabled():
        print(
            "Kōan MCP server disabled; set mcp.enabled: true in instance/config.yaml",
            file=sys.stderr,
        )
        return 1

    try:
        from app.mcp.server import create_server
    except ModuleNotFoundError as exc:
        if exc.name == "mcp" or (exc.name or "").startswith("mcp."):
            print(
                "Kōan MCP SDK missing; run `make mcp-setup`",
                file=sys.stderr,
            )
            return 1
        raise

    server = create_server(
        allow_destructive=get_mcp_tools_allow_destructive(),
    )
    try:
        from app.apiclient import ApiClientError, RestApiClient
        from app.mcp.server import DEFAULT_SPEC
        from app.mcp.config import get_api_base_url
        from app.config import get_api_token

        probe = RestApiClient(
            DEFAULT_SPEC,
            get_api_base_url(),
            get_api_token(),
            timeout=1,
        )
        probe.execute_operation("health_get")
    except ApiClientError as exc:
        print(f"Kōan MCP warning: {exc}", file=sys.stderr)

    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
