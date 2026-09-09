---
type: component-spec
title: "Component Spec — Web Dashboard & REST API"
description: "Documents the Flask dashboard and token-gated REST API, their shared `dashboard_service`/`usage_service`/`log_reader` logic, the code-derived OpenAPI spec + drift guard, and the invariants keeping the two surfaces from drifting."
tags: [web]
created: 2026-06-27
updated: 2026-09-09
---

# Component Spec — Web Dashboard & REST API

**Packages:** `koan/app/dashboard/`, `koan/app/dashboard_service/`, `koan/app/api/`,
`koan/app/cli/`
+ shared `usage_service.py`, `log_reader.py`

## Purpose

Two read/write surfaces over the same runtime state Telegram exposes: a human-facing
Flask **dashboard** (port 5001) and a token-gated **REST API** (port 8420). Both are
built by `create_app()` factories and share pure business-logic and data-shaping helpers.

## Architecture

```
dashboard/  (Flask blueprints via create_app())
  ├─ core      index, auth, status/health/forecast/provider
  ├─ missions  mission CRUD + attention
  ├─ chat      chat + progress/state SSE
  ├─ usage     usage/metrics/efficiency/journal/logs
  ├─ agent     soul/memory/skills/config + pause/resume/restart
  ├─ config    config/nickname/rules/recurring
  ├─ prs       PRs + plans
  ├─ state.py     ← single home for patchable module globals (tests patch state.X)
  └─ _helpers.py  ← passphrase gate, cache-buster, context processor, template filters

dashboard_service/  (pure logic, no Flask client needed to test)
  missions · journal · plans · progress · stats + read_file/mask_sensitive/validate_yaml

api/  (Flask blueprints via create_app())
  auth (require_token) · mission_index (sidecar) · routes_missions/projects/status/
  admin/observability · server.py (waitress entrypoint) · openapi_gen.py (spec generator)

cli/  (runtime OpenAPI REST client)
  ├─ spec.py      operation discovery, stable command names, collision checks
  ├─ config.py    mode-0600 named profiles and environment overrides
  ├─ commands.py  argparse generation and generic request construction
  ├─ http.py      confirmation, transport, JSON output, exit-code mapping
  └─ main.py      generated commands plus configure/raw built-ins
```

## Key types & functions

| Symbol | Contract |
|---|---|
| `dashboard.create_app()` / `api.create_app()` | Factory pattern; `from app.dashboard import app` exposes the module instance for tests. |
| `dashboard/state.py` | All patchable globals (paths, `CHAT_TIMEOUT`, `DASHBOARD_PWD`, caches, regexes). Route code reads `state.X` at call time → tests patch one target. |
| `dashboard_service/*` | Pure business logic — unit-tested without a Flask client. **New logic goes here, not in routes.** |
| `api/auth.require_token` | Bearer parse + `hmac.compare_digest`. Token: env `KOAN_API_TOKEN` → `api.token` → `""`. |
| `api/mission_index.py` | Sidecar `instance/.api-missions.json` (atomic). `record/get/list/reconcile/cancel`; `reconcile()` maps stored text → current `missions.md` section, and prefers the durable `OutcomeStore` for authoritative terminal status + the `outcome` field. Typed `result`/`result_ref` store: `attach_result()` (size-cap spill, summary-preserving), `load_full_result()` (inline-or-spill). `find_active_mission_id()` resolves a mission title back to its id (in_progress→pending→recent) for usage attribution. |
| `api/mission_results.py` | Command→resolver registry (`register_resolver`, `resolve_mission_result`, `always_inline_keys`); built-in `/review`+`/ultrareview` resolver reads the PR-keyed findings sidecar. |
| `routes_missions.get_mission_route()` | `GET /v1/missions/{id}` returns the reconciled record (with typed `result`/`result_ref`) **plus** a `usage` object (`aggregate_mission_usage()` over `created`→today): token/cache/cost totals, `call_count`, `models`/`providers`, and an `unattributed` block for id-less title matches. Response is a copy — the sidecar is never mutated with `usage`. |
| `usage_service.build_usage_payload()` | Shared usage payload (week/month buckets) for dashboard **and** `GET /v1/usage`. |
| `log_reader.tail_log()/read_logs()` | Shared log tailing for dashboard **and** `GET /v1/logs`. |
| `api/server.py` | Validates token at startup (fail-closed), warns on non-loopback bind, serves via waitress. |
| `api/openapi_metadata.py` | Defines route-adjacent request-schema and query-parameter declarations. `openapi_operation()` stores metadata on the registered view, mirroring the auth marker pattern without performing runtime validation. |
| `api/openapi_gen.py` | Generates the committed OpenAPI 3.1 document from the live route table plus metadata attached to each view. Paths, methods, path parameters, auth, JSON request bodies, and query parameters are code-derived; response schemas remain a separate enrichment. |

