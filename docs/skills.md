# Skills Reference

Complete reference for all Koan slash commands. Use these via Telegram, Slack, or GitHub @mentions.

> **Extensible:** Drop a `SKILL.md` in `instance/skills/` or install from a Git repo with `/skill install <url>`.
> See [koan/skills/README.md](../koan/skills/README.md) for the authoring guide.

---

## Mission Management

| Command | Aliases | Description |
|---------|---------|-------------|
| `/mission <text>` | — | Queue a new mission. Use `--now` to prioritize |
| `/list` | `/queue`, `/ls` | List pending and in-progress missions |
| `/priority <n> <pos>` | — | Reorder a pending mission in the queue |
| `/cancel <n or keyword>` | `/remove`, `/clear` | Cancel a pending mission |
| `/idea <text>` | `/ideas`, `/buffer` | Add to the ideas backlog (promote to mission later) |

## Recurring Missions

| Command | Aliases | Description |
|---------|---------|-------------|
| `/daily <text>` | — | Schedule a daily recurring mission |
| `/hourly <text>` | — | Schedule an hourly recurring mission |
| `/weekly <text>` | — | Schedule a weekly recurring mission |
| `/recurring` | — | List all recurring missions |
| `/cancel_recurring <n>` | `/cancel-recurring` | Remove a recurring mission |

## Code & Project Operations

| Command | Aliases | Description | GitHub @mention |
|---------|---------|-------------|:-:|
| `/plan <desc>` | — | Deep-think an idea, create a GitHub issue with structured plan | — |
| `/implement <issue>` | `/impl` | Queue implementation for a GitHub issue | Yes |
| `/fix <issue>` | — | Understand → plan → test → implement → submit PR | Yes |
| `/review <PR>` | `/rv` | Review a pull request | Yes |
| `/rebase <PR>` | `/rb` | Rebase a PR onto its base branch | Yes |
| `/recreate <PR>` | `/rc` | Re-implement a PR from scratch on a fresh branch | Yes |
| `/refactor <desc>` | `/rf` | Targeted refactoring mission | Yes |
| `/check <project>` | `/inspect` | Run project health checks (rebase, review, plan) | — |
| `/pr <PR>` | — | Review and update a GitHub pull request | — |
| `/claudemd [project]` | `/claude`, `/claude.md` | Refresh or create a project's CLAUDE.md | — |

Skills marked **GitHub @mention** can be triggered by commenting `@koan-bot <command>` on a PR or issue. See [github-commands.md](github-commands.md).

## Exploration & Analysis

| Command | Aliases | Description |
|---------|---------|-------------|
| `/ai <topic>` | `/ia` | Queue an AI exploration mission (deep, with codebase access) |
| `/magic <topic>` | — | Instant creative exploration (quick, no mission queue) |
| `/sparring` | — | Strategic challenge session — thinking, not code |
| `/gha-audit [project]` | `/gha` | Scan GitHub Actions workflows for security vulnerabilities |
| `/changelog [project]` | `/changes` | Generate changelog from recent commits and journal entries |
| `/stats` | — | Show session outcome statistics per project |

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
| `/status` | `/st`, `/ping`, `/usage`, `/metrics` | Show agent status, missions, and loop health |
| `/live` | `/progress` | Show live progress from the current run |
| `/quota` | `/q` | Check LLM quota (live, no cache) |

## Configuration

| Command | Aliases | Description |
|---------|---------|-------------|
| `/projects` | `/proj` | List configured projects |
| `/add_project <url>` | `/add-project` | Clone a GitHub repo and add it to the workspace |
| `/focus <project>` | — | Lock the agent to one project (suppress exploration) |
| `/unfocus` | — | Exit focus mode |
| `/explore [project]` | `/exploration`, `/noexplore` | Toggle per-project exploration mode |
| `/language <lang>` | `/lng`, `/fr`, `/en` | Set reply language preference |
| `/verbose` | — | Enable real-time progress updates |
| `/silent` | — | Disable real-time progress updates |

## System

| Command | Aliases | Description |
|---------|---------|-------------|
| `/shutdown` | — | Shutdown both agent loop and messaging bridge |
| `/update` | `/upgrade`, `/restart` | Update Koan to latest upstream and restart |
| `/start` | — | Start the agent loop |

---

## Skill Types

- **Instant** (`worker: false`) — Executes immediately, returns a response. Examples: `/status`, `/list`, `/gha-audit`.
- **Worker** (`worker: true`) — Runs in a background thread (Claude calls, API requests). Examples: `/magic`, `/chat`, `/sparring`.
- **Hybrid** (`audience: hybrid`) — Available from both Telegram/Slack and as agent-dispatched skills. Examples: `/plan`, `/implement`, `/review`.

## Custom Skills

Install skills from Git repos:

```
/skill install https://github.com/your-org/koan-skills.git
/skill update <scope>
/skill remove <scope>
```

Or create your own in `instance/skills/<scope>/<name>/` with a `SKILL.md` file. See [koan/skills/README.md](../koan/skills/README.md) for the full authoring guide.
