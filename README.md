<p align="center">
  <img src="instance.example/avatar.png" alt="Kōan" width="180" />
</p>

<h1 align="center">Kōan</h1>

<p align="center">
  <strong>An autonomous AI agent that works while you sleep.</strong><br/>
  Turns idle Claude Max quota into code reviews, bug fixes, and strategic insights.
</p>

<p align="center">
  <a href="INSTALL.md"><strong>Install Guide</strong></a> &bull;
  <a href="docs/user-manual.md"><strong>User Manual</strong></a> &bull;
  <a href="docs/skills.md"><strong>Skills Reference</strong></a> &bull;
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#how-it-works">How It Works</a> &bull;
  <a href="#features">Features</a> &bull;
  <a href="#configuration">Configuration</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/tests-9000+-green.svg" alt="Tests" />
  <img src="https://img.shields.io/badge/skills-44-blueviolet.svg" alt="Skills" />
  <img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg" alt="License" />
</p>

---

> **New here?** Start with the [Install Guide](INSTALL.md) to get running in minutes, then read the [User Manual](docs/user-manual.md) for the full walkthrough. All 44 commands are documented in the [Skills Reference](docs/skills.md).

---

## What Is This?

You pay for AI coding quota. You use it 8 hours a day. The other 16? Wasted quota.

Koan fixes that. It's a background agent that runs on your machine, pulls tasks from a shared mission queue, executes them via your configured CLI provider (Claude Code, Codex, Copilot, or local), and reports back through Telegram or Slack. It writes code in isolated branches, never touches `main`, and waits for your review before anything ships.

**The agent proposes. The human decides.**

This isn't a chatbot wrapper. It's a collaborator with memory, personality, and opinions. It tracks its own learnings across sessions, evolves its working style, and writes a zen koan at the end of every run. Because why not.

## Quick Start

```bash
git clone https://github.com/sukria/koan.git
cd koan
make install    # Interactive web wizard — sets up everything
make start      # Launches the full stack
make logs       # Watch it work
```

On macOS, keep your machine awake while Koan runs:

```bash
caffeinate -s &
```

That's it. Send it a mission via Telegram: *"audit the auth module for security issues"* — and go live your life.

For manual setup or advanced configuration, see [INSTALL.md](INSTALL.md).

## What Makes Koan Special

Koan isn't a chatbot wrapper or a code generator. It's the best AI *collaborator* for GitHub projects.

