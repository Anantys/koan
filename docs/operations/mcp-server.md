---
type: doc
title: "MCP Server"
description: "Configure Kōan's opt-in stdio MCP server for Claude Code, Claude Desktop, and other local MCP clients."
tags: [operations]
created: 2026-09-09
updated: 2026-09-09
---

# MCP Server

Kōan can expose a curated part of its REST API as local MCP tools. MCP clients
launch this server as a subprocess and communicate over stdio. No new port or
network service gets created; tool calls go through the existing REST API and
appear in `logs/api.log`.

Both layers default off. Enable the REST API, configure its bearer token, and
then enable MCP:

```yaml
api:
  enabled: true
  host: "127.0.0.1"
  port: 8420

mcp:
  enabled: true
  tools_allow_destructive: false
```

MCP derives its URL from `api.host` and `api.port`. It gets the token from
`KOAN_API_TOKEN`, falling back to `api.token`; no MCP-specific secret exists.
With `mcp.enabled: false` or no `mcp` mapping, the subprocess refuses to run and
prints the setting needed to enable it.

## Install and configure

```bash
make api-token    # generate the shared bearer token
make api          # run the required REST API
make mcp-setup    # install optional SDK dependencies
make mcp-config   # print JSON for your MCP client configuration
```

Paste the printed `mcpServers.koan` block into Claude Code, Claude Desktop, or
another stdio-capable client. `make mcp-config` only prints JSON; it never edits
client configuration. Paths are absolute so clients can launch Kōan outside
the checkout's current directory.

Use `make mcp` to run the server in a terminal while debugging. MCP clients
normally launch `bin/koan-mcp` using the checkout virtual environment, as shown
by `make mcp-config`.

## Optional dependency

Core Kōan does not install the MCP SDK because MCP defaults off and the SDK adds
a substantial async/web and validation stack. `koan/requirements-mcp.txt`
currently installs MCP SDK 2.x and its direct dependency families, including
Pydantic, AnyIO, HTTPX 2/HTTPCore 2, Starlette, Uvicorn, JSON Schema tooling,
multipart/SSE support, OpenTelemetry API, JWT, and cryptography. Keeping that
stack separate preserves the lean main requirements and Python test matrix.

Running `bin/koan-mcp` without the SDK prints `run make mcp-setup` and exits
without a traceback.

## Available tools

Fourteen tools appear by default: health, status, mission list/get/result,
project list, usage, metrics, logs, masked config, mission create/reorder,
pause, and resume. Names begin with `koan_`. Their input schemas describe path,
query, and write-body fields, and read-only hints let clients avoid unnecessary
write confirmations.

`koan_missions_delete` becomes the fifteenth named tool only when:

```yaml
mcp:
  enabled: true
  tools_allow_destructive: true
```

It carries a destructive hint. Shutdown, restart, updates, and all project
create/update/delete operations never appear as named tools.

`exec_operation` remains a deliberately conservative escape hatch. It accepts
an OpenAPI `operation_id`, `path`, `query`, and optional JSON `body`, and can
reach any operation in `koan/openapi.yaml`. Because this includes administrative
operations, it always carries a destructive hint.

Named exposure fails closed. A REST route must carry its explicit MCP marker
and appear in Kōan's fixed curation table. Adding a route to OpenAPI alone never
publishes a model-facing tool.

## API downtime

An unreachable REST API does not prevent MCP startup. The server logs a warning
to stderr and remains available, allowing recovery without restarting the MCP
client. Failed tool calls include the attempted URL and these repairs:

1. Set `api.enabled: true` in `instance/config.yaml`.
2. Run `make api-token` and configure its token.
3. Start the API with `make api`.

## Provider MCP configuration compatibility

Kōan can also load third-party MCP servers into its own CLI provider roles.
Those client config paths now use `mcp.configs` when the mapping above exists:

```yaml
mcp:
  enabled: true
  tools_allow_destructive: false
  configs:
    - "/path/to/mcp-config.json"
```

Legacy top-level list syntax remains accepted for provider configuration, but
cannot also contain server settings. Per-project `mcp` lists in `projects.yaml`
remain unchanged.

## See also

- [REST API](rest-api.md) — required HTTP layer, authentication, and audit log
- [Claude provider](../providers/claude.md) — loading third-party MCP servers into Kōan roles
- [MCP component contract](../../specs/components/mcp.md) — curation and safety invariants
