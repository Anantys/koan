# koan/app/ — Architecture & module map

This file is auto-loaded by Claude Code when working under `koan/app/`. It holds
the detailed architecture and per-module reference; the root `CLAUDE.md` keeps
only the two-process overview.

## Architecture

Two parallel processes run independently:

- **`awake.py`** (Telegram bridge): Polls Telegram every 3s. Classifies messages as "chat" (instant Claude reply) or "mission" (queued to `missions.md`). Flushes `outbox.md` messages back to Telegram. Command handling is split into `command_handlers.py`, shared state in `bridge_state.py`, colored log output in `bridge_log.py`.
- **`run.py`** (agent loop): Pure-Python main loop with restart wrapper. Core execution host: `run_claude_task()` (CLI subprocess invocation and monitoring), `_finalize_mission()` (lifecycle state machine: Done/Failed/requeue), `_classify_and_handle_cli_error()` (error → action mapping), and `_probe_exit0_quota()` (false-success detection). Signal handling uses double-tap CTRL-C protection (`protected_phase` context manager). Writes real-time status to `.koan-status`. Per-iteration dispatch delegated to `mission_executor.py`; stateless pipeline helpers delegated to `mission_runner.py`.

Communication between processes happens through shared files in `instance/` with atomic writes (`utils.atomic_write()` using temp file + rename + `fcntl.flock()`). Exclusive process instances enforced via `pid_manager.py` (PID file + `fcntl.flock()`).

### Key modules (`koan/app/`)

**Core data & config:**

- **`mission_store/`** — The authoritative mission store behind the `MissionStore` port. `get_mission_store()` resolves `missions.backend` (default `sqlite`; a dotted `module:Class` loads an out-of-tree adapter). `SqliteMissionStore` keeps mission state in `instance/missions.db` (WAL); `CiQueueStore`/`IdeaStore`/`QuarantineStore` are sibling tables. Mutations round-trip through `utils._locked_missions_rw` (render → `missions.py` transform → `reconcile_all` → export). `missions.md` is a generated read-only export. See `specs/004-mission-store/`.
- **`missions.py`** — Parsing + `content -> content` lifecycle transforms for the mission text form (sections: Pending / In Progress / Done; French equivalents also accepted). Missions can be tagged `[project:name]`. `start_mission()` (Pending→In Progress with stale-flush sanity), `complete_mission()`, `fail_mission()`, `insert_mission()` (normalizes to a `- ` prefix). Applied inside the store's write chokepoint; also used for one-time ingest and export rendering — **not** a separate source of truth.
- **`projects_config.py`** — Project configuration loader for `projects.yaml`. `load_projects_config()`, `get_projects_from_config()`, `get_project_config()` (merged defaults + overrides), `get_project_auto_merge()`, `get_project_cli_provider()`, `get_project_models()`, `get_project_tools()`. Per-project overrides for CLI provider, model selection, and tool restrictions. `ensure_github_urls()` auto-populates `github_url` fields from git remotes at startup.
- **`projects_migration.py`** — One-shot migration from env vars (`KOAN_PROJECTS`/`KOAN_PROJECT_PATH`) to `projects.yaml`. Runs at startup if `projects.yaml` doesn't exist.
- **`utils.py`** — File locking (thread + file locks), config loading, atomic writes, `get_branch_prefix()`, `get_known_projects()` (projects.yaml > KOAN_PROJECTS), `koan_tmp_dir()` (per-uid scratch/lock dir)
- **`config.py`** — Centralized configuration loading and access: tool config, model selection, Claude CLI flag building, behavioral settings, auto-merge config
- **`constants.py`** — Centralized numeric constants for the agent loop (thresholds, timeouts, tuning parameters). Import-as pattern preserves module-level attribute names for test compatibility.
- **`run_log.py`** — Shared colored logging wrapper (`log_safe(category, msg)`). Replaces per-module `_log_*` helpers.
- **`commit_conventions.py`** — Project commit convention detection and parsing. `get_project_commit_guidance()` reads CLAUDE.md commit-related sections or infers conventions from recent commit history. `parse_commit_subject()` extracts `COMMIT_SUBJECT:` markers from Claude output. Used by `rebase_pr.py` and `ci_queue_runner.py` to produce convention-aware commit messages.

