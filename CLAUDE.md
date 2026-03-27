# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Kōan

Kōan is an autonomous background agent that uses idle Claude API quota to work on local projects. It runs as a continuous loop, pulling missions from a shared file, executing them via Claude Code CLI, and communicating progress via Telegram. Philosophy: "The agent proposes. The human decides." — no unsupervised code modifications.

## Commands

```bash
make setup          # Create venv, install dependencies
make start          # Start full stack (auto-detects provider: awake+run or ollama+awake+run)
make stop           # Stop all running processes (run + awake + ollama)
make status         # Show running process status
make logs           # Watch live output from all processes + agent progress
make run            # Start main agent loop (foreground)
make awake          # Start Telegram bridge (foreground)
make ollama         # Start full Ollama stack (ollama serve + awake + run)
make dashboard      # Start Flask web dashboard (port 5001)
make test           # Run full test suite (pytest)
make say m="..."    # Send test message as if from Telegram
make rename-project old=X new=Y [apply=1]  # Rename a project everywhere (dry-run by default)
make clean          # Remove venv
```

Run a single test file:
```bash
KOAN_ROOT=/tmp/test-koan .venv/bin/pytest koan/tests/test_missions.py -v
```

## Test suite

- **`KOAN_ROOT` must be set** when running tests. Many modules (`utils.py`, `awake.py`) check for `KOAN_ROOT` at import time and raise `SystemExit` if it's missing. Use `KOAN_ROOT=/tmp/test-koan` (or any path) as a prefix: `KOAN_ROOT=/tmp/test-koan .venv/bin/pytest koan/tests/ -v`
- Never call Claude (subprocess) in tests. Mock `format_and_send` which invokes Claude CLI for message formatting.
- With `runpy.run_module()` (CLI tests), patch both `app.<module>.format_and_send` **and** `app.notify.format_and_send` — `runpy` re-executes the module so the import-level binding escapes the first patch.
- When `load_dotenv()` would reload env vars from `.env` (defeating `monkeypatch.delenv`), patch `app.notify.load_dotenv` too.
- **Test behavior, not implementation.** Unless the project's own conventions say otherwise, tests should validate what code does (inputs → outputs, side effects, observable state), not how it does it. Mocking internal dependencies of the unit under test is fine, but tests must never read or inspect actual source code to verify whether specific code is present or absent — that couples tests to implementation text rather than behavior. Prefer asserting on return values, raised exceptions, file contents, or other observable outcomes.

## Architecture

Two parallel processes run independently:

- **`awake.py`** (Telegram bridge): Polls Telegram every 3s. Classifies messages as "chat" (instant Claude reply) or "mission" (queued to `missions.md`). Flushes `outbox.md` messages back to Telegram. Command handling is split into `command_handlers.py`, shared state in `bridge_state.py`, colored log output in `bridge_log.py`.
- **`run.py`** (agent loop): Pure-Python main loop with restart wrapper. Picks pending missions, transitions them through Pending→In Progress→Done/Failed lifecycle, executes via Claude Code CLI or direct skill dispatch. Signal handling uses double-tap CTRL-C protection (`protected_phase` context manager). Writes real-time status to `.koan-status`. Uses `mission_runner.py` (execution pipeline), `loop_manager.py` (sleep/focus/validation), `quota_handler.py` (quota detection), `contemplative_runner.py` (reflection sessions).

Communication between processes happens through shared files in `instance/` with atomic writes (`utils.atomic_write()` using temp file + rename + `fcntl.flock()`). Exclusive process instances enforced via `pid_manager.py` (PID file + `fcntl.flock()`).

### Key modules (`koan/app/`)

**Core data & config:**
- **`missions.py`** — Single source of truth for `missions.md` parsing (sections: Pending / In Progress / Done; French equivalents also accepted). Missions can be tagged `[project:name]`. Provides explicit lifecycle transitions: `start_mission()` (Pending→In Progress with stale-flush sanity enforcement), `complete_mission()`, `fail_mission()`.
- **`projects_config.py`** — Project configuration loader for `projects.yaml`. `load_projects_config()`, `get_projects_from_config()`, `get_project_config()` (merged defaults + overrides), `get_project_auto_merge()`, `get_project_cli_provider()`, `get_project_models()`, `get_project_tools()`. Per-project overrides for CLI provider, model selection, and tool restrictions. `ensure_github_urls()` auto-populates `github_url` fields from git remotes at startup.
- **`projects_migration.py`** — One-shot migration from env vars (`KOAN_PROJECTS`/`KOAN_PROJECT_PATH`) to `projects.yaml`. Runs at startup if `projects.yaml` doesn't exist.
- **`utils.py`** — File locking (thread + file locks), config loading, atomic writes, `get_branch_prefix()`, `get_known_projects()` (projects.yaml > KOAN_PROJECTS)

