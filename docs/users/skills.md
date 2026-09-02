---
type: doc
title: "Skills Reference"
description: "Complete reference for all Koan slash commands (mission management, code/PR operations, scheduling, status, configuration, and system commands) usable via Telegram, Slack, or GitHub @mentions."
tags: [users]
created: 2026-05-28
updated: 2026-09-10
---

# Skills Reference

> **New here?** Start with the [Quickstart](quickstart.md) — a 5-minute, copy-paste guide to the most-used commands across GitHub, Jira, and your messaging app.
>
> **For a guided introduction**, see the [User Manual](user-manual.md) — organized by skill level with use cases and workflow examples.

Complete reference for all Koan slash commands. Use these via Telegram, Slack, or GitHub @mentions.

> **Extensible:** Drop a `SKILL.md` in `instance/skills/` or install from a Git repo with `/skill install <url>`.
> See [koan/skills/README.md](../../koan/skills/README.md) for the authoring guide.

---

## Mission Management

| Command | Aliases | Description |
|---------|---------|-------------|
| `/mission <text>` | — | Queue a new mission. Use `--now` to prioritize |
| `/list [state]` | `/queue`, `/ls` | List missions; default pending+in-progress, or `done`/`failed`/`all` |
| `/priority <n> <pos>` | — | Reorder a pending mission in the queue |
| `/cancel <n or keyword>` | `/remove`, `/clear` | Cancel a pending mission |
| `/abort` | — | Abort the current in-progress mission |
| `/idea <text>` | `/ideas`, `/buffer` | Add to the ideas backlog (promote to mission later) |

## Recurring Missions

| Command | Aliases | Description |
|---------|---------|-------------|
| `/daily <text>` | — | Schedule a daily recurring mission |
| `/hourly <text>` | — | Schedule an hourly recurring mission |
| `/weekly <text>` | — | Schedule a weekly recurring mission |
| `/recurring` | — | List all recurring missions |
| `/recurring resume <n>` | — | Re-enable a disabled recurring mission |
| `/recurring run [n]` | — | Force an immediate run of a recurring mission |
| `/recurring pause <n>` | — | Disable a recurring mission without deleting |
| `/recurring cancel <n>` | — | Remove a recurring mission |
| `/recurring days <n> <days>` | — | Set a day-of-week filter on a recurring mission |

## Code & Project Operations

| Command | Aliases | Description | GitHub @mention |
|---------|---------|-------------|:-:|
| `/plan [--iterations N] <desc>` | — | Deep-think an idea, create a tracker issue with task-level plan (file map, checkbox steps, code blocks, self-review). `--iterations N` (1-5) runs N critique+refine rounds. Loads project MCP servers when `plan` is in `mcp_roles` (default). | — |
| `/deepplan <desc>` | `/deeplan` | Spec-first design: explore approaches, post spec, queue /plan | — |
| `/implement <issue>` | `/impl` | Queue implementation for a GitHub or Jira issue; creates a draft PR, then privately reviews/fixes Important+ findings by default | Yes |
| `/fix <issue>` | — | Diagnose → understand → plan → test → implement → submit PR, then privately reviews/fixes Important+ findings by default. Use `--skip-diagnose` to bypass the diagnostic. On a PR, including GitHub `@bot fix`, this is queued explicitly as `/rebase --fix`; bare `@bot rebase` remains a simple rebase. Trailing context is passed to the feedback prompt. | Yes |
| `/debug <issue>` | `/dbg` | Structured 4-step debug loop: reproduce → hypothesize → minimal fix → verify. Auto-queued when `/fix` fails (opt-in via `debug_escalation.on_fix_failure` in config.yaml) | Yes |
| `/review <PR> [PR ...] [--bot-comments]` | `/rv`, `/rereview`, `/re_review` | Review one or more pull requests; each URL queues a separate review mission. Reviews are read-only and run against a fresh checkout of the PR head: the tool set is a positive allowlist, every shell command is gated by a hook (read-only inspection only, one command per call), and a provider that cannot enforce that is refused rather than run unrestricted. `--bot-comments` triages bot findings | Yes |
| `/ultrareview <PR>` | `/urv` | Ultra-thorough review: architecture + silent-failure passes combined | Yes |
| `/explain <PR>` | `/xp` | Explain a PR's changes in plain language with examples and alternative approaches | Yes |
| `/rebase [--fix] <PR> [focus area]` | `/rb` | Rebase a PR onto its base branch. **By default rebases only.** Add `--fix` to also address review feedback (implied when you add a focus area or severity keyword after the URL); trailing text is threaded into the mission as focus context — including a commit-message-only request such as "rename the subject to PROJ-99", which is amended onto the branch and pushed like any other change. Conflict resolution has a 10-minute per-round budget before the existing recreate fallback. | Yes |
| `/squash <PR>` | `/sq` | Squash all PR commits into one clean commit | Yes |
| `/recreate <PR>` | `/rc` | Re-implement a PR from scratch on a fresh branch | Yes |
| `/refactor <desc>` | `/rf` | Targeted refactoring mission | Yes |
| `/check <url>` | `/inspect` | Run project health checks on a PR or issue (rebase, review, plan) | — |
| `/check_need <url>` | `/need`, `/needs` | Analyze if a PR or issue is still needed vs. current main | — |
| `/ci_check <PR>\|--enable\|--disable` | — | Check and fix CI failures on a PR (non-blocking, bounded, one attempt per mission); toggle CI system | — |
| `/pr <PR>` | — | Review and update a GitHub pull request | — |
| `/claudemd [project] [learnings]` | `/claude`, `/claude.md` | Refresh or create a project's CLAUDE.md, or sync Kōan's learnings into it | — |
| `/doc <project> [cats]` | `/docs` | Extract structured documentation to docs/ | Yes |
| `/profile <project>` | `/perf`, `/benchmark` | Performance profiling mission | Yes |

