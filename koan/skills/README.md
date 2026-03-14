# Kōan Skills

Skills are self-contained plugins that add commands to Kōan's Telegram interface. Each skill lives in its own directory and declares its commands through a `SKILL.md` file.

## Directory layout

```
skills/
  <scope>/
    <skill-name>/
      SKILL.md        # Required — metadata + optional prompt body
      handler.py      # Optional — Python handler
```

**Scope** is the top-level grouping directory. Built-in skills use `core`. Custom skills use any scope name — typically matching your team or project.

```
skills/
  core/               # Built-in (ships with Kōan)
    status/
    idea/
    ...
  myteam/             # Custom scope (your own skills)
    deploy/
    oncall/
```

## SKILL.md format

Every skill needs a `SKILL.md` with YAML frontmatter:

```yaml
---
name: greet
scope: myteam
description: Send a greeting to the channel
version: 1.0.0
audience: bridge
commands:
  - name: greet
    description: Say hello
    aliases: [hi, hello]
  - name: goodbye
    description: Say goodbye
    aliases: []
handler: handler.py
---
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Skill identifier (matches directory name). **Must use underscores, not hyphens** — hyphens break Telegram command parsing. |
| `scope` | no | Defaults to parent directory name |
| `description` | no | One-line summary shown in `/help` |
| `version` | no | Semver, defaults to `0.0.0` |
| `commands` | no | List of commands this skill exposes |
| `handler` | no | Path to Python handler (relative to skill dir) |
| `worker` | no | Set to `true` for skills that block (call Claude, APIs, etc.) |
| `audience` | no | Who consumes this skill: `bridge`, `agent`, `command`, or `hybrid` (default: `bridge`) |
| `cli_skill` | no | Provider slash command to invoke (e.g. `audit`). Requires `audience: agent`. See [CLI skill bridge](#cli-skill-bridge). |
| `github_enabled` | no | Set to `true` to allow triggering via GitHub @mentions (default: `false`) |
| `github_context_aware` | no | Set to `true` if the skill accepts additional context after the command (default: `false`) |

### Audience

The `audience` field controls where a skill is available:

| Value | Description |
|-------|-------------|
| `bridge` | Telegram-only. Process control, quick checks, interactive commands. Default. |
| `agent` | Exposed to Claude Code CLI as a plugin skill. Auto-triggered by context during missions. |
| `command` | Exposed to Claude Code CLI as a slash command. Explicit invocation by the agent. |
| `hybrid` | Available in both worlds — Telegram command + Claude Code plugin. |

Example:

```yaml
---
name: refactor
audience: hybrid
description: Refactor and simplify code
---
```

Skills default to `bridge` when `audience` is omitted (backward compatible).

### GitHub @mention integration

Skills with `github_enabled: true` can be triggered via GitHub @mentions in PR/issue comments. When a user posts `@bot-nickname rebase` in a PR comment, Kōan creates a mission automatically.

```yaml
---
name: implement
github_enabled: true
github_context_aware: true  # Accepts extra context: "@bot implement phase 1"
---
```

Currently github-enabled skills: `rebase`, `recreate`, `implement`, `review`, `refactor`.

Configuration in `config.yaml`:
```yaml
github:
  nickname: "koan-bot"          # Required if enabled
  commands_enabled: true        # Master switch
  authorized_users: ["*"]       # "*" = all, or ["alice", "bob"]
```

### Commands

A single skill can expose multiple commands. Each command has:

- **`name`** — what the user types after `/` (e.g., `/greet`)
- **`description`** — shown in help listings
- **`aliases`** — alternative names (e.g., `/hi` resolves to the `greet` command)

### Prompt-only skills (no handler)

If you omit `handler`, the markdown body after the frontmatter is sent to Claude as a prompt:

```yaml
---
name: haiku
commands:
  - name: haiku
    description: Write a haiku about the current project
    aliases: []
---

