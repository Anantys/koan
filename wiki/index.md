# Wiki Index

The catalog of all pages in this wiki. Each entry: a link to the page and a one-line summary. Read this first (after `SCHEMA.md`) when answering a query or planning a feature, to identify candidate pages before reading them.

This wiki spans two content roots — `docs/` (operational "how to use", see [`docs/README.md`](docs/README.md)) and the durable half of `specs/` (design "why/contract", see [`specs/README.md`](../specs/README.md)) — reached from here via the `wiki/docs`, `wiki/specs-components`, `wiki/specs-skills` symlinks. Speckit's ephemeral per-feature folders (`specs/<NNN-slug>/`) are listed under "Specs — Active Features" below with a computed status, not frontmattered. See `SCHEMA.md` for the full rationale.

~85 pages total — well under the ~150-page / 300-line shard threshold, so this stays flat.

---

## Docs

### Architecture
- [`architecture/artifact-db.md`](docs/architecture/artifact-db.md) — Documents the artifact_db.py harness migrating markdown/JSONL artifacts to a rebuildable SQLite projection: TableSpec/ColumnSpec schemas, connect/create_tables/verify_schema, dual_write (replace/append), rebuild_from_file recovery, and read_from_db_or_file with file fallback.
- [`architecture/daemon.md`](docs/architecture/daemon.md) — Describes how the Koan daemon is assembled: startup/process management, the bridge's chat/bg worker lanes, the agent loop's modular pieces, runtime modes, parallel sessions, and the bounded-memory model for CLI stdout capture.
- [`architecture/github-and-trackers.md`](docs/architecture/github-and-trackers.md) — Covers GitHub/Jira notification flow, PR workflows (footer, receiving-code-review protocol), review issue-tracker enrichment, and the instance/ tracker files used to dedupe work.
- [`architecture/bridge-memory.md`](docs/architecture/bridge-memory.md) — How awake.py bounds RSS over long uptime: tail-read history, periodic mid-session compaction, one-cycle mission-store read cache, and an opt-in MemoryMonitor watchdog backstop.
- [`architecture/hooks.md`](docs/architecture/hooks.md) — Documents the lifecycle-event system (session_start/session_end/pre_mission/post_mission/post_review): instance-wide and skill-bound Python hooks via `HookRegistry`, plus the declarative automation-rules layer (notify/create_mission/pause/resume/auto_merge) with its per-rule loop guard.
- [`architecture/memory.md`](docs/architecture/memory.md) — Details Koan's Markdown+JSONL memory store, the SQLite FTS5 secondary index (confidence-weighted BM25 ranking, dual-write, fallback), entry schema, read/write paths, and compaction.
- [`architecture/mission-lifecycle.md`](docs/architecture/mission-lifecycle.md) — Explains the mission queue format and lifecycle (Pending/In Progress/Done/Failed), org-wide missions, branch prep, direct skill dispatch, scheduling, recovery/retries, and missions.md integrity/size-bound safeguards.
- [`architecture/overview.md`](docs/architecture/overview.md) — High-level architecture summary of Koan's two main processes (bridge and agent loop), major subsystems, and the human-decides safety model.
- [`architecture/providers.md`](docs/architecture/providers.md) — Documents the CLI provider abstraction layer, provider responsibilities (including KOAN_ROOT `project_context` isolation), resolution flow, and the current supported providers (Claude, Cline, Codex, Copilot, Haze, Grok, Ollama-launch).
- [`architecture/shared-state.md`](docs/architecture/shared-state.md) — Explains Koan's file-based (no-database) shared state under instance/, locking/atomic-write conventions, per-uid temp/scratch directories, and configuration sources.
- [`architecture/skills-system.md`](docs/architecture/skills-system.md) — Describes the skill definition format, dispatch paths, the private implementation review gate (challenge loop, cost controls, dedup), and the documentation contract for skill changes.

