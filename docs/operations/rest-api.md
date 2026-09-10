---
type: doc
title: "REST API"
description: "Documents Kōan's optional, token-authenticated HTTP control layer (missions, projects, pause/resume, config, admin, usage/metrics/logs endpoints), its generated OpenAPI spec + drift guard, and its security model."
tags: [operations]
created: 2026-05-31
updated: 2026-09-10
---

# REST API

Kōan exposes an **optional HTTP control layer** so external tools can queue missions, poll status, and manage the agent programmatically — in addition to the Telegram / Matrix / Slack messaging bridge.

The API is **disabled by default** and requires an explicit bearer token before it will serve any requests (fail-closed).

---

## Enable & configure

In `instance/config.yaml`:

```yaml
api:
  enabled: true       # Include API in managed processes (default: false)
  host: "127.0.0.1"  # Bind address (default: 127.0.0.1 — loopback only)
  port: 8420          # HTTP port (default: 8420)
  threads: 2          # waitress worker threads (default: 2)
  # token: ""         # Bearer token fallback (prefer KOAN_API_TOKEN env var)
```

Generate a random token and configure it:

```bash
make api-token    # prints a random token + setup instructions
```

Or set manually in `.env` (preferred — keeps secrets out of the config tree):

```bash
KOAN_API_TOKEN=your-secret-token
```

Alternatively set `api.token` in `config.yaml`, but environment variable takes precedence.

---

## Start/stop

```bash
make api          # standalone foreground server
make start        # includes API when api.enabled: true
make stop         # stops all managed processes including API
make status       # shows API PID when running
```

---

## Authentication

Every endpoint except `GET /v1/health` requires:

```
Authorization: Bearer <your-token>
```

| Response | Condition |
|---|---|
| `401` | `Authorization` header missing or malformed |
| `403` | Token present but incorrect |

Token comparison uses `hmac.compare_digest` to prevent timing attacks. If no token is configured, **all authenticated requests return 403** — the server never accepts unauthenticated control requests.

---

## OpenAPI specification

The API ships a machine-readable **OpenAPI 3.1 document** at
[`koan/openapi.yaml`](../../koan/openapi.yaml). It is **generated from the live Flask route
table** — it can only describe endpoints that actually exist, so it never drifts from the
code. Point any OpenAPI tool at it to preview docs, generate a client, or validate requests.

### View & render the spec online

The raw YAML is not fun to read by hand. To render it as browsable, interactive API docs
in your browser — no install, no running server — open it in **Swagger Editor**:

> **[▶ Open `koan/openapi.yaml` in Swagger Editor](https://editor.swagger.io/?url=https://raw.githubusercontent.com/Anantys-oss/koan/main/koan/openapi.yaml)**

That link tells [editor.swagger.io](https://editor.swagger.io/) to fetch the spec from
`main` and render it. The editor loads the file client-side (GitHub's raw host allows
cross-origin reads), so nothing is uploaded anywhere. To preview a spec from a branch or a
fork, swap the raw URL — the pattern is:

```
https://editor.swagger.io/?url=https://raw.githubusercontent.com/<owner>/<repo>/<ref>/koan/openapi.yaml
```

Prefer to render **local, uncommitted** changes (e.g. right after `make openapi`)? Any
offline viewer works on the file directly:

```bash
npx @redocly/cli preview-docs koan/openapi.yaml   # Redoc, live-reloading, http://localhost:8080
# or drag-and-drop koan/openapi.yaml into https://editor.swagger.io/
```

### Regenerate after any API change

```bash
make openapi                          # rewrite koan/openapi.yaml from the code
git add koan/openapi.yaml && git commit
```

Generation needs **no running server, no token, and no `api.enabled: true`** — it inspects
the app object in-process. The file is derived output: **never hand-edit it.**

### Check for drift

```bash
make openapi-check   # exit non-zero (with a fix instruction) if the file is stale
```

CI runs this automatically via [`.github/workflows/openapi.yml`](../../.github/workflows/openapi.yml),
but **only when an API-defining file changes** (`koan/app/api/**`, `koan/openapi.yaml`,
`koan/requirements.txt`, the `Makefile`, or the workflow itself) — unrelated PRs spend no
CI time on it. If the check
fails, the log tells you to run `make openapi` and commit the result.

> **Scope (iteration 1):** the document precisely covers **paths, methods, path parameters,
> and bearer-auth security** for every route. Per-operation request/response **body** schemas
> are a planned enrichment and are not yet included. Two known non-`200` successes are
> reflected: `POST /v1/missions` → `202`, `POST /v1/projects` → `201`.

---

## Endpoint reference

All responses are JSON. Errors use a uniform envelope:
```json
{"error": {"code": "...", "message": "..."}}
```

### Health

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/v1/health` | none | Liveness probe — always returns `{"status":"ok","name":"koan","version":"..."}` |

### Status

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/v1/status` | yes | Agent state, mode, mission counts, signal flags, attention count, live execution truth |

Response:
```json
{
  "agent": {
    "state": "working|sleeping|paused|stopped|idle|contemplating|error_recovery",
    "mode": "REVIEW|IMPLEMENT|DEEP|null",
    "run_info": "12/20",
    "project": "my-project",
    "focus": false,
    "status_text": "Run 12/20 — executing",
    "pause": {},
    "elapsed_seconds": 42,
    "execution": {
      "state": "idle|working|stalled|zombie",
      "pid": 12345,
      "project": "my-project",
      "run_num": 12,
      "elapsed": 42,
      "last_output_age": 3.1,
      "sessions": 0
    }
  },
  "missions": {
    "pending": 3,
    "in_progress": 1,
    "done": 42,
    "failed": 0
  },
  "signals": {
    "stop_requested": false,
    "quota_paused": false,
    "paused": false
  },
  "attention_count": 2,
  "execution": {
    "provider_state": "idle|working|stalled|zombie",
    "in_progress_lines": 1,
    "zombie": false
  }
}
```

The `execution` block reports **observed** runtime state — backed by the live
provider PID in `.koan-active` and provider-output recency — not the
declarative `missions.md` ▶ timestamp, which can silently diverge (#2086):

- `working` — a live provider PID with recent (or not-yet-produced) output, or
  a live parallel worktree session (tracked in `sessions.json`).
- `stalled` — a live PID but no output for over 120s (hung session). A recorded
  stdout file that has vanished is treated as stalled, never as `working`.
- `zombie` — a recorded PID that is no longer alive.
- `idle` — no provider running.

The top-level `execution.zombie` is `true` when an *In Progress* mission line
exists but no live provider process backs it. To avoid flapping during the
brief start/stop windows where the `missions.md` line and the `.koan-active`
signal momentarily disagree, the orphan check requires that the run-loop
heartbeat (`.koan-run-heartbeat`) has gone stale before flagging the `idle`
case — a recorded-but-dead PID is always flagged immediately. Live parallel
sessions also suppress the flag. The same cross-check backs the `make status`
`execution:` line.

### Missions

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/v1/missions` | yes | List missions from the mission store (all queue sources: Slack/GitHub/API/dashboard). Query params: `?status=pending\|in_progress\|done\|failed`, `?project=name`, `?limit=N` |
| `POST` | `/v1/missions` | yes | Queue a new mission |
| `GET` | `/v1/missions/{id}` | yes | Get mission by id (reconciles vs missions.md) |
| `PATCH` | `/v1/missions/{id}` | yes | Edit a pending mission's text (409 if not pending) |
| `DELETE` | `/v1/missions/{id}` | yes | Cancel a pending mission (409 if already started) |
| `POST` | `/v1/missions/reorder` | yes | Reorder a pending mission in the queue |

The list endpoint reads the authoritative mission store — the same source as
`GET /v1/status`'s counts — so every queued mission is visible regardless of
how it was created. `?status` filters to a store state (`pending`,
`in_progress`, `done`, `failed`); unknown values return `400`. `?limit=N`
caps the rows returned per state (terminal states can be large). The
single-mission detail and result endpoints (`GET /v1/missions/{id}`,
`/result`) still operate on API-queued records only.

> **Backward-compatibility note.** The list previously returned sidecar
> records carrying `result`/`result_ref`/`usage`/`created` and a `removed`
> status for API-queued missions. Now that the list is store-backed, those
> fields are no longer present and `removed` is not a valid `?status`
> filter. Consumers that need result or cancellation state should use
> `GET /v1/missions/{id}` and `GET /v1/missions/{id}/result`.

**POST /v1/missions** body:
```json
{
  "command": "/fix https://github.com/org/repo/issues/42",
  "project": "my-project",
  "urgent": false
}
```
Use `command` for slash commands or `text` for free-form missions. `project` adds a `[project:name]` tag. `urgent` inserts at the top of the queue.

Response (202):
```json
{"id": "uuid", "status": "pending"}
```

**GET /v1/missions/{id}** response:
```json
{
  "id": "uuid",
  "text": "- /review https://github.com/o/r/pull/5",
  "project": "koan",
  "status": "pending|in_progress|done|failed|removed",
  "created": 1748700000.0,
  "result_line": "✅ (2026-05-31 14:22) Review posted on PR #5",
  "result": {
    "kind": "review",
    "file_comments": [
      {"file": "a.py", "line_start": 1, "line_end": 1, "severity": "warning",
       "title": "…", "comment": "…", "code_snippet": ""}
    ],
    "review_summary": {"lgtm": false, "summary": "…", "checklist": []},
    "review_comment": {"id": 555, "html_url": "https://github.com/o/r/pull/5#issuecomment-555"}
  },
  "result_ref": null,
  "outcome": {
    "status": "failed",
    "reason_category": "timeout",
    "detail": "killed after 1800s"
  },
  "usage": {
    "input_tokens": 12000,
    "output_tokens": 3400,
    "cache_creation_input_tokens": 800,
    "cache_read_input_tokens": 9000,
    "cost_usd": 0.184200,
    "call_count": 3,
    "models": ["opus", "sonnet"],
    "providers": ["claude"],
    "unattributed": {
      "input_tokens": 0,
      "output_tokens": 0,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0,
      "cost_usd": 0.0,
      "call_count": 0
    }
  }
}
```

Mission status is reconciled on each read against the live `missions.md` state,
but **terminal status is sourced from the durable outcome log** (the
`mission_outcomes` table in `missions.db`, written by the agent loop at the
Done/Failed transition) once the mission has left the live sections and an
authoritative outcome exists. A mission still present in `pending`/`in_progress`
keeps its live status and reports `outcome: null`, so a stale outcome from a
prior run of a requeued mission never overrides a fresh live state. This corrects
the previous absence-inference heuristic, which mis-reported an `in_progress`
mission whose row was pruned, renamed, or lost to a mid-write crash as `done`
(issue #2285).

`outcome` is a machine-readable terminal record: `{status, reason_category,
detail}`. It is `null` until the mission reaches a terminal state. `status` and
`result_line` are unchanged for existing consumers. `reason_category` is one of:

| Category | Meaning |
|----------|---------|
| `timeout` | Killed by the mission-timeout watchdog (SIGTERM/SIGKILL). |
| `stagnation` | Aborted by the stagnation monitor (stuck-in-a-loop). |
| `agent_error` | Non-zero CLI exit with no finer signal. |
| `quota` | Hard quota exhaustion failed the run (rare — quota usually pauses). |
| `tool_error` | A required tool/subprocess failed. |
| `cancelled` | Operator-cancelled in-progress mission. |

> **Note:** `classify_failure()` currently emits only `timeout`, `stagnation`,
> and `agent_error`. `quota`, `tool_error`, and `cancelled` are reserved for
> future callers that pass an explicit `failure_reason` to `_finalize_mission`;
> no producer sets them today, so they will not appear on the API yet.

On a successful `done` outcome, `reason_category` is `null`.

`result` is a typed structured payload emitted by skills that produce one
(e.g. `/review`); other missions leave it `null`. `result_line` remains the
short free-text summary for backward compatibility.

For `/review` (and `/ultrareview`) missions, `result.review_comment` carries the
`id` and `html_url` of the PR comment koan posted, letting a consumer deep-link
to (and dedup) the exact review rather than only the PR. It is `null` when no
comment ref was captured (e.g. no head SHA at post time, or the GitHub response
could not be parsed). It is retained in the trimmed inline `result` even when the
payload spills to `instance/.api-results/<id>.json`.

When a result exceeds the inline size cap (`DEFAULT_RESULT_CAP_BYTES`, 256 KB)
the full payload is written to `instance/.api-results/<id>.json` and
`result_ref` points at that relative path. The record still carries a trimmed
inline `result` with `kind`, the verdict/summary, and `"result_truncated": true`
so the merge verdict is always readable without a second call.

**GET /v1/missions/{id}/result** — returns the *complete* structured result
(inline or spilled) as JSON, so a remote client that cannot read the instance
filesystem can always retrieve the full findings. `404` if the mission has no
structured result.

The `usage` object aggregates every model call recorded for this mission id in
`instance/usage/*.jsonl` over the mission's lifetime (its `created` date → today).
Fields are zeroed and `call_count` is `0` when the mission has made no model calls yet.

**Meter on token counts, not `cost_usd`.** `cost_usd` is best-effort and is `0`
when the provider does not report a cost; `input_tokens`/`output_tokens` are the
reliable billing signal.

`usage.unattributed` sums calls whose title matches this mission but which could
not be joined to its id (best-effort title→id resolution missed). A non-zero
`unattributed.call_count` means the headline totals under-report — distinguish it
from "0 because the mission was free" (`unattributed.call_count == 0`).

**PATCH /v1/missions/{id}** body:
```json
{"text": "Updated mission description"}
```

Response (200):
```json
{"id": "uuid", "status": "pending"}
```

Returns 409 if the mission is not in `pending` status. Returns 422 if `text` is missing or empty.

**POST /v1/missions/reorder** body:
```json
{"mission_id": "uuid", "target_position": 1}
```

Response (200):
```json
{"id": "uuid", "status": "pending"}
```

`target_position` is 1-indexed within the pending queue. Returns 409 if the mission is not pending. Returns 422 if the target position is out of range.

### Projects

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/v1/projects` | yes | List known projects |
| `POST` | `/v1/projects` | yes | Add a project (runs `add_project` skill) |
| `PATCH` | `/v1/projects/{name}` | yes | Update allow-listed per-project settings |
| `DELETE` | `/v1/projects/{name}` | yes | Remove a project (runs `delete_project` skill) |

**POST /v1/projects** body:
```json
{"github_url": "https://github.com/org/repo", "name": "optional-name"}
```

**PATCH /v1/projects/{name}** body:
```json
{"patch": {"cli_provider": "cline", "github_url": "https://github.com/org/repo", "git_auto_merge.enabled": true}}
```

Response (200):
```json
{"name": "my-project", "config": {"...": "merged project config"}}
```

Only allow-listed fields may be patched — see `EDITABLE_PROJECT_FIELDS` in `koan/app/projects_config.py` (mirrors the dashboard's Projects (form) tab; both surfaces reuse the same `apply_project_patch()`). Returns `404` for an unknown project, `422` for a non-editable field or an invalid value (unknown `cli_provider`, malformed `github_url`, unrecognized `git_auto_merge.strategy`).

### Pause / resume

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/pause` | yes | Pause the agent |
| `POST` | `/v1/resume` | yes | Resume the agent |

**POST /v1/pause** body (optional):
```json
{"duration": "2h"}
```
Duration formats: `2h`, `30m`, `1h30m`. Omit for indefinite pause.

### Config

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/v1/config` | yes | Effective config + project list. Secrets masked. |

Secret fields (keys containing `token`, `password`, `secret`, `api_key`) are replaced with `"***"`.

### Admin

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/restart` | yes | Signal restart via per-consumer markers `.koan-restart-run` + `.koan-restart-bridge` (picked up by run loop and bridge) |
| `POST` | `/v1/shutdown` | yes | Write `.koan-stop` signal |
| `POST` | `/v1/update` | yes | Pull latest commit on main + signal restart |
| `POST` | `/v1/update_release` | yes | Checkout most recent release tag + signal restart |

### Observability

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/v1/usage` | yes | Token usage payload (mirrors the dashboard `/api/usage`) |
| `GET` | `/v1/metrics` | yes | Mission metrics, global or per-project |
| `GET` | `/v1/logs` | yes | Recent `run.log` / `awake.log` lines |

**GET /v1/usage** query params:

| Param | Default | Description |
|---|---|---|
| `days` | `7` | Window length, clamped to `[1, 100]` |
| `offset` | `0` | Shift the window back by this many `granularity` units |
| `granularity` | `day` | `day`, `week`, or `month` bucketing |
| `stacked` | `false` | Include per-project breakdown in the series |
| `project` | _(all)_ | Restrict totals/series to a single project |

**GET /v1/metrics** query params:

| Param | Default | Description |
|---|---|---|
| `days` | `30` | Lookback window, clamped to `[0, 365]` |
| `project` | _(all)_ | Per-project metrics + trend; omit for global metrics with per-project trends |

The global (no `project`) response includes `security_blocks_7d` — the count of auto-merge blocks recorded in `.security-audit.jsonl` over the last 7 days (see [Security Review](../security/security-review.md#audit-trail)).

**GET /v1/logs** query params:

| Param | Default | Description |
|---|---|---|
| `source` | `all` | `run`, `awake`, or `all` |
| `limit` | `200` | Max lines per source, clamped to `[1, 2000]` |
| `q` | _(none)_ | Case-insensitive substring filter |

Response:
```json
{"lines": [{"n": 1, "text": "...", "source": "run"}], "total": 1}
```

Malformed integer params (`days`, `offset`, `limit`) return `422` with an `invalid_request` error rather than silently substituting the default.

---

## Security

- **Loopback-only by default** — `host: "127.0.0.1"` prevents external access without explicit configuration change.
- A warning is logged at startup when bound to a non-loopback address.
- **TLS and rate limiting** are delegated to a reverse proxy (nginx, Caddy). The API has no built-in TLS.
- Per-request audit log: `logs/api.log` — `YYYY-MM-DDTHH:MM:SS <ip> METHOD /path STATUS`.
- Token is never logged.

### Reverse proxy example (nginx)

```nginx
server {
    listen 443 ssl;
    server_name koan.example.com;

    ssl_certificate     /etc/ssl/certs/koan.pem;
    ssl_certificate_key /etc/ssl/private/koan.key;

    location /v1/ {
        proxy_pass http://127.0.0.1:8420;
        proxy_set_header X-Forwarded-For $remote_addr;
        limit_req zone=koan_api burst=20 nodelay;
    }
}
```

---

## External multi-instance registration

Each Kōan instance has its own `KOAN_API_TOKEN`. An external operator (e.g. a CI system managing multiple instances) can:

1. Register an instance: `GET /v1/health` — 200 means the instance is reachable.
2. Queue work: `POST /v1/missions` — returns an `id` for polling.
3. Poll status: `GET /v1/missions/{id}` — status transitions `pending → in_progress → done/failed`.
4. Pause/resume on demand: `POST /v1/pause` / `POST /v1/resume`.

Each instance is fully independent; there is no shared coordination layer.

---

## Audit log

All authenticated requests are logged to `logs/api.log`:

```
2026-05-31T14:22:10 127.0.0.1 POST /v1/missions 202
2026-05-31T14:22:15 127.0.0.1 GET /v1/missions/abc123 200
```

Tokens are never written to the log.

---

## See also

- [`docs/operations/dashboard.md`](dashboard.md) — web dashboard (separate process, same config pattern)
- [`instance.example/config.yaml`](../../instance.example/config.yaml) — documented `api:` section
- [`koan/openapi.yaml`](../../koan/openapi.yaml) — generated OpenAPI 3.1 document (`make openapi`) · [render in Swagger Editor](https://editor.swagger.io/?url=https://raw.githubusercontent.com/Anantys-oss/koan/main/koan/openapi.yaml)