Write a haiku about the project described in the soul file.
Keep it relevant to recent work from the journal.
```

## CLI skill bridge

The `cli_skill` field lets you expose any provider slash command (Claude Code, GitHub Copilot, or any other supported CLI) as a Kōan Telegram command — no handler code required.

**How it works:**

1. You type `/ops.audit my-project` in Telegram
2. Kōan recognises `audience: agent` and queues a mission: `- [project:my-project] /ops.audit`
3. The runner picks it up and translates it to `/audit` before invoking the Claude CLI
4. Claude runs `/audit` in the project directory, exactly as if you had typed it in the terminal

The `cli_skill` value is the bare command name without the leading `/`. It is provider-agnostic — Kōan simply passes it through to whichever CLI is configured for the project.

### Complete example — wrapping a custom `/audit` command

**Step 1 — Create the skill directory**

```bash
mkdir -p instance/skills/ops/audit
```

**Step 2 — Write the SKILL.md**

```
instance/skills/ops/audit/SKILL.md
```

```yaml
---
name: audit
scope: ops
description: Run a security and maintainability audit via /audit
version: 1.0.0
audience: agent
cli_skill: audit
commands:
  - name: audit
    description: Queue a security/maintainability audit for a project
    usage: /ops.audit <project> [args]
---
```

No `handler` field, no `handler.py` — the `cli_skill` field is all that's needed.

**Step 3 — Use it from Telegram**

```
/ops.audit my-project Run a deep scan on the auth module
```

Kōan replies:
```
✅ Mission queued (project: my-project):

/ops.audit Run a deep scan on the auth module
```

Everything after the project name is forwarded as-is to the CLI task. The runner executes the Claude CLI with task `/audit Run a deep scan on the auth module` inside `my-project`.

**Step 4 — (Optional) Create the `/audit` command for Claude Code**

If it doesn't exist yet, add the slash command in your project:

```bash
mkdir -p my-project/.claude/commands
cat > my-project/.claude/commands/audit.md << 'EOF'
Perform a comprehensive security and maintainability audit of this project.

Check for:
- Dependency vulnerabilities (outdated packages, known CVEs)
- Secrets or credentials accidentally committed
- Insecure patterns (hardcoded IPs, eval, shell injection, etc.)
- Docker and deployment security (root user, exposed ports, etc.)
- .gitignore coverage for sensitive files

Produce a severity-classified report (Critical / High / Medium / Low) with
actionable fixes for each finding.
EOF
```

Claude Code automatically discovers `.claude/commands/audit.md` as `/audit`.

### How project detection works

When you type `/ops.audit my-project extra args`, Kōan checks whether the first word (`my-project`) matches a known project in your `projects.yaml`. If it does, it prefixes the mission entry with `[project:my-project]` so the runner executes in the right directory. Otherwise all words are passed as arguments to the slash command.

```
/ops.audit my-project                              → [project:my-project] /ops.audit
/ops.audit my-project --deep                       → [project:my-project] /ops.audit --deep
/ops.audit my-project Run a deep scan on auth      → [project:my-project] /ops.audit Run a deep scan on auth
/ops.audit                                         → /ops.audit   (no project tag)
```

### Using with any provider slash command

`cli_skill` is not limited to custom commands. You can wrap any slash command your CLI provider supports:

```yaml
cli_skill: compact         # Claude Code built-in: /compact
cli_skill: maint-check     # custom command in .claude/commands/maint-check.md
cli_skill: security-scan   # any name your CLI recognises
```

The value is passed verbatim as the task text to the CLI, so it must match an existing command in the target project.

## Writing a handler

A handler is a Python module with a `handle(ctx)` function:

```python
def handle(ctx):
    """Handle the command. Return a string to send to Telegram."""
    if not ctx.args:
        return "Usage: /greet <name>"
    return f"Hello, {ctx.args}!"
