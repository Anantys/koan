---
type: doc
title: "Jira Integration"
description: "Full reference for controlling Kōan via `@mention` commands in Jira issue comments, including project mapping, ADF parsing, and coexistence with GitHub."
tags: [messaging]
created: 2026-05-28
updated: 2026-09-09
---

# Jira Integration

Control Koan directly from Jira issue comments using `@mention` commands.

> **Introduced in**: commit `fd3ccf8`. Enhanced with Jira URL support in skills, comment acknowledgment, and per-project target branches.

## Overview

Koan can poll your Jira Cloud instance for @mentions in issue comments. When a user posts:

```
@koan-bot plan
```

...in a Jira issue comment, Koan detects the mention, validates the command and the user's permissions, and queues a mission — all without webhooks or external services.

Jira-originated missions are marked with 🎫 in the mission queue (vs 📬 for GitHub-originated missions), making it easy to trace where a mission came from.

> **Jira + GitHub**: Both integrations can run simultaneously. See [Running Both Integrations](#running-both-integrations) below.

## Quick Start

### 1. Get a Jira API token

1. Go to [https://id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click **Create API token**, give it a name (e.g. "Koan bot")
3. Copy the token

### 2. Configure Koan

In `instance/config.yaml`:

```yaml
jira:
  enabled: true
  base_url: "https://myorg.atlassian.net"
  email: "bot@example.com"
  nickname: "koan-bot"
  authorized_users: ["*"]
```

Set the API token via environment variable (recommended) or config:

```bash
# In .env
KOAN_JIRA_API_TOKEN=your-api-token-here
```

### 3. Map Jira projects to Koan projects

Jira project ownership lives in `projects.yaml`, one tracker config per Koan project:

```yaml
projects:
  myproject:
    path: "/path/to/myproject"
    github_url: "myorg/myproject"   # PRs are still created on GitHub
    issue_tracker:
      provider: jira
      jira_project: FOO             # FOO-123 → project "myproject"
      jira_issue_type: Task         # Default issue type when Koan creates issues
      default_branch: "11.126"      # Optional PR target branch
```

The project path can either be declared with `path:` or discovered from
`KOAN_ROOT/workspace/<project-name>`. Jira ownership still belongs in
`projects.yaml`, but workspace-discovered projects do not need a duplicate
path entry as long as the project name matches the workspace directory.

You can inspect or update this from Telegram:

```
/tracker
/tracker set myproject jira key:FOO type:Task branch:11.126
```

The older `instance/config.yaml jira.projects` mapping is ignored. Koan logs a warning, sends one Telegram warning, and `/config_check` reports it so operators can migrate the mapping into `projects.yaml`.

### 4. Post a command in a Jira issue comment

```
@koan-bot plan
```

Koan will:
1. Detect the @mention during its next polling cycle
2. Validate the command and user permissions
3. Create a pending mission: `- [project:myproject] /plan https://myorg.atlassian.net/browse/FOO-123 🎫`
4. Post a `👍 Mission queued: /plan` acknowledgment reply on the Jira comment
5. Send a Telegram notification confirming the mission was queued
6. Execute it in the next agent loop iteration — fetching the full Jira issue context (title, description, and all comments)

## Configuration Reference

All settings live under the `jira:` key in `instance/config.yaml`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Master switch for Jira integration |
| `base_url` | string | — | Jira instance URL (e.g. `https://myorg.atlassian.net`). **Required** when enabled |
| `email` | string | — | Atlassian account email for Basic auth. **Required** when enabled |
| `api_token` | string | — | Jira API token. Can also be set via `KOAN_JIRA_API_TOKEN` env var (takes precedence). **Required** when enabled |
| `nickname` | string | — | Bot's @mention name in Jira comments (without `@`). **Required** when enabled |
| `commands_enabled` | bool | `false` | Reserved for future per-command filtering |
| `authorized_users` | list | `[]` | `["*"]` = all users, or list of Jira account emails |
| `max_age_hours` | int | `24` | Ignore comments older than this (stale protection) |
| `notification_polling.check_interval_seconds` | int | `60` | Shared base polling interval in seconds (min: 10) |
| `notification_polling.max_check_interval_seconds` | int | `300` | Shared maximum backoff interval when idle (min: 30) |
| `jira.check_interval_seconds` | int | unset | Optional Jira-only override for the shared base interval |
| `jira.max_check_interval_seconds` | int | unset | Optional Jira-only override for the shared backoff cap |
| `max_issues_per_cycle` | int | `200` | Per-cycle cap on issues inspected for @mentions (min: 1). Each inspected issue triggers a separate `/comment` API call, so this directly bounds cold-start API consumption. A WARNING logs when the cap fires |
| `projects` | dict | `{}` | Deprecated and ignored. Use `projects.yaml issue_tracker.jira_project` instead. |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `KOAN_JIRA_API_TOKEN` | Jira API token (overrides `jira.api_token` in config) |

### Startup validation

When `jira.enabled: true`, Koan validates the configuration at startup and warns if any required field is missing (`base_url`, `email`, `api_token`, `nickname`). The integration is silently skipped if `enabled: false`.

## Available Commands

Jira reuses the same `github_enabled: true` skill flag for command discovery — **both GitHub and Jira discover the same command set**. No separate Jira flag is needed.
Argument validation still applies per command, so PR-only or comment-URL-only skills require a matching GitHub URL in the Jira comment context.

> **Custom skills under `instance/skills/<scope>/`** (e.g. a team-specific integration shipping `/my_fix` and `/my_plan`) are exposed here the same way: set `github_enabled: true` and `group: integrations` in their SKILL.md. Such skills with a `handler.py` are dispatched **in-process** by the Jira bridge — not queued as slash missions — and the handler automatically receives the originating Jira issue key in `ctx.args` when the commenter omitted one. See `koan/skills/README.md` for the full pattern.

| Command | Aliases | What it does | Context-aware |
|---------|---------|--------------|---------------|
| `ask` | — | Ask Koan a question about a Jira issue | **Yes** |
| `audit` | — | Audit a project codebase and create tracker issues | **Yes** |
| `brainstorm` | — | Decompose a topic into linked tracker issues | **Yes** |
| `deepplan` | `deeplan` | Spec-first design with Socratic exploration | **Yes** |
| `fix` | — | Fix an issue end-to-end | **Yes** |
| `gh_request` | — | Natural-language GitHub request dispatch | **Yes** |
| `implement` | `impl` | Implement an issue | **Yes** |
| `plan` | — | Deep-think and create a structured plan | **Yes** |
| `profile` | `perf`, `benchmark` | Queue a performance profiling mission | **Yes** |
| `rebase` | `rb` | Rebase a PR onto latest upstream | **Yes** |
| `recreate` | `rc` | Recreate a diverged PR from scratch | **Yes** |
| `refactor` | `rf` | Queue a refactoring mission | **Yes** |
| `review` | `rv` | Queue a code review mission | **Yes** |
| `reviewrebase` | `rr` | Review then rebase combo | **Yes** |
| `security_audit` | `security`, `secu` | Security-focused audit | **Yes** |
| `squash` | `sq` | Squash all PR commits into one | **Yes** |

For commands that require GitHub PR/comment URLs (for example `rebase`, `recreate`, `review`, `squash`, `ask`), include that GitHub URL explicitly in the Jira comment context.

### Context-aware commands

Commands with context awareness accept additional text after the command word:

```
@koan-bot implement phase 1 only
```

This creates a mission: `/implement https://myorg.atlassian.net/browse/FOO-123 phase 1 only`

### Project override with `repo:`

You can override the default project mapping using the `repo:` token:

```
@koan-bot plan repo:other-project focus on API layer
```

This routes the mission to `other-project` instead of the project mapped to the Jira issue's project key.

### Branch override with `branch:`

You can override the target branch for PRs using the `branch:` token:

```
@koan-bot fix branch:main
```

This takes highest priority — overriding both the per-project `issue_tracker.default_branch` configured in `projects.yaml` and the repository's default branch. Useful for one-off requests targeting a different release branch.

When a target branch is set (via config or override), the feature branch is created from it and the PR targets it with `--base`.

### Project tracker configuration

Each project can choose `github` or `jira` as its issue tracker in `projects.yaml`:

```yaml
projects:
  app:
    github_url: "myorg/app"
    issue_tracker:
      provider: jira
      jira_project: APP
      jira_issue_type: Task
      default_branch: "main"
```

For Jira-backed projects, `/plan <idea>`, `/brainstorm`, `/deepplan`, and audit issue creation post tracker issues in Jira. `/fix` and `/implement` still create GitHub draft PRs for code review, then comment the PR URL back on the Jira issue.

`/brainstorm` in particular creates its sub-issues and master tracking issue in
Jira with **rich ADF bodies**, resolves `SUB-N` cross-references to real Jira
keys, and **natively links** the master to each sub-issue — see
[Brainstorm on Jira](#brainstorm-on-jira). No `jira_issue_type` override is
needed; sub-issues use the project's configured `jira_issue_type`.

## How It Works

### Architecture

```
run.py                       ← Pre-iteration check (before plan_iteration)
loop_manager.py              ← Also polls during sleep cycle (throttled, after GitHub check)
  ↓
jira_notifications.py        ← Fetches & filters Jira comments, parses @mentions
  ↓
jira_command_handler.py      ← Validates commands, checks permissions, creates missions
  ↓
issue_tracker/config.py      ← Reads projects.yaml tracker ownership + branches
projects_merged.py           ← Merges projects.yaml with workspace/ projects
  ↓
skills.py                    ← Skill flags: github_enabled (reused for Jira)
```

### Notification processing flow

Jira notifications are checked in two places:
- **Pre-iteration**: At the start of each agent loop iteration (so `plan_iteration()` sees Jira missions immediately)
- **During sleep**: Between iterations (same as GitHub, with exponential backoff)

```
1. process_jira_notifications()
2. Build JQL query (POST /rest/api/3/search/jql): issues updated in projects registered in projects.yaml since last check
3. Paginate results using cursor-based nextPageToken
4. Fetch recent comments on matching issues
5. For each comment containing @nickname:
   a. Skip if already processed (in-memory set + .jira-processed.json)
   b. Skip if stale (> max_age_hours)
   c. Parse @mention → extract (command, context)
   d. Handle repo: override if present
   e. Handle branch: override if present (or use per-project config default)
   f. Validate command → skill must have github_enabled: true
   g. Check user permission → allowlist of Jira account emails
   h. Insert mission into missions.md (with branch:X token if set)
   i. Mark comment as processed (in-memory + persistent tracker)
   j. Post 👍 acknowledgment reply on the Jira comment
   k. Notify via Telegram (🎫 emoji prefix)
```

### Multiple instances

When `enable_multiple_instances: true` is set, each Koan instance should declare only the Jira project keys it owns in that instance's `projects.yaml`. Jira polling searches only those registered keys. If Jira ever returns an issue whose project key is not registered to this instance, Koan skips it without acknowledging the comment, marking it processed, or queueing a mission, so another instance can handle it.

### ADF (Atlassian Document Format) handling

Jira Cloud stores comment bodies as ADF — a JSON tree format. Koan recursively extracts plain text from ADF nodes while skipping code blocks (`codeBlock`, `code`, `inlineCard`) to prevent false @mention matches inside code examples.

Both ADF (Jira Cloud) and plain text (Jira Server/older) formats are supported.

### GitHub alert syntax

GitHub-flavored alert blocks — `> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`,
`> [!WARNING]`, `> [!CAUTION]` — have no native Jira equivalent and would
otherwise render as literal `> [!WARNING]` text. When Koan translates a PR body
or `/plan` output into a Jira comment, it degrades each alert block into a plain
`TYPE: text` line (for example, `WARNING: Data loss possible`) so the note stays
readable. The detection is deliberately narrow — only the five canonical alert
keywords on an exact `> [!TYPE]` opener line are converted, so an ordinary
human-written Jira blockquote is left untouched. Alert syntax inside a fenced
code block is preserved verbatim (it is example text, not an alert), and
back-to-back alert blocks with no separating blank line are degraded
independently rather than merged.

### Rich Jira messages

Koan renders both Jira issue descriptions and comments as **rich ADF** through
the shared `markdown_to_adf()` converter. Headings, unordered and ordered
lists, checklists, rules, blockquotes, fenced or indented code blocks, inline
emphasis/code, explicit Markdown links, bare `http://` and `https://` URLs,
and simple GitHub-style tables become native Jira nodes. Bare URLs receive an
ADF `link` mark automatically, so acknowledgments, plans, errors, reviews, and
future message types do not need template-specific auto-link logic.

HTML comments are removed before ordinary Jira prose is converted. This
prevents internal markers such as `<!-- koan-jira-outcome:… -->` from becoming
visible text. Comment syntax inside inline code spans or fenced code blocks is
preserved verbatim because it is example code rather than hidden metadata.

Jira has no collapsible-section equivalent. Koan removes GitHub `<details>`
wrappers, renders their `<summary>` as a visible label, and keeps the contained
code expanded. GitHub alert blocks degrade to readable `TYPE: text` lines.
Unsupported or malformed Markdown remains readable paragraph text rather than
being discarded.

### Brainstorm on Jira

`/brainstorm` decomposes a topic into sub-issues under a master tracking issue on
whichever tracker the project uses. On Jira it produces the same "link them
properly together" result as GitHub, expressed with Jira's native mechanisms:

- **Rich bodies** — each sub-issue and the master render as ADF (see above).
- **Cross-references resolve to real keys** — `SUB-N` placeholders in a
  sub-issue body (used before the real issue keys are known) are rewritten to the
  actual created keys, e.g. `SUB-2` → `APP-4213`, via the neutral `update_issue`
  operation. On GitHub the same step rewrites `SUB-2` → `#4213`.
- **Native master↔sub links** — the master tracking issue is linked to each
  sub-issue through Jira "Linked issues" relationships (`link_issues`, POSTing
  `/rest/api/3/issueLink`). On GitHub this is a no-op — the master's task list and
  `#N` references already express the relationship.

Both reference-resolution and linking are best-effort: a failure on any single
issue is logged and skipped, never aborting the brainstorm run.

### Deduplication

Two-tier approach matching the GitHub integration pattern:

1. **In-memory BoundedSet**: Tracks processed comment IDs within a session (capped at 10,000 entries). Fast, but lost on restart.
2. **Persistent tracker**: `.jira-processed.json` in the instance directory. Loaded on startup, trimmed to 5,000 entries to prevent unbounded growth. Written via atomic file operations.

### Polling and backoff

| Condition | Check interval |
|-----------|---------------|
| Mentions found | `check_interval_seconds` (default: 60s) |
| 1 empty check | 2x base interval |
| 2 consecutive empty | 4x base interval |
| 3+ consecutive empty | `max_check_interval_seconds` cap (default: 300s) |

Backoff resets immediately when any mention is found. When `auto_pause` is
disabled and there is no work, the agent loop parks on the same backoff instead
of repeatedly re-planning before the next Jira poll is due.

## Jira Issue Context in Skills

When a mission originates from a Jira URL (e.g. `/fix https://myorg.atlassian.net/browse/FOO-123`), the skill runners (`/fix`, `/plan`, `/implement`) automatically detect the Jira URL and fetch full issue context from the Jira REST API:

- **Title**: Issue summary
- **Description**: Full issue body (converted from ADF to plain text)
- **All comments**: Every comment with author attribution (ADF to plain text)

This context is fed to Claude the same way GitHub issue context would be — the agent sees the complete Jira issue when working on the fix or plan.

Skills that accept GitHub issue/PR URLs also accept Jira browse URLs:
- `/fix https://myorg.atlassian.net/browse/FOO-123`
- `/plan https://myorg.atlassian.net/browse/FOO-123`
- `/implement https://myorg.atlassian.net/browse/FOO-123`

When the source is Jira, Koan fetches the Jira context through the issue tracker abstraction, creates the GitHub draft PR against the mapped project repo, and comments the PR link back on the Jira issue. Configure the repo with `github_url` or `submit_to_repository.repo` in `projects.yaml`.

For Jira-linked missions, Koan publishes one final status update per
`(issue, command)`:

- a successful PR outcome starts with `Kōan · draft pull request created`,
  renders mission/PR/target metadata as a bullet list, and labels the link
  `PR #N — <title>`;
- a failed PR outcome uses the same structured metadata and an actionable
  **Next** section;
- optional What/Why/How/Validation content is derived from the generated PR
  body as before.

Outcome idempotency is stored in the hidden Jira comment property
`koan.jira.outcome`; no bookkeeping marker is appended to the visible body.
On the first update after upgrading, Koan still recognizes an existing
`<!-- koan-jira-outcome:… -->` body marker, edits that comment in place without
the marker, and attaches the property. Because the property is now the
comment's only identity, Koan reads the comment back after each write and
reports the update only once the property is observed; a write Jira accepted
without storing the property is reported as unverified and logged, rather than
reported as a success that the next run would silently duplicate. If comment
lookup fails, Koan continues to fail closed and does not create a potentially
duplicate status comment.

For Jira `/plan` updates, Koan maintains one human-readable **current plan** comment (plain text adapted for Jira rendering), identified by a trailing `Koan current plan (rev <digest>)` footer. A plan too large for one Jira comment (over 29,000 characters) is published as consecutive `Part N of M` comments, each independently verified and footered `(rev <digest>, part N/M)`; because Jira's public REST API cannot create a reply under an existing comment, the parts are linked with `?focusedCommentId=` previous/next URLs instead of being threaded. Shrinking a plan retires the now-orphaned trailing parts rather than stranding them. Koan stages the plan on disk, retries the Jira write three times, and reports success only after reading back a comment carrying that revision — Jira's write endpoints return success for writes that never produce a visible comment, so an unverified write is treated as a failure. Because a failed comment *lookup* is indistinguishable from an empty issue, Koan never creates a comment on a lookup error; a flaky read path therefore cannot stack duplicates. A publish failure keeps the staged plan for a later retry instead of regenerating it, and the stage is dropped after three consecutive failed runs so a permanently undeliverable plan does not wedge the issue. The retry only replays the staged plan when the new `/plan` adds nothing — running `/plan <issue> <instructions>` (or passing a base branch) regenerates, since the staged copy predates those instructions; a replayed publish is reported as such rather than as a fresh plan. Koan posts an explicit failure status comment when plan generation itself fails.

## Security Model

### Authentication

Jira API calls use **HTTP Basic authentication** with your Atlassian account email and an API token. The token is never logged. It can be provided via:
- `KOAN_JIRA_API_TOKEN` environment variable (recommended)
- `jira.api_token` in config.yaml

### Permission checks

Every command goes through:

1. **Allowlist check**: The commenter's email must be in `authorized_users` (or wildcard `*` is set)
2. **Stale comment protection**: Comments older than `max_age_hours` are silently discarded

> **Note**: Unlike GitHub, Jira does not expose a "write access" check via its REST API. Permission control relies on the `authorized_users` allowlist. Use explicit email lists instead of `["*"]` for tighter security.

### Code block protection

@mentions inside Jira code blocks (`{code}...{code}`, `{{...}}`, `{noformat}...{noformat}`) are ignored, preventing accidental command triggers from code examples.

### JQL injection prevention

Jira project keys used in JQL queries are validated against a strict alphanumeric pattern (`^[A-Z0-9]+$`). Non-conforming keys are silently filtered out.

## Running Both Integrations

Jira and GitHub integrations are designed to coexist. They serve complementary roles:

| | GitHub | Jira |
|---|---|---|
| **Primary use** | Code-level actions (PR rebase, code review, implementation) | Issue tracking and project planning |
| **Trigger location** | PR/issue comments on GitHub | Issue comments on Jira |
| **Mission marker** | 📬 | 🎫 |
| **Auth method** | `gh` CLI + `GH_TOKEN` | HTTP Basic + API token |
| **Permission model** | Allowlist + GitHub write access check | Allowlist (email-based) |
| **Polling** | GitHub notifications API | JQL search + comment fetch |

### Combined configuration

```yaml
# GitHub integration
github:
  nickname: "koan-bot"
  commands_enabled: true
  authorized_users: ["*"]

# Jira integration
jira:
  enabled: true
  base_url: "https://myorg.atlassian.net"
  email: "bot@example.com"
  nickname: "koan-bot"
  authorized_users: ["*"]

# In projects.yaml
projects:
  myproject:
    github_url: "myorg/myproject"
    issue_tracker:
      provider: jira
      jira_project: PROJ
      default_branch: "main"
  infrastructure:
    github_url: "myorg/infrastructure"
    issue_tracker:
      provider: jira
      jira_project: INFRA
      default_branch: "11.126"
```

```bash
# In .env
GH_TOKEN=ghp_xxxx
KOAN_JIRA_API_TOKEN=xxxx
```

Both integrations poll independently during the agent's sleep cycle — GitHub notifications are checked first, then Jira. Each has its own backoff schedule. Missions from both sources enter the same `missions.md` queue and are processed identically by the agent loop.

### When to use which

- **GitHub @mentions**: Best for code-centric actions — rebasing a PR, reviewing a diff, implementing a specific issue with linked code context.
- **Jira @mentions**: Best for project-level planning — turning a Jira epic into implementation tasks, planning a feature described in a ticket, auditing code related to a Jira story.

Both can trigger the same set of commands. The difference is the context URL attached to the mission — a GitHub URL gives the agent direct access to diffs and PR metadata, while a Jira URL provides issue descriptions and comment threads.

## Troubleshooting

### Commands not being picked up

1. **Check feature is enabled**: `jira.enabled: true` in config.yaml
2. **Verify required fields**: `base_url`, `email`, `api_token`, and `nickname` must all be set. Check logs for startup validation warnings.
3. **Check project mapping**: The Jira issue's project key must be in `projects.yaml` under `issue_tracker.jira_project`. A comment on `FOO-123` requires a project mapped to `FOO`.
4. **Check polling**: Look for `[jira]` log entries in `make logs`. If you see "no recently-updated issues found", the JQL query isn't matching.
5. **Verify API access**: Test manually:
   ```bash
   curl -X POST -u "email@example.com:YOUR_API_TOKEN" \
     -H "Content-Type: application/json" \
     "https://myorg.atlassian.net/rest/api/3/search/jql" \
     -d '{"jql": "project = FOO", "maxResults": 1}'
   ```
   > **Note**: Jira Cloud deprecated `GET /rest/api/3/search` (returns HTTP 410). Koan uses `POST /rest/api/3/search/jql` with cursor-based pagination.

### Mission queued but not executed

The 🎫 mission was written to `missions.md`. Check:
- `instance/missions.md` — the mission should be in the Pending section
- Agent loop logs — the mission will be picked up in the next iteration
- Project name resolution — the `repo:` override or project mapping must point to a valid Koan project in `projects.yaml` or `KOAN_ROOT/workspace/`

### "No valid project keys after sanitization"

Jira project keys must be uppercase alphanumeric (e.g., `FOO`, `MYPROJ`). Keys with special characters are silently filtered out. Check your `projects.yaml` `issue_tracker.jira_project` values use valid keys.

### Duplicate missions after restart

Expected behavior. The in-memory processed set is lost on restart, but the persistent tracker (`.jira-processed.json`) prevents most duplicates. If a crash occurred between mission creation and tracker update, a duplicate may appear — it's harmless and the agent handles already-completed missions gracefully.

## Related

- [GitHub Notification Commands](github-commands.md) — GitHub @mention integration (complementary)
- [Messaging: Telegram](telegram.md) — Primary command interface
- [Messaging: Slack](slack.md) — Alternative messaging provider
- [Messaging: Matrix](matrix.md) — Alternative messaging provider
- [Skills Reference](../users/skills.md) — Full skill documentation
- [User Manual](../users/user-manual.md) — Complete usage guide