## Mission record: typed structured `result`

The mission record (`.api-missions.json`, exposed by `GET /v1/missions/{id}`
and `GET /v1/missions`) carries a typed `result` payload in addition to the
free-text `result_line`:

| Field         | Type            | Contract |
|---------------|-----------------|----------|
| `result_line` | `str \| null`   | Short human summary; unchanged, backward-compatible. |
| `result`      | `object \| null`| Structured result emitted by skills that produce one (e.g. `/review` → `{kind, file_comments[], review_summary{lgtm, summary, checklist}}`). `null` for skills with no resolver. Carries a `kind` discriminator. |
| `result_ref`  | `str \| null`   | Relative path (`.api-results/<id>.json`) to the full blob when it exceeds the inline cap; else `null`. |

**Resolver mechanism (pull-based, generic).** `api/mission_results.py` holds a
command→resolver registry (`register_resolver`, `resolve_mission_result`,
`always_inline_keys`). On the transition into `done`/`failed`, `reconcile()`
calls the resolver for the mission's slash-command and `attach_result()`s
whatever it returns — once, only while `result`/`result_ref` are unset
(idempotent). Skills run as subprocesses with no API mission id, so the API
*pulls* from the artifact the skill already persisted rather than the skill
pushing. The built-in `/review` (and `/ultrareview`) resolver reads the
PR-keyed findings sidecar `instance/.review-findings/{owner}_{repo}_{pr}.json`
(which `review_runner._write_review_findings_sidecar()` writes with both
`file_comments` and `review_summary`).

**Size cap + HTTP reachability.** Results over `DEFAULT_RESULT_CAP_BYTES`
(256 KB) spill to `instance/.api-results/<id>.json`; the record keeps
`result_ref` plus a trimmed inline copy of the resolver's `always_inline` keys
(`/review`: `kind`, `review_summary`, plus `result_truncated: true`) so the
verdict/summary never drop from list/GET payloads. `GET /v1/missions/{id}/result`
streams the complete result (inline or spilled) so remote clients that cannot
read the instance filesystem can always retrieve the full findings.

**Posted-comment reference (additive).** The `/review` resolver's `result`
object also carries `review_comment` — `{id, html_url}` of the PR review comment
koan posted, or `null` when none was captured. It is a backward-compatible
addition: the sidecar `_write_review_findings_sidecar()` persists it alongside
`file_comments`/`review_summary`, and it is an `always_inline` key so it survives
the size-cap trim in list/GET payloads.

**Correlation is best-effort by PR key.** The `/review` resolver keys off the
PR URL in the mission text → latest PR-keyed sidecar (a re-review overwrites
it). Each mission attaches whatever the sidecar held at its own terminal
transition; serialized re-reviews of the same PR therefore attach the correct
run's result. Storage-agnostic: `attach_result`/`load_full_result`/resolver
port unchanged onto the #2140 missions table.

## Authoritative terminal status + `outcome` (issue #2285)

`reconcile()` historically derived terminal status by *inference*: an
`in_progress` record that had vanished from every `missions.md` section was
guessed to be `done`. That mis-reports a mission whose row was pruned, renamed,
or lost to a mid-write crash. The contract is now:

- **The durable `OutcomeStore` (`mission_outcomes` table, keyed by
  `canonical_mission_key`) is the authoritative source of terminal status —
  but only once the mission has left the live sections.** When the section scan
  places the mission in `pending`/`in_progress`, that live status wins and
  `outcome` stays `null`; the outcome log is consulted only when the mission is
  gone/terminal. This matters because a mission requeued after a prior terminal
  run shares its `canonical_mission_key`, so a stale outcome row must never
  override a fresh live state. When the mission is not live and
  `OutcomeStore.latest(text)` yields a `done`/`failed` record, `reconcile()` uses
  that status and **overrides** the absence inference. The agent loop writes the
  row at the authoritative Done/Failed transition (`run._finalize_mission` →
  `app.mission_outcome.record_outcome`), so terminal status comes from the state
  machine, not file position.
- The record gains an `outcome` field: `{status, reason_category, detail}` or
  `null` until a terminal outcome exists. `reason_category` ∈
  `quota|timeout|tool_error|agent_error|cancelled|stagnation` (`null` on `done`).
- **Backward-compatible:** `status` and `result_line` are unchanged for existing
  consumers; `outcome` is purely additive. When no authoritative outcome exists
  yet, the section-scan path is retained as the fallback and `outcome` is `null`.