```

### SkillContext

Every handler receives a `SkillContext` object:

| Attribute | Type | Description |
|-----------|------|-------------|
| `ctx.koan_root` | `Path` | Root koan directory |
| `ctx.instance_dir` | `Path` | `instance/` directory (runtime state) |
| `ctx.command_name` | `str` | The command that was invoked (e.g., `"greet"` or `"hi"`) |
| `ctx.args` | `str` | Everything after the command |
| `ctx.send_message` | `callable` | Send a message directly to Telegram |
| `ctx.handle_chat` | `callable` | Trigger a conversational Claude response |

### Return values

- Return a **string** — sent to Telegram as a reply
- Return **empty string** — signals "already handled, don't send anything"
- Return **None** — no message sent

### Conventions

- Use **lazy imports** inside `handle()` to avoid circular dependencies
- Access shared state via `ctx.instance_dir` (missions.md, soul.md, memory/, etc.)
- Use `fcntl.flock()` when reading/writing shared files concurrently
- Mark `worker: true` in SKILL.md if your handler blocks (API calls, subprocess, etc.)

## Skill prompts

Skills that need LLM prompt templates store them in a `prompts/` subdirectory:

```
skills/core/plan/
  SKILL.md
  handler.py
  prompts/
    plan.md          ← prompt template with {PLACEHOLDER} syntax
```

Load prompts with `load_skill_prompt()` from `app.prompts`:

```python
from pathlib import Path
from app.prompts import load_skill_prompt

prompt = load_skill_prompt(Path(__file__).parent, "plan", IDEA=idea, CONTEXT=context)
```

This looks for `<skill-dir>/prompts/<name>.md` first, then falls back to the global `system-prompts/` directory. Prompt-only skills (SKILL.md body, no handler) remain unaffected — this convention is only for handler-based skills with complex prompts.

Infrastructure prompts used by `koan/app/` modules stay in `koan/system-prompts/`.

## Loading custom skills

Kōan loads skills from two locations:

1. **`koan/skills/`** — built-in core skills (shipped with the repo)
2. **`instance/skills/`** — custom skills (gitignored, instance-specific)

Skills from `instance/skills/` are merged into the registry at startup. If a custom command name collides with a core one, the last-loaded wins.

## Installing skills from Git repos

Use `/skill install` from Telegram to install skills from a Git repository:

```
/skill install myorg/koan-skills-ops
/skill install https://github.com/team/skills.git ops
/skill install myorg/skills ops --ref=v1.0.0
```

This clones the repo into `instance/skills/<scope>/` and tracks it in `instance/skills.yaml`.

### Managing installed skills

```
/skill sources                — list installed sources with metadata
/skill update                 — update all installed sources
/skill update ops             — update a specific source
/skill remove ops             — remove an installed source
```

### Manual installation

You can also clone repos manually:

```bash
cd instance/skills/
git clone git@github.com:myorg/koan-skills-ops.git ops
```

Manually cloned repos work identically but won't be tracked in `skills.yaml` (no `/skill update` support).

### Organizing a shared skills repo

A skills repo is just a directory of skill subdirectories. Minimal structure:

```
koan-skills-ops/          # repo root = scope directory
  deploy/
    SKILL.md
    handler.py
  oncall/
    SKILL.md
  rollback/
    SKILL.md
    handler.py
  README.md               # optional, for humans
```

Each subdirectory follows the same `SKILL.md` + optional `handler.py` pattern. The scope name is determined by the directory name you clone/install into, not the repo name.

### Multiple shared repos

You can mix multiple repos under `instance/skills/`:

```
instance/
  skills/
    ops/                  # /skill install myorg/koan-skills-ops
      deploy/
      oncall/
    analytics/            # /skill install myorg/koan-skills-analytics
      report/
      dashboard/
    personal/             # your own local skills, no repo needed
      scratch/
```

Each top-level directory becomes its own scope. Skills are invocable directly (`/deploy`) or with explicit scope (`/ops.deploy`).

### Versioning

Skills declare their version in `SKILL.md` using semver:

```yaml
---
name: deploy
version: 2.1.0
---
```

Use `--ref=<tag>` with `/skill install` to pin to a specific version:

```
/skill install myorg/skills ops --ref=v2.1.0
```

Use `/skill update` to pull the latest from the tracked ref.

### Scoped commands

When command names conflict across scopes, use the fully qualified form:

```
/deploy              → first match wins
/ops.deploy          → explicitly from ops scope
/ops.deploy.rollback → subcommand form
```