**Agent loop pipeline** (called from `run.py`):

- **`iteration_manager.py`** — Per-iteration decision-making: usage refresh, mode selection, recurring injection, mission picking, project resolution.
- **`mission_executor.py`** — Per-iteration dispatch layer extracted from `run.py`. Contains `_run_iteration()` (full iteration orchestration: pick mission → dispatch → execute → finalize), `_handle_skill_dispatch()` (slash-command routing), and `_maybe_retry_mission()` (single transient-error retry). Calls back into `run.py` for `run_claude_task()` and `_finalize_mission()`.
- **`mission_runner.py`** — Execution pipeline helpers: `build_mission_command()` (CLI prompt + flags), `parse_claude_output()` (JSON → text extraction), and post-mission processing (usage tracking, pending.md archival, reflection, auto-merge). Called by `mission_executor.py` and `run.py`.
- **`loop_manager.py`** — Focus area resolution, pending.md creation, interruptible sleep with wake-on-mission, project validation
- **`contemplative_runner.py`** — Contemplative session runner (probability roll, prompt building, CLI invocation)
- **`quota_handler.py`** — Quota exhaustion detection from CLI output; parses reset times, creates pause state, writes journal entries
- **`prompt_builder.py`** — Agent prompt assembly for the agent loop. Includes budget-aware context trimming.
- **`event_scheduler.py`** — One-shot datetime-scheduled mission triggers. Reads `instance/events/*.json`, fires missions on schedule.
- **`suggestion_engine.py`** — Automation suggestion engine: surfaces recurring/schedule system recommendations with copy-pasteable commands
- **`pr_review_learning.py`** — Extracts actionable lessons from human PR reviews using Claude CLI (lightweight model). Fetches review data from GitHub, sends raw comments to Claude for natural-language analysis, and persists new lessons to `memory/projects/{name}/learnings.md` (write-once, read-many). Uses content-hash caching to skip re-analysis when reviews haven't changed.
- **`review_comment_dispatch.py`** — Automatic mission dispatch when human reviewers leave comments on Koan's open PRs. `fetch_unresolved_review_comments()` gathers unresolved inline + review-body comments (bot-filtered), `compute_comment_fingerprint()` produces a SHA-256 dedup key, and `check_and_dispatch_review_comments()` inserts a mission only when the fingerprint changes (tracked in `.review-dispatch-tracker.json`). Wired into `process_github_notifications()` in `loop_manager.py`. Opt-in via `review_dispatch: { enabled: true }` in `config.yaml`.
- **`skill_dispatch.py`** — Direct skill execution from agent loop. Detects `/command` missions, parses project prefix and command, dispatches to skill-specific runners (plan, rebase, recreate, check, claudemd) bypassing the Claude agent. Note: skill runners emit structured agent transcripts to stdout (DATA), not raw CLI output. `mission_executor.py` already passes `trust_stdout=False` to `_classify_and_handle_cli_error()` for these dispatches so the transcript text isn't mistaken for a quota/auth error message — keep that default when adding new dispatch pathways; individual runners do not call the classifier themselves.
- **`stagnation_monitor.py`** — Daemon thread that hashes the last N lines of Claude CLI stdout at configurable intervals. After K consecutive identical hashes, kills the subprocess group so a stuck-in-a-loop session does not burn quota for the full `mission_timeout`. Wired into `run_claude_task()`; stagnated missions are re-queued to Pending up to `max_retry_on_stagnation` times (per-mission counter persisted in `instance/.stagnation-retries.json`) before being tagged `[stagnation]` in `missions.md` and triggering the regular `_notify_stagnation()` Telegram warning. Each requeue sends a separate `_notify_stagnation_retry()` message. On requeue, `get_retry_info()` feeds `prompt_builder._get_stagnation_retry_section()`, which injects pattern-specific recovery coaching (template `system-prompts/stagnation-retry-context.md`) into the next session's user prompt so it doesn't repeat the same stuck approach.
- **`hooks.py`** — Hook system for extensible lifecycle events. Discovers `.py` modules from `instance/hooks/`, registers handlers by event name, fires them sequentially with per-handler error isolation. Events: `session_start`, `session_end`, `pre_mission`, `post_mission`.
- **`devcontainer.py`** — Devcontainer execution support. Detects spec-defined config locations (`is_devcontainer_present()`), resolves the container workspace path (`_get_container_workspace_path()` via `devcontainer read-configuration` with manual JSON fallback), brings the container up with feature injection and bind-mounts (`ensure_container_up()`), runs post-start git credential setup (`_run_container_setup()`), and wraps CLI commands with `devcontainer exec` prefix while translating host tmp paths to container paths (`wrap_command()`). Enabled per-project via `devcontainer: true` in `projects.yaml`. Provider-aware: the three `ghcr.io` features and the `gh auth login` credential step are claude-only.