The difference is philosophical. [Vibe coding](https://alexissukrieh.com/blog/du-vibe-coding-a-l-agentic-coding/en/) is reactive — you talk, it responds. Agentic coding is something else entirely: the machine acts autonomously, within defined boundaries, with memory, personality, and objectives. It doesn't wait for your prompts. It works while you sleep.

What this means in practice:

- **It handles any number of GitHub projects, on its own, without ever overstepping its scope.** Bug fixes, code reviews, rebasing, feature planning — across all your repos, with per-project memory and configuration. ([See this comparison](https://www.linkedin.com/feed/update/urn:li:activity:7436096761732956160/))
- **It grows with you.** Koan maintains persistent memory across sessions, accumulates learnings per project, and improves its own codebase. It has opinions. It disagrees when it thinks you're wrong. It spots bugs and proposes features you didn't ask for.
- **Safety is built in, not bolted on.** It never commits to `main`. It never deploys. It always creates draft PRs and waits for your review. This is a highly productive collaborator, not an unsupervised automation.
- **It turns idle quota into output.** You pay for Claude Max 24 hours a day but use it for 8. Koan uses the other 16 — continuously, autonomously, at high velocity.

*The agent proposes. The human decides.*

### How Koan Compares

The autonomous coding agent space is evolving fast. [OpenClaw](https://alexissukrieh.com/blog/du-vibe-coding-a-l-agentic-coding/en/) was the original inspiration — it proved that Claude Code could serve as a universal "brain" for local, autonomous task execution. [ZeroClaw](https://zeroclaw.net/) rewrote the concept from scratch in Rust, delivering a super generic, ultra-secure agent runtime that supports 40+ LLM providers and 15+ messaging channels. It's an impressive piece of infrastructure — [a notable player in autonomous agentic coding](https://www.linkedin.com/feed/update/urn:li:activity:7436096761732956160/).

But Koan takes a different path entirely.

| | **OpenClaw** | **ZeroClaw** | **Koan** |
|---|---|---|---|
| **What it is** | Node.js autonomous AI super-agent (278k+ stars) | Rust agent runtime (~3 MB binary) | Python AI collaborator for GitHub projects |
| **Philosophy** | General-purpose personal assistant — can do anything on your behalf | Generic, secure, vendor-agnostic infrastructure | Purpose-built GitHub collaborator — the agent proposes, the human decides |
| **GitHub integration** | Generic (shell/browser tools) | Generic (tool-based) | Native and deep — draft PRs, issue triage, @mention triggers, rebase, code review, branch isolation |
| **Multi-project** | Single workspace with multi-agent routing | Single workspace | Up to 50 projects with per-project memory, config, and smart rotation |
| **Getting started** | `npm install -g openclaw` + onboarding wizard | TOML config, pairing codes, allowlists | `make install` — interactive web wizard, ready in minutes |
| **Safety model** | Pairing codes, sandbox optional — but has shell access, browser control, and can send emails autonomously | Mandatory sandboxing, command allowlists, encrypted keys | Branch isolation, draft PRs only, never touches `main`, human review required |
| **Memory** | Local Markdown files, session persistence | Hybrid BM25/vector search, multiple backends | Markdown-based — per-project learnings, session journals, personality evolution. No database needed |
| **Communication** | 21+ channels (WhatsApp, Telegram, Slack, Discord, iMessage, Signal…) | 15+ channels (Telegram, Discord, Slack, iMessage…) | Telegram/Slack with personality-aware formatting, spontaneous messages, and verbose mode |
| **Quota awareness** | No | No | Adapts work depth to remaining API quota (DEEP → IMPLEMENT → REVIEW → WAIT) |
| **Extensibility** | 100+ AgentSkills, skill marketplace, 50+ integrations | Trait-based plugin system | 44 built-in skills + pluggable skill system (install from Git repos) |
| **Scope** | Everything — emails, web browsing, car negotiations, legal filings | Everything — any LLM task in any context | One thing, done right — autonomous GitHub collaboration |

OpenClaw and ZeroClaw are general-purpose autonomous agents that can do *anything* — browse the web, send emails, control your phone. Koan does one thing: **it's the best AI collaborator for your GitHub projects.** It understands your codebase, creates clean PRs, reviews code, plans features, and never oversteps its scope. No pairing codes, no allowlists, no TOML to hand-edit. Just `make install`, point it at your repos, and go live your life.

## How It Works

```
         You (Telegram/Slack)
              │
              ▼
    ┌─────────────────┐        ┌──────────────────┐
    │    awake.py      │◄──────►│   instance/      │
    │  (msg bridge)    │        │   missions.md    │
    └─────────────────┘        │   outbox.md      │
                               │   config.yaml    │
                               └────────┬─────────┘
                                        │
                               ┌────────▼─────────┐
                               │     run.py        │
                               │  (agent loop)     │
                               └────────┬─────────┘
                                        │
                               ┌────────▼─────────┐
                               │  Your Projects    │
                               │  (koan/* branches) │
                               └──────────────────┘
```

Two processes run in parallel:

- **Bridge** (`make awake`) — Polls your messaging platform. Classifies incoming messages as *chat* (instant reply) or *mission* (queued for deep work). Formats outgoing messages through Claude with personality context.
- **Agent loop** (`make run`) — Picks the next mission, executes it via the configured CLI provider, writes journal entries, pushes branches, creates draft PRs. Adapts its work intensity based on remaining API quota.

Communication happens through shared markdown files in `instance/` — atomic writes, file locks, no database needed.

## Features

### Core

- **Multi-project support** — Up to 50 projects with per-project config, memory isolation, and smart rotation
- **Mission lifecycle** — Pending → In Progress → Done/Failed with crash recovery and stale-mission cleanup
- **Budget-aware modes** — Automatically adapts work depth based on remaining API quota:
  - **DEEP** (>40%) — Strategic work, thorough exploration
  - **IMPLEMENT** (15-40%) — Focused development, quick wins
  - **REVIEW** (<15%) — Read-only analysis, code audits
  - **WAIT** (<5%) — Graceful pause until quota resets

### Agent Intelligence

- **Smart mission picker** — Claude-based prioritization across projects (skips LLM call when trivial)
- **Persistent memory** — Session summaries, per-project learnings, personality evolution
- **Contemplative mode** — Occasional reflection sessions between missions (configurable probability)
- **Daily reports** — Digest messages at session boundaries
- **Post-mission reflection** — Writes deeper insights to a shared journal after significant work

### Git & GitHub

- **Branch isolation** — All work happens in `koan/*` branches. Never commits to `main`
- **Auto-merge** — Configurable per-project merge strategies (squash/merge/rebase)
- **Git sync awareness** — Tracks branch state, detects merges, reports sync status
- **GitHub integration** — Draft PRs, issue creation, PR reviews, rebasing — all via `gh` CLI
- **GitHub @mention triggers** — Koan responds to @mentions on issues and PRs

### Communication

- **Telegram & Slack** — Pluggable messaging with flood protection
- **Email digests** — Optional SMTP email notifications for session summaries (rate-limited, deduplicated)
- **Personality-aware formatting** — Every outbox message passes through Claude with soul + memory context
- **Verbose mode** — Real-time progress updates streamed to your phone
- **Spontaneous messages** — Koan occasionally initiates conversation when something feels worth saying

### Developer Experience

- **44 slash commands** — From `/plan` to `/review` to `/sparring` — see [Skills](#skills)
- **Web dashboard** — Local Flask UI for status, missions, chat, and journal browsing
- **Setup wizard** — Web-based guided setup (`make install`)
- **4500+ tests** — Comprehensive test suite with `make test`

## Skills

Skills are pluggable commands — some are instant, others spawn Claude work sessions. They're organized into groups, mirroring the `/help` output in Telegram:

**📋 missions** — Create, list, cancel missions
| `/mission` | Queue a new mission (`--now` to jump the queue) |
| `/list` | View current queue (alias: `/queue`, `/ls`) |
| `/cancel` | Remove a pending mission (alias: `/remove`) |
| `/priority` | Reorder a pending mission |
| `/live` | Watch real-time progress of the current mission |
| `/recurring` | Set hourly/daily/weekly repeating missions |
| `/chat` | Force chat mode for a message that looks like a mission |

**🔧 code** — Review, refactor, PR, fix, implement
| `/implement` | Write code for a feature or fix |
| `/plan` | Create an implementation plan |
| `/review` | Audit a pull request |
| `/fix` | Targeted bug fix |
| `/refactor` | Code cleanup and simplification |
| `/check` | Project health checks |
| `/claudemd` | Refresh a project's CLAUDE.md |
| `/dead_code` | Find unused code |
| `/tech_debt` | Technical debt report |
| `/profile` | Queue a performance profiling mission |
| `/scaffold_skill` | Generate a new skill from a description |

**🔀 pr** — Pull request management
| `/pr` | Create a pull request |
| `/rebase` | Rebase a PR onto its base branch |
| `/recreate` | Re-implement a PR from scratch on a fresh branch |

**📊 status** — System state, quota, logs
| `/status` | Quick system overview |
| `/quota` | API usage and budget breakdown |
| `/journal` | Read today's journal entries |
| `/stats` | Activity summary |
| `/snapshot` | Memory snapshot |
| `/doctor` | Run diagnostics |
| `/changelog` | Recent completed missions |

**⚙️ config** — Projects, language, focus, verbose
| `/projects` | List configured projects |
| `/focus` | Lock agent to one project |
| `/language` | Set output language |
| `/verbose` / `/silent` | Toggle real-time progress updates |
| `/explore` | Toggle per-project exploration mode |
| `/add_project` | Add a project to the registry |
| `/email` | Configure email digest notifications |

**💡 ideas** — Ideas, reflection, sparring
| `/idea` | Save an idea to the backlog |
| `/reflect` | Write a journal entry |
| `/sparring` | Strategic challenge — thinking, not code |
| `/ai` | Creative exploration prompt |
| `/magic` | Quick creative deep-dive on a project |

**🔄 system** — Pause, stop, update, restart
| `/shutdown` | Stop the agent |
| `/update` | Self-update Kōan from upstream |
| `/gha_audit` | Scan GitHub Actions for security vulnerabilities |
| `/incident` | Log an incident |

**[User Manual →](docs/user-manual.md)** — From beginner to power user, everything Kōan can do.

**[Full skills reference →](docs/skills.md)** — all 44 commands with aliases, descriptions, and usage details.

Skills are extensible — drop a `SKILL.md` in `instance/skills/` or install from a Git repo with `/skill install <url>`. See [koan/skills/README.md](koan/skills/README.md) for the authoring guide.

## Configuration

All behavioral config lives in `instance/config.yaml`. Secrets stay in `.env`.

```yaml
# How hard should Kōan work
max_runs_per_day: 10
interval_seconds: 60

# Model selection per role
models:
  mission: null        # Default (sonnet)
  chat: null           # Default (sonnet)
  lightweight: haiku   # Quick tasks (formatting, picking)

# Budget thresholds
budget:
  warn_at_percent: 20
  stop_at_percent: 5
```

### Multi-Project Setup

Define your projects in `projects.yaml` at `KOAN_ROOT`:

```yaml
defaults:
  git_auto_merge:
    enabled: false

projects:
  webapp:
    path: ~/Code/webapp
  api:
    path: ~/Code/api
    cli_provider: copilot    # Per-project provider override
    models:
      mission: opus
```

### Renaming a Project

To rename a project across `projects.yaml`, memory, journals, missions, and all instance files:

```bash
make rename-project old=webapp new=my-webapp          # dry-run (preview changes)
make rename-project old=webapp new=my-webapp apply=1   # apply changes
```

The tool updates the project key in `projects.yaml`, renames `memory/projects/<old>/` to `memory/projects/<new>/`, renames journal files (`journal/*/<old>.md`), and replaces `[project:<old>]` tags and `"project": "<old>"` references in all instance files.

### CLI Providers

Koan isn't locked to Claude. Swap the backend per-project:

| Provider | Best for |
|----------|----------|
| **Claude Code** (default) | Full-featured agent, best reasoning |
| **OpenAI Codex** | ChatGPT users (Plus/Pro/Business/Edu/Enterprise) |
| **GitHub Copilot** | Teams with existing Copilot licenses |
| **Local LLM** | Offline, privacy, zero API cost |

See provider guides:
- [docs/provider-claude.md](docs/provider-claude.md)
- [docs/provider-codex.md](docs/provider-codex.md)
- [docs/provider-copilot.md](docs/provider-copilot.md)
- [docs/provider-local.md](docs/provider-local.md)

## Architecture

```
koan/
  app/                    # Core Python modules (24K LOC)
    run.py                #   Main agent loop
    awake.py              #   Messaging bridge
    missions.py           #   Mission parsing & lifecycle
    mission_runner.py     #   Execution pipeline
    skill_dispatch.py     #   Direct skill execution
    memory_manager.py     #   Per-project memory isolation
    usage_tracker.py      #   Budget tracking & mode selection
    provider/             #   CLI provider abstraction
      claude.py           #     Claude Code CLI
      codex.py            #     OpenAI Codex CLI
      copilot.py          #     GitHub Copilot CLI
      local.py            #     Local LLM backends
  skills/                 # Pluggable command system (44 core skills)
  system-prompts/         # All LLM prompts (20 files, no inline prompts)
  templates/              # Dashboard Jinja2 templates
  tests/                  # 4500+ tests (pytest)
instance/                 # Your private data (gitignored)
  soul.md                 #   Agent personality — this is who Kōan is
  missions.md             #   Task queue
  config.yaml             #   Behavioral settings
  memory/                 #   Persistent context across sessions
  journal/                #   Daily logs (YYYY-MM-DD/project.md)
```

**Design principle:** Code is generic and open source. Instance data is private. Fork the repo, write your own soul.

## Make Targets

| Target | Description |
|--------|-------------|
| `make install` | Interactive web-based setup wizard |
| `make start` | Start full stack (agent + bridge) |
| `make logs` | Tail live output from all processes |
| `make stop` | Stop all processes |
| `make status` | Show running process status |
| `make dashboard` | Web UI (port 5001) |
| `make test` | Run test suite |
| `make say m="..."` | Send a test message |
| `make rename-project old=X new=Y` | Rename a project everywhere (dry-run by default, add `apply=1` to execute) |
| `make clean` | Remove virtualenv |

## Philosophy

Koan was born from a simple question: *what do you do with a Claude Max subscription when you're not at your desk?*

The answer: you build a collaborator. Not an assistant — a sparring partner. One that reads your code before suggesting changes, tracks its own mistakes, and has the spine to say *"I think this is wrong"* when it means it.

It works in `koan/*` branches. It never merges to `main`. It writes a journal. It evolves. And at the end of every session, it writes a koan — a zen question born from the work it just did. Because reflection matters more than velocity.

*The agent proposes. The human decides.*

## Security

Koan is designed for **local, single-user operation**. It is not a web service.

- All work happens in isolated `koan/*` branches — your `main` is never modified
- Chat tools are restricted (read-only) vs. mission tools (full access) to limit prompt injection surface
- Dashboard binds to `localhost` only — no external access by default
- Telegram/Slack auth uses platform-level identity verification

Do not expose Koan services to the public internet. For remote access, use SSH tunnels.

## Contributing

Koan is open source under GPL-3.0. Contributions welcome.

```bash
make setup
make test   # Run the test suite
```

Check [CLAUDE.md](CLAUDE.md) for coding conventions and architecture details.

## AI Policy

This project uses AI tools to assist development. Humans review and approve every change before it is merged. See [AI_POLICY.md](AI_POLICY.md) for details.

## License

[GPL-3.0](LICENSE) — Free as in freedom.
