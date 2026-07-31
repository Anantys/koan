---
type: doc
title: "Kōan User Manual"
description: "A tiered (beginner/intermediate/power-user) walkthrough of everything Kōan can do, from queuing your first mission through parallel sessions, deep exploration, and full configuration."
tags: [users]
created: 2026-05-28
updated: 2026-09-09
---

# Kōan User Manual

**From beginner to power user — everything Kōan can do.**

This manual is organized in three progressive tiers. Start with the basics, then unlock more advanced workflows as you grow comfortable.

> **New here?** Make sure you've completed the [Quick Start](../../README.md#quick-start) or [Full Install Guide](../../INSTALL.md) first. This manual assumes Kōan is already running.
>
> **Just want the commands?** The [Quickstart](quickstart.md) is a 5-minute, copy-paste guide to driving Kōan from GitHub, Jira, and your messaging app.

---

## Table of Contents

- [Beginner — Daily Basics](#beginner--daily-basics)
  - [Your First Mission](#your-first-mission)
  - [Mission Lifecycle](#mission-lifecycle)
  - [Chatting with Kōan](#chatting-with-kōan)
  - [Managing Your Queue](#managing-your-queue)
  - [Checking Progress](#checking-progress)
  - [Branch Isolation & Reviewing Work](#branch-isolation--reviewing-work)
  - [Multi-Project Basics](#multi-project-basics)
- [Intermediate — Productivity Workflows](#intermediate--productivity-workflows)
  - [Code Operations](#code-operations)
  - [PR Management](#pr-management)
  - [Project Maintenance](#project-maintenance)
  - [Scheduling Work](#scheduling-work)
  - [Ideas Backlog](#ideas-backlog)
  - [Reflection & Journal](#reflection--journal)
  - [Email Digests](#email-digests)
  - [Statistics](#statistics)
  - [Understanding Quota Modes](#understanding-quota-modes)
  - [Exploration Mode](#exploration-mode)
  - [Workflow Example: Feature from Idea to PR](#workflow-example-feature-from-idea-to-pr)
- [Power User — Advanced Configuration](#power-user--advanced-configuration)
  - [Parallel Sessions](#parallel-sessions)
  - [Deep Exploration](#deep-exploration)
  - [Configuration Deep-Dive](#configuration-deep-dive)
  - [Per-Project Overrides](#per-project-overrides)
  - [Custom Skills](#custom-skills)
  - [GitHub @mention Integration](#github-mention-integration)
  - [CLI Providers](#cli-providers)
  - [Language Preference](#language-preference)
  - [System Management](#system-management)
  - [Memory System](#memory-system)
  - [Personality Customization](#personality-customization)
  - [Auto-Update](#auto-update)
  - [Adding New Projects](#adding-new-projects)
  - [Performance Profiling](#performance-profiling)
  - [Incident Triage](#incident-triage)
  - [Web Dashboard](#web-dashboard)
  - [Deployment](#deployment)
- [Quick Reference](#quick-reference)

---

## Beginner — Daily Basics

Everything you need to use Kōan day-to-day. If you've just installed Kōan, start here.

### Your First Mission

Send a message to Kōan via Telegram (or Slack). If it looks like a task, Kōan automatically queues it as a mission:

> *"Audit the auth module for security issues"*

For explicit control, use the `/mission` command:

```
/mission Refactor the payment service to use async/await
```

**`/mission`** — Queue a new mission for the agent to work on.

- **Usage:** `/mission <description>`
- **Options:**
  - `/mission --now <description>` — Insert at the top of the queue (next to run)
  - `/mission [project:webapp] <description>` — Target a specific project

<details>
<summary>Use cases</summary>

- `/mission Add input validation to the signup form` — Queue a feature task
- `/mission --now Fix the broken CI pipeline` — Urgent fix, skip the queue
- `/mission [project:api] Write integration tests for the /users endpoint` — Target a specific project
</details>

### Mission Lifecycle

Every mission flows through a simple lifecycle:

```
Pending  →  In Progress  →  Done ✓
                          →  Failed ✗
```

1. **Pending** — Queued and waiting. Kōan picks missions from the top of the queue.
2. **In Progress** — Kōan is actively working on it via the configured CLI provider.
3. **Done** — Completed successfully. Code is in a `koan/*` branch, often with a draft PR.
4. **Failed** — Something went wrong. Kōan logs the reason and moves on.

By default, Kōan processes one mission at a time. When idle, it picks the next pending mission automatically. For concurrent execution, see [Parallel Sessions](#parallel-sessions).

### Chatting with Kōan

Just send a regular message — Kōan classifies it automatically. Short conversational messages get instant replies (chat mode). Task-like messages get queued as missions.

**Bare skill shortcut:** if the first word of a plain message is the name (or alias) of a core skill, Kōan treats the whole message as that slash command — `time` runs `/time`, `review <url>` runs `/review <url>`. Only core skills trigger this; custom/instance skills do not. If a common word collides with a skill name and you meant to chat, prefix with `/chat`.

**Bang prefix:** `!command` works everywhere `/command` does — `!help`, `!status`, `!review <url>`. Useful if you came from Slack, where `!` avoids clashing with Slack's own slash commands. A bang before a non-letter (`!!`, `!42`) is plain text. On Telegram there's no autocomplete for `!`, and in a group with Privacy Mode ON Telegram never delivers `!` messages to the bot — use `/` there.

If Kōan misclassifies your message, use `/chat` to force chat mode:

**`/chat`** — Force a message to be treated as chat, not a mission.

- **Usage:** `/chat <message>`

<details>
<summary>Use cases</summary>

- `/chat What do you think about using Redis for caching?` — Ask for an opinion without creating a mission
- `/chat How's your day going?` — Just talk
</details>

### Managing Your Queue

**`/list [state]`** — See missions. With no argument, lists pending and in-progress. Add a state to see more: `/list done`, `/list failed`, or `/list all` (handy now that `missions.md` is a generated read-only export — the done/failed history is in the store).

- **Aliases:** `/queue`, `/ls`

<details>
<summary>Use cases</summary>

- `/list` — Check what's queued up before adding more work
- `/ls` — Quick glance at the queue
</details>

**`/cancel`** — Remove a pending mission from the queue.

- **Usage:** `/cancel <number>` or `/cancel <keyword>`
- **Aliases:** `/remove`, `/clear`

<details>
<summary>Use cases</summary>

- `/cancel 3` — Cancel the 3rd pending mission
- `/cancel auth` — Cancel the mission matching "auth"
</details>

**`/abort`** — Abort the current in-progress mission and move to the next one.

- **Usage:** `/abort`
- The running Claude subprocess is killed, the mission is moved to Failed, and the agent loop picks the next pending item.

> **Bridge unresponsive?** `/cancel`, `/abort`, and `/list` all go through the Telegram bridge, which can stop answering when the loop is badly stuck. From a terminal on the host you can inspect and edit the queue directly (no bridge needed): `make missions` to list, `make mission-rm sel=i1` to abort the stuck one, then `make stop && make start`. See [Mission-queue break-glass CLI](../operations/mission-cli.md).

**`/priority`** — Move a pending mission to a different position in the queue.

- **Usage:** `/priority <n>` (move to top) or `/priority <n> <position>`

<details>
<summary>Use cases</summary>

- `/priority 5` — Move mission #5 to the top of the queue
- `/priority 3 2` — Move mission #3 to position #2
</details>

### Checking Progress

**`/status`** — Get a quick overview of Kōan's state: what's running, what's queued, loop health.

- **Aliases:** `/st`
- **Related:** `/ping` (is the loop alive?), `/usage` (detailed quota), `/metrics` (success rates), `/version` or `/v` (version only)

<details>
<summary>Use cases</summary>

- `/status` — "Is Kōan working? What's it doing?"
- `/ping` — Quick health check
- `/metrics` — See mission success/failure rates
</details>

**`/brief`** — Daily digest combining pending missions, recent completions, quota health, and journal highlights in one message.

- **Aliases:** `/digest`
- **Flags:** `--schedule` seeds the daily auto-delivery chain (via event scheduler)

<details>
<summary>Use cases</summary>

- `/brief` — Quick morning overview: what happened, what's queued, how's quota
- `/brief --schedule` — Start daily auto-delivery at 07:00 (self-rescheduling)
- Copy `instance.example/events/daily-brief.json` to `instance/events/` and update `run_at` for custom timing
</details>

**`/live`** — See real-time progress from the currently running mission.

- **Aliases:** `/progress`

<details>
<summary>Use cases</summary>

- `/live` — Check what Kōan is doing right now during a long mission
</details>

**`/logs [run|awake|all]`** — Show the last 20 lines from log files, formatted in code blocks.

- **Default:** Shows only `run.log`. Use `awake` for bridge logs, `all` for both.

<details>
<summary>Use cases</summary>

- `/logs` — Quick check of recent agent output (run.log only)
- `/logs awake` — Check bridge/Telegram polling output
- `/logs all` — See both run and awake logs
</details>

**`/quota [remaining_%]`** — Check remaining API quota (live, no cache), or override the internal estimate.

- **Aliases:** `/q`

<details>
<summary>Use cases</summary>

- `/quota` — See how much API budget is left before adding heavy missions, plus the rolling burn rate (%/h) and estimated time to exhaustion
- `/quota 32` — Tell Kōan it has 32% remaining (fixes drift when internal estimate is wrong)
- If Kōan is paused due to quota but the API is actually available, `/quota 50` will correct the estimate and clear the pause
- When the burn rate predicts session exhaustion in less than 30 min, the autonomous mode is automatically soft-throttled one tier (deep→implement→review) — burn rate never forces a pause (`wait`); absolute budget thresholds still own that. Estimates need ≥5 samples over ≥15 minutes of wall clock so a short burst of heavy missions does not look like a multi-hundred-%/h sustained rate. Only samples from the last 5 hours (the session-quota window) count toward the estimate, so a state file left over from an earlier session cannot shape the current one — older samples stay on disk and age out of the buffer naturally, and a buffer holding nothing fresh yields no estimate rather than a false alarm. A Telegram alert fires once when projected exhaustion is under 60 min and the next quota reset is still more than 2 h away. Session reset (`/resume` after a quota pause) also clears the burn-rate sample buffer so pre-pause costs cannot re-trigger false alarms.
</details>

**`/check_notifications`** — Force an immediate check of GitHub and Jira notifications, bypassing the exponential backoff timer.

- **Aliases:** `/read`

<details>
<summary>Use cases</summary>

- `/read` — When the queue is empty and you know there are pending notifications
- `/check_notifications` — After posting a GitHub comment that should trigger a mission
</details>

**`/inbox`** — Force a GitHub notification check and show how many GitHub-originated missions are queued. Works while paused — notifications are fetched inline and missions queue for pickup after resume.

GitHub polling also scans configured repositories for open PRs that still
request the bot as reviewer. If GitHub does not expose a review request through
the notifications API, Kōan still queues `/review <pr-url>` from the PR's
requested-reviewer state. To limit API usage, each repo is rescanned at most
once per `github.review_scan_interval_minutes` (default 15; set `0` to scan
every cycle).

<details>
<summary>Use cases</summary>

- `/inbox` — Quick check: "do I have GitHub mail?" — triggers a fetch and shows pending mission count
- `/inbox` while paused — Fetch notifications even during quota pause; queued missions run after resume
</details>

**`/verbose`** / **`/silent`** — Toggle real-time progress updates. When verbose is on, Kōan sends progress messages as it works.

<details>
<summary>Use cases</summary>

- `/verbose` — Turn on updates when you want to follow along
- `/silent` — Turn off updates when you're busy (default)
</details>

**`/messaging_level [debug|normal]`** (alias `/msglevel`) — Show or set the bridge verbosity tier. `normal` (the default) is quiet: failures, command replies, and one-line PR results still come through, but per-mention queue lines and mission-start chatter are collapsed or suppressed. `debug` restores the full lifecycle firehose. Distinct from `/verbose` (which toggles in-mission progress). Every suppressed message is still written to the logs. See [Quieter bridge](#quieter-bridge) and `docs/messaging/messaging-level.md`.

#### Quieter bridge

By default Kōan's bridge runs in `normal` mode — quiet and operator-focused. Set `messaging.level: debug` in `config.yaml`, run `/messaging_level debug`, or export `KOAN_MESSAGING_LEVEL=debug` to restore the legacy chatty behavior. Precedence: env var > `/messaging_level` runtime override > `config.yaml` > `normal`.

With `messaging.level=normal`, each skill mission emits a single status line — the PR/issue URL on success (e.g. `✅ Reviewed https://github.com/o/r/pull/2098`) or a short context string on failure — instead of the step-by-step play-by-play (`Reviewing PR…`, `Analyzing code changes…`, `Posting review…`). Switch to `debug` (`/messaging_level debug`) to see every intermediate progress line again; suppressed progress is always written to the log regardless.

### Branch Isolation & Reviewing Work

Kōan **never commits to `main`**. All work happens in `koan/*` branches (the prefix is configurable). After completing a mission, Kōan typically:

1. Creates a branch like `koan/refactor-payment-service`
2. Commits changes with clear messages
3. Pushes the branch and creates a **draft PR**

Draft PR bodies include a Kōan footer with best-effort provider/model
attribution, submitted HEAD, and runtime. This makes it clear which CLI
provider and model produced the implementation.

Your workflow:

```bash
# See what Kōan produced
git log koan/refactor-payment-service

# Review the PR on GitHub
# Merge when you're satisfied — or ask Kōan to iterate
```

**The agent proposes. The human decides.** — You always have the final say.

### Multi-Project Basics

Kōan can manage multiple projects simultaneously. It rotates between them based on queue priority and quota.

**`/projects`** — List all configured projects.

- **Aliases:** `/proj`
- Shows each project's configured issue tracker when set.
- A repository registered in both `projects.yaml` and the auto-discovered
  `instance/workspace/` directory — even under names that differ only by a
  dash or underscore (e.g. `my-repo` vs `myrepo`) — is listed **once**, under
  its `projects.yaml` (canonical) name. `projects.yaml` settings take
  precedence over the auto-discovered defaults.

<details>
<summary>Use cases</summary>

- `/projects` — See which repos Kōan is managing
</details>

**`/tracker`** — Show or configure per-project issue tracker routing.

- **Usage:** `/tracker`
- **Set GitHub:** `/tracker set <project> github [repo:owner/repo] [branch:main]`
- **Set Jira:** `/tracker set <project> jira key:PROJ [type:Task] [branch:11.126]`

This controls where `/plan` creates new tracker issues and how Jira-origin `/fix` and `/implement` resolve the target repo and branch.

Jira project keys are registered per project in `projects.yaml`. The project path can be configured there with `path:` or discovered from `KOAN_ROOT/workspace/<project-name>`. The old `instance/config.yaml jira.projects` mapping is ignored; `/config_check` reports it as a migration warning.

**`/alias`** — Create short aliases for project names. Once set, typing `/<shortcut> <text>` queues a mission tagged with the aliased project.

- **Usage:** `/alias <project> <shortcut>` — create an alias. `/alias` — list all aliases.
- **Examples:** `/alias Template2 tt`, then `/tt fix the build` queues a mission for Template2.

**`/unalias`** — Remove a project alias.

- **Usage:** `/unalias <shortcut>`

**`/focus`** — Lock Kōan to a single project. While focused, it only processes missions for that project and skips exploration/reflection.

- **Usage:** `/focus [duration]` (default: 5 hours)
- **Examples:** `/focus`, `/focus 3h`, `/focus 2h30m`

**`/unfocus`** — Exit focus mode, resume normal multi-project rotation.

<details>
<summary>Use cases</summary>

- `/focus` — "I need all attention on the webapp for the next few hours"
- `/focus 1h` — Short focused sprint
- `/unfocus` — "OK, back to normal"
</details>

**`/passive`** — Enter passive (read-only) mode. The agent loop keeps running (heartbeat, GitHub notification polling, Telegram commands) but never executes missions or autonomous work. Missions accumulate as Pending.

- **Usage:** `/passive [duration]` — no duration = indefinite
- **Examples:** `/passive`, `/passive 4h`, `/passive 2h30m`

**`/active`** — Exit passive mode and resume normal execution. Queued missions drain naturally.

<details>
<summary>Use cases</summary>

- `/passive` — "I'm at the desk, don't touch anything"
- `/passive 4h` — "Hands off for the next 4 hours"
- `/active` — "I'm done, you can work again"
</details>

### Permanent Focus Mode

Focus mode can be made permanent via config, turning Kōan into a pure mission executor. When enabled, the agent only runs missions you explicitly queue — it never picks up GitHub issues autonomously, never runs contemplative reflection, and never enters DEEP mode. The loop keeps polling Telegram, GitHub notifications, and recurring schedules, so it still wakes up the moment you queue something.

This extends the `/focus` Telegram command (which is time-bounded) into a permanent config-level switch.

- **Enable globally in `instance/config.yaml`:**
  ```yaml
  focus: true
  ```
- **Or via environment variable:** `KOAN_FOCUS=1` (takes precedence over `config.yaml`).
- **Per-project in `projects.yaml`:**
  ```yaml
  defaults:
    focus: true          # All projects focused by default
  projects:
    myapp:
      focus: false       # Override: allow autonomous work on myapp
  ```
- **Disable:** set back to `false`, or `KOAN_FOCUS=0`.

What continues to run under focus mode:

- Missions queued via `/mission`, GitHub `@mention` commands, and recurring schedules.
- Heartbeat, auto-update, Telegram polling, GitHub notification polling, CI queue drain.

What is disabled:

- DEEP mode (capped at `implement`).
- Contemplative sessions (random reflection rolls are skipped).
- Autonomous exploration (the loop idles with wake-on-mission when no mission is pending).
- The agent prompt's `GitHub Issue Selection` section is replaced with an explicit "do not pick up issues" instruction.

**How it differs from `/passive`:** passive mode blocks all execution (missions sit as Pending until you `/active`). Focus mode keeps the executor running for any mission you queue — it only gates *autonomous work selection*.

**When to use:**

- You want Kōan to act strictly on demand, no surprises on the PR list.
- You're handing off mission dispatch to another system (CI, a team workflow) and want Kōan to be a quiet executor.
- Multi-bot setups where only one instance should pick up issues autonomously.
- Per-project: focus some repos while allowing exploration on others.

---

## Intermediate — Productivity Workflows

These features turn Kōan from a task runner into a full development workflow partner.

### Code Operations

**`/brainstorm`** — Decompose a broad topic into 3-8 high-leverage sub-issues grouped under a master tracking issue, on whichever tracker the project uses (GitHub Issues by default, or Jira when the project is Jira-backed in `projects.yaml`).

The decomposer runs as a senior-engineer-style ideation pass: it explores the codebase (if provided) or external source, hunts for compounding improvements, and refuses to pad with generic refactors. Every sub-issue body follows this template:

```markdown
## Why This Matters
<leverage rationale — why this is unusual or high-leverage>

## Approach
<concrete implementation strategy, grounded in real files and patterns>

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Risks & Caveats
<hidden complexity, operational risk, maintenance burden>

## Scores
- Impact:          ████████░░ 8/10
- Difficulty:      ██████░░░░ 6/10
- Short-Term ROI:  ███████░░░ 7/10
- Long-Term Value: █████████░ 9/10

## Priority
Immediate | Prototype First | Research Further | Skip

## Dependencies
<SUB-N references to other sub-issues, or "None">
```

The master tracking issue then synthesizes the set with three optional sections:

- **Top Ranked** — sub-issues ordered by ROI / feasibility / strategic value, each with a one-line rationale.
- **Fast Wins** — bucketed by horizon: `< 1 day`, `< 1 week`, `< 1 month`.
- **Overall Assessment** — short critical verdict on whether the initiative is worth pursuing and what to prioritize.

- **Usage:** `/brainstorm <topic>`, `/brainstorm <project> <topic>`, `/brainstorm <topic> --tag <label>`
- **GitHub @mention:** `@koan-bot /brainstorm <topic>` on an issue
- **Jira-backed projects:** sub-issues and the master are created in Jira with rich (ADF) bodies; `SUB-N` cross-references resolve to real Jira keys and the master is natively linked to each sub-issue via "Linked issues". See [Jira integration → Brainstorm on Jira](../messaging/jira-integration.md#brainstorm-on-jira). (`--tag` labels apply to GitHub only.)

<details>
<summary>Use cases</summary>

- `/brainstorm Improve caching strategy for API responses` — Creates 3-8 sub-issues + master issue
- `/brainstorm koan Add observability and monitoring` — Target a specific project
- `/brainstorm Refactor auth module --tag auth-refactor` — With explicit tag for grouping
</details>

**`/plan`** — Deep-think an idea and produce a structured, task-level implementation plan as a tracker issue.

For an existing Jira issue, Koan keeps one **current plan** comment, signed off with a `Koan current plan (rev …)` footer. A plan too big for one Jira comment becomes consecutive `Part N of M` comments linked with previous/next jump links — Jira's public API has no threaded replies — and shrinking a later plan retires the leftover parts. `/implement` reassembles such a plan before it starts work, joining the parts of the newest revision in numeric order; if some parts are missing it uses the ones available. It stages the generated plan locally, retries publishing three times, and reports success only after Jira hands that exact revision back on a read-back — so a write Jira accepted but never showed is reported as a failure rather than a ✅. If Jira remains unavailable the mission fails honestly, and the staged plan is retried on the next run without spending another model call to regenerate it; after three failed runs Koan drops the stage so the next `/plan` starts fresh.

Plans include a **File Map** (table of every file to create/modify/test), **checkbox steps** within each phase (write test → implement → verify → commit), and **actual code blocks** in steps that change code. Each code block is wrapped in a collapsible `<details>` block so the plan stays scannable — readers see step descriptions first and expand the code only when needed. A built-in self-review pass checks spec coverage, scans for placeholders, and verifies name consistency across phases before output. Multi-subsystem ideas trigger a scope check suggesting separate plans per subsystem. Before posting, an advisory **assumptions audit** pressure-tests the plan's hidden assumptions and folds any unverified-critical ones into the plan's *Open Questions* section — so you can resolve them on the issue before `/implement` (which receives the same findings as "verify before coding" context; the audit never blocks either skill — toggle with `plan_review.assumptions_check`).

- **Usage:** `/plan [--iterations N] <idea>`, `/plan <project> <idea>`, `/plan <issue-url>` (iterate on existing)
- **GitHub @mention:** `@koan-bot /plan <idea>` on an issue
- **Option:** `--iterations N` (1-5, default 1) — Run N rounds of critique+refine. A critic identifies gaps and contradictions after each generation, then the plan is regenerated with that feedback. Only the final iteration is posted. Cost scales linearly (~5× tokens at `--iterations 3`).
- **MCP:** By default `plan` is in `mcp_roles`, so project MCP servers (Jira, docs, etc.) are loaded during plan generation. See [Claude provider docs](../providers/claude.md#per-role-mcp-access-mcp_roles). Other roles (`chat`, `github_reply`) stay opt-in only.

<details>
<summary>Use cases</summary>

- `/plan Add WebSocket support for real-time notifications` — Get a phased plan before writing any code
- `/plan --iterations 3 Add WebSocket support` — Generate a plan with 3 rounds of critic-driven refinement
- `/plan https://github.com/org/repo/issues/42` — Iterate on an existing issue's plan
- `/plan https://myorg.atlassian.net/browse/PROJ-123` — Iterate on a Jira issue's plan
- `/plan https://myorg.atlassian.net/browse/PROJ-123 branch:main` — Iterate with an explicit base-branch hint
- `/plan webapp Add rate limiting to public API endpoints` — Target a specific project
</details>

For URL-based `/plan`, `/deepplan`, `/implement`, and `/fix`, Kōan resolves the project
from the issue URL and the known project registry. Known projects include both
`projects.yaml` entries and repositories discovered under `KOAN_ROOT/workspace/`,
so an explicit project prefix is not required when the URL maps to one of
those projects.

For URL-based `/plan`, `/deepplan`, `/implement`, and `/fix`, you can append
`branch:<name>` to override the base branch for that mission (for `/plan` and
`/deepplan`, this is passed as a planning hint in the generated context).

**`/deepplan`** — Spec-first design with Socratic exploration of 2-3 approaches before planning. For complex missions where design matters more than speed.

- **Usage:** `/deepplan <idea>`, `/deepplan <project> <idea>`, `/deepplan <issue-url>`
- **Aliases:** `/deeplan`
- **GitHub @mention:** `@koan-bot /deepplan <idea>` on an issue

The workflow: (1) explores your codebase and surfaces 2-3 distinct design approaches with trade-offs, (2) runs a spec review loop (up to 5 iterations) to ensure the spec is concrete and complete, (3) posts the approved spec to the configured issue tracker, (4) queues a `/plan <issue-url>` mission for your review and approval.

When given an issue URL, the issue title, body, and all comments are fetched to provide full context for the design exploration.

Use this before `/plan` when the idea is architecturally complex, when you want to explore alternatives before committing, or when design mistakes would be expensive to fix later.

<details>
<summary>Use cases</summary>

- `/deepplan Refactor the auth middleware to support OAuth2` — Explore design approaches before writing any code
- `/deepplan koan Add multi-tenant project isolation` — Target a specific project with spec-first design
- `/deepplan https://github.com/org/repo/issues/42` — Deep plan from an existing GitHub issue with full context
- `/deepplan https://myorg.atlassian.net/browse/PROJ-123` — Deep plan from an existing Jira issue
- `/deepplan Redesign the mission queue for concurrent execution` — Surface trade-offs for a complex architectural change
</details>

**`/implement`** — Queue an implementation mission for a GitHub or Jira issue. Never bails on ambiguity — resolves blockers with the simplest viable solution and retries once before surfacing a problem.

For Jira plans split into `Part N of M` comments, `/implement` selects the newest verified plan generation and joins its available parts in numeric order before reviewing or implementing it.

- **Usage:** `/implement <issue-url> [additional context]`
- **Aliases:** `/impl`
- **GitHub @mention:** `@koan-bot /implement` on an issue

<details>
<summary>Use cases</summary>

- `/implement https://github.com/org/repo/issues/42` — Implement what the issue describes
- `/implement https://github.com/org/repo/issues/42 Focus on the backend only` — Add guidance
- `/implement https://myorg.atlassian.net/browse/PROJ-123 phase 1 only` — Implement a Jira-backed plan and post the PR link back to Jira
</details>

> **Blocker handling:** When the plan is ambiguous or under-specified, `/implement` chooses the simplest interpretation consistent with existing code patterns, documents the assumption in a commit message, and delivers a draft PR. If the first pass produces no committed changes, an escalated retry pass runs automatically. Only a genuine hard impossibility (no repo access, no actionable plan) results in a soft failure notification.  

After a draft PR is created, `/implement` runs the private review gate when it is enabled (opt-in; disabled by default during the testing phase — set `private_review_gate.enabled: true`). The gate reuses the `/review` analysis path without posting review comments, fixes Blocking/Important findings on the same branch, pushes those fixes, and repeats up to the configured round limit.

**`/fix`** — Fix a GitHub or Jira issue end-to-end: diagnose, understand, plan, test, implement, and submit a PR.

- **Usage:** `/fix <issue-url> [additional context]`
- **GitHub @mention:** `@koan-bot /fix` on an issue
- **Flags:** `--skip-diagnose` — Skip the pre-fix diagnostic step (useful for trivial issues where the root cause is obvious)

Before attempting a fix, `/fix` runs a lightweight read-only diagnostic phase using a smaller model to form a hypothesis about the root cause. The fix session receives this analysis as context. If diagnostic confidence is LOW, a Telegram warning is sent but the fix still proceeds.

After a draft PR is created, `/fix` also runs the private review gate when it is enabled (opt-in; disabled by default during the testing phase). Findings and fix attempts stay backend-only: no review comment, verdict, or issue comment is posted by the gate.

If you point `/fix` at a **PR URL** instead of an issue, it redirects to `/rebase --fix` — addressing review concerns on an existing PR is exactly what `/rebase --fix` does. GitHub `@koan-bot fix` on a PR is canonicalized to that same command before it is queued; bare `@koan-bot rebase` remains rebase-only. It injects `--fix` (because a bare `/rebase` now only rebases), and the `--now` flag and any trailing context are preserved as explicit feedback focus.

<details>
<summary>Use cases</summary>

- `/fix https://github.com/org/repo/issues/99` — Full bug-fix pipeline (with automatic diagnostic)
- `/fix https://github.com/org/repo/issues/99 Regression from v2.3` — Provide extra context
- `/fix https://github.com/org/repo/issues/99 --skip-diagnose` — Skip diagnostic for a trivial fix
- `/fix https://myorg.atlassian.net/browse/PROJ-123 branch:main` — Fix a Jira ticket using a one-off target branch
- `/fix https://github.com/org/repo/pull/42 address the security concern` — PR URL redirects to `/rebase --fix`, preserving the trailing context
</details>

**`/debug`** — Structured 4-step debugging when a previous fix attempt failed.

- **Usage:** `/debug <issue-url> [additional context]`
- **Aliases:** `/dbg`
- **GitHub @mention:** `@koan-bot /debug` on an issue
- **Auto-escalation:** Enable `debug_escalation.on_fix_failure: true` in `config.yaml` to automatically queue `/debug` when a `/fix` mission fails.

The debug loop enforces four steps:
1. **Reproduce** — write a minimal failing test before touching production code
2. **Hypothesize** — form and document a specific root cause theory
3. **Minimal fix** — apply the narrowest change that addresses the hypothesis
4. **Verify** — run the reproduction test plus the full suite

<details>
<summary>Use cases</summary>

- `/debug https://github.com/org/repo/issues/42` — Debug a failed fix
- `/debug https://github.com/org/repo/issues/42 check the auth middleware path` — Provide extra context
</details>

**`/review`** — Queue a code review for a pull request or issue.

- **Usage:** `/review <github-pr-or-issue-url> [additional-pr-or-issue-url ...] [--architecture] [--errors] [--comments] [--bot-comments] [--plan-url <issue-url>]`
- **Aliases:** `/rv`, `/rereview`, `/re_review`
- **GitHub @mention:** `@koan-bot /review` on a PR
- **Multiple URLs:** Queues one independent review mission per PR/issue URL. Shared flags such as `--errors` and `--plan-url <issue-url>` are applied to each queued review.
- **Flags:**
  - `--architecture` — Architecture-focused review (SOLID principles, layering, coupling, abstraction boundaries)
  - `--errors` — Run an additional **silent-failure-hunter** pass that scans for swallowed exceptions, silent null returns, unhandled promises, and other silent error paths. Also auto-triggered when the diff contains error-handling patterns (`try/except`, `catch`, etc.)
  - `--comments` — Comment quality review (factual accuracy, completeness, stale TODOs, misleading language)
  - `--bot-comments` — Triage inline comments from code-review bots (CodeRabbit, Copilot Review, Sourcery) and post replies to actionable findings
- **Tool boundary:** A review is advisory — it reads, and it never commits, pushes, or merges. The tool set is a **positive allowlist** (`Read`, `Glob`, `Grep`, `Bash`): anything not named — `Write`, `Edit`, `MultiEdit`, and `Task`/`Skill`, which could otherwise spawn a write-capable subagent — is withheld by construction rather than by being enumerated as forbidden. MCP servers are not inherited either, so a review's reach does not depend on what you happen to have installed. On Codex the same posture comes from an OS-level read-only sandbox. `skip_permissions: true` is deliberately ignored for reviews. The boundary **fails closed**: on any provider Kōan cannot *prove* enforces it (`cline`, `haze`, `copilot`, `grok`, `gemini`), the review is refused with a configuration error rather than run unrestricted — pin `cli.review_mode` to `claude`, `codex`, or `ollama-launch`. Those providers remain available for every other role.
- **The review shell:** reviews can run read-only inspection commands (`git log`, `git blame`, `git grep`, `rg`, `sed -n '10,40p'`, `find`, …) so a finding can be checked against history and real callers instead of guessed from the diff. Every command is adjudicated by a hook before it runs: one simple command per call, no pipeline (a `|` **inside a quoted regex pattern**, e.g. `git grep -n 'a|b'`, is fine), no chaining, no command substitution, and no writer, network client, or interpreter. Redirection is rejected too, except `2>/dev/null` and `2>&1` — discarding or merging stderr writes nothing. Paths are confined to the pinned worktree by resolution, so an absolute path inside it works just like a relative one. A rejected command is refused, never silently rewritten.
- **What the review reads:** each review runs in a fresh, disposable checkout of the pull request's head commit — not your working copy, and not the base branch. If that checkout cannot be prepared the review **fails** rather than falling back, because a review of the wrong code that reports its findings as fact is worse than no review. Expect `Could not prepare a clean checkout of PR #N` when git cannot reach the remote. On `claude`/`ollama-launch` the reviewed repository's own `.claude/` settings and `CLAUDE.md` are **not** loaded — a pull request must not be able to configure the agent that judges it — though its conventions are still passed to the model as untrusted text. (On `codex` that suppression is not yet wired, so it still reads the reviewed `AGENTS.md`; see `docs/providers/codex.md`.)
- **Output:** Findings are grouped into severity buckets (🔴 Blocking /
  🟡 Important / 🟢 Suggestions), each folded into a collapsible section.
  Finding IDs increment across all buckets and continue into Silent Failure
  Analysis, so an ID uniquely identifies one finding within the review.
  Every finding's location is shown as a clickable link pinned to the reviewed
  commit. A valid second-pass reflection filters the final list authoritatively:
  rejected findings are not restored by their original checklist references or
  verdict, and failed checklist entries supported only by rejected findings are
  removed. The request-changes verdict and its red/yellow alert are derived from
  the retained findings. Malformed reflection metadata fails open to the complete
  primary review.
- **Project memory:** Reviews automatically inject the project's filtered learnings plus human-curated `context.md`/`priorities.md`, ranked against the PR's title, body, and diff via the SQLite FTS5 memory index. Set `review_memory.enabled: true` in `config.yaml` to *also* include recent typed project memory (decisions, observations) for extra reviewer context. Both apply to `/review` and the backend private review gate.
- **Structural quality:** The main review passes (default, plan-aware, and `--architecture`) also judge whether the change leaves the surrounding code simpler or messier — ad-hoc branches bolted onto unrelated flows, feature logic in a shared module, a bespoke helper duplicating a canonical one, or a file pushed past a healthy size (the limit your repo's convention docs state, else ~1000 lines). Findings name the structural remedy and stay 🟢 Suggestions unless concrete harm is named, so they never block a merge on style alone.
- **Prior review context:** On a re-review, the bot's own most recent structured review is surfaced in a dedicated, head-preserving prompt slot so the new review builds on it (confirming whether prior findings are resolved) instead of losing it to the recency-truncated conversation thread. That prior review comment is also collapsed to a short "superseded" pointer so it doesn't echo or crowd out human feedback — set `review_history.preserve_previous: true` to keep it intact instead. Tune the prompt slot via `review_context` in `config.yaml` (`include_bot_feedback`, `prior_review_max_chars`).

<details>
<summary>Use cases</summary>

- `/review https://github.com/org/repo/pull/55` — Get a thorough code review
- `/rv https://github.com/org/repo/pull/55` — Same thing, shorter
- `/review https://github.com/org/repo/pull/55 --architecture` — Architecture-focused review
- `/review https://github.com/org/repo/pull/55 --errors` — Include silent-failure-hunter analysis
- `/review https://github.com/org/repo/pull/55 --comments` — Comment quality review
- `/review https://github.com/org/repo/pull/55 --bot-comments` — Triage and reply to bot review comments
- `/review https://github.com/org/repo/pull/55 --architecture --errors` — Both passes
- `/review https://github.com/org/repo/pull/55 https://github.com/org/repo/pull/56 --errors` — Queue separate error-focused reviews for both PRs
</details>

**`/ultrareview`** — Queue the most thorough review Kōan can run for a PR.

- **Usage:** `/ultrareview [--now] <github-pr-url> [context]`
- **Aliases:** `/urv`, `/ultra_review`
- **GitHub @mention:** `@koan-bot /ultrareview` on a PR
- **What it does:** Combines the architecture-focused main pass with the silent-failure-hunter pass in a single review comment — equivalent to `/review --architecture --errors`, exposed as one switch. Use it on high-stakes PRs where a standard pass isn't enough.

<details>
<summary>Use cases</summary>

- `/ultrareview https://github.com/org/repo/pull/55` — Deep architecture + silent-failure review
- `/urv https://github.com/org/repo/pull/55` — Same thing, shorter
- `@koan-bot /ultrareview` — Trigger an ultra review from a PR comment
</details>

**`/explain`** — Explain a PR's intent and changes in plain, simple language.

- **Usage:** `/explain <github-pr-url>`
- **Aliases:** `/xp`
- **GitHub @mention:** `@koan-bot /explain` on a PR

Produces a pedagogical walkthrough of the PR: what problem it solves (with examples), how the fix works step-by-step, the data flow after the change, and whether a simpler approach could have worked.

<details>
<summary>Use cases</summary>

- `/explain https://github.com/org/repo/pull/55` — Get a plain-language explanation
- `/xp https://github.com/org/repo/pull/55` — Same thing, shorter
</details>

**`/refactor`** — Queue a targeted refactoring mission.

- **Usage:** `/refactor <github-url-or-path>`
- **Aliases:** `/rf`
- **GitHub @mention:** `@koan-bot /refactor` on a PR or issue

<details>
<summary>Use cases</summary>

- `/refactor https://github.com/org/repo/pull/60` — Refactor code in a PR
- `/rf https://github.com/org/repo/issues/70` — Refactor based on an issue description
</details>

### PR Management

**`/ask`** — Ask a question about a GitHub PR or issue and get an AI-generated reply posted directly to the thread.

- **Usage:** `/ask <github-comment-url>`
- **Aliases:** `/question` (also a bare-keyword trigger — `question <url>` runs `/ask`)
- **GitHub @mention:** `@koan-bot ask <your question>` on any PR or issue

<details>
<summary>Use cases</summary>

- `@koan-bot ask why does this test fail?` — Kōan investigates the thread context and replies on GitHub
- `@koan-bot ask what is the purpose of this PR?` — Get a structured explanation with context summary
- `/ask https://github.com/org/repo/issues/42#issuecomment-123456` — Reply to a specific comment
</details>

**`/rebase`** — Rebase a PR onto its base branch.

- **Usage:** `/rebase [--fix] <pr-url> [focus area]`
- **Aliases:** `/rb`
- **GitHub @mention:** `@koan-bot /rebase` on a PR

**By default `/rebase` only rebases** the PR branch onto its base (resolving
conflicts). To *also* read the PR's review comments and apply the requested
changes, add **`--fix`**. `--fix` is **implied by any trailing text after the
URL**, so `/rebase <pr-url> address the security concern` (a focus area) and
`/rebase <pr-url> critical` (a severity keyword) both address feedback — only a
bare `/rebase <pr-url>` skips the feedback leg. Trailing text is threaded into
the mission as extra focus context. A `/fix` invoked on a PR URL redirects here
**with `--fix`**, preserving that context.

> **Transition (through 2026-08-17):** `/rebase` used to always apply review
> feedback. Now a bare `/rebase` rebases only. During this window a bare
> `/rebase` shows a temporary notice (in chat and as a PR comment) pointing you
> to `/fix` (or alternatively `/rebase --fix`); the notice disappears
> automatically after the deadline.

By default, Telegram `/rebase` only queues PRs created by this instance
(branch prefix match). Set `allow_rebase_foreign_prs: true` in
`instance/config.yaml` to allow rebasing other writable PRs.
By default, `/rebase` feedback analysis includes bot-authored comments
(`rebase_include_bot_feedback: true`). Set it to `false` to keep noisy
third-party CI/bot output out of the feedback prompt. Kōan's own comments
are always kept (never treated as bot output), so review feedback it left on
a previous iteration is available to a later rebase — important for combined
review + rebase flows.

After `/rebase` pushes the updated PR branch, it also runs the private
review gate when `private_review_gate` enables `rebase`. The gate reuses the
backend-only `/review` analysis path, fixes Blocking/Important findings on the
same branch, force-pushes any gate fix commits with the normal `/rebase` push
strategy, and re-reviews up to the configured round limit. Gate failures are
reported in the rebase summary but do not fail an otherwise successful rebase.

When `/rebase` runs long, Kōan uses activity-aware limits for review and CI-fix phases: it allows long sessions when CLI output keeps flowing, but still aborts stalled phases after inactivity or a max-duration cap. If the review-feedback step *stalls* (idle/max-duration timeout), Kōan now restores the clean rebased checkpoint and still pushes the rebase (without partial feedback edits), so timeout noise does not discard a valid rebase. If the feedback step hits a *provider quota limit*, the rebase still stops so you can retry after quota reset. Any other transient feedback error remains best-effort and does not block pushing the rebase.

Conflict resolution has a separate per-round agent budget of 600 seconds (`rebase_conflict_timeout`). If it expires or conflicts remain unresolved, Kōan aborts the partial rebase and retains the existing recreate fallback; the failure summary identifies an agent timeout when that was the cause.

`/rebase` is strict about *what* it rebases onto: the branch is rebased only
onto the PR's actual base repository's branch, freshly fetched. The mission
fails loudly — instead of pushing a suspect result — when no local git remote
matches the PR's base repository (`[no_base_remote]`; fix with
`git remote add`), when the base branch can't be fetched (`[fetch_failed]`),
or when a post-rebase sanity check finds the branch not sitting on the base's
current tip or carrying *more* unique commits than before the rebase
(`[sanity_check_failed]` — the branch is restored to its pre-rebase state and
nothing is pushed). A correct rebase can only keep or shrink a PR's commit
count; growth means already-merged commits were resurrected.

When a target repository pre-commit hook formats files during the feedback
commit, Kōan stages the hook-created edits and retries the commit once. If local
hooks still reject the feedback commit, Kōan commits the feedback with
`--no-verify` so valid review edits are not discarded by broad local gates; CI
remains the final validation. If the feedback step itself still fails, Kōan
pushes the clean rebase and reports that review feedback was not applied instead
of labeling the result as a simple rebase.

After completion, Kōan posts a structured comment on the PR with these sections:

1. **Summary** — Classifies the rebase (simple / with adjustments / with conflict resolution). When feedback was reviewed but nothing needed changing, it says so explicitly ("no code changes were needed") rather than implying a fix.
2. **Changes applied** — Only the modifications actually made beyond the rebase (review feedback that changed code, conflict resolution, CI fixes)
3. **Not changed (and why)** — Reviewer points intentionally left as-is, each with the reason (already fixed in an earlier pass, advisory / below the severity filter, or disagreed). Kept separate from *Changes applied* so an unchanged point is never presented as if it were just fixed.
4. **Stats** — Diff summary (files changed, insertions, deletions)
5. **Actions performed** — Pipeline steps in a collapsible `<details>` block
6. **CI status** — Test/CI outcome

<details>
<summary>Use cases</summary>

- `/rebase https://github.com/org/repo/pull/42` — Rebase only (resolve conflicts, no feedback)
- `/rebase --fix https://github.com/org/repo/pull/42` — Rebase **and** address review feedback
- `/rebase https://github.com/org/repo/pull/42 address the security concern` — Rebase with a focus area (implies `--fix`)
</details>

**`/reviewrebase`** — Review a PR then rebase it, so review insights feed the rebase.

- **Usage:** `/reviewrebase <pr-url>`
- **Aliases:** `/rr`
- **GitHub @mention:** `@koan-bot /rr` on a PR

<details>
<summary>Use cases</summary>

- `/rr https://github.com/org/repo/pull/42` — Queues `/review` then `/rebase --fix` at the **end** of the queue (review stays ahead of rebase; the rebase leg addresses the fresh review)
- `/rr --now https://github.com/org/repo/pull/42` — Jumps the queue: inserts the combo at the **top** so it runs next
- Extra context after the URL is passed to the review step (e.g., `/rr <url> focus on error handling`)
</details>

**`/planimplement`** — Plan an issue then implement it, so plan insights feed the implementation.

- **Usage:** `/planimplement <issue-url>`
- **Aliases:** `/planimp`, `/planimpl`, `/planit`, `/plandoit`
- **GitHub @mention:** `@koan-bot /planit` on an issue

<details>
<summary>Use cases</summary>

- `/planit https://github.com/org/repo/issues/42` — Queues `/plan` then `/implement` in sequence
- Extra context after the URL is passed to both steps (e.g., `/planit <url> phase 1 only`)
</details>

**`/squash`** — Squash all PR commits into a single clean commit.

- **Usage:** `/squash <pr-url>`
- **Aliases:** `/sq`
- **GitHub @mention:** `@koan-bot /squash` on a PR

<details>
<summary>Use cases</summary>

- `/squash https://github.com/org/repo/pull/42` — Clean up messy commit history before merge
</details>

**`/recreate`** — Re-implement a PR from scratch on a fresh branch. Useful when a PR has diverged too far.

- **Usage:** `/recreate <pr-url>`
- **Aliases:** `/rc`
- **GitHub @mention:** `@koan-bot /recreate` on a PR

<details>
<summary>Use cases</summary>

- `/recreate https://github.com/org/repo/pull/42` — Start fresh when rebasing won't cut it
</details>

**`/pr`** — Review and update a GitHub pull request (interactive).

- **Usage:** `/pr <pr-url>`

<details>
<summary>Use cases</summary>

- `/pr https://github.com/org/repo/pull/55` — Review a PR and apply updates
</details>

**`/branches`** — List koan branches and open PRs with recommended merge order and stats.

- **Usage:** `/branches [project_name]`
- **Aliases:** `/br`, `/prs`

<details>
<summary>Use cases</summary>

- `/branches` — Show all koan branches for the default project with merge recommendations
- `/branches koan` — Show branches for a specific project
</details>

**`/checkup`** — Health-check every open PR you authored across all projects. For each PR with merge conflicts it queues a `/rebase`; for each with failing CI it queues a `/check`. Results are deduplicated against already-queued missions and skipped when a PR is unchanged since the last checkup.

- **Usage:** `/checkup`
- **Aliases:** `/checkprs`

<details>
<summary>Use cases</summary>

- `/checkup` — Sweep all open PRs and auto-queue fixes for conflicts and CI failures
</details>

**`/orphans`** — Recover orphan branches by rebasing onto the default branch and creating draft PRs.

- **Usage:** `/orphans <project_name>`
- **Aliases:** `/orphan`

<details>
<summary>Use cases</summary>

- `/orphans koan` — Find orphan branches in the koan project, rebase each onto main, and create draft PRs
- Orphan branches are automatically detected during git sync — use this to recover them in one step
- Each PR's title and description are derived from the branch's own commits (no AI call): the title is the first commit's subject, the body is that commit's full message (single commit) or the first three commit messages (multiple commits)
</details>

**`/check`** — Run project health checks on a PR or issue (rebase, review, plan as needed).

- **Usage:** `/check <pr-or-issue-url>`
- **Aliases:** `/inspect`

Kōan also **auto-forwards unresolved human review comments** on its open PRs. During the GitHub notification polling loop, `review_comment_dispatch` checks Kōan-created PRs for new review comments and creates missions to address the feedback — no explicit @mention required. Fingerprint-based deduplication (SHA-256 of sorted comment IDs) prevents re-dispatching the same set of comments. Bot comments are filtered out automatically.

Configure this behavior in `config.yaml`:

```yaml
review_dispatch:
  enabled: true            # Opt-in (default: false)
  cooldown_minutes: 30     # Min minutes between checks per project (default: 30)
  tracker_max_age_days: 30 # Prune dedup entries older than this (default: 30)
```

To prevent the bot from replying to itself in review comment threads (and to cap thread depth), configure:

```yaml
review_reply:
  max_thread_depth: 5      # Stop replying after this many comments per thread (default: 5)
```

When the bot is the last poster in a thread and no human has followed up, the thread is excluded from future replies. This prevents self-reply loops where repeated `/review` runs keep adding replies to the same comment.

<details>
<summary>Use cases</summary>

- `/check https://github.com/org/repo/pull/42` — Let Kōan decide what a PR needs
- Reviewer leaves comments on a PR → next notification check creates a mission to address them
</details>

**`/check_need`** — Analyze whether a PR or issue is still needed given the current state of the repository.

- **Usage:** `/check_need <pr-or-issue-url>`
- **Aliases:** `/need`, `/needs`

Fetches the PR diff or issue description, compares it against the current main branch, and posts a detailed relevance analysis as a GitHub comment. The analysis covers whether changes have been superseded, are partially addressed, or remain fully valuable — with specific file-level evidence and a clear recommendation.

<details>
<summary>Use cases</summary>

- `/check_need https://github.com/org/repo/pull/42` — Check if a PR is still relevant after recent main branch changes
- `/need https://github.com/org/repo/issues/99` — Verify an issue hasn't been addressed already
- `@koan-bot need` on a PR — Trigger relevance check via GitHub @mention
</details>

**`/ci_check`** — Check and fix CI failures on a GitHub PR using Claude.

- **Usage:** `/ci_check <pr-url>` or `/ci_check --enable` / `/ci_check --disable`

Usually auto-triggered when CI fails after a `/rebase`, but can also be invoked manually. Fetches failure logs, checks out the PR branch, and runs Claude to attempt a fix. If the fix produces a commit, it force-pushes and re-enqueues the PR for CI monitoring. Requires `ci_check.enabled: true` in config.yaml (the default).

CI fixing is designed to stay out of the queue's way: the auto-injected fix mission is queued **non-urgently** (behind your other work), each fix mission performs **one** bounded Claude fix attempt and then yields the slot, and retries are spread across iterations up to the `ci_fix_max_attempts` total budget. A single unfixable PR can no longer freeze the agent for hours. Tunables (all optional): `ci_check.timeout` (per-step wall-clock cap, default 3600s), `ci_check.max_fix_attempts_per_mission` (default 1), and `ci_check.idle_timeout` (between-output watchdog, default `first_output_timeout`).

<details>
<summary>Use cases</summary>

- `/ci_check https://github.com/org/repo/pull/42` — Attempt to fix CI failures on a PR
- `/ci_check --enable` — Enable CI check system via config
- `/ci_check --disable` — Disable CI check system via config
- Auto-injected by the CI queue when a post-rebase CI run fails
</details>

**`/diagnose`** — Find the last failed mission, extract journal context, and queue a fix attempt.

- **Usage:** `/diagnose [project]`
- **Alias:** `/dx`

Reads the Failed section of `missions.md`, finds the most recent failure (optionally filtered by project), pulls journal context from that session, and queues an urgent diagnostic mission with all the context baked in.

<details>
<summary>Use cases</summary>

- `/diagnose` — Retry the last failed mission with full failure context
- `/diagnose myapp` — Retry the last failure for a specific project
- `/dx` — Quick alias
</details>

**`/gh_request`** — Route a natural-language GitHub request to the appropriate action.

- **Usage:** `/gh_request <github-url> <request text>`
- **GitHub @mention:** Used automatically when `natural_language: true` is enabled — free-form @mentions are routed here instead of failing with URL validation errors.

<details>
<summary>Use cases</summary>

- `/gh_request https://github.com/org/repo/pull/42 can you review this?` — Classifies as `/review` and queues
- `/gh_request https://github.com/org/repo/issues/10 please fix this` — Classifies as `/fix` and queues
- `@koan-bot can you rebase this PR?` — Automatically routed to `/gh_request` when `natural_language` is on
</details>

### Project Maintenance

**`/claudemd`** — Refresh or create a project's `CLAUDE.md` based on recent architectural changes, or sync Kōan's per-project learnings into it.

- **Usage:** `/claudemd [project-name] [learnings]`
- **Aliases:** `/claude`, `/claude.md`, `/claude_md`

Add the `learnings` sub-argument to distill the durable, generalizable
conventions Kōan has accumulated in its private per-project learnings into a
clearly-delimited, auto-managed `## Learnings from Kōan` block inside the
project's own `CLAUDE.md`. Interactive Claude Code sessions never see Kōan's
private `instance/memory`, so this surfaces those conventions for any AI
assistant on the repo. The block is regenerated in place on each run (no
duplication, human-authored content untouched) and delivered as a draft PR.

<details>
<summary>Use cases</summary>

- `/claudemd webapp` — Update the CLAUDE.md after a big refactor
- `/claudemd` — Refresh for the default/focused project
- `/claudemd webapp learnings` — Distill Kōan's learnings into CLAUDE.md
</details>

#### Project-specific instructions (`KOAN.md`)

Drop an optional `KOAN.md` at a project's root to give instructions to the
autonomous Kōan agent **only**. It uses the same format as `CLAUDE.md`, but
interactive Claude Code sessions never load it — so you can steer koan's
autonomous work (e.g. "prefer docs over code here", "always run `make lint`")
without touching the shared `CLAUDE.md` your whole team sees. Precedence:
mission instruction > `KOAN.md` > `CLAUDE.md`/defaults. A project can also ship
a `.koan/` directory to steer koan per-repo — a second general `.koan/KOAN.md`
and per-skill `.koan/skills/<skill>/*.md` hooks. See
[KOAN.md — koan-only project instructions](koan-md.md) for details.

**`/gha_audit`** — Scan GitHub Actions workflows for security vulnerabilities.

- **Usage:** `/gha_audit [project-name]`
- **Aliases:** `/gha`

<details>
<summary>Use cases</summary>

- `/gha_audit` — Quick security check of your CI/CD pipelines
- `/gha_audit api` — Audit a specific project's workflows
</details>

**`/changelog`** — Generate a changelog from recent commits and journal entries.

- **Usage:** `/changelog [project] [--since=YYYY-MM-DD] [--format=md|telegram]`
- **Aliases:** `/changes`

<details>
<summary>Use cases</summary>

- `/changelog` — What changed recently?
- `/changelog webapp --since=2025-01-01` — Changes since a specific date
- `/changelog --format=md` — Get markdown output for release notes
</details>

**`/done`** — List PRs merged in the last 24 hours across all projects.

- **Usage:** `/done [project] [--hours=N]`
- **Aliases:** `/merged`

<details>
<summary>Use cases</summary>

- `/done` — What got merged today?
- `/done webapp` — Merged PRs for a specific project
- `/done --hours=48` — Merged PRs in the last 2 days
</details>

### Scheduling Work

Kōan supports recurring missions that automatically re-queue at set intervals.

**`/daily`** — Schedule a mission to run every day.
- **Usage:** `/daily <text> [project:<name>]`

**`/hourly`** — Schedule a mission to run every hour.
- **Usage:** `/hourly <text> [project:<name>]`

**`/weekly`** — Schedule a mission to run every week.
- **Usage:** `/weekly <text> [project:<name>]`

> **Targeting a project:** the bracketed `[project:<name>]` form is recommended, but a trailing `project:<name>` (no brackets, at the end of the text) is also accepted — e.g. `/daily run the API audit project:webapp`. The same applies to `/every`.

**`/recurring`** — List, manage, or force-run recurring missions. All management actions are sub-commands of `/recurring`.
- **Usage:** `/recurring` (list), `/recurring resume <n>` (re-enable), `/recurring run [n]` (force immediate run), `/recurring pause <n>` (disable), `/recurring cancel <n>` (remove), `/recurring days <n> <days>` (day-of-week filter)
- **Aliases:** —

<details>
<summary>Use cases</summary>

- `/daily Review open PRs and summarize status [project:webapp]` — Daily PR digest
- `/weekly Run the full test suite and report flaky tests` — Weekly health check
- `/hourly Check CI status [project:api]` — Frequent monitoring
- `/recurring` — See what's scheduled
- `/recurring resume 1` — Re-enable a paused mission
- `/recurring run 2` — Force an immediate run of mission #2
- `/recurring pause 1` — Temporarily disable mission #1
- `/recurring cancel 2` — Stop a recurring mission
</details>

### Automation Suggestions

When Kōan is idle (no pending missions, not in focus mode), it can proactively suggest recurring tasks tailored to your project. Suggestions are generated using a lightweight model that analyzes project learnings, existing recurring tasks, and patterns from other projects you manage.

Suggestions appear as Telegram messages with copy-pasteable commands — just forward the command back to activate it.

**Configuration** (`config.yaml`):

```yaml
suggestions:
  enabled: true              # Master switch (default: true)
  min_interval_hours: 24     # Cooldown between suggestions per project
  max_per_day: 2             # Daily cap per project
```

Suggestions are automatically deduplicated against existing recurring tasks. The feature only triggers in `implement` or `deep` autonomous modes (when there's enough budget to be useful).

### Ideas Backlog

Not ready to commit to a mission? Save it as an idea.

**`/idea`** — Add an idea to the backlog, or manage existing ideas.

- **Usage:**
  - `/idea <text>` — Add a new idea
  - `/idea <project> <text>` — Add idea for a specific project
  - `/idea promote <n>` — Promote idea #n to a mission
  - `/idea delete <n>` — Delete idea #n
- **Aliases:** `/buffer`

**`/ideas`** — List all ideas in the backlog.

<details>
<summary>Use cases</summary>

- `/idea Maybe we should add GraphQL support` — Save for later
- `/ideas` — Browse the backlog
- `/idea promote 3` — "OK, let's do idea #3"
</details>

### Reflection & Journal

**`/reflect`** — Write a reflection to the shared journal. Both you and Kōan contribute to this shared space.

- **Usage:** `/reflect <observation>`
- **Aliases:** `/think`

<details>
<summary>Use cases</summary>

- `/reflect The new caching layer reduced API latency by 40%` — Share an observation
- `/reflect I think we should prioritize mobile performance next quarter`
</details>

**`/journal`** — View journal entries.

- **Usage:** `/journal [project] [date]`
- **Aliases:** `/log`

<details>
<summary>Use cases</summary>

- `/journal` — Today's journal entries
- `/journal webapp` — Journal for a specific project
- `/journal 2025-03-01` — Historical entries
</details>

### Email Digests

**`/email`** — Check email digest status or send a test email.

- **Usage:** `/email`, `/email test`

<details>
<summary>Use cases</summary>

- `/email` — Check if email digests are configured
- `/email test` — Send a test email to verify setup
</details>

### Statistics

**`/stats`** — View session outcome statistics per project: success rates, mission counts, productivity trends.

- **Usage:** `/stats [project]`

<details>
<summary>Use cases</summary>

- `/stats` — Overall productivity snapshot
- `/stats webapp` — How's Kōan doing on a specific project?
</details>

**`/report`** — Pull-Request activity report per-project and global, posted as a markdown code block. A plain `/report` emits both the weekly and the monthly digest; add `--week` or `--month` to narrow it. Shortcuts: `/reports` (same as `/report`), `/weekly_report`, `/monthly_report`.

- **Usage:** `/report` (both) | `/report --week` | `/report --month`
- **Metrics:**
  - **Created** — PRs Kōan opened in the window.
  - **Merged (%)** — of those created, how many are now merged (cohort success rate).
  - **Interacted** — PRs Kōan was involved in (commented, reviewed) updated in the window, including human-authored PRs.
  - **Interacted+merged** — PRs Kōan interacted with that merged during the window.
- **Note:** "Interacted" is sourced from GitHub search (`involves:`). A bare force-push/rebase on a *human-authored* PR (no comment) won't be counted; Kōan's own PRs always are.

<details>
<summary>Use cases</summary>

- `/weekly_report` — Monday digest of last week's PR throughput
- `/report --month` — Monthly review across all projects
</details>

### Understanding Quota Modes

Kōan automatically adapts its work intensity based on remaining API quota:

| Mode | Quota | Behavior |
|------|-------|----------|
| **DEEP** | >40% | Strategic work, thorough exploration, comprehensive reviews |
| **IMPLEMENT** | 15–40% | Focused development, quick wins, efficient execution |
| **REVIEW** | <15% | Read-only analysis, code audits, lightweight tasks |
| **WAIT** | <5% | Graceful pause until quota resets |

You don't need to manage this — Kōan adjusts automatically. Use `/quota` to see the current mode. If the internal estimate drifts from reality, use `/quota <N>` to override (e.g., `/quota 50` tells Kōan it has 50% remaining).

When the provider reports a hard quota/session limit, Kōan pauses immediately,
moves the current mission back to Pending, and resumes 10 minutes after the
reported reset time. If the reset time cannot be parsed, Kōan pauses for 5
hours.

### Exploration Mode

When exploration is enabled, Kōan may autonomously explore a project's codebase between missions — discovering improvements, noting issues, and building context.

**`/explore`** — Enable exploration or show status.
- **Usage:** `/explore [project|all|none]`
- **Aliases:** `/exploration`

**`/noexplore`** — Disable exploration for a project.
- **Usage:** `/noexplore [project|all]`

Using `all` or `none` also sets the default for future projects added via `/add_project` or workspace discovery.

<details>
<summary>Use cases</summary>

- `/explore webapp` — Let Kōan explore the webapp codebase
- `/explore all` — Enable exploration for all projects + set default
- `/noexplore backend` — Disable exploration for one project
- `/noexplore all` — Disable exploration for all projects + set default
</details>

### Autoreview Mode

When autoreview is enabled for a project, Kōan automatically queues `/review <pr-url>` then `/rebase --fix <pr-url>` after any successful mission that creates a PR (and was not auto-merged). The rebase leg carries `--fix` so it addresses the review it just generated. This provides an extra quality gate without manual intervention. Off by default.

**`/autoreview`** — Enable autoreview or show status.
- **Usage:** `/autoreview [project|all|none]`
- **Aliases:** `/auto_review`

**`/noautoreview`** — Disable autoreview for a project.
- **Usage:** `/noautoreview [project]`

<details>
<summary>Use cases</summary>

- `/autoreview webapp` — Enable autoreview for webapp project
- `/autoreview all` — Enable autoreview for all projects
- `/noautoreview webapp` — Disable autoreview for webapp
</details>

### Workflow Example: Feature from Idea to PR

Here's a typical multi-step workflow combining several commands:

```
1. /idea Add rate limiting to the public API          # Save the idea
2. /idea promote 1                                     # Ready to work on it
3. /plan Add rate limiting to the public API           # Get a structured plan
4. /implement https://github.com/org/repo/issues/123   # Implement the plan
5. /review https://github.com/org/repo/pull/124        # Review the result
6. # Merge the PR on GitHub when satisfied
```

---

## Power User — Advanced Configuration

Unlock Kōan's full potential with advanced configuration and extensibility features.

### Parallel Sessions

Kōan can work on multiple missions simultaneously using **git worktrees** for isolation. Each parallel session runs in its own worktree with a dedicated branch, so sessions never interfere with each other.

#### How It Works

When parallel sessions are enabled, Kōan can pick up additional pending missions while one is already running. Each session gets:

- **Isolated worktree** — a separate checkout of the repository under `.worktrees/`
- **Dedicated branch** — `koan/session-<id>` branches created automatically
- **Independent subprocess** — a Claude Code process running in the worktree

Sessions are coordinated through a persistent registry (`instance/sessions.json`) with file-level locking for process safety.

#### Configuration

Add `max_parallel_sessions` to your `instance/config.yaml`:

```yaml
# Parallel session configuration
max_parallel_sessions: 2    # Number of concurrent sessions (1-5, default: 2)
```

Set to `1` to disable parallel execution and use the classic sequential mode.

#### Shared Dependencies

To avoid duplicating heavy dependency directories across worktrees, configure `shared_deps` in your project's `projects.yaml`:

```yaml
projects:
  webapp:
    path: ~/Code/webapp
    shared_deps:
      - node_modules
      - .venv
```

These directories are symlinked from the main project into each worktree, saving disk space and setup time.

> **Note:** Shared deps are best used for read-only caches. If a mission's build step modifies dependencies (e.g., `npm install`), it may affect other sessions sharing the same directory.

#### Monitoring

Parallel sessions appear in the standard status commands:

- **`/status`** — Shows count of active parallel sessions
- **`/live`** — Shows progress of all running sessions

Session output is captured to temporary files and collected when each session completes.

#### Cleanup

Worktrees and session branches are automatically cleaned up when a session finishes (success or failure). On startup, Kōan also recovers stale sessions from previous crashes — marking them as failed and removing their worktrees.

To manually clean up orphaned worktrees:

```bash
# From the project directory
git worktree list    # See all worktrees
git worktree prune   # Remove stale references
```

### Deep Exploration

**`/ai`** — Queue an AI exploration mission. Runs as a full agent mission with codebase access — deeper and more thorough than `/magic`.

- **Usage:** `/ai [project]`
- **Aliases:** `/ia`

<details>
<summary>Use cases</summary>

- `/ai webapp` — Deep dive into a project, discover insights, suggest improvements
- `/ai` — Explore the default/focused project
</details>

**`/deep`** — Launch a thorough autonomous exploration session. Full tool access (Read, Grep, Bash), higher turn limits, and structured mission output. Goes deeper than `/ai` — traces execution paths, checks test coverage, finds real bugs.

- **Usage:** `/deep [project] [focus context]`

<details>
<summary>Use cases</summary>

- `/deep koan` — Thorough exploration of the koan project
- `/deep koan error handling` — Focused deep dive on error handling patterns
- `/deep` — Deep explore a random project
</details>

**`/magic`** — Instant creative exploration. Quick single-turn analysis without queuing a mission.

- **Usage:** `/magic [project]`

<details>
<summary>Use cases</summary>

- `/magic` — "Surprise me — what's interesting in this codebase?"
- `/magic api` — Quick creative scan of a specific project
</details>

**`/sparring`** — Start a strategic sparring session. This is about thinking, not code — Kōan challenges your assumptions and pushes your ideas.

<details>
<summary>Use cases</summary>

- `/sparring` — "Challenge me on my architecture decisions"
</details>

### Configuration Deep-Dive

All behavioral config lives in `instance/config.yaml`. Key settings:

```yaml
# Work intensity
max_runs_per_day: 60          # Max missions per day (default: 60)
interval_seconds: 60          # Seconds between mission checks

# Model selection (see docs/users/model-configuration.md)
models:
  default:
    mission: null             # Default (sonnet) for mission work
    chat: null                # Default for chat replies
    lightweight: haiku        # Quick tasks (formatting, picking)
    review_mode: null         # Override autonomous review mode and /review

# Budget thresholds
budget:
  warn_at_percent: 20         # Warn when quota drops below
  stop_at_percent: 5          # Stop working below this

# Usage estimation mode
usage:
  session_token_limit: 500000 # Tokens per 5h window
  weekly_token_limit: 5000000 # Tokens per 7-day window
  budget_mode: session_only   # full | session_only | disabled
  # unlimited_quota: true     # Provider has no quota limit (see below)

# Tool restrictions (limit what the agent can do)
tools:
  allowed: []                 # Whitelist (empty = all allowed)
  blocked: []                 # Blacklist specific tools

# Start on pause — boot directly into pause mode
# Useful for scheduled launches (cron, launchd) where you want
# the stack running but idle until you explicitly /resume.
start_on_pause: false

# Multiple instances sharing one GitHub account — suppresses
# warnings about @mentions on repos not in this instance's projects.yaml.
enable_multiple_instances: false

# Shared GitHub/Jira notification polling guard. Provider-specific
# github/jira settings can override this, but the shared setting is preferred.
# When auto_pause is false, quiet idle loops still wait for this backoff
# instead of repeatedly re-planning with no work.
notification_polling:
  check_interval_seconds: 60
  max_check_interval_seconds: 300

# Schedule (when Kōan is allowed to work)
schedule:
  timezone: UTC
  active_hours: "00:00-23:59" # Default: always active

# Skill execution limits
skill_timeout: 3600           # Max seconds for /fix, /implement, /incident
first_output_timeout: 600     # Kill silent skills after N seconds (0 disables)
rebase_first_output_timeout: 1800  # Optional longer silence budget for /rebase
rebase_review_idle_timeout: 1800   # /rebase review phase: kill on inactivity
rebase_review_max_duration: 10800  # /rebase review phase: absolute cap
rebase_ci_idle_timeout: 1800       # /rebase CI-fix phase: kill on inactivity
rebase_ci_max_duration: 7200       # /rebase CI-fix phase: absolute cap
rebase_conflict_timeout: 600       # Per conflict-resolution round before recreate fallback
rebase_include_bot_feedback: true  # Include bot-authored PR comments in feedback analysis (set false to filter them out)
allow_rebase_foreign_prs: false    # Telegram /rebase can target non-instance PRs
strip_co_authored_by: false        # Strip Co-Authored-By trailers from generated commits (set true to enable)
skill_max_turns: 200          # Max agentic turns for heavy skills

# Stagnation detection — kill Claude sessions stuck in a loop early
# (identical trailing stdout hash across `abort_after_cycles` samples).
# Prevents quota burn when Claude keeps retrying the same failing tool.
# Stagnated missions are re-queued for retry up to `max_retry_on_stagnation`
# times before being marked Failed, since a fresh start often unsticks Claude.
stagnation:
  enabled: true               # Set false to disable globally
  check_interval_seconds: 60  # How often to sample subprocess stdout
  abort_after_cycles: 3       # Identical samples required to kill (min 2)
  sample_lines: 50            # Trailing lines hashed each sample
  max_retry_on_stagnation: 3  # Stagnation requeues before marking Failed (0 disables retry)

# Crash and error recovery — how the loop tolerates failures before pausing
# or giving up. Backoff grows linearly (attempt * multiplier) up to the caps.
recovery:
  max_consecutive_errors: 10    # Pause after this many iteration errors
  max_main_crashes: 5          # Give up after this many crashes in main()
  backoff_multiplier: 10       # Seconds per attempt step
  max_backoff_main: 60         # Ceiling for main() crash backoff
  max_backoff_iteration: 300    # Ceiling for iteration error backoff
  error_notification_interval: 5  # Notify every N errors after the first

# Prompt guard (content safety)
prompt_guard:
  enabled: true               # Enable prompt injection detection (default: true)
  block_mode: true            # true = reject mission (default), false = warn + quarantine

# Output optimizations — caveman directive ("no filler, 3–6 word sentences,
# direct answers"). ``enabled`` controls the agent loop (default true);
# skills are opt-in via SKILL.md ``caveman: true`` or this ``include`` list.
optimizations:
  caveman:
    enabled: true
    include: []                # canonical skill names, aliases auto-resolved
  ponytail:
    enabled: true              # six-gate code minimalism ladder (default: true)

# Review ignore — exclude files from /review PR diffs
# Reduces token spend on generated/vendored code
# review_ignore:
#   glob:
#     - "vendor/**"    # all files under vendor/
#     - "*.lock"       # lock files at any depth
#   regex:
#     - '.*\.pb\.go$'  # protobuf-generated files (full path regex)

# Private review gate for /fix, /implement, and /rebase (opt-in during testing)
private_review_gate:
  enabled: true              # Default: false — opt-in; set true to turn on
  enabled_skills: [fix, implement, rebase]
  max_rounds: 3              # Review/fix rounds before reporting remaining findings
  min_severity: warning      # warning = Important; critical = Blocking only
  budget_aware: true         # Skip/limit rounds when quota is tight (default: true)
  dedup: true                # Skip re-reviewing the same clean PR head (default: true)
  tracker_max_age_days: 30   # Dedup tracker entry retention (default: 30)
```

See `instance.example/config.yaml` for all available options.

`usage.budget_mode: disabled` turns off Koan's internal token-budget gating.
Hard provider quota/session-limit errors are still detected from CLI output and
will still pause and requeue missions.

`usage.unlimited_quota: true` is a stronger override: it disables all proactive
quota gating — budget-mode downgrades, burn-rate warnings, and preflight quota
probes. Use this when your CLI provider has no metered quota (e.g. a self-hosted
API proxy or a plan with no hard token limits). If the CLI actually fails with a
quota error, Koan still detects it and pauses.

`preflight_cache_minutes` (default 10, `0` disables) controls how long a
*successful* pre-flight quota probe is reused. The loop probes provider quota
before every mission attempt; Claude reads local usage for free, but providers
with no usage introspection (haze, cline) probe with a real LLM call — observed
at ~16s per attempt. Within the TTL a recent success is reused instead of
re-probing. Failures are never cached, so exhaustion verdicts always re-check
and the pause/recovery behavior is unchanged. The trade-off is bounded: at worst
one mission starts on a stale "available" verdict inside the TTL, which the
in-run quota detection then catches.

**`/models`** (alias `/model`) — Show the resolved model configuration for the active CLI provider. Useful when debugging model-routing issues — displays which model wins for each of the 6 slots (`mission`, `chat`, `lightweight`, `fallback`, `review_mode`, `reflect`) after applying the full resolution chain: per-project `models:` → `models.{provider}:` → `models.default:` → built-in defaults.

```
/models
```

When a slot is routed to a **custom CLI** via the `cli:` config section (e.g. `cli.default.review_mode: "claude:/root/.local/bin/claude-deep"`), the line is annotated with the resolved binary — `review_mode: opus  [cli: claude-deep]`. Slots on the global provider show no annotation. This makes it obvious at a glance which mission types run a different CLI binary than the default.

The active provider is also shown in `/status` output. See [Provider-specific model config](#provider-specific-model-config) below for how to configure `models.claude:` / `models.codex:` sections.

**`/config_check`** — Detect drift between your `instance/config.yaml` and the template at `instance.example/config.yaml`. Reports two things:

- **Missing keys** — in the template but absent from your config. These are new features released since you last synced and are probably worth reviewing.
- **Extra keys** — in your config but absent from the template. These are usually deprecated/removed settings (or typos).

Run it after every Kōan update to stay in sync:

```
/config_check
```

The same check runs automatically as part of `/doctor` — use `/config_check` when you only want the config slice without the rest of the diagnostic report.

### Health Check & Auto-Repair

**`/doctor`** — Run diagnostic self-checks on configuration, environment, instance structure, processes, projects, and connectivity.

```
/doctor           # Quick diagnostics
/doctor --full    # Include connectivity checks (Telegram, GitHub, CLI)
/doctor --fix     # Auto-repair common issues
```

The `--fix` mode safely repairs:
- **Stale PID files** — removes orphaned PID files when the process is no longer alive
- **Missions.md structural issues** — fixes duplicate headers, foreign sections
- **Stale in-progress missions** — recovers stuck missions back to Pending
- **Missing directories** — creates missing `memory/` and `journal/` directories

When `--fix` is not used, fixable issues are flagged with a hint to re-run with `--fix`.

### Remote HEAD Rescan

**`/rescan`** — Re-check all project workspaces for remote default branch changes (e.g. when a repository renames its default branch from `master` to `main`).

```
/rescan
```

Kōan also checks for remote HEAD changes automatically at startup (throttled to once every 12 hours). Use `/rescan` to force an immediate check across all projects. When a change is detected, the local workspace is updated: the symbolic ref is set, the new branch is fetched and created locally, and if the workspace was on the old branch, it's switched to the new one.

### Caveman Output Optimization

Caveman appends a "no filler, 3–6 word sentences, direct answers" directive to Claude prompts to reduce output tokens.

**Where it applies by default:**

- **Agent loop (regular missions)** — caveman is on by default. This is the highest-volume Claude entry point, so the directive yields the most savings here.
- **Skills and chat — opt-in.** A skill receives caveman only when it explicitly says so. New skills (core or custom) inherit *no* caveman until the author or operator turns it on.

**Core skills shipping with caveman opted in (`caveman: true`)** — these produce terse, status-style output where the directive helps:

| Skill | Why caveman fits |
|-------|------------------|
| `/rebase`, `/recreate`, `/squash` | Git-plumbing skills; output is mostly status |
| `/fix` | Focused issue-fix flow |
| `/debug` | Structured hypothesis-driven debugging |
| `/ci_check` | Diagnostic, action-oriented |
| `/check` | PR/issue check report |
| `/implement` | Mission narration during implementation |

**Core skills shipping with caveman opted out (`caveman: false`)** — terseness directly hurts these (kept explicit for clarity, even though it matches the default):

`/plan`, `/deepplan`, `/review`, `/security_audit`, `/audit`, `/brainstorm`, `/sparring`, `/incident`, `/claudemd`, `/chat`, `/explain`.

**Operator override — the `include:` list:**

```yaml
optimizations:
  caveman:
    enabled: true
    include: [my_custom_skill, deeplan]   # aliases auto-resolved → deepplan
```

Names match **canonical command names**; aliases declared in `koan/app/skill_dispatch.py` (`deeplan` → `deepplan`, `security`/`secu` → `security_audit`, `private_security`/`psecu` → `private_security_audit`, …) resolve automatically. The operator's `include:` list overrides a SKILL.md `caveman: false`, giving instance owners the final say.

**Switching the global flag off** disables caveman everywhere — agent loop included:

```yaml
optimizations:
  caveman:
    enabled: false
```

**Custom skill authors:** add `caveman: true` to your SKILL.md frontmatter when your skill produces terse output that benefits from the directive — see `koan/skills/README.md`.

#### Ponytail Code Minimalism

Ponytail is a complementary optimization that reduces the amount of **code** Claude generates (caveman reduces **prose** verbosity). When enabled, the agent prompt includes a six-gate decision ladder: Is it necessary? Does stdlib handle it? Is it a native feature? Does an existing dep cover it? Can it be a one-liner? Only then write new code.

Ponytail is **enabled by default**. To disable it:

```yaml
optimizations:
  ponytail:
    enabled: false
```

### Per-Project Overrides

Projects are configured in `projects.yaml` at `KOAN_ROOT`. Repositories under
`KOAN_ROOT/workspace/<name>` are also discovered automatically as projects;
add a `projects.yaml` entry when you need overrides such as model selection,
tracker routing, or a project name that differs from the directory name. Each
project can override defaults:

```yaml
defaults:
  git_auto_merge:
    enabled: false
    strategy: squash

projects:
  webapp:
    path: ~/Code/webapp
    cli_provider: claude       # CLI provider override
    models:
      mission: opus            # Use Opus for this project
      review_mode: sonnet      # Use Sonnet for review mode and /review
    tools:
      blocked: [WebSearch]     # Restrict certain tools
    git_auto_merge:
      enabled: true            # Auto-merge for this project
      strategy: squash
    issue_tracker:
      provider: jira           # github | jira
      jira_project: PROJ       # Jira project key for ticket routing
      jira_issue_type: Task    # Default type for issues Koan creates
      default_branch: main     # Target branch for Jira-triggered work
    authorized_users:          # Who can trigger via GitHub @mention
      - username1
```

Key per-project settings:
- **`cli_provider`** — `claude`, `cline`, `codex`, `copilot`, `haze`, `grok`, `gemini`, `local`, or `ollama-launch`
- **`models`** — Override model selection per role
- **`tools`** — Restrict available tools
- **`git_auto_merge`** — Auto-merge completed PRs (strategy: squash/merge/rebase)
- **`issue_tracker`** — Issue provider routing for GitHub/Jira-backed projects
- **`security_review`** — Automatic diff analysis for dangerous patterns before auto-merge (see below)
- **`review_verdict`** — Control formal APPROVE/REQUEST_CHANGES verdict submission (see below)
- **`private_review_gate`** — Override the private `/fix`, `/implement`, and `/rebase` post-PR review/fix loop
- **`authorized_users`** — GitHub users allowed to trigger via @mention
- **`exploration`** — Enable/disable autonomous exploration

#### Security Review

When enabled, Kōan scans mission diffs for security-sensitive patterns before auto-merge:
- **Blast radius** — files changed, modules affected, infrastructure/dependency changes
- **Content patterns** — eval, exec, shell injection, hardcoded secrets, unsafe deserialization, XSS, wildcard CORS, etc.
- **Risk classification** — low / medium / high / critical based on cumulative score

Results are logged to the project journal. In blocking mode, auto-merge is skipped when the risk level meets or exceeds the configured threshold.

```yaml
defaults:
  security_review:
    enabled: true              # Scan diffs for dangerous patterns
    blocking: false            # true = block auto-merge on high risk
    severity_threshold: high   # low / medium / high / critical
```

Per-project override example:
```yaml
projects:
  production-api:
    security_review:
      enabled: true
      blocking: true           # Block auto-merge for risky changes
      severity_threshold: medium
```

See [docs/security/security-review.md](../security/security-review.md) for the full list of detected patterns, risk scoring details, and pipeline integration.

#### Review Verdict

Controls the formal APPROVE / REQUEST_CHANGES verdict submitted via the GitHub Pull Request Reviews API after `/review`. Review comments and PR feedback are always posted regardless of this setting.

```yaml
# instance/config.yaml
review_verdict:
  approved: true              # Submit the verdict status (default: true)
                               # Set to false to skip the formal verdict entirely
  body_enabled: true           # Include a body with the verdict (default: true)
  include_blockers: true       # List blocking finding titles in REQUEST_CHANGES body
```

Per-project override in `projects.yaml`:
```yaml
projects:
  sensitive-repo:
    review_verdict:
      approved: false          # Comments only, no formal GitHub verdict
```

When `approved: false`, the bot still posts review comments and PR feedback but skips the formal GitHub review status (green check / red X in the Reviewers panel). Configuration errors are fail-closed: if loading project overrides fails, or if the `review_verdict` section is malformed (non-dict value or non-boolean entries for known keys), the verdict is skipped to preserve operator intent.

GitHub forbids APPROVE / REQUEST_CHANGES on a PR you authored (HTTP 422). When Kōan reviews its own PR, the verdict body is automatically posted as a `COMMENT` review instead, so the feedback still appears in the Reviewers panel rather than being lost to a submission error.

**Inline comments (opt-in):** Set `review_inline_comments.enabled: true` in `config.yaml` to also post each finding as an inline PR comment anchored to its code location, in addition to the bucketed summary comment (which is unchanged). Each inline thread shows the same severity marker (🔴/🟡/🟢) and the full finding detail, so reviewers can react or resolve in place. Cap the volume with `review_inline_comments.max_comments` (default 25). Disabled by default; findings without a resolvable line, or reviews with no head SHA, are skipped. Re-running `/review` is idempotent — findings already anchored on the PR are not re-posted. Multi-line findings anchor to their full range. If findings exist but every inline post fails, Kōan notifies you instead of failing silently.

```yaml
review_inline_comments:
  enabled: false        # Master switch (default: false)
  max_comments: 25      # Cap inline threads posted per review (default: 25)
```

**Review history (preserving prior reviews):** By default, on a re-review (new commits landed, or a review was re-requested), the bot collapses its previous review comment to a short "superseded" pointer so the PR timeline stays tidy and the latest review is the one that stands out. Set `review_history.preserve_previous: true` to leave the prior review comment untouched — the new review is posted alongside it instead. The flag only affects re-reviews; a review of the same commits with no re-request still updates the existing comment in place. Overridable per-project in `projects.yaml`.

```yaml
review_history:
  preserve_previous: false   # Keep the previous review comment intact (default: false)
```

**Draft-PR review gate (opt-in):** By default Kōan reviews a PR as soon as it is attached as a reviewer, whether or not the PR is a draft. Set `review_draft_skip.enabled: true` to defer that automatic review while a PR is in draft state — the author has marked it not-ready, so the bot waits. **To review a deferred PR, send `/review` once it is ready — that is the reliable remedy.** Do **not** rely on automatic resume: GitHub does not reliably re-surface the review request on the draft→ready transition, so a deferred review is not guaranteed to fire on its own. (The deferral is a soft skip — it writes no dedup or cooldown state — so *if* GitHub ever does re-surface the request it is re-evaluated fresh; but treat that as a bonus, not a mechanism you can count on.) So you are never left guessing, a deferred review sends one info message telling you it was intentionally postponed and to send `/review` when ready. This gates **only** the "bot attached as reviewer" path: an explicit `/review` from chat or a GitHub `@mention` is always honored immediately, regardless of this flag or the PR's draft state.

```yaml
review_draft_skip:
  enabled: false          # Defer auto-review of draft PRs (default: false)
```

**Pause-label review gate (default on):** While a PR carries the configured
label (default `PauseReview`), Kōan skips LLM review — no prompt, no provider
call, no review comment. Auto-queue from `review_requested` is soft-skipped
(notification marked read; no dedup/cooldown). Already-queued or explicit
`/review` missions also exit early at execution time. **Remedies:** remove the
label (auto-review resumes on the next request), or send
`/review --force <url>` to review once while the label stays. Set
`review_pause_label: ""` to disable label checking entirely. Label match is
exact and case-sensitive (GitHub label names are exact).

```yaml
review_pause_label: "PauseReview"   # default; "" disables
```

### Custom Skills

Kōan's skill system is fully extensible. Install skills from Git repos or create your own.

**Install from Git:**
```
/skill install https://github.com/your-org/koan-skills.git
/skill approve <scope> <fingerprint>
/skill update <scope>
/skill remove <scope>
```

Freshly installed and scaffolded skills are **quarantined** until you approve
them. Kōan replies with a short hex fingerprint of the on-disk files; loaded
handlers are skipped by the registry until you run `/skill approve` with that
fingerprint. This blocks blind / prompt-injected installs from running code in
the bridge process. Inspect the files in `instance/skills/<scope>/` first.

Optional `config.yaml` allow-list to refuse clones outside trusted hosts
(defense-in-depth; the approval gate still applies if you do not set it):

```yaml
skills:
  allowed_hosts:
    - github.com/your-org
```

**Create your own:** Add a `SKILL.md` file in `instance/skills/<scope>/<name>/`:

```yaml
---
name: my-skill
scope: my-scope
description: What this skill does
audience: bridge
commands:
  - name: mycommand
    description: One-line description
    usage: /mycommand <args>
handler: handler.py
---
```

The handler follows a simple pattern:

```python
def handle(ctx):
    # ctx.args — command arguments
    # ctx.project — current project
    # ctx.instance_dir — instance directory path
    return "Response message"  # or None for no reply
```

For prompt-only skills (no handler), put the prompt text after the YAML frontmatter — it's sent directly to Claude.

**Scaffold a skill from a description:**

Instead of writing SKILL.md and handler.py by hand, use `/scaffold_skill` to generate them:

```
/scaffold_skill myteam deploy Deploy to production with rollback support
```

This invokes Claude to produce a valid SKILL.md + handler.py stub in `instance/skills/myteam/deploy/`, validated against the parser before writing. Restart the bridge to load the new skill.

See [koan/skills/README.md](../../koan/skills/README.md) for the full authoring guide.

### GitHub @mention Integration

Ten skills can be triggered by commenting `@koan-bot <command>` on GitHub issues and PRs:

| Skill | GitHub trigger |
|-------|---------------|
| `/brainstorm` | `@koan-bot /brainstorm <topic>` on an issue |
| `/implement` | `@koan-bot /implement` on an issue |
| `/fix` | `@koan-bot /fix` on an issue |
| `/debug` | `@koan-bot /debug` on an issue |
| `/review` | `@koan-bot /review` on a PR |
| `/rebase` | `@koan-bot /rebase` on a PR |
| `/reviewrebase` | `@koan-bot /rr` on a PR |
| `/planimplement` | `@koan-bot /planit` on an issue |
| `/recreate` | `@koan-bot /recreate` on a PR |
| `/refactor` | `@koan-bot /refactor` on a PR or issue |
| `/plan` | `@koan-bot /plan <idea>` on an issue |
| `/profile` | `@koan-bot /profile` on a PR or issue |

Setup requires configuring `github_nickname` and `github_commands_enabled` in `config.yaml`. See [docs/messaging/github-commands.md](../messaging/github-commands.md) for full setup and configuration details.

### CLI Providers

Kōan supports multiple CLI backends. Configure globally via `KOAN_CLI_PROVIDER` env var or per-project in `projects.yaml`.

| Provider | Best for | Docs |
|----------|----------|------|
| **Claude Code** (default) | Full-featured agent, best reasoning | [claude.md](../providers/claude.md) |
| **Cline** | Multi-backend (OpenRouter, Anthropic, OpenAI) | [cline.md](../providers/cline.md) |
| **OpenAI Codex** | ChatGPT users who want Codex models | [codex.md](../providers/codex.md) |
| **GitHub Copilot** | Teams with existing Copilot licenses | [copilot.md](../providers/copilot.md) |
| **Haze** | Minimal multi-backend agentic CLI (OpenAI, OpenRouter, local endpoints) — [official docs](https://denizokcu.github.io/haze/) | [haze.md](../providers/haze.md) |
| **Grok Build** | xAI Grok Build CLI (`grok`) with headless streaming-json | [grok.md](../providers/grok.md) |
| **Gemini** | Google Gemini CLI (`gemini`) with headless stream-json | [gemini.md](../providers/gemini.md) |
| **Ollama Launch** | Local/offline models behind the Claude CLI harness | [ollama-launch.md](../providers/ollama-launch.md) |
| **OpenRouter** (via Claude CLI) | Claude CLI behavior with OpenRouter's model catalog/billing | [openrouter.md](../providers/openrouter.md) |

#### Provider-specific model config

When switching between providers, model names are not interchangeable. Use `models.{provider}:` sections in `instance/config.yaml` to configure provider-specific defaults without touching the global `models.default:` fallback:

```yaml
cli_provider: "codex"

models:
  # Provider-specific overrides (resolved before models.default)
  codex:
    mission: "gpt-5.6-sol"
    chat: "gpt-5.6-terra"
    lightweight: "gpt-5.6-luna"
    fallback: ""  # Codex has no fallback mechanism (ignored)
    review_mode: "gpt-5.6-sol"
    reflect: "gpt-5.6-luna"

  claude:
    review_mode: "haiku"      # use haiku for cheaper REVIEW mode audits

  ollama-launch:
    mission: "qwen2.5-coder:14b"
    chat: "qwen2.5-coder:14b"
    lightweight: "qwen2.5-coder:7b"

  # Global fallback for providers without a specific section
  default:
    lightweight: "haiku"
    fallback: "sonnet"
```

Resolution order per key: per-project `models:` → `models.{provider}:` → `models.default:` → built-in default. Provider names may use hyphens or underscores. The legacy flat `models:` / `models_for_{provider}:` layout still works but emits a one-time `DEPRECATED` warning at startup. See [Model Configuration](model-configuration.md) for the full migration guide.

Use `/models` to inspect the resolved values for the active provider at any time.

### Language Preference

**`/language`** — Set or reset the reply language. Defaults to **English** on a
fresh install — no `/english` needed.

- **Usage:** `/language <lang>`, `/language reset`
- **Aliases:** `/lng`

**`/french`** / **`/english`** — Quick language switches.

- **Aliases:** `/fr`, `/francais`, `/français` / `/en`, `/anglais`

<details>
<summary>Use cases</summary>

- `/fr` — Switch to French replies
- `/en` — Switch back to English
- `/language reset` — Reply in the same language as each incoming message
</details>

> **Upgrade note:** English is now the default when no preference is set. Two
> cohorts change behavior on upgrade:
> - If you previously ran `/language reset` to reply in each message's own
>   language, that state was stored by deleting the preference file — now
>   indistinguishable from a fresh install and read as English. Re-run
>   `/language reset` once to restore input-language mode.
> - If you relied on a non-English `soul.md` for replies without ever setting
>   `/language`, replies are now enforced to English. Set your language
>   explicitly (e.g. `/french`) to keep it.

### System Management

**`/pause`** — Pause mission processing. Kōan stays running but won't pick up new missions.

- **Aliases:** `/sleep`

<details>
<summary>Use cases</summary>

- `/pause` — Temporarily stop mission work without shutting down
- Resume with `/resume` when ready
</details>

**`/resume`** — Resume mission processing after a pause (manual or automatic).

- **Aliases:** `/work`, `/awake`, `/run`, `/start`

<details>
<summary>Use cases</summary>

- `/resume` — Unpause after a manual `/pause` or quota exhaustion
</details>

**`/shutdown`** — Shutdown both the agent loop and the messaging bridge.

<details>
<summary>Use cases</summary>

- `/shutdown` — Gracefully stop everything (e.g., before system maintenance)
</details>

**`/update`** — Update to the latest commit on main, then restart.

- **Aliases:** `/upgrade`
- Pulls the latest code from upstream/main (fast-forward only) and restarts.
- Waits for the current mission to complete before pulling.
- If the update fails, Kōan still restarts (you asked for it).
- Use `/restart` if you just need a fresh start without pulling code.

**`/update_last_release`** — Update to the most recent release tag, then restart.

- Checks out the latest release tag instead of pulling the latest commit on main.
- Recommended when you want a stable, tagged release rather than the bleeding edge.
- When a new release tag is detected, Kōan's notification suggests this command.

<details>
<summary>Use cases</summary>

- `/update` — "Pull the latest code from main and restart"
- `/upgrade` — Same as `/update`
- `/update_last_release` — "Switch to the most recent tagged release"
</details>

**`/reset`** — Reset the run counter to 0 without restarting. If Kōan is paused because it reached `max_runs`, `/reset` also resumes execution.

<details>
<summary>Use cases</summary>

- `/reset` — Reset counter mid-session when you want more runs
- `/reset` — Resume from a max_runs pause without losing current state
</details>

**`/restart`** — Restart both agent and bridge processes without pulling new code.

<details>
<summary>Use cases</summary>

- `/restart` — Force a restart when Kōan is already up to date but you need a fresh start
</details>

**`/snapshot`** — Export memory state to a portable snapshot file for backup or migration.

<details>
<summary>Use cases</summary>

- `/snapshot` — Back up Kōan's memory before a major change
</details>

### Memory System

Kōan maintains persistent memory across sessions through several interconnected files:

- **`memory/summary.md`** — Global summary of learnings across all projects
- **`memory/projects/<name>/`** — Per-project learnings and context
- **`journal/YYYY-MM-DD/project.md`** — Daily logs of what Kōan did
- **`soul.md`** — Agent personality definition (see [Personality Customization](#personality-customization))

Memory is automatically compacted over time. Kōan uses it to build context for each mission, remembering past decisions, patterns, and mistakes.

#### Memory Compaction

Kōan runs automatic memory maintenance every 24 hours (configurable) during the startup cleanup cycle:

1. **Learnings dedup** — Removes exact-duplicate lines from `learnings.md` files
2. **Semantic compaction** — Uses Claude (lightweight model) to merge redundant entries, remove references to deleted code, and consolidate by topic. Cross-references the project's file tree to identify obsolete entries.
3. **Hard cap** — Safety-net truncation that keeps only the most recent entries if the file is still too large after compaction
4. **Global memory rotation** — Truncates append-only files (`personality-evolution.md`, `emotional-memory.md`) to prevent unbounded growth

Configure thresholds in `config.yaml`:

```yaml
memory:
  learnings_max_lines: 100        # Target after semantic compaction
  learnings_hard_cap: 200         # Absolute max (safety net)
  global_personality_max: 150     # Max lines for personality-evolution.md
  global_emotional_max: 100       # Max lines for emotional-memory.md
  compaction_interval_hours: 24   # How often cleanup runs
```

Manual compaction via CLI: `python3 memory_manager.py <instance_dir> compact-learnings [project]`

### Personality Customization

Edit `instance/soul.md` to define Kōan's personality. This file shapes how Kōan communicates, what tone it uses, and what personality traits it exhibits. It's loaded into every interaction.

The design principle: code is generic and open source; instance data (including personality) is private. Fork the repo, write your own soul.

### CI Check System

The CI check system monitors your PRs for CI failures and can automatically attempt fixes. It includes CI queue draining (after `/rebase`), auto-dispatch of fix missions, and the `/ci_check` skill. Enabled by default — disable to save tokens if you don't need CI monitoring.

```yaml
ci_check:
  enabled: true                     # Master switch (default: true)
  timeout: 3600                     # Per-step wall-clock cap in seconds (default: 3600).
                                    # Bounds a single CI-fix Claude step so a stalled fix
                                    # cannot hold the serial mission queue for the full
                                    # 2-hour skill_timeout.
  max_fix_attempts_per_mission: 1   # Claude fix attempts inside one /ci_check mission
                                    # (default: 1). Total per-PR retries are governed
                                    # separately by ci_fix_max_attempts and interleaved
                                    # with other work by the async monitor.
  idle_timeout: 600                 # Idle (between-output) watchdog in seconds for a
                                    # CI-fix step (default: first_output_timeout, 600).
                                    # Dedicated knob so tuning first_output_timeout does
                                    # not silently change CI-fix idle behavior. 0 disables.
```

The auto-injected `/ci_check` fix mission is queued **non-urgently**, so CI fixing never jumps ahead of your backlog; each mission does one bounded attempt and yields, so a failing PR cannot monopolize the queue.

When disabled, all CI-related automation is skipped: queue draining, CI dispatch, CI enqueue after rebase, and the `/ci_check` command returns an error.

### CI Dispatch

Kōan can automatically create fix missions when CI fails on its own PRs. When enabled, each iteration checks open Koan-authored PRs for failing check runs and inserts a fix mission with the failure log snippet. Dedup prevents re-dispatching the same failure. Only active when `ci_check.enabled` is true.

```yaml
ci_dispatch:
  enabled: true              # Master switch (default: false)
  cooldown_minutes: 30       # Min time between checks per project (default: 30)
  log_snippet_bytes: 4096    # Max CI log snippet in mission text (default: 4096, floored at 64)
  tracker_max_age_days: 30   # Prune dedup entries older than this (default: 30)
```

### Auto-Update

Kōan notifies you via Telegram when a new release tag appears upstream (throttled to 48 h). Run `/update_last_release` to switch to the tagged release, or `/update` to pull the latest commit on main.

Optionally, you can enable automatic pulling in `config.yaml`:

```yaml
auto_update:
  enabled: false          # Opt-in only — set to true to auto-pull
  check_interval: 10      # Check every N iterations (default: 10)
  notify: true            # Notify on Telegram before/after update
```

See [docs/operations/auto-update.md](../operations/auto-update.md) for details.

### Adding New Projects

**`/add_project`** — Clone a GitHub repo and add it to the workspace.

- **Usage:** `/add_project <github-url> [name]`
- **Aliases:** —

The repo is cloned into the **active workspace directory** — `instance/workspace/`
on hosted deploys (persistent volume), or `workspace/` on local installs — and
registered via auto-discovery. No manual `projects.yaml` edit is required; the
project appears in `/projects` immediately.

<details>
<summary>Use cases</summary>

- `/add_project https://github.com/org/new-repo` — Add a new repo for Kōan to manage
- `/add_project https://github.com/org/new-repo myproject` — Add with a custom name
</details>

### Removing Projects

**`/delete_project`** — Remove a project from the workspace.

- **Usage:** `/delete_project <project-name>`
- **Aliases:** `/delete`, `/del`

<details>
<summary>Use cases</summary>

- `/delete_project myrepo` — Remove a project directory and its projects.yaml entry
- `/del myrepo` — Same, using short alias
</details>

### Renaming Projects

**`/rename`** — Rename a project across all configuration and instance files.

- **Usage:** `/rename <old_name> <new_name>`
- **Aliases:** `/rename_project`

<details>
<summary>Use cases</summary>

- `/rename anantys-back aback` — Rename a project everywhere (projects.yaml, memory, journals, instance files)
- `/rename my-long-project mlp` — Shorten a project name for easier typing
</details>

### Performance Profiling

**`/profile`** — Queue a performance profiling mission for a project.

- **Usage:** `/profile <project-name-or-pr-url>`
- **Aliases:** `/perf`, `/benchmark`
- **GitHub @mention:** `@koan-bot /profile` on a PR or issue

<details>
<summary>Use cases</summary>

- `/profile webapp` — Profile the webapp project for performance issues
- `/profile https://github.com/org/repo/pull/42` — Profile changes in a PR
</details>

### Tech Debt Scan

**`/tech_debt`** — Scan a project for duplicated code, complex functions, testing gaps, and infrastructure issues. Produces a prioritized debt register saved to project learnings, and optionally queues the top improvement missions.

- **Usage:** `/tech_debt [project-name] [--no-queue]`
- **Aliases:** `/td`, `/debt`

<details>
<summary>Use cases</summary>

- `/tech_debt koan` — Scan the koan project for tech debt
- `/td webapp --no-queue` — Scan without queuing follow-up missions
- `/debt` — Scan the default project
</details>

### Documentation Extraction

**`/doc`** — Investigate a project codebase and produce structured documentation files under docs/. Extracts architecture, code style, test patterns, anti-patterns, and recommended modules.

- **Usage:** `/doc <project-name> [categories] [--mode=create|update|replace]`
- **Aliases:** `/docs`
- **GitHub @mention:** `@koan-bot /doc` on an issue or PR
- Categories: architecture, code-style, test-style, anti-patterns, modules (comma-separated, default: all)

<details>
<summary>Use cases</summary>

- `/doc koan` — Extract all documentation categories for koan
- `/docs koan architecture,test-style` — Extract specific categories only
- `/doc webapp --mode=update` — Merge new findings into existing docs
- `/doc mylib --mode=replace` — Overwrite existing documentation
</details>

### Dead Code Scan

**`/dead_code`** — Scan a project for unused imports, functions, classes, variables, and dead branches. Produces a certainty-classified report saved to project memory, and optionally queues the top removal missions.

- **Usage:** `/dead_code [project-name] [--no-queue]`
- **Aliases:** `/dc`

<details>
<summary>Use cases</summary>

- `/dead_code koan` — Scan the koan project for unused code
- `/dc webapp --no-queue` — Scan without queuing follow-up missions
- `/dead_code` — Scan the default project
</details>

### Spec-Drift Audit

**`/spec_audit`** — Check that documentation (user-manual.md, github-commands.md, CLAUDE.md) stays in sync with the actual codebase. Produces a divergence report saved to project learnings, and queues fix missions for each finding.

- **Usage:** `/spec_audit [project-name]`
- **Aliases:** `/sa`, `/drift`

<details>
<summary>Use cases</summary>

- `/spec_audit koan` — Audit docs alignment for the koan project
- `/sa` — Audit the default project
- Set up as a recurring mission: `/weekly /spec_audit` for continuous drift detection
</details>

### Codebase Audit

**`/audit`** — Audit a project for optimizations, simplifications, and potential issues. Creates a GitHub issue for each finding with detailed problem description, impact analysis, suggested fix, and severity/effort classification.

- **Usage:** `/audit <project-name> [extra context] [limit=N]`
- **GitHub @mention:** `@koan-bot /audit` on an issue or PR
- Default: top 5 most important findings. Use `limit=N` to override.

<details>
<summary>Use cases</summary>

- `/audit koan` — Full audit of the koan project (top 5 findings)
- `/audit webapp focus on the auth module` — Audit with specific focus
- `/audit mylib look for performance bottlenecks limit=10` — Targeted audit with custom limit
</details>

Each finding becomes a GitHub issue with:
- **Problem** — What's wrong and why it matters
- **Why This Matters** — Impact on bugs, performance, or maintainability
- **Suggested Fix** — Concrete description of what to change
- **Details table** — Severity, category, location, and effort estimate

### Security Audit

**`/security_audit`** — Perform a security-focused SDLC audit of a project. Searches for critical vulnerabilities (injection, auth flaws, secrets exposure, path traversal, SSRF, insecure deserialization, etc.) and creates a GitHub issue for each finding.

- **Usage:** `/security_audit <project-name> [extra context] [limit=N]`
- **Aliases:** `/security`, `/secu`
- **GitHub @mention:** `@koan-bot /security_audit` on an issue or PR
- Default: top 5 most critical findings. Use `limit=N` to override.

<details>
<summary>Use cases</summary>

- `/security_audit koan` — Full security audit (top 5 critical findings)
- `/security myapp focus on the API endpoints` — Security audit with specific focus
- `/secu webapp limit=3` — Quick security scan with custom limit
</details>

Each finding becomes a GitHub issue with:
- **Problem** — The vulnerability and how it could be exploited
- **Why This Matters** — Real-world impact (data breach, RCE, privilege escalation)
- **Suggested Fix** — Concrete remediation steps
- **Details table** — Severity, category, location, and effort estimate

**Private Vulnerability Reporting (PVRS):** When the target repository has GitHub's Private Vulnerability Reporting enabled, critical and high severity findings are automatically submitted as private security advisories instead of public issues. This prevents disclosure of exploitable vulnerabilities before a fix is applied. Lower-severity findings still create public issues.

Configure PVRS behavior per-project in `projects.yaml`:

```yaml
defaults:
  security:
    pvrs: auto          # auto (detect), true (force), false (public only)
    pvrs_threshold: high # minimum severity for PVRS (critical, high, medium, low)
projects:
  myapp:
    security:
      pvrs: false  # always use public issues for this project
```

### Private Security Audit

**`/private_security_audit`** — Same security analysis as `/security_audit`, but findings are written **only** to today's project journal. Nothing is posted to GitHub: no public issues, no Private Vulnerability Reports. Use this when you want a security review without disclosing any details to GitHub — for example, while triaging a sensitive area before deciding what to share.

- **Usage:** `/private_security_audit <project-name> [extra context] [limit=N]`
- **Aliases:** `/private_security`, `/psecu`
- Default: top 5 most critical findings. Use `limit=N` to override.
- **Output:** appended to `instance/journal/<YYYY-MM-DD>/<project>.md` under a `🔒 Private Security Audit` heading, plus a summary file at `instance/memory/projects/<project>/private_security_audit.md`.

<details>
<summary>Use cases</summary>

- `/private_security_audit koan` — Full audit, findings stay local
- `/psecu webapp focus on token handling limit=3` — Focused review, kept off GitHub
</details>

### Full Audit Suite

**`/audit_all`** — Run `/security_audit`, `/dead_code`, and `/profile` in parallel. All three missions are batch-inserted atomically into the Pending queue, so they run as soon as slots are available without waiting for each other.

- **Usage:** `/audit_all [project-name]`
- **Alias:** `/aa`

<details>
<summary>Use cases</summary>

- `/audit_all koan` — Queue all three audit skills for the koan project
- `/aa` — Quick shortcut for the default project
</details>

### Incident Triage

**`/incident`** — Triage a production error from a stack trace or log snippet. Kōan will parse the error, identify the root cause, propose a fix with tests, and submit a draft PR.

- **Usage:** `/incident <error text or stack trace>`

<details>
<summary>Use cases</summary>

- `/incident TypeError: Cannot read property 'id' of undefined at UserService.getUser (user.js:42)` — Paste a stack trace and get a fix
</details>

### Interactive launcher (`make koan`)

`make koan` is the interactive way to start Kōan. On first launch, if no
`instance/` exists or onboarding progress is waiting in `.koan-onboarding.json`,
it runs the CLI onboarding wizard before starting anything. After setup, it
starts the stack and drops you straight into the terminal dashboard. The home
screen is the **Status** tab (KŌAN hero + live flags), alongside **Logs**,
**Config**, and **Usage** tabs.

Single-tap toggles (accent dot `◉` on / `○` off):

- **`w` — web dashboard**: start/stop the web UI and open your browser at
  `localhost:5001`.
- **`k` — keep awake**: runs `caffeinate -s` (macOS) or `systemd-inhibit`
  (Linux) so your machine doesn't sleep while Kōan works. On by default; tap
  `k` to turn it off.

Keys: `1`–`4` (or `s`/`l`/`u`/`c`) switch tabs; `m` queues a new mission; in
Config, arrows browse the tree, Enter edits a value, `t` toggles a boolean;
`p` pauses, `r` reloads. `d` **detaches** (closes the dashboard, leaves Kōan
running); `q` **quits** and stops Kōan (with a confirmation). When stdin is not
a TTY (services, CI, pipes)
`make koan` falls back to the headless path with no prompt. `make start` is
unchanged and remains the launcher used by services and scripts.

The terminal dashboard requires `textual` (installed by `make setup`); if it is
missing, Kōan stays running and you can follow it with `make logs`.

- `make logs` — watch Kōan work as a readable feed (🧠 thoughts, ✏️/📖/💻 tool
  calls, ↩ results); `make logs raw=1` shows the raw log stream.

### Web Dashboard

Run `make dashboard` to start a local web UI on port 5001. The dashboard provides:

- Real-time status overview
- Mission queue management
- Chat interface
- Journal browsing
- Real-time config sync

The dashboard binds to `localhost` only — not accessible from the network.

**Real-time config sync:** Edits to `instance/config.yaml` and
`instance/projects.yaml` are reflected on the Config page within ~2s without a
`make stop && make start` round-trip. Safe settings (`dashboard.nickname`,
`tools.*`, `automation_rules.*`, `messaging_level`, `verbose`) are already
re-read per use, so they toast "Settings updated" and stay live. Unsafe settings
(`cli_provider`, `models`, anything in `projects.yaml`, and any other key) show a
"Restart required" modal; the restart only runs when the agent is idle. Disable
with `config_sync: { enabled: false }`. See
[Real-time Config Sync](../operations/config-sync.md).

### Deployment

For advanced deployment scenarios, see the existing documentation:

- [Docker deployment](../setup/docker.md)
- [SSH tunnel setup](../setup/ssh-setup.md)
- [Always-up Railway deployment](../design/spec-always-up-railway.md)

### Contributing to Kōan's Own Docs

If you're working on Kōan's codebase (not just running it), `docs/` and `specs/` are
each an OKF-conformant knowledge bundle consulted via Claude Code's `/brain` project
skill (`ask`/`sync`/`ingest`/`lint`/`init`) — see `docs/SPEC.md`/`docs/SCHEMA.md` and
the root `CLAUDE.md`'s "Documentation first" section. This is a contributor-facing
convention, not a Kōan runtime command — it isn't triggerable from Telegram or GitHub.

---

## Quick Reference

All commands at a glance. **Tier:** B = Beginner, I = Intermediate, P = Power User.

| Command | Aliases | Tier | Description |
|---------|---------|:----:|-------------|
| `/mission <text>` | — | B | Queue a new mission (`--now` for top priority) |
| `/list [state]` | `/queue`, `/ls` | B | List missions; default pending+in-progress, or `done`/`failed`/`all` |
| `/cancel <n>` | `/remove`, `/clear` | B | Cancel a pending mission |
| `/abort` | — | B | Abort current mission, pick next pending |
| `/priority <n>` | — | B | Reorder a pending mission in the queue |
| `/status` | `/st` | B | Quick status overview |
| `/brief` | `/digest` | B | Daily digest — pending, completions, quota, journal highlights |
| `/ping` | — | B | Check if the agent loop is alive |
| `/usage` | — | B | Detailed quota and progress |
| `/metrics` | — | B | Mission success rates and reliability stats |
| `/live` | `/progress` | B | Show live progress of current mission |
| `/logs [run\|awake\|all]` | — | B | Show last 20 lines from logs (default: run) |
| `/check_notifications` | `/read` | B | Force immediate GitHub + Jira notification check |
| `/inbox` | — | B | Force GitHub notification check + show queued mail count (works while paused) |
| `/quota [N]` | `/q` | B | Check LLM quota (live), or override remaining % |
| `/chat <msg>` | — | B | Force chat mode (bypass mission detection) |
| `/gh` | — | B | Show GitHub CLI auth status |
| `/time` | `/date` | B | Show current server date and time |
| `/version` | `/ver`, `/v` | B | Show Kōan version |
| `/verbose` | — | B | Enable real-time progress updates |
| `/silent` | — | B | Disable real-time progress updates |
| `/messaging_level [debug\|normal]` | `/msglevel` | B | Show or set bridge verbosity (debug / normal) |
| `/projects` | `/proj` | B | List configured projects |
| `/tracker` | — | B | Show or set issue tracker routing |
| `/alias <proj> <short>` | — | B | Create project shortcut (e.g. /alias Template2 tt) |
| `/unalias <short>` | — | B | Remove a project alias |
| `/focus [duration]` | — | B | Lock agent to one project |
| `/unfocus` | — | B | Exit focus mode |
| `/passive [duration]` | — | B | Enter read-only passive mode |
| `/active` | — | B | Exit passive mode, resume execution |
| `/brainstorm <topic>` | — | I | Decompose topic into linked sub-issues + master issue |
| `/plan <desc>` | — | I | Create a structured implementation plan |
| `/deepplan <idea\|issue-url>` | `/deeplan` | I | Spec-first design: explore approaches, post spec, queue /plan |
| `/implement <issue>` | `/impl` | I | Implement a GitHub or Jira issue |
| `/fix <issue>` | — | I | Full bug-fix pipeline (understand → plan → test → fix → PR); a PR URL redirects to `/rebase --fix` |
| `/debug <issue>` | `/dbg` | I | Structured 4-step debug loop (reproduce → hypothesize → fix → verify) |
| `/review <PR> [PR ...] [--architecture] [--errors] [--bot-comments]` | `/rv`, `/rereview`, `/re_review` | I | Review one or more pull requests |
| `/explain <PR>` | `/xp` | I | Explain a PR in plain language with examples |
| `/refactor <desc>` | `/rf` | I | Targeted refactoring mission |
| `/ask <comment-url>` | `/question` | I | Ask a question about a PR/issue — posts AI reply to GitHub |
| `/rebase [--fix] <PR> [focus area]` | `/rb` | I | Rebase a PR onto its base branch (rebase-only by default); `--fix` (or trailing text) also addresses review feedback |
| `/reviewrebase <PR>` | `/rr` | I | Review then rebase a PR (combo: /review → /rebase --fix) |
| `/planimplement <issue>` | `/planimp`, `/planimpl`, `/planit`, `/plandoit` | I | Plan then implement an issue (combo) |
| `/squash <PR>` | `/sq` | I | Squash all PR commits into one clean commit |
| `/recreate <PR>` | `/rc` | I | Re-implement a PR from scratch |
| `/pr <PR>` | — | I | Review and update a GitHub PR |
| `/branches [project]` | `/br`, `/prs` | B | List koan branches + PRs with merge order |
| `/checkup` | `/checkprs` | B | Health-check all open PRs — auto-queue /rebase + /check |
| `/orphans <project>` | `/orphan` | B | Recover orphan branches — rebase + draft PR |
| `/check <url>` | `/inspect` | I | Run project health checks on a PR/issue |
| `/check_need <url>` | `/need`, `/needs` | I | Analyze if a PR/issue is still needed |
| `/ci_check <PR>` | — | I | Check and fix CI failures on a PR |
| `/diagnose [project]` | `/dx` | B | Analyze last failure and queue a fix attempt |
| `/gh_request <url> <text>` | — | I | Route natural-language GitHub request to the right skill |
| `/claudemd [project] [learnings]` | `/claude`, `/claude.md`, `/claude_md` | I | Refresh a project's CLAUDE.md, or sync Kōan's learnings into it |
| `/models` | `/model` | P | Show resolved model config for the active CLI provider |
| `/config_check` | `/cfgcheck`, `/configcheck` | P | Detect config.yaml drift against instance.example template |
| `/rescan` | `/rescan_heads` | P | Re-check all projects for remote HEAD branch changes |
| `/gha_audit [project]` | `/gha` | I | Audit GitHub Actions for security issues |
| `/changelog [project]` | `/changes` | I | Generate changelog from commits/journal |
| `/daily <text>` | — | I | Schedule a daily recurring mission |
| `/hourly <text>` | — | I | Schedule an hourly recurring mission |
| `/weekly <text>` | — | I | Schedule a weekly recurring mission |
| `/recurring` | — | I | List all recurring missions |
| `/recurring resume <n>` | — | I | Re-enable a disabled recurring mission |
| `/recurring run [n]` | — | I | Force an immediate run of a recurring mission |
| `/recurring pause <n>` | — | I | Disable a recurring mission without deleting |
| `/recurring cancel <n>` | — | I | Cancel a recurring mission |
| `/recurring days <n> <days>` | — | I | Set a day-of-week filter on a recurring mission |
| `/idea <text>` | `/buffer` | I | Add to the ideas backlog |
| `/ideas` | — | I | List all ideas |
| `/reflect <msg>` | `/think` | I | Write a reflection to the shared journal |
| `/journal` | `/log` | I | View journal entries |
| `/email` | — | I | Email digest status or test |
| `/stats [project]` | — | I | Session outcome statistics |
| `/report [--week\|--month]` | `/reports`, `/weekly_report`, `/monthly_report` | I | PR activity report (created, merged %, interacted) per-project + global; defaults to both weekly and monthly |
| `/done [project]` | `/merged` | I | List PRs merged in the last 24 hours |
| `/explore [project]` | `/exploration` | I | Enable/show exploration mode |
| `/noexplore [project]` | — | I | Disable exploration mode |
| `/autoreview [project]` | `/auto_review` | I | Enable/show autoreview mode (auto-queue review+rebase after PR) |
| `/noautoreview [project]` | — | I | Disable autoreview mode |
| `/ai [project]` | `/ia` | P | Queue an AI exploration mission |
| `/deep [project]` | — | P | Thorough autonomous deep exploration |
| `/magic [project]` | — | P | Instant creative exploration |
| `/sparring` | — | P | Strategic sparring session |
| `/language <lang>` | `/lng` | P | Set reply language |
| `/french` | `/fr`, `/francais`, `/français` | P | Switch to French |
| `/english` | `/en`, `/anglais` | P | Switch to English |
| `/pause` | `/sleep` | P | Pause mission processing |
| `/resume` | `/work`, `/awake`, `/run`, `/start` | P | Resume mission processing |
| `/shutdown` | — | P | Shutdown all processes |
| `/update` | `/upgrade` | P | Update to latest commit on main, restart |
| `/update_last_release` | — | P | Update to most recent release tag, restart |
| `/reset` | — | P | Reset run counter to 0 |
| `/restart` | — | P | Restart processes (no code pull) |
| `/snapshot` | — | P | Export memory state |
| `/add_project <url>` | `/add_project` | P | Add a project from GitHub |
| `/delete_project <name>` | `/delete`, `/del` | P | Remove a project from workspace |
| `/rename <old> <new>` | `/rename_project` | P | Rename a project everywhere |
| `/profile <project>` | `/perf`, `/benchmark` | P | Performance profiling mission |
| `/audit <project> [ctx] [limit=N]` | — | P | Audit project, create tracker issues (top N, default 5) |
| `/security_audit <project> [ctx] [limit=N]` | `/security`, `/secu` | P | Security audit, find critical vulnerabilities (top N, default 5) |
| `/private_security_audit <project> [ctx] [limit=N]` | `/private_security`, `/psecu` | P | Security audit, findings to journal only (no GitHub) |
| `/doc <project> [categories]` | `/docs` | P | Extract structured documentation to docs/ |
| `/tech_debt [project]` | `/td`, `/debt` | P | Scan project for tech debt |
| `/dead_code [project]` | `/dc` | P | Scan for unused code |
| `/spec_audit [project]` | `/sa`, `/drift` | P | Audit docs/code alignment, queue fix missions |
| `/audit_all [project]` | `/aa` | P | Run security_audit, dead_code, and profile in parallel |
| `/incident <error>` | — | P | Triage a production error |
| `/scaffold_skill <scope> <name> <desc>` | `/scaffold`, `/new_skill` | P | Generate SKILL.md + handler.py for a new custom skill |
| `/rtk [setup\|uninstall\|gain\|on\|off]` | — | P | Manage optional [rtk](https://github.com/rtk-ai/rtk) integration for compressed tool output (60-90 % token savings on Bash commands). See [docs/operations/rtk.md](../operations/rtk.md). |

Skills marked with GitHub @mention support: `/audit`, `/brainstorm`, `/debug`, `/doc`, `/fix`, `/implement`, `/plan`, `/profile`, `/rebase`, `/recreate`, `/refactor`, `/review`, `/security_audit`, `/gh_request`. See [GitHub Commands](../messaging/github-commands.md) for details.

---

*For the full command reference with tabular format, see [docs/users/skills.md](skills.md). For skill authoring, see [koan/skills/README.md](../../koan/skills/README.md).*
