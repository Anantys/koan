"""Optional MCP front-end for Kōan's REST API.

Two transports share one tool registry built through ``app.mcp.server``:

- ``stdio`` — client-launched subprocess, no listener (the default).
- ``http`` — authenticated Streamable HTTP served at ``/mcp`` as the
  ``mcp`` daemon (see ``app.mcp.http_transport``).
"""