For URL-based `/plan`, `/deepplan`, `/implement`, and `/fix`, append `branch:<name>` to
override the base branch for that mission.

`plan` runs with the project's MCP servers loaded when `plan` is in `mcp_roles`
(the default), so it can consult MCP-backed trackers and knowledge bases. See
[docs/providers/claude.md](../providers/claude.md) → "Per-role MCP access".
Conversational roles (`chat`, `github_reply`) stay excluded by default; add them
to `mcp_roles` to opt in. `mcp_roles: []` is a kill switch for every role.

**Assumptions audit (advisory, never blocks):** before a `/plan` result is posted, a
lightweight auditor pressure-tests the plan's hidden assumptions; unverified-critical
ones are folded into the plan's *Open Questions* section so you can resolve them on
the issue. When `/implement` runs, the same audit's findings are injected into the
implementation context as "verify before coding" guidance — the mission always
proceeds. Toggle with `plan_review.assumptions_check` in `config.yaml` (default on).

The private post-PR review gate for `/fix`, `/implement`, and `/rebase` is
backend-only: it reuses `/review` analysis, fixes Blocking/Important findings
on the same branch, and does not post review comments or verdicts. It is
opt-in (disabled by default during the testing phase) — enable and configure it
with `private_review_gate` in `config.yaml` or `projects.yaml`.

`/review` (and the private gate) inject the project's filtered learnings and
human-curated context/priorities into the review prompt, ranked against the PR
content. Enable `review_memory` in `config.yaml` to also include recent typed
project memory (decisions, observations) from the SQLite memory index.

