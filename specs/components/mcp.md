---
type: component-spec
title: "Component Spec — MCP Server"
description: "Defines Kōan's opt-in MCP front-end over stdio or Streamable HTTP, curated REST operation tools, destructive-tool gate, shared OpenAPI HTTP client boundary, and HTTP authentication/audit invariants."
tags: [web]
created: 2026-09-09
updated: 2026-09-11
---

# Component Spec — MCP Server

**Packages:** `koan/app/mcp/`, `koan/app/apiclient/`

## Purpose

Kōan offers an optional MCP server for LLM clients. It translates tool calls
into authenticated requests to Kōan's REST API. It never reads or mutates Kōan
runtime state directly: every call crosses the HTTP API and keeps its
authentication, validation, and `logs/api.log` audit trail.

## Architecture

```
MCP client ── stdio ──> app/mcp/ ──> app/apiclient/ ── HTTP ──> REST API
CLI user  ─────────────> app/cli/ ──> app/apiclient/ ── HTTP ──> REST API
                                      ↑
                              committed openapi.yaml
```

`app/apiclient/` owns OpenAPI loading, operation discovery, request planning,
and synchronous HTTP execution. `app/cli/` owns terminal parsing, profiles,
confirmation, output, and exit codes. `app/mcp/` owns curation, MCP schemas,
annotations, configuration gates, and transport lifecycle.

## Transports

`mcp.transport` accepts `stdio` or `http` and defaults to `stdio`.

- `stdio` is client-launched, creates no listener, and keeps its existing
  behavior.
- `http` serves MCP Streamable HTTP at `/mcp`, binds `mcp.host` and
  `mcp.port`, and is managed as the `mcp` daemon.
- Both transports construct tools only through `create_server()` and
  `build_tool_definitions()`.

HTTP requests require `Authorization: Bearer <api-token>`. Authentication
resolves the same secret as the REST API through `get_api_token()` and calls
`app.api.auth.check_token()`, which fails closed and compares tokens with
`hmac.compare_digest`.

HTTP request audits are written to `logs/mcp.log` as:

`YYYY-MM-DDTHH:MM:SS <peer-ip> METHOD /path STATUS`

Authorization headers, bearer tokens, request bodies, and query strings are
never written to the audit line.

## HTTP safety invariants

- HTTP binds `127.0.0.1:8421` by default.
- A non-loopback IP bind emits a TLS/reverse-proxy warning.
- No configured API token prevents HTTP startup; the middleware also rejects
  requests fail-closed if the token becomes unavailable.
- Missing or empty bearer credentials return 401; invalid credentials return 403.
- HTTP mode holds `.koan-pid-mcp` under `fcntl.flock()` for its lifetime.
- TLS and rate limiting remain reverse-proxy responsibilities.
- Legacy SSE endpoints are not exposed.

## Configuration and startup

`mcp.enabled` defaults to `false`. A client may launch the configured stdio
command at any time, so disabled mode means that process exits immediately with
an actionable message. Kōan never edits or installs client configuration.

`mcp.tools_allow_destructive` defaults to `false`. It only controls whether the
named destructive mission-delete tool appears in `tools/list`.

The `mcp` key predates this component as a bare list of provider client config
paths. Both shapes stay valid: a list means "provider configs only", a mapping
carries this component's settings beside `mcp.configs`. **A shape change to a
key an operator already sets must never be a hard startup stop.** Two mechanisms
uphold that, and both are required:

1. `app.config_migration.migrate_mcp_config` rewrites a legacy list into the
   mapping form in place at startup, before strict validation. It preserves
   comments, verifies the re-parsed result differs only in `mcp`, backs the file
   up once, and is idempotent.
2. `config_validator.accepts_non_mapping` keeps the list form valid regardless,
   so an unmigrated config (read-only mount, manual revert) still starts. That
   predicate is the single shared source for every `_NESTED` shorthand:
   `validate_config` and `validate_config_or_raise` must never disagree about
   which shapes are legal, because a value one accepts and the other rejects
   turns a working config into a boot failure.

MCP has no credential setting of its own. Its bearer token comes from
`config.get_api_token()` and its base URL comes from `api.host` plus `api.port`.
In stdio mode an unspecified/wildcard bind host resolves to loopback for client
requests. HTTP mode introduces `mcp.transport`, `mcp.host`, and `mcp.port`
(loopback 8421 by default). Consequently `api.enabled` must also be true and
the REST server must run.