| Field     | Type            | Contract |
|-----------|-----------------|----------|
| `outcome` | `object \| null`| Authoritative terminal record `{status, reason_category, detail}` from the durable log; `null` pre-terminal. |

## Live progress stream (`/progress`)

**Source of truth remains** `instance/journal/pending.md` (writers in
`loop_manager` / `run.py` / skill pump). Display surfaces must not change
that file's format or writers.

| Endpoint | Contract |
|---|---|
| `GET /api/progress` | Snapshot: `{active, content, header, entries}` |
| `GET /api/progress/stream` | SSE events with the same JSON shape; heartbeat comments every ~15s |

- `content` — full raw `pending.md` text (unchanged; raw-view fallback).
- `header` — best-effort parse of the pending header block:
  `{title, project, started, run, mode}` (empty strings when absent).
- `entries` — display-only structured rows derived from `content` by the
  shared `[cli]` classifier (`log_fmt.classify_cli`). Kinds include
  `thinking`, `tool_use`, `text`, `tool_error`, `tool_end`, `result`,
  `session`, `warning`, `meta`, `raw`. Successful `tool_result` lines are
  omitted (same as `make logs`). Unknown shapes become `raw`.
- Mission **elapsed / agent state** are *not* invented by the progress
  stream; the progress page may also consume `/api/state/stream`
  (`elapsed`, `project`, `label`, `execution`).

Invariant: progress parsing is presentational only — never used for
control flow, lifecycle, or quota decisions.

## Invariants

- **Logic in `dashboard_service/`, wiring in `dashboard/`.** Routes stay thin; testable
  logic must not live in route handlers.
- **Patch one target.** Module globals live in `state.py`; tests patch
  `app.dashboard.state.X`, not scattered module attributes.
- **API is fail-closed.** No token configured → server refuses to start; secrets are
  masked in `GET /v1/config`.
- **Dashboard and API share data shapers** (`usage_service`, `log_reader`) so the two
  surfaces never drift in what they report.
- **Default binds are loopback** (`127.0.0.1`); non-loopback bind warns.
- **GET mission usage is derived, never stored.** `usage` is computed on read from
  `instance/usage/*.jsonl`; it is attached to a response copy and must not be
  persisted into `.api-missions.json`.
- **The OpenAPI doc is derived, never hand-edited.** `koan/openapi.yaml` is generated from
  the route table; a route's auth requirement comes from the `require_token` decorator marker,
  not a maintained list. Adding/removing/modifying a route requires regenerating
  (`make openapi`) and committing the artifact in the same change; the path-filtered CI drift
  check (`.github/workflows/openapi.yml`) enforces this only when API-defining files change.
  Generation is deterministic (unchanged code → byte-identical output) and needs no server,
  token, or `api.enabled`.
- **OpenAPI request metadata stays beside the handler.** Every view that reads a JSON body
  or query parameter declares that input through `openapi_operation()`. The generator reads
  those markers and never maintains a second method/path schema map. Handler-access tests
  guard the declared field names, and numeric defaults used by both metadata and parsing come
  from the same constants.
- **The REST CLI consumes the committed OpenAPI document at runtime.** It does
  not commit generated client code. Every documented operation must map to one
  collision-free public command and its hidden `operationId` alias.
- **Sparse specifications remain usable.** Every operation accepts generic
  JSON through `--data` and repeatable query pairs through `--query`; schema-
  derived flags are additive when request/query schemas exist.
- **CLI credentials fail closed.** Tokens come from a mode-0600 or mode-0400
  profile or `KOAN_API_TOKEN`, never from argv. Destructive requests require
  confirmation on a TTY and `--yes` in non-interactive execution.

## Integration points

- Reads agent state from signal files (`.koan-status`, pause/focus/passive) and
  `missions.md`.
- Mutating endpoints (pause/resume/restart/shutdown/update) drive the same managers the
  bridge uses (`pause_manager`, `restart_manager`, `update_manager`).
- Mission creation writes through `missions.py` + the API sidecar index.

## Known debt / watch-outs

- The dashboard and API are intentionally parallel factories — a feature exposed on one
  often belongs on the other; check both when adding observability.
- Per-request audit logging in the API must not log secrets.

## Change protocol

New endpoints add the pure logic to `dashboard_service/` (or a shared service), wire a
thin route, and — if observability — expose it on both surfaces. For **API** changes, also
run `make openapi` and commit the regenerated `koan/openapi.yaml` in the same change. Update
`docs/operations/rest-api.md` for API changes and this spec for structural ones.