### Design
- [`design/decisions.md`](docs/design/decisions.md) — Records durable Koan design decisions (human authority, local files over DB, branch isolation, provider isolation, prompt files, public artifact genericity, documentation-first).
- [`design/introspection-beyond-code.md`](docs/design/introspection-beyond-code.md) — A session-107 introspective proposal for making Kōan a relational companion, identifying five missing dimensions (emotional memory, self-reflection, proactivity, relationship rituals, identity) and a phased implementation plan.
- [`design/memory-injection.md`](docs/design/memory-injection.md) — Documents the shipped memory-injection feature threading project memory (learnings/context/priorities) into the five mission-driving skills plus anti-thrash and semantic-dedup safeguards for memory compaction.
- [`design/review-consistency.md`](docs/design/review-consistency.md) — Why `/review` is stable across re-runs (deterministic identity/reuse/freeze), how the yellow-tier bar and `[Pre-Existing Issue]` labeling work, and the deliberate "human decides" posture for honoring PR-comment dispositions and its prompt-injection tradeoff.
- [`design/spec-always-up-railway.md`](docs/design/spec-always-up-railway.md) — A deployment proposal for running Kōan as an always-on Docker service on Railway, covering CLI auth, persistent state via git, and security hardening.
- [`design/spec-changes-are-architectural.md`](docs/design/spec-changes-are-architectural.md) — Why durable design-contract specs (specs/components/**, specs/skills/**) are changed contract-first, kept rare, and declared in the PR for review before approval — and how the spec-change guard enforces it.

### Messaging
- [`messaging/discord.md`](docs/messaging/discord.md) — Setup guide for using Discord as Kōan's messaging bridge via REST polling instead of the Gateway/WebSocket API.
- [`messaging/github-alerts.md`](docs/messaging/github-alerts.md) — How to emit GitHub alert callouts (NOTE/TIP/IMPORTANT/WARNING/CAUTION) via the shared `build_alert()` helper, with the type→situation mapping and the ≤1–2-per-comment parsimony rule.
- [`messaging/github-commands.md`](docs/messaging/github-commands.md) — Full reference for triggering Kōan via `@mention` commands in GitHub PR/issue comments, including config, dedup, security, and fallback scanning.
- [`messaging/github-webhooks.md`](docs/messaging/github-webhooks.md) — Describes the opt-in push-based GitHub webhook receiver that collapses notification-polling latency while polling remains the reliability fallback.
- [`messaging/jira-integration.md`](docs/messaging/jira-integration.md) — Full reference for controlling Kōan via `@mention` commands in Jira issue comments, including project mapping, ADF parsing, and coexistence with GitHub.
- [`messaging/matrix.md`](docs/messaging/matrix.md) — Setup guide for using a Matrix homeserver as Kōan's messaging provider via the Client-Server HTTP API.
- [`messaging/messaging-level.md`](docs/messaging/messaging-level.md) — Explains the `messaging.level` setting (`normal`/`debug`) that controls how much lifecycle/progress chatter Kōan's Telegram/Slack bridge sends versus only logs.
- [`messaging/slack.md`](docs/messaging/slack.md) — Step-by-step guide to configuring Kōan with Slack (Socket Mode app setup, scopes, env vars) plus Slack-specific behavior like threading, reactions, and the assistant "thinking" status.
- [`messaging/telegram.md`](docs/messaging/telegram.md) — Step-by-step guide to configuring Kōan with Telegram (bot creation, chat ID, env vars), including group-chat privacy-mode setup and troubleshooting.

*(`docs/messaging/slack-app-manifest.json` is a config asset, not a wiki page — not indexed.)*

### Operations
- [`operations/auto-update.md`](docs/operations/auto-update.md) — Describes Kōan's opt-in auto-update feature that checks for and pulls upstream commits, plus the always-on release-tag notification.
- [`operations/config-sync.md`](docs/operations/config-sync.md) — Documents real-time config sync: the dashboard reflects config.yaml/projects.yaml edits within ~2s over the existing SSE, classifying safe hot-reload keys vs restart-required changes and gating restarts on agent idleness.
- [`operations/dashboard.md`](docs/operations/dashboard.md) — Documents the local Flask web dashboard's architecture, blueprints, pages, passphrase gate, design-system integration, and structured `/progress` mission timeline.
- [`operations/interactive-launcher.md`](docs/operations/interactive-launcher.md) — Describes `make koan`, the TTY-gated interactive launcher and its textual terminal dashboard (tabs, toggles, keybindings).
- [`operations/log-formatting.md`](docs/operations/log-formatting.md) — Documents the display-side `[cli]` log formatter (log_fmt.py) behind `make logs` and the shared `classify_cli` grammar used by the dashboard `/progress` timeline.
- [`operations/maint.md`](docs/operations/maint.md) — Covers Kōan's release process and branch philosophy (`main` vs `stable`), the `make release` procedure, versioning scheme, and recovery steps.
- [`operations/memory-footprint.md`](docs/operations/memory-footprint.md) — Why the container memory graph plateaus high after missions (page cache + slab, not a leak), the /tmp leftovers that inflate it, the post-mission sweep, the per-mission cgroup scope that kills leaked build daemons, and the anon-first triage rule.
- [`operations/memory-watchdog.md`](docs/operations/memory-watchdog.md) — Explains the memory watchdog that restarts the agent loop between missions when RSS stays over a threshold, its config knobs, and health-endpoint observability.
- [`operations/mission-cli.md`](docs/operations/mission-cli.md) — Terminal commands (`make missions` / `make mission-rm`, or `python -m app.mission_ctl`) to inspect and edit the SQLite mission store directly when the Telegram bridge is unresponsive.
- [`operations/pr-reports.md`](docs/operations/pr-reports.md) — Documents the `/report` skill that posts per-project and global GitHub PR activity digests (created/merged/interacted metrics) over weekly/monthly windows.
- [`operations/rest-api.md`](docs/operations/rest-api.md) — Documents Kōan's optional, token-authenticated HTTP control layer (missions, projects, pause/resume, config, admin, usage/metrics/logs endpoints) and its security model.
- [`operations/rtk.md`](docs/operations/rtk.md) — Explains the optional rtk CLI-proxy integration (detection, awareness injection, hook setup) that compresses dev-command output for token savings.
- [`operations/skill-evals.md`](docs/operations/skill-evals.md) — Describes the deterministic eval harness that scores LLM-driven skills (review, fix, plan, brainstorm, rebase) against golden datasets in offline (CI) and live modes.
- [`operations/troubleshooting.md`](docs/operations/troubleshooting.md) — Catalogs common operational issues (agent loop, git/worktrees, memory, bridge, GitHub, CLI provider, parallel sessions, config) and their fixes.

### Providers
- [`providers/claude-cli-commands-official.md`](docs/providers/claude-cli-commands-official.md) — Official upstream Claude Code CLI reference listing all commands and flags.
- [`providers/claude.md`](docs/providers/claude.md) — Setup and configuration guide for Kōan's default Claude Code CLI provider, including models, tools, per-role CLI config, MCP (`mcp_roles` per-role opt-in), KOAN_ROOT project-context isolation (`--setting-sources user`), and devcontainer mode.
- [`providers/cline.md`](docs/providers/cline.md) — Setup and feature-mapping guide for using Cline CLI as Kōan's underlying multi-backend AI provider.
- [`providers/codex.md`](docs/providers/codex.md) — Setup and behavior guide for using OpenAI's Codex CLI as Kōan's provider, including quota/usage handling and troubleshooting.
- [`providers/copilot.md`](docs/providers/copilot.md) — Setup guide and feature/tool-mapping differences for using GitHub Copilot CLI as Kōan's provider.
- [`providers/fake.md`](docs/providers/fake.md) — Fail-closed no-op provider for deterministic, offline tests of the skill pipeline; refuses to run unless `KOAN_ALLOW_FAKE_PROVIDER=1` is set.
- [`providers/haze.md`](docs/providers/haze.md) — Setup and behavior guide for using haze (multi-backend agentic CLI) as Kōan's provider, including stream-json integration, usage accounting, capabilities and limitations.
- [`providers/grok.md`](docs/providers/grok.md) — Setup and behavior guide for using xAI's Grok Build CLI as Kōan's provider, including headless streaming-json, auth, models, and limitations.
- [`providers/local.md`](docs/providers/local.md) — Explains that the `local` Ollama provider was removed and points to `ollama-launch` or a custom Claude CLI endpoint as the supported replacements.
- [`providers/ollama-launch.md`](docs/providers/ollama-launch.md) — Documents the `ollama-launch` provider, which runs the Claude Code CLI through `ollama launch claude` for full tool-use/streaming parity with native Claude.
- [`providers/ollama-wrapper.md`](docs/providers/ollama-wrapper.md) — Describes the `bin/ollama-claude` wrapper that routes Koan's default `claude` provider through a local Ollama model via `ollama launch claude`, without changing `cli_provider`.
- [`providers/opencode.md`](docs/providers/opencode.md) — Describes the `bin/oc-claude` wrapper that routes Koan's Claude CLI invocations through the `ocgo` proxy to run against an OpenCode Go subscription (Kimi, DeepSeek, Qwen, etc.).
- [`providers/openrouter.md`](docs/providers/openrouter.md) — Explains how to run Koan's Claude CLI provider against OpenRouter models via a local `claude-code-router` (CCR) translation server, including setup, model routing, and caveats.
- [`providers/zai.md`](docs/providers/zai.md) — Documents the `bin/zai-claude` wrapper that points the real Claude CLI at Z.ai's Anthropic-compatible endpoint and maps Anthropic model tiers to GLM models.

### Security
- [`security/mission-hooks.md`](docs/security/mission-hooks.md) — Threat model and safe-use guidance for repo-config-driven pre/post shell hooks (`.koan/config.yaml` `pre_hooks`/`post_hooks`): the RCE surface, the default-off operator opt-in gate and per-project override that contain it, and the timeout/isolation/audit defense-in-depth.
- [`security/prompt-guard.md`](docs/security/prompt-guard.md) — Documents `prompt_guard.py`'s input-side defenses against prompt injection in missions and its configuration/complementary defenses (outbox scanner, data fencing, memory scanning).
- [`security/security-review.md`](docs/security/security-review.md) — Documents the automated post-mission security review that scans diffs for dangerous patterns, scores risk, optionally blocks auto-merge, and logs an audit trail.
- [`security/threat-model-agent-disalignment.md`](docs/security/threat-model-agent-disalignment.md) — A threat-model analysis of the blast radius if Koan's autonomous agent becomes disaligned, covering attack surface, exfiltration vectors, MCP per-role exclusions, protections, and recommended mitigations.

### Setup
- [`setup/docker.md`](docs/setup/docker.md) — Covers Docker Compose setup for Koan (pull vs. build from source), workspace project mounts, authentication (Claude/GitHub), volume layout, and troubleshooting common container issues.
- [`setup/env-var-deployment.md`](docs/setup/env-var-deployment.md) — Explains how Koan can run purely from injected environment variables (Railway/Docker/Kubernetes/systemd) without a hand-authored `.env` file, and the precedence rules between env vars and the synthesized `.env`.
- [`setup/launchd.md`](docs/setup/launchd.md) — Documents running Koan as a macOS launchd user service for auto-restart and login-time startup, including setup, logs, SSH agent forwarding, and troubleshooting.
- [`setup/railway.md`](docs/setup/railway.md) — Details deploying Koan as a single hosted container on Railway via `KOAN_DEPLOY=railway`, covering required service variables, the GitHub token bot-identity caveat, dashboard passphrase gating, and re-deploy behavior.
- [`setup/ssh-setup.md`](docs/setup/ssh-setup.md) — Walks through SSH authentication setup for Koan's git operations across macOS direct-run, Linux systemd, and Docker deployment modes, including fallback key generation.
- [`setup/systemd-user.md`](docs/setup/systemd-user.md) — Describes running Koan as a per-user (rootless) systemd service on Linux, covering unit installation, linger for boot persistence, and PATH preservation for CLI providers.

### Users
- [`users/koan-md.md`](docs/users/koan-md.md) — Documents the optional project-root `KOAN.md` file and the `.koan/` directory (a second `.koan/KOAN.md`, per-skill `.koan/skills/<skill>/*.md` hooks, and a structured `.koan/config.yaml` with `review.always_check`): koan-only steering for the autonomous agent, 16k caps, runner `project_path` wiring, and this repo's dogfood quality-gate layout.
- [`users/koan-cli.md`](docs/users/koan-cli.md) — Explains the runtime OpenAPI REST client, secure named profiles, generated commands, generic JSON/query input, output and exit-code contracts, and destructive-request confirmation.
- [`users/model-configuration.md`](docs/users/model-configuration.md) — Explains how to configure which model handles each Koan role (mission, chat, lightweight, fallback, etc.) per provider via `config.yaml`, including resolution order and CLI-provider-per-role routing.
- [`users/onboarding.md`](docs/users/onboarding.md) — Documents the interactive 12-step onboarding wizard that sets up a new Koan instance, its resumability, personality presets, and non-interactive/CI mode.
- [`users/quickstart.md`](docs/users/quickstart.md) — A 5-minute guide to the commands for driving Koan from GitHub PRs/issues, Jira, and messaging apps (Telegram/Slack), with minimal and context-augmented examples for each.
- [`users/skills.md`](docs/users/skills.md) — Complete reference for all Koan slash commands (mission management, code/PR operations, scheduling, status, configuration, and system commands) usable via Telegram, Slack, or GitHub @mentions.
- [`users/user-manual.md`](docs/users/user-manual.md) — A tiered (beginner/intermediate/power-user) walkthrough of everything Kōan can do, from queuing your first mission through parallel sessions, deep exploration, and full configuration.

### Overview
- [`README.md`](docs/README.md) — Top-level router explaining the docs/ tree's purpose, its relationship to specs/ design contracts, and pointers to user, architecture, and directory-map content.
- [`SPEC.md`](docs/SPEC.md) — Normative Open Knowledge Format rules the docs/ and specs/ bundles conform to: frontmatter, index/log files, and conformance requirements.
- [`SCHEMA.md`](docs/SCHEMA.md) — OKF conventions specific to the docs/ bundle: page types, tag taxonomy, and frontmatter requirements.

## Specs — Components

- [`components/agent-loop.md`](specs-components/agent-loop.md) — Design contract for the core mission pipeline (iteration manager, mission executor/runner, quota handling, stagnation monitor) that pulls missions, invokes the CLI provider, and finalizes lifecycle state.
- [`components/bridge.md`](specs-components/bridge.md) — Design contract for the Telegram bridge process that classifies human messages into chat vs. mission, dispatches commands/skills, and flushes the agent's outbox crash-safely.
- [`components/comment-formatting.md`](specs-components/comment-formatting.md) — Design contract for `build_alert()`, the single constructor for GitHub alert callouts, plus the type→situation mapping and the parsimony rule every skill must follow.
- [`components/core.md`](specs-components/core.md) — Design contract for the foundation layer (mission queue contract, config resolution, atomic-write/lock primitives) that every other Kōan component depends on.
- [`components/git-github.md`](specs-components/git-github.md) — Design contract for everything touching git history or the GitHub API: branch/PR creation, sync, webhook/notification handling, and rebase/recreate/CI-fix workflows.
- [`components/issue-tracking.md`](specs-components/issue-tracking.md) — Design contract for the provider-neutral issue-tracker abstraction (GitHub/Jira) that routes fetch/comment/create calls through one service layer.
- [`components/providers.md`](specs-components/providers.md) — Design contract for the CLI provider abstraction that decouples the agent loop from any single AI coding CLI (Claude, Cline, Codex, Copilot, Haze, Grok) behind one `CLIProvider` contract, including the MCP per-role safety boundary.
- [`components/skills.md`](specs-components/skills.md) — Documents the skills system that discovers, routes, and executes `/command` skills (SKILL.md contract, dispatch, MCP access for skill runners, the new-skill checklist, and the eval harness).
- [`components/web.md`](specs-components/web.md) — Documents the Flask dashboard and token-gated REST API, shared `dashboard_service` logic (including the live progress stream contract), OpenAPI drift guard, and surface-parity invariants.

## Specs — Skills

Per `specs/README.md`'s coverage policy: only the ~10 highest-impact skills have a spec today (as templates for the remaining ~80), filled in on-demand as touched. This index reflects only what exists.

- [`skills/ask.md`](specs-skills/ask.md) — Specifies the `/ask` skill, which answers a question about a GitHub PR/issue by fetching context and posting an AI-generated reply as a read-only, non-mutating worker.
- [`skills/brainstorm.md`](specs-skills/brainstorm.md) — Specifies the `/brainstorm` skill, which decomposes a topic into structured, linked sub-issues (GitHub or Jira) under a master tracking issue and is covered by the skill-eval harness.
- [`skills/ci_check.md`](specs-skills/ci_check.md) — Specifies the `/ci_check` skill, which checks a PR's CI status, runs the shared CI-fix loop on failures, and toggles automatic CI-fix dispatch.
- [`skills/fix.md`](specs-skills/fix.md) — Specifies the `/fix` skill, which fixes a tracker issue end-to-end (or batch-queues fixes for a repo) and redirects PR URLs to `/rebase --fix`, with eval coverage on its diagnostic output.
- [`skills/implement.md`](specs-skills/implement.md) — Specifies the `/implement` skill, which queues an end-to-end implementation mission for a tracker issue that results in a draft PR, and is eval-exempt as pure orchestration.
- [`skills/mission.md`](specs-skills/mission.md) — Specifies the `/mission` skill, the base primitive that queues a free-form mission to `missions.md` for later agent-loop execution, also eval-exempt as a non-LLM queue utility.
- [`skills/orphans.md`](specs-skills/orphans.md) — Documents the `/orphans` skill that rebases and opens draft PRs for unmerged, PR-less branches, with commit-derived (non-LLM) PR titles/descriptions and per-branch error isolation.
- [`skills/plan.md`](specs-skills/plan.md) — Documents the `/plan` skill that deep-thinks an idea (or iterates an existing issue) into a structured tracker-issue plan via a critic→regenerate loop, covered by the deterministic eval harness.
- [`skills/rebase.md`](specs-skills/rebase.md) — Documents the `/rebase` skill that rebases a PR onto its current base by default and, with `--fix` (or any trailing context), also addresses review feedback, including its already-solved detection JSON scored by the eval harness.
- [`skills/recreate.md`](specs-skills/recreate.md) — Documents the `/recreate` skill that rebuilds a too-far-diverged PR from scratch on current upstream via a fresh branch and reimplementation, rather than rebasing.
- [`skills/review.md`](specs-skills/review.md) — Documents the `/review` skill that queues a code-review mission on PRs/issues, posting findings as a comment with severity-driven LGTM logic and re-review comment handling, covered by the eval harness.
- [`skills/security_audit.md`](specs-skills/security_audit.md) — Documents the `/security_audit` skill that runs a background SDLC security audit of a project and files up to 5 critical-vulnerability tracker issues via the provider-neutral tracker service.

### Overview
- [`specs/README.md`](../specs/README.md) — The top-level index and conventions doc for `specs/`, explaining the specs-vs-docs distinction, directory layout, naming rules, and the mandatory read-before/update-after spec discipline.
- [`SCHEMA.md`](../specs/SCHEMA.md) — OKF conventions specific to the specs/ bundle: page types, tag taxonomy, frontmatter requirements, and why speckit feature folders are excluded.

## Specs — Active Features

Speckit's ephemeral per-feature planning folders. Status is computed from `tasks.md`'s checkbox ratio, not hand-maintained — recompute on every ingest/CI pass. No frontmatter is injected into these files (see `SCHEMA.md`). Entry point for each is `spec.md`.

- [`001-speckit-native-support/spec.md`](../specs/001-speckit-native-support/spec.md) — **in-progress** (16/36 tasks, `Status: Draft`). Native speckit slash-command orchestration (chat-trigger, issue-URL, @mention, from-branch). Code for setup/foundational plumbing and User Stories 2/3/5 is merged; the MVP chat-trigger orchestration (User Story 1) and CI-review-loop wiring (User Story 4) remain.
- [`002-review-skill-evals/spec.md`](../specs/002-review-skill-evals/spec.md) — **draft** (0/19 tasks, `Status: Draft`). Planning artifact only, not started.
- [`003-core-skill-evals/spec.md`](../specs/003-core-skill-evals/spec.md) — **draft** (0/32 tasks, `Status: Draft`). Planning artifact only, not started.
- [`004-mission-store/spec.md`](../specs/004-mission-store/spec.md) — **implemented S1–S9** (`tasks.md` all ✅, incl. S7 `/list <state>` visibility). Missions migrated to an authoritative SQLite store behind the `MissionStore` port; `missions.md` is a generated read-only export (only the `sqlite` adapter ships in-tree; out-of-tree adapters loadable via `missions.backend`). Supersedes the #2209 mirror; Constitution amended to v2.0.0 (#2296). Durable contract graduated into `specs/components/core.md`. PR #2310.
- [`005-spec-change-governance/spec.md`](../specs/005-spec-change-governance/spec.md) — **in-progress** (8/9 tasks, `Status: Draft`). Treat durable-contract spec changes (specs/components/**, specs/skills/**) as reviewed architectural changes: contract-first, rare, and declared in the PR — enforced by `scripts/spec_change_guard.py` + a blocking CI check. Amends Constitution Principle II (v3.0.0). Origin: customer concern on PR #2052.
- [`010-review-consistency-triage/spec.md`](../specs/010-review-consistency-triage/spec.md) — **shipped** (33/33 tasks, `Status: Draft`). Makes `/review` consistent across re-runs (reuse on unchanged head+base; re-review freeze of first-time non-critical findings on untouched code), sharpens the blocking 🟡 Important bar, labels/demotes pre-existing issues (`[Pre-Existing Issue]`), honors human PR-comment dispositions (dismiss/defer, attributed, injection-guarded), and adds an opt-in comprehensive multi-perspective discovery mode (default off). Durable contract updated in `specs/skills/review.md` (architectural change).