The server performs a best-effort health probe at startup. An unreachable API
produces a stderr warning but does not terminate the server. Each failed tool
call reports the attempted URL and directs the operator to enable
`api.enabled`, run `make api-token`, and start `make api`.

The MCP Python SDK remains optional in `koan/requirements-mcp.txt`. Import
failure exits cleanly with the `make mcp-setup` repair command.

## Tool exposure contract

Routes opt in through `openapi_operation(mcp=True)`. The OpenAPI generator
emits that marker as `x-koan-mcp: true`; absence means hidden. MCP additionally
uses a fixed allow-list, so a marker alone cannot publish an unexpected tool.

Named tools use prefix `koan_`:

| Tool | Operation | Annotation |
|---|---|---|
| `koan_health` | `GET /v1/health` | read-only |
| `koan_status` | `GET /v1/status` | read-only |
| `koan_missions_list` | `GET /v1/missions` | read-only |
| `koan_missions_get` | `GET /v1/missions/{mission_id}` | read-only |
| `koan_missions_result` | `GET /v1/missions/{mission_id}/result` | read-only |
| `koan_projects_list` | `GET /v1/projects` | read-only |
| `koan_usage` | `GET /v1/usage` | read-only |
| `koan_metrics` | `GET /v1/metrics` | read-only |
| `koan_logs` | `GET /v1/logs` | read-only |
| `koan_config` | `GET /v1/config` | read-only |
| `koan_missions_create` | `POST /v1/missions` | write |
| `koan_missions_reorder` | `POST /v1/missions/reorder` | write |
| `koan_pause` | `POST /v1/pause` | write |
| `koan_resume` | `POST /v1/resume` | write |
| `koan_missions_delete` | `DELETE /v1/missions/{mission_id}` | destructive, separately gated |

Named-tool input schemas come from Python signatures through MCP SDK. Before
registration, Kōan augments each signature with Pydantic `Field` metadata from
matching OpenAPI path, query, or request-body properties. Supported numeric and
pattern bounds become both advertised and enforced. Python defaults remain
authoritative; OpenAPI defaults never replace them.

Every published tool has a title, a non-empty description, parameter
descriptions, and explicit `idempotentHint` and `openWorldHint` values.
Read-only tools, resume, and mission deletion count as idempotent. Timed pause,
mission creation/reordering, and `exec_operation` remain conservatively
non-idempotent. Curated tools remain closed-world; `exec_operation` stays
open-world because it can reach broader REST operations.

`exec_operation` accepts an OpenAPI `operation_id`, path arguments, query
object, and optional JSON body. It can invoke every documented operation,
including admin and project-management operations intentionally absent from the
named tool set. This explicit escape hatch mirrors the REST CLI's `raw`
capability; clients may apply a single conservative approval policy to it.

`DENIED_NAMED_OPERATIONS` limits named-tool publication only.
`exec_operation` continues to reach every documented operation.

## Safety invariants

- Missing `x-koan-mcp` always hides an operation from named tools.
- Fixed curation and a deny-list prevent accidental publication when routes
  or markers change.
- Shutdown, restart, update, release update, and project create/update/delete
  never receive named tools.
- Mission deletion stays absent unless `mcp.tools_allow_destructive` is true.
- Read tools carry `readOnlyHint`; mission deletion carries `destructiveHint`.
- Every tool explicitly publishes idempotence and open-world semantics.
- Named writes do not claim read-only or destructive behavior.
- MCP never bypasses REST bearer authentication or server-side secret masking.
- In HTTP mode, the outer ASGI layer authenticates every request before MCP
  parsing; missing credentials produce 401 and invalid credentials 403.
- That layer allow-lists the scope types it forwards without a credential
  check: only `lifespan`. Any other non-HTTP scope is refused and audited, so a
  transport added later cannot inherit an unauthenticated path by default.
- stdio remains the default transport and is never daemonized.

## Change protocol

Adding a named tool requires all three: an adjacent route marker, an entry in
the fixed curation table, and tests covering schema, annotation, and deny-list
behavior. Regenerate `koan/openapi.yaml` after marker changes. Update this spec
before changing the architectural contract. REST generator ownership remains
documented in [Web Dashboard & REST API](web.md).