**Bridge (Telegram):**

- **`awake.py`** — Main bridge loop, Telegram polling, outbox flushing
- **`command_handlers.py`** — Telegram command handlers extracted from awake.py; core commands (help, stop, pause, resume, skill) + skill dispatch
- **`bridge_state.py`** — Shared module-level state for bridge (config, paths, registries); avoids circular imports
- **`bridge_log.py`** — Colored log output for bridge process (mirrors run.py's `log()`)
- **`notify.py`** — Telegram notification helper with flood protection

**Process management:**

- **`pid_manager.py`** — Exclusive PID file enforcement for run, awake, and ollama processes. Provides `start_all()` (unified stack launcher with provider auto-detection), `start_runner()`, `start_awake()`, `start_ollama()`, and `stop_processes()` (graceful SIGTERM with force-kill fallback)
- **`pause_manager.py`** — Pause state management (`.koan-pause` / `.koan-pause-reason` files). Supports time-bounded pauses with auto-resume (e.g., `/pause 2h`)
- **`restart_manager.py`** — File-based restart signaling between bridge and run loop (`.koan-restart`)
- **`focus_manager.py`** — Focus mode management (`.koan-focus` JSON); skips contemplative sessions when active
- **`passive_manager.py`** — Passive mode management (`.koan-passive` JSON); read-only mode that blocks all execution while keeping loop alive
- **`cli_health.py`** — Startup CLI-binary-on-PATH probe (`check_primary_cli()` wrapping `CLIProvider.is_available()`) + in-memory degraded (no-mission) state and warn throttle. Set by `startup_manager.check_cli_binary()`, gated in `iteration_manager.plan_iteration` (`cli_unavailable_wait`), surfaced by `/status` and `/doctor`. Restart clears it (no signal file).

**CLI provider abstraction** (`koan/app/provider/`):

- **`provider/base.py`** — `CLIProvider` base class + tool name constants + per-provider usage tracking hooks (`supports_usage_tracking()`, `record_usage()`)
- **`provider/claude.py`** — `ClaudeProvider` (Claude Code CLI)
- **`provider/cline.py`** — `ClineProvider` (Cline CLI)
- **`provider/codex.py`** — `CodexProvider` (Codex CLI); quota surfaces only via the stream-json summary
- **`provider/copilot.py`** — `CopilotProvider` (GitHub Copilot CLI) with tool name mapping
- **`provider/__init__.py`** — Provider registry, resolution (env → config → default), cached singleton, and convenience functions (`run_command()`, `run_command_streaming()`, `build_full_command()`). Also per-role provider selection for the `cli:` config section: `get_provider_for_role()` (fresh path-bearing instance, never poisons the singleton), `get_fallback_provider()`, `resolve_role_provider()` (pre-flight fallback), and `describe_cli_roles()` (status/banner summary). Main entry point for the provider package.
- **`cli_provider.py`** — Re-export facade (legacy); prefer importing from `provider` directly

**Git & GitHub:**

- **`git_sync.py`** / **`git_auto_merge.py`** — Branch tracking, sync awareness, configurable auto-merge. Branch cleanup is time-throttled (default 24h per project, persisted in `.branch-cleanup-tracker.json`). Orphan branch detection (unmerged, no open PR) notifies via outbox.
- **`github.py`** — Centralized `gh` CLI wrapper (`run_gh()`, `pr_create()`, `issue_create()`)
- **`github_url_parser.py`** — Centralized GitHub URL parsing for PRs and issues
- **`github_skill_helpers.py`** — Shared helpers for GitHub-related skills (URL extraction, project resolution, mission queuing)
- **`github_config.py`** — GitHub notification config helpers (`get_github_nickname()`, `get_github_commands_enabled()`, `get_github_authorized_users()`)
- **`github_notifications.py`** — GitHub notification fetching, @mention parsing, reaction-based deduplication, permission checks
- **`github_command_handler.py`** — Bridges GitHub @mention notifications to missions: validate command → check permissions → react → create mission
- **`github_webhook.py`** — Opt-in push-based notification triggering (default off). A stdlib `http.server` receiver (started in the bridge via `maybe_start_from_config()`, or standalone via `make webhook`) verifies the HMAC-SHA256 signature, filters to known repos + actionable event types, and writes the `.koan-check-notifications` signal so the run loop performs an immediate forced poll — collapsing the 60-180s polling latency to ~10s. Reuses the full polling pipeline; polling remains the reliability fallback. Secret via `KOAN_GITHUB_WEBHOOK_SECRET`. See `docs/messaging/github-webhooks.md`.
- **`rebase_pr.py`** — PR rebase workflow
- **`recreate_pr.py`** — PR recreation: fetch metadata/diff, create fresh branch, reimplement from scratch
- **`claude_step.py`** — Shared helpers for git operations and Claude CLI invocation (used by pr_review, rebase_pr, recreate_pr). Also provides `run_ci_fix_loop()` — shared CI fix loop with configurable recheck semantics (polling vs single-shot) via `use_polling` flag and caller-specific `prompt_builder` callable.
- **`remote_rename_detector.py`** — Detects and fixes renamed GitHub remotes in workspace projects
- **`head_tracker.py`** — Detects remote HEAD branch changes (e.g. master → main) and updates local workspace. State persisted in `instance/.head-tracker.json`, throttled to once per 12h. Integrated into startup, manual trigger via `/rescan`.

**Issue tracking** (`koan/app/issue_tracker/`):

- **`issue_tracker/base.py`** — `IssueTracker` ABC: provider-neutral contract for fetch/comment/create operations
- **`issue_tracker/config.py`** — Per-project tracker routing (`get_tracker_for_project()`), Jira key → project mapping, code repository resolution. Configured via `tracker:` section in `projects.yaml` per-project overrides.
- **`issue_tracker/github.py`** — `GitHubIssueTracker` — GitHub Issues/PRs backend via `gh` CLI
- **`issue_tracker/jira.py`** — `JiraIssueTracker` — Jira backend via REST API
- **`issue_tracker/types.py`** — Shared data types (`IssueRef`, `IssueContent`)
- **`issue_tracker/enrichment.py`** — PR-review issue context enrichment. Parses tracker references (`PROJ-123` Jira keys / `owner/repo#123` cross-repo GitHub refs) out of a PR body, fetches a short summary via the project's configured provider, and returns a capped `{ISSUE_CONTEXT}` block for the review prompt. Best-effort: every path returns `""` on failure. Gated by `review_issue_context.enabled` (default on) and wired into `review_runner.build_review_prompt()`.
- **`issue_tracker/__init__.py`** — Service layer: `fetch_issue()`, `add_comment()`, `create_issue()`, `find_existing_plan_issue()`. Callers use these instead of branching on GitHub vs Jira.
- **`issue_cli.py`** — CLI entry point for issue tracker operations (fetch, comment, create) — used by prompts and subprocesses
- **`notification_config.py`** — Shared notification polling configuration helpers (interval resolution across GitHub/Jira providers)

**Other:**

- **`memory_manager.py`** — Per-project memory isolation, compaction, and cleanup. Includes semantic learnings compaction (Claude-powered dedup/merge), global memory file rotation, and configurable thresholds via `config.yaml` `memory:` section. Dual-writes to SQLite FTS5 index alongside JSONL truth log. `read_memory_window()` supports FTS5-ranked two-phase retrieval (relevance + recency fill).
- **`memory_db.py`** — SQLite FTS5 secondary index over the JSONL memory truth log. Provides `ensure_db()`, `insert_entry()`, `search_entries()` (BM25-ranked), `search_learnings()` (transient in-memory FTS5), `recent_entries()`, `delete_before()`, and `migrate_jsonl_to_sqlite()`. All functions catch `DatabaseError` and return empty results. Graceful degradation when FTS5 unavailable.
- **`usage_tracker.py`** — Per-provider budget tracking; decides autonomous mode (REVIEW/IMPLEMENT/DEEP/WAIT) based on each provider's independent quota percentage. Pure parser + threshold class — burn-rate-driven downgrades live in `iteration_manager._downgrade_if_burning_fast` next to the existing affordability downgrade.
- **`burn_rate.py`** — Rolling burn-rate estimator (% session quota per minute). Maintains a 20-sample circular buffer in `instance/.burn-rate.json` with `fcntl.flock(LOCK_SH)` on reads, exposes `record_run()`, `burn_rate_pct_per_minute()` (total cost / span; requires ≥5 samples and ≥15 min wall-clock span), `time_to_exhaustion(session_pct, mode=None)`, `clear_samples()` (called on session reset so pre-reset costs don't poison TTE), and the canonical `MODE_MULTIPLIERS` table shared with `usage_tracker.can_afford_run`. Also tracks the last-warning timestamp so the iteration manager fires at most one Telegram alert per quota cycle. Burn-rate mode downgrades soft-throttle only (deep→implement→review); they never force `wait`.
- **`recover.py`** — Crash recovery for stale in-progress missions
- **`prompts.py`** — System prompt loader; `load_prompt()` for `koan/system-prompts/*.md`, `load_skill_prompt()` for skill-bound prompts. Supports `{@include partial-name}` directive for reusable prompt fragments from `koan/system-prompts/_partials/`.
- **`skill_manager.py`** — External skill package manager: install from Git repos, update, remove, track via `instance/skills.yaml`
- **`claudemd_refresh.py`** — CLAUDE.md refresh pipeline: gathers git context, invokes Claude to update/create CLAUDE.md. When CLAUDE.md is missing, dispatches the built-in `/init` skill instead of a generic prompt.
- **`update_manager.py`** — Kōan self-update: stash, checkout main, fetch/pull from upstream, report changes
- **`auto_update.py`** — Automatic update checker and self-commit tracker. Periodically fetches upstream, triggers pull + restart when new commits are available. Also tracks Kōan's own HEAD across startups — records current SHA in `instance/.commit-tracker.json`, reports new commits via Telegram on subsequent startups. Configurable via `auto_update` section in `config.yaml` (`enabled`, `check_interval`, `notify`)
- **`ci_dispatch.py`** — Auto-dispatch fix missions when CI fails on Koan-authored PRs. Checks open PRs by branch prefix, fetches check-run status via GitHub API, inserts fix missions with log snippets. Dedup via `.ci-dispatch-tracker.json` keyed by PR+SHA+job. Configurable via `ci_dispatch` section in `config.yaml` (`enabled`, `cooldown_minutes`, `log_snippet_bytes`).
- **`security_review.py`** — Differential security review on mission diffs: blast radius analysis, risk classification, journal logging. Runs before auto-merge decisions.
- **`rename_project.py`** — CLI tool to rename a project across `projects.yaml` and all `instance/` files (missions, memory dir, journal files, JSON references). Dry-run by default, `--apply` to execute. Invoked via `make rename-project old=X new=Y [apply=1]`.
- **`mission_ctl.py`** — Break-glass CLI to inspect/edit the mission store from the terminal when the Telegram bridge is unresponsive (agent stuck on a mission). `list [state]` reads the authoritative store directly; `delete <selector>` (`i<N>`/`p<N>`/keyword) removes a pending mission or aborts an in-progress one (→ Failed) through the same flock-protected `utils.modify_missions_file` chokepoint the daemons use, so the store + `missions.md` export stay consistent. Invoked via `make missions` / `make mission-rm sel=i1`. See `docs/operations/mission-cli.md`.
- **`usage_service.py`** — Shared usage-payload builder (`build_usage_payload()` + week/month bucketing) used by both the dashboard and the REST API (`GET /v1/usage`).
- **`log_reader.py`** — Shared log-tailing helpers (`tail_log()`, `read_logs()`) used by both the dashboard and the REST API (`GET /v1/logs`).
- **`artifact_db.py`** — Unified artifact schema + migration harness. `ARTIFACT_SCHEMAS` (TableSpec/ColumnSpec dataclasses) for missions/journal_entries/memory_entries/outbox_messages/audit_log; `create_tables()`, `verify_schema()` (PRAGMA drift check), `dual_write()` (file authoritative + best-effort DB projection), `rebuild_from_file()` (re-read file + replace projection, clears dirty flag — recovery handle for diverged append projections), `read_from_db_or_file()` (DB-first, file fallback, file-stable order). File stays source of truth (mirrors `memory_db`). Library only — downstream issues wire it into live artifact paths. See `docs/architecture/artifact-db.md`.

**Web dashboard** (`koan/app/dashboard/`):

- **`dashboard/`** — Flask blueprint package built by a `create_app()` factory (mirrors `api/__init__.py`). Blueprints: `core` (index, auth, status/health/forecast/provider), `missions` (mission CRUD + attention), `chat` (chat + progress/state SSE), `usage` (usage/metrics/efficiency/journal/logs), `agent` (soul/memory/skills/config + pause/resume/restart), `config` (config/nickname/rules/recurring), `prs` (PRs + plans), `projects` (registry/welcome screen at `/projects` + `/api/projects/<name>/status` + `/projects/add`). Runnable entry: `app/dashboard/__main__.py` (used by `make dashboard` and `pid_manager.start_dashboard()`). `from app.dashboard import app` exposes the module-level instance for the test suite.
  - **`dashboard/state.py`** — Single home for patchable module globals (paths, `CHAT_TIMEOUT`, `DASHBOARD_PWD`, caches, regexes). Route/service code reads `state.X` at call time so tests patch one target (`patch.object(app.dashboard.state, …)`).
  - **`dashboard/_helpers.py`** — Cross-cutting Flask wiring: passphrase gate, static cache-buster, context processor, template filters (`strip_project_tag`, `project_badge`, `linkify`); attached via `register_helpers(app)`.
- **`dashboard_service/`** — Pure business logic extracted from the routes, unit-tested without a Flask client: `missions` (parse/filter/project+skill names), `journal` (date/day readers + rule history), `plans` (plan-issue fetch + progress parsing), `stats` (forecast, skill metrics, agent-state readers), `projects` (per-project registry card assembly: counts, github_url, provider/model, last-activity, config checklist); package-level `read_file`/`mask_sensitive`/`validate_yaml`. Dashboard templates live under `koan/templates/dashboard/`.

**REST API** (`koan/app/api/`):

- **`api/__init__.py`** — `create_app()` Flask factory; registers blueprints, health endpoint, JSON error handlers, per-request audit logging.
- **`api/auth.py`** — `require_token` decorator (Bearer parse + `hmac.compare_digest`); token resolution (env → config).
- **`api/mission_index.py`** — Sidecar reader/writer for `instance/.api-missions.json` (atomic via `utils.atomic_write_json`). `record_mission()`, `get_mission()`, `list_missions()`, `reconcile()` (maps stored text → current `missions.md` section), `cancel_mission()`.
- **`api/routes_missions.py`** — `GET/POST /v1/missions`, `GET/DELETE /v1/missions/{id}`.
- **`api/routes_projects.py`** — `GET /v1/projects`, `POST /v1/projects`, `DELETE /v1/projects/{name}`.
- **`api/routes_status.py`** — `GET /v1/status` (agent state + mission counts from signal files).
- **`api/routes_admin.py`** — `POST /v1/pause`, `POST /v1/resume`, `GET /v1/config` (secrets masked), `POST /v1/restart`, `POST /v1/shutdown`, `POST /v1/update`.
- **`api/routes_observability.py`** — `GET /v1/usage`, `GET /v1/metrics`, `GET /v1/logs` (token-gated; delegate to usage_service / mission_metrics / log_reader).
- **`api/server.py`** — Runnable entrypoint (`make api`); validates token at startup (fail-closed), warns on non-loopback bind, calls `waitress.serve(create_app(), ...)`.
- **`api/openapi_gen.py`** — Generates the OpenAPI 3.1 document (`koan/openapi.yaml`) by introspecting the live `create_app()` route table. `build_spec(app)` (pure) → `dump_yaml()` (deterministic) → `generate()`/`check()`; CLI `python -m app.api.openapi_gen [--output PATH] [--check]`. Security is derived from the `require_token` marker (`_koan_requires_token`), not an allow-list. **When you add/remove/modify a route, run `make openapi` and commit `koan/openapi.yaml` in the same change** — never hand-edit it. `make openapi-check` (and CI `.github/workflows/openapi.yml`, path-filtered to API changes) fails on drift.

Config additions in `config.py`: `is_api_enabled()`, `get_api_host()` (default `127.0.0.1`), `get_api_port()` (default `8420`), `get_api_token()` (env `KOAN_API_TOKEN` → `api.token` → `""`), `get_api_threads()` (default `8`). `pid_manager.py` adds `"api"` to `PROCESS_NAMES` and provides `start_api()` / `_is_api_enabled()`. See `docs/operations/rest-api.md`.

### Instance directory

`instance/` (gitignored, copy from `instance.example/`) holds all runtime state:

- `missions.db` — Authoritative SQLite mission store (missions + CI queue + ideas + quarantine tables)
- `missions.md` — Generated **read-only export** of the mission store (human-readable; edits ignored after the one-time sync)
- `outbox.md` — Bot → Telegram message queue (written atomically by `append_to_outbox()`)
- `outbox-sending.md` — Crash-safety staging file for outbox flush; `OutboxManager.recover_staged()` re-sends on restart
- `.notify-dedup.json` — Persistent `{hash: last_sent_ts}` map for cross-restart dedup of idempotent lifecycle notices (`notify_dedup.py`, #2426); prunes entries older than the dedup window
- `config.yaml` — Per-instance configuration (tools, auto-merge rules)
- `soul.md` — Agent personality definition
- `memory/` — Global summary + per-project learnings/context + `memory.db` (SQLite FTS5 index)
- `journal/` — Daily logs organized as `YYYY-MM-DD/project.md`
- `events/` — One-shot scheduled missions (JSON files consumed by `event_scheduler.py`)
- `pending-jira-plan-publishes/` — Jira `/plan` comments generated but not yet confirmed delivered (`jira_plan_publish.py`); replayed on the next `/plan` for the same issue so a failed publish never costs a second model run, and dropped after three failed runs or on expiry
- `hooks/` — User-defined Python hook modules for lifecycle events (see `instance.example/hooks/README.md`)
- `recovery.jsonl` — Append-only audit log written by `recover.py` each time a stale In Progress mission is processed at startup
- `.oauth-usage.json` — Cached authoritative usage anchor (session/weekly %, real reset timestamps, and the local token-counter values at poll time) written by `authoritative_usage.py` when the optional OAuth usage source is active (issue #2455). Best-effort; absence/corruption degrades to the local heuristic.