**Agent loop pipeline** (called from `run.py`):
- **`iteration_manager.py`** — Per-iteration decision-making: usage refresh, mode selection, recurring injection, mission picking, project resolution.
- **`mission_runner.py`** — Full mission lifecycle: build CLI command, execute, parse JSON output, usage tracking, archival, reflection, auto-merge
- **`loop_manager.py`** — Focus area resolution, pending.md creation, interruptible sleep with wake-on-mission, project validation
- **`contemplative_runner.py`** — Contemplative session runner (probability roll, prompt building, CLI invocation)
- **`quota_handler.py`** — Quota exhaustion detection from CLI output; parses reset times, creates pause state, writes journal entries
- **`prompt_builder.py`** — Agent prompt assembly for the agent loop
- **`pr_review_learning.py`** — Extracts actionable lessons from human PR reviews using Claude CLI (lightweight model). Fetches review data from GitHub, sends raw comments to Claude for natural-language analysis, and persists new lessons to `memory/projects/{name}/learnings.md` (write-once, read-many). Uses content-hash caching to skip re-analysis when reviews haven't changed.
- **`skill_dispatch.py`** — Direct skill execution from agent loop. Detects `/command` missions, parses project prefix and command, dispatches to skill-specific runners (plan, rebase, recreate, check, claudemd) bypassing the Claude agent
- **`hooks.py`** — Hook system for extensible lifecycle events. Discovers `.py` modules from `instance/hooks/`, registers handlers by event name, fires them sequentially with per-handler error isolation. Events: `session_start`, `session_end`, `pre_mission`, `post_mission`.

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

**CLI provider abstraction** (`koan/app/provider/`):
- **`provider/base.py`** — `CLIProvider` base class + tool name constants
- **`provider/claude.py`** — `ClaudeProvider` (Claude Code CLI)
- **`provider/copilot.py`** — `CopilotProvider` (GitHub Copilot CLI) with tool name mapping
- **`provider/__init__.py`** — Provider registry, resolution (env → config → default), cached singleton, and convenience functions (`run_command()`, `run_command_streaming()`, `build_full_command()`). Main entry point for the provider package.
- **`cli_provider.py`** — Re-export facade (legacy); prefer importing from `provider` directly

**Git & GitHub:**
- **`git_sync.py`** / **`git_auto_merge.py`** — Branch tracking, sync awareness, configurable auto-merge
- **`github.py`** — Centralized `gh` CLI wrapper (`run_gh()`, `pr_create()`, `issue_create()`)
- **`github_url_parser.py`** — Centralized GitHub URL parsing for PRs and issues
- **`github_skill_helpers.py`** — Shared helpers for GitHub-related skills (URL extraction, project resolution, mission queuing)
- **`github_config.py`** — GitHub notification config helpers (`get_github_nickname()`, `get_github_commands_enabled()`, `get_github_authorized_users()`)
- **`github_notifications.py`** — GitHub notification fetching, @mention parsing, reaction-based deduplication, permission checks
- **`github_command_handler.py`** — Bridges GitHub @mention notifications to missions: validate command → check permissions → react → create mission
- **`rebase_pr.py`** — PR rebase workflow
- **`recreate_pr.py`** — PR recreation: fetch metadata/diff, create fresh branch, reimplement from scratch
- **`claude_step.py`** — Shared helpers for git operations and Claude CLI invocation (used by pr_review, rebase_pr, recreate_pr)

**Other:**
- **`memory_manager.py`** — Per-project memory isolation and compaction
- **`usage_tracker.py`** — Budget tracking; decides autonomous mode (REVIEW/IMPLEMENT/DEEP/WAIT) based on quota percentage
- **`recover.py`** — Crash recovery for stale in-progress missions
- **`prompts.py`** — System prompt loader; `load_prompt()` for `koan/system-prompts/*.md`, `load_skill_prompt()` for skill-bound prompts
- **`skill_manager.py`** — External skill package manager: install from Git repos, update, remove, track via `instance/skills.yaml`
- **`claudemd_refresh.py`** — CLAUDE.md refresh pipeline: gathers git context, invokes Claude to update/create CLAUDE.md
- **`update_manager.py`** — Kōan self-update: stash, checkout main, fetch/pull from upstream, report changes
- **`auto_update.py`** — Automatic update checker: periodically fetches upstream, triggers pull + restart when new commits are available. Configurable via `auto_update` section in `config.yaml` (`enabled`, `check_interval`, `notify`)
- **`rename_project.py`** — CLI tool to rename a project across `projects.yaml` and all `instance/` files (missions, memory dir, journal files, JSON references). Dry-run by default, `--apply` to execute. Invoked via `make rename-project old=X new=Y [apply=1]`.

