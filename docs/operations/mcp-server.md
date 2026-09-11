---
type: doc
title: "MCP Server"
description: "Configure Kōan's MCP server for local stdio clients or remote Streamable HTTP clients: shared bearer auth, TLS proxying, audits, and lifecycle."
tags: [operations]
created: 2026-09-09
updated: 2026-09-10
---

# MCP Server

Kōan can expose a curated part of its REST API as MCP tools. Two transports are
available. By default MCP clients launch the server as a subprocess over
**stdio** (no new port). Opt-in **Streamable HTTP** serves the same tools over
`/mcp` on a loopback listener for remote clients. Either way tool calls go
through the existing REST API and appear in `logs/api.log`; HTTP requests are
additionally audited in `logs/mcp.log`.

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
Because the client spawns the server itself rather than going through `make`,
the entrypoint loads `$KOAN_ROOT/.env` at startup, so a token kept there (the
documented preference) reaches it without being repeated in the client's `env`.
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

## Streamable HTTP

HTTP mode is opt-in:

```yaml
api:
  enabled: true
  host: "127.0.0.1"
  port: 8420

mcp:
  enabled: true
  transport: "http"
  host: "127.0.0.1"
  port: 8421
  tools_allow_destructive: false
```

Install the optional runtime once with `make mcp-setup`, then `make start`.
The endpoint is `http://127.0.0.1:8421/mcp`.

Every HTTP request requires the same token as the REST API:

```http
Authorization: Bearer <KOAN_API_TOKEN>
```

Missing or empty credentials return 401; incorrect credentials return 403.
HTTP startup refuses to listen when no API token is configured. Request audits
appear in `logs/mcp.log` without headers, bodies, query strings, or tokens.

`make mcp-config` prints a transport-appropriate client block. In HTTP mode
the JSON contains the bearer token, so do not paste it into a tracked file or
attach it to an issue.

## TLS and reverse proxy

Kōan does not terminate TLS or apply public-network rate limits. Keep
`mcp.host` on loopback and expose it through a hardened reverse proxy:

```nginx
server {
    listen 443 ssl;
    server_name koan.example.com;

    ssl_certificate     /etc/ssl/certs/koan.pem;
    ssl_certificate_key /etc/ssl/private/koan.key;

    location /mcp {
        proxy_pass http://127.0.0.1:8421;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_set_header Host 127.0.0.1:8421;
        proxy_set_header X-Forwarded-For $remote_addr;
        limit_req zone=koan_api burst=20 nodelay;
    }
}
```

Remote clients connect to `https://koan.example.com/mcp` and send the bearer
header on every request. Nginx forwards `Authorization` by default. The
loopback `Host` override preserves the MCP SDK's DNS-rebinding protection.

Binding MCP directly to a non-loopback address emits a warning. It does not
add TLS, rate limiting, or firewall rules.

Rotate `KOAN_API_TOKEN` as one credential for both hops, then restart the REST
API and MCP daemon so the outbound MCP REST client uses the new value.

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

### Automatic migration of the legacy list

An `instance/config.yaml` written before the MCP server landed holds `mcp` as a
bare list:

```yaml
mcp:
  - "/path/to/mcp-config.json"
```

On the first startup after upgrading, Kōan rewrites that block in place to the
mapping form (`mcp.configs`) so server settings can be added by hand later. The
rewrite is:

- **comment-preserving** — only the `mcp:` block's lines change; the rest of the
  file, including every comment, is untouched;
- **verified** — the result is re-parsed and compared before it is written, so a
  rewrite that would change any other key is discarded;
- **backed up** — the pre-migration file is copied to
  `instance/config.yaml.bak-mcp-mapping` once;
- **idempotent** — a config already in mapping form is left alone.

The migration is a convenience, not a requirement: the list form stays valid, so
a read-only or hand-reverted config still starts. Look for
`[migration] mcp: converted legacy list to 'mcp.configs' mapping` in the startup
log.

## See also

- [REST API](rest-api.md) — required HTTP layer, authentication, and audit log
- [Claude provider](../providers/claude.md) — loading third-party MCP servers into Kōan roles
- [MCP component contract](../../specs/components/mcp.md) — curation and safety invariants
