---
type: component-spec
title: "Component Spec — MCP Server"
description: "Defines Kōan's opt-in stdio MCP front-end, curated REST operation tools, destructive-tool gate, and shared OpenAPI HTTP client boundary."
tags: [web]
created: 2026-09-09
updated: 2026-09-09
---

# Component Spec — MCP Server

**Packages:** `koan/app/mcp/`, `koan/app/apiclient/`

## Purpose

Kōan offers an optional MCP server for local LLM clients. It translates stdio
tool calls into authenticated requests to Kōan's REST API. It never reads or
mutates Kōan runtime state directly: every call crosses the HTTP API and keeps
its authentication, validation, and `logs/api.log` audit trail.

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
annotations, configuration gates, and stdio lifecycle.

## Configuration and startup

`mcp.enabled` defaults to `false`. A client may launch the configured stdio
command at any time, so disabled mode means that process exits immediately with
an actionable message. Kōan never edits or installs client configuration.

`mcp.tools_allow_destructive` defaults to `false`. It only controls whether the
named destructive mission-delete tool appears in `tools/list`.

MCP has no credential or address settings. Its bearer token comes from
`config.get_api_token()` and its base URL comes from `api.host` plus `api.port`.
An unspecified/wildcard bind host resolves to loopback for client requests.
Consequently `api.enabled` must also be true and the REST server must run.

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

Write-operation input schemas stay hand-authored until OpenAPI request schemas
fully replace them. Read-operation query and path schemas come from OpenAPI.

`exec_operation` accepts an OpenAPI `operation_id`, path arguments, query
object, and optional JSON body. It can invoke every documented operation,
including admin and project-management operations intentionally absent from the
named tool set. This explicit escape hatch mirrors the REST CLI's `raw`
capability; clients may apply a single conservative approval policy to it.

## Safety invariants

- Missing `x-koan-mcp` always hides an operation from named tools.
- Fixed curation and a deny-list prevent accidental publication when routes
  or markers change.
- Shutdown, restart, update, release update, and project create/update/delete
  never receive named tools.
- Mission deletion stays absent unless `mcp.tools_allow_destructive` is true.
- Read tools carry `readOnlyHint`; mission deletion carries `destructiveHint`.
- Named writes do not claim read-only or destructive behavior.
- MCP never bypasses REST bearer authentication or server-side secret masking.
- stdio remains the only transport; no network listener belongs to this component.

## Change protocol

Adding a named tool requires all three: an adjacent route marker, an entry in
the fixed curation table, and tests covering schema, annotation, and deny-list
behavior. Regenerate `koan/openapi.yaml` after marker changes. Update this spec
before changing the architectural contract. REST generator ownership remains
documented in [Web Dashboard & REST API](web.md).