### Skills system (`koan/skills/`)

Extensible command plugin system. Each skill lives in `skills/<scope>/<skill-name>/` with a `SKILL.md` (YAML frontmatter defining commands, aliases, metadata) and an optional `handler.py`.

- **`skills.py`** — Registry that discovers SKILL.md files, parses frontmatter (custom lite YAML parser, no PyYAML), maps commands/aliases to skills, and dispatches execution.
- **Core skills** live in `koan/skills/core/` (audit, cancel, chat, check, claudemd, delete_project, focus, idea, implement, journal, language, list, live, magic, mission, passive, plan, pr, priority, projects, quota, rebase, recreate, recurring, refactor, reflect, review, shutdown, sparring, start, status, update, verbose)
- **Custom skills** loaded from `instance/skills/<scope>/` — each scope directory can be a cloned Git repo for team sharing.
- **Handler pattern**: `def handle(ctx: SkillContext) -> Optional[str]` — return string for Telegram reply, empty string for "already handled", None for no message.
- **`worker: true`** flag in SKILL.md marks blocking skills (Claude calls, API requests) that run in a background thread.
- **`github_enabled: true`** flag marks skills that can be triggered via GitHub @mentions. **`github_context_aware: true`** means the skill accepts additional context after the command.
- **Prompt-only skills**: omit `handler`, put prompt text after the frontmatter — sent to Claude directly.
- See `koan/skills/README.md` for the full authoring guide.

### Instance directory

`instance/` (gitignored, copy from `instance.example/`) holds all runtime state:
- `missions.md` — Task queue
- `outbox.md` — Bot → Telegram message queue
- `config.yaml` — Per-instance configuration (tools, auto-merge rules)
- `soul.md` — Agent personality definition
- `memory/` — Global summary + per-project learnings/context
- `journal/` — Daily logs organized as `YYYY-MM-DD/project.md`
- `hooks/` — User-defined Python hook modules for lifecycle events (see `instance.example/hooks/README.md`)

## Conventions

- Claude always creates **`<prefix>/*` branches** (default `koan/`, configurable via `branch_prefix` in `config.yaml`), never commits to main
- Project config via `projects.yaml` at KOAN_ROOT (primary), with `KOAN_PROJECTS` env var as fallback. Supports per-project overrides for `cli_provider`, `models`, `tools`, and `git_auto_merge`.
- Environment config via `.env` file and `KOAN_*` variables for secrets and system settings. **CLI provider** is configured via `KOAN_CLI_PROVIDER` env var (primary), with fallback to `CLI_PROVIDER` for backward compatibility. The centralized `get_cli_provider_env()` helper in `utils.py` handles this resolution.
- Multi-project support: up to 50 projects, each with isolated memory under `memory/projects/{name}/`
- Tests use temp directories and isolated env vars — no real Telegram calls
- `system-prompt.md` defines the Claude agent's identity, priorities, and autonomous mode rules
- **No inline prompts in Python code** — LLM prompts MUST be extracted to `.md` files. Skill-bound prompts go in `skills/<scope>/<name>/prompts/` and are loaded via `load_skill_prompt()`. Infrastructure prompts used by `koan/app/` modules stay in `koan/system-prompts/` and are loaded via `load_prompt()`.
- **System prompts must be generic** — Never reference specific instance details like owner names in system prompts. Use generic terms like "your human" instead of personal names. Prompts are in English; instance-specific personality and language preferences come from `soul.md`.
- **User manual maintenance** — When adding, removing, or modifying a core skill, update `docs/user-manual.md` accordingly: add the skill to the appropriate tier section and the quick-reference appendix. The manual must stay in sync with `koan/skills/core/`.
- **Help group enforcement** — Every core skill MUST have a `group:` field in its SKILL.md frontmatter (one of: missions, code, pr, status, config, ideas, system). This ensures commands are discoverable via `/help`. If adding a new hardcoded core command (not skill-based), add it to `_CORE_COMMAND_HELP` in `command_handlers.py`. The test suite enforces this — `TestCoreSkillGroupEnforcement` will fail if a core skill is missing its group.
- **No hyphens in skill names or aliases** — Skill command names, aliases, and directory names MUST use underscores (`_`), never hyphens (`-`). Hyphens break Telegram command parsing because Telegram treats the hyphen as a word boundary, cutting the command short. Example: use `dead_code` not `dead-code`, `scaffold_skill` not `scaffold-skill`.
- **Documentation maintenance** — When adding or modifying a feature, update the corresponding section in `README.md` and/or the relevant `docs/*.md` file (e.g., `docs/user-manual.md`, `docs/skills.md`, `docs/auto-update.md`). If no documentation file exists for the feature, create one under `docs/`. Public-facing documentation must stay in sync with the codebase — undocumented features are invisible to users.