**Structural quality:** The main review passes (default, plan-aware, and
`--architecture`) also judge whether the change leaves the surrounding code
simpler or messier — spaghetti growth from ad-hoc branches bolted onto unrelated
flows, feature logic landing in a shared module, a bespoke helper duplicating a
canonical one, thin pass-through wrappers, and files the PR pushes past a healthy
size (the limit your repo's convention docs state, else ~1000 lines). Findings
name the structural remedy rather than a cosmetic one. The severity bar is unchanged: these are 🟢 Suggestions unless the reviewer
can name concrete harm, so they never block a merge on style alone.

**Inline comments (opt-in):** Set `review_inline_comments.enabled: true` in
`config.yaml` to also post each finding as an inline PR comment anchored to its
code location, in addition to the bucketed summary comment (which is unchanged).
Each inline thread shows the same severity marker (🔴/🟡/🟢) and the full finding
detail, so reviewers can react or resolve in place. Cap the volume with
`review_inline_comments.max_comments` (default 25). Re-running `/review` is
idempotent (already-anchored findings are skipped); multi-line findings anchor
to their full range; if all posts fail, you are notified. Disabled by default.

Summary findings receive one sequence of IDs across Blocking, Important,
Suggestions, and Silent Failure Analysis sections. Numbering never restarts at
a section boundary, so follow-up requests can identify any finding by ID alone.

**Verdict consistency:** A valid reflection pass is authoritative for the final
finding list. Findings rejected below the configured threshold stay hidden;
checklist references are remapped, and a failed checklist entry is removed when
all of its referenced findings were rejected. The formal APPROVE /
REQUEST_CHANGES state and its green/red/yellow alert are then derived from the
retained findings. If reflection returns malformed or inconsistent metadata,
Kōan preserves the primary review unchanged.

**Stale-HEAD alert:** the review comment's footer shows the commit it was run
against (`HEAD=<short-sha>`). A review can take minutes, and you may push (or
force-push) new commits meanwhile. If the branch tip moved between when the
review started and when the comment is posted, Kōan appends an **IMPORTANT**
banner to the comment noting that the findings predate your latest push — so you
don't act on stale feedback. Re-run `/review` to cover the new commits. Nothing
changes for the common case where the branch didn't move.

**Consistent reviews (no whiplash).** Re-running `/review` is stable, not a fresh
roll of the dice:

- **Unchanged code → reproduced.** If the PR head *and* base are unchanged since
  the last review and you ask for the same kind of review, Kōan reproduces the
  prior review instead of re-analysing (so the findings don't drift). Turn off via
  `review_consistency.reuse_enabled: false`.
- **Fix-and-push → no new complaints on untouched code.** After you fix findings
  and push, a re-review won't suddenly raise *new* non-critical issues on files you
  didn't touch — those should have been caught the first time (and the review now
  aims to catch everything in one pass). Only a genuine **critical** on pre-existing
  code still surfaces, marked `[Pre-Existing Issue]`. Turn off via
  `review_consistency.freeze_enabled: false`.

**Trustworthy 🟡 Important tier.** The yellow (blocking) tier is reserved for issues
that clearly should block merge; borderline "should-fix" items are demoted to 🟢
recommendations and don't block the PR. Issues that **predate your PR** are shown as
🟢 `[Pre-Existing Issue]` recommendations (a pre-existing critical keeps its severity
but is still labeled), so you're not blocked for code you didn't write.

**Your comments are honored.** On a re-review, if a human comment says a finding
should be ignored / isn't a problem, Kōan won't re-raise it as a blocker; if a
comment says "fix later", the finding becomes a non-blocking `[Deferred]`
recommendation. The change is always attributed to the commenter. Disable with
`review_dispositions.enabled: false`.

**Comprehensive mode (opt-in).** Set `review_discovery.enabled: true` (per-instance
or per-project) for a more thorough multi-perspective pass that catches more in a
single review (higher token/time cost). Off by default — the normal `/review` is
unchanged.

**Large diffs & partial-coverage warning:** Review diffs are packed to fit a
token budget by the diff compressor (`optimizations.review_compressor.token_budget`,
default 80,000 tokens — the single knob controlling review diff size). The
fetch-time character cap is *derived* from that budget (budget × 3.5 chars/token
× 4 headroom), so the compressor — not a blind character cut — decides coverage
on large-context models. If the compressor is disabled, a token-safe backstop
(budget × 3.5, no headroom) truncates the diff instead, so the size guard holds
in every config. Whenever any file is omitted (fetch-time backstop,
compressor packing, or trivial-file triage), the posted review opens with a
`⚠️ Partial review` block listing every omitted file, so partial coverage is
never silent.

**Pin files that must always be reviewed:** a target repo can commit a
`.koan/config.yaml` with `review.always_check: ["SKILL.md", "*.md", …]` to keep
matching files from ever being dropped by the compressor — useful for repos that
ship skills or docs. See [KOAN.md & the `.koan/` directory](koan-md.md#the-koanconfigyaml-file).

Skills marked **GitHub @mention** can be triggered by commenting `@koan-bot <command>` on a PR or issue. See [GitHub commands](../messaging/github-commands.md).

## PR Management

| Command | Aliases | Description | GitHub @mention |
|---------|---------|-------------|:-:|
| `/ask <comment-url>` | `/question` | Ask a question about a PR/issue — posts AI reply to GitHub | Yes |
| `/reviewrebase <PR>` | `/rr` | Review then rebase a PR (combo: /review → /rebase --fix, so the rebase addresses the fresh review) | Yes |
| `/planimplement <issue>` | `/planimp`, `/planimpl`, `/planit`, `/plandoit` | Plan then implement an issue (combo: /plan → /implement) | Yes |
| `/checkup` | `/checkprs` | Health-check all open PRs across projects — auto-queues `/rebase` on conflicts, `/check` on CI failures | — |
| `/branches [project]` | `/br`, `/prs` | List koan branches + open PRs with merge order | — |
| `/orphans <project>` | `/orphan` | Recover orphan branches — rebase onto main + draft PR | — |
| `/done [project]` | `/merged` | List PRs merged in the last 24 hours | — |
| `/diagnose [project]` | `/dx` | Find the last failed mission and queue a fix attempt | — |
| `/gh_request <url> <text>` | — | Route a natural-language GitHub request to the right skill | Yes |

## Exploration & Analysis

| Command | Aliases | Description | GitHub @mention |
|---------|---------|-------------|:-:|
| `/brainstorm <topic>` | — | Decompose topic into linked sub-issues + master tracking issue | Yes |
| `/ai <topic>` | `/ia` | Queue an AI exploration mission (deep, with codebase access) | — |
| `/deep [project] [focus]` | — | Thorough autonomous exploration with full tool access | — |
| `/magic <topic>` | — | Instant creative exploration (quick, no mission queue) | — |
| `/sparring` | — | Strategic challenge session — thinking, not code | — |
| `/audit <project>` | — | Audit project, create tracker issues for each finding (top 5) | Yes |
| `/security_audit <project>` | `/security`, `/secu` | Security audit, find critical vulnerabilities (top 5) | Yes |
| `/private_security_audit <project>` | `/private_security`, `/psecu` | Security audit, findings to journal only (no GitHub) | — |
| `/tech_debt [project]` | `/td`, `/debt` | Scan for duplicated code, complex functions, testing gaps | — |
| `/dead_code [project]` | `/dc` | Scan for unused imports, functions, classes, dead branches | — |
| `/spec_audit [project]` | `/sa`, `/drift` | Audit docs/code alignment, queue fix missions | — |
| `/gha_audit [project]` | `/gha` | Scan GitHub Actions workflows for security vulnerabilities | — |
| `/audit_all [project]` | `/aa` | Run security_audit, dead_code, and profile in parallel | — |
| `/changelog [project]` | `/changes` | Generate changelog from recent commits and journal entries | — |
| `/stats [project]` | — | Show session outcome statistics per project | — |

## Communication & Reflection

| Command | Aliases | Description |
|---------|---------|-------------|
| `/chat <msg>` | — | Force chat mode (bypass mission detection) |
| `/reflect <msg>` | `/think` | Write a reflection to the shared journal |
| `/journal [project] [date]` | `/log` | View journal entries |
| `/email` | — | Email status digest (use `/email test` to verify setup) |

## Status & Monitoring

| Command | Aliases | Description |
|---------|---------|-------------|
| `/status` | `/st` | Show agent state, missions, and loop health |
| `/brief` | `/digest` | Daily digest — pending, completions, quota, journal highlights |
| `/ping` | — | Check if the agent loop is alive |
| `/live` | `/progress` | Show live progress from the current run |
| `/logs [run\|awake\|all]` | — | Show last 20 lines from logs (default: run) |
| `/quota [N]` | `/q` | Check LLM quota (live), or override remaining % |
| `/usage` | — | Detailed quota and progress |
| `/metrics` | — | Mission success rates and reliability stats |
| `/report [--week\|--month]` | `/reports`, `/weekly_report`, `/monthly_report` | PR activity report (created, merged %, interacted) per-project + global; defaults to both weekly and monthly |
| `/doctor` | — | Diagnostic self-checks; `--fix` auto-repairs, `--full` adds connectivity |
| `/models` | `/model` | Show resolved model config for the active CLI provider |
| `/config_check` | `/cfgcheck`, `/configcheck` | Detect drift between instance/config.yaml and the template |
| `/check_notifications` | `/read` | Force immediate GitHub + Jira notification check |
| `/inbox` | — | Force GitHub notification check + show queued mail count (works while paused) |
| `/rescan` | `/rescan_heads` | Re-check all projects for remote HEAD branch changes |
| `/gh` | — | Show GitHub CLI auth status and connected user |
| `/time` | `/date` | Show current server date and time |
| `/version` | `/ver`, `/v` | Show Kōan version (tag, commit, commits ahead) |
| `/verbose` | — | Enable real-time progress updates |
| `/silent` | — | Disable real-time progress updates |
| `/messaging_level` | `/msglevel` | Show or set bridge verbosity (`debug` / `normal`) |

## Configuration

| Command | Aliases | Description |
|---------|---------|-------------|
| `/projects` | `/proj` | List configured projects |
| `/tracker` | — | Show or set per-project issue tracker routing |
| `/alias <proj> <short>` | — | Create project shortcut (e.g. `/alias Template2 tt`) |
| `/unalias <short>` | — | Remove a project alias |
| `/focus [duration]` | — | Lock the agent to one project (suppress exploration) |
| `/unfocus` | — | Exit focus mode |
| `/passive [duration]` | — | Enter read-only passive mode |
| `/active` | — | Exit passive mode, resume execution |
| `/explore [project\|all\|none]` | `/exploration`, `/noexplore [project\|all]` | Toggle per-project exploration mode; `all`/`none` also sets default for future projects |
| `/autoreview [project]` | `/auto_review`, `/noautoreview` | Toggle automatic review+rebase after PR creation per project |
| `/language <lang>` | `/lng`, `/fr`, `/en` | Set reply language preference |

## System

| Command | Aliases | Description |
|---------|---------|-------------|
| `/pause` | `/sleep` | Pause mission processing |
| `/resume` | `/work`, `/awake`, `/run`, `/start` | Resume mission processing |
| `/shutdown` | — | Shutdown both agent loop and messaging bridge |
| `/update` | `/upgrade` | Update to latest commit on main, restart |
| `/update_last_release` | — | Update to most recent release tag, restart |
| `/reset` | — | Reset run counter to 0 (resumes if paused by max_runs) |
| `/restart [--force]` | — | Restart processes (no code pull); `--force` kills the in-flight mission |
| `/snapshot` | — | Export memory state to a portable file |

## Project Management

| Command | Aliases | Description |
|---------|---------|-------------|
| `/add_project <url>` | — | Clone a GitHub repo and add it to the workspace |
| `/delete_project <name>` | `/delete`, `/del` | Remove a project from workspace |
| `/rename <old> <new>` | `/rename_project` | Rename a project everywhere (config, memory, journals) |

## Power Tools

| Command | Aliases | Description |
|---------|---------|-------------|
| `/incident <error>` | — | Triage a production error from a stack trace or log snippet |
| `/scaffold_skill <scope> <name> <desc>` | `/scaffold`, `/new_skill` | Generate SKILL.md + handler.py for a new custom skill |
| `/rtk [setup\|uninstall\|gain\|on\|off]` | — | Manage optional rtk integration for compressed tool output |
| `/ideas` | — | List all ideas in the backlog |

---

## Skill Types

- **Instant** (`worker: false`) — Executes immediately, returns a response. Examples: `/status`, `/list`, `/gha_audit`.
- **Worker** (`worker: true`) — Runs in a background thread (Claude calls, API requests). Examples: `/magic`, `/chat`, `/sparring`.
- **Hybrid** (`audience: hybrid`) — Available from both Telegram/Slack and as agent-dispatched skills. Examples: `/plan`, `/implement`, `/review`.

## Custom Skills

Install skills from Git repos:

```
/skill install https://github.com/your-org/koan-skills.git
/skill approve <scope> <fingerprint>
/skill update <scope>
/skill remove <scope>
```

New installs and `/scaffold_skill` output are **quarantined** behind an
approval gate — the registry will not load them until `/skill approve` is run
with the fingerprint shown in the install reply. Inspect the cloned files
before approving. Set `skills.allowed_hosts` in `config.yaml` to restrict
which Git hosts `/skill install` can fetch from.

Or create your own in `instance/skills/<scope>/<name>/` with a `SKILL.md` file. See [koan/skills/README.md](../../koan/skills/README.md) for the full authoring guide.
