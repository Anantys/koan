---
type: doc
title: "Claude Code CLI Provider"
description: "Setup and configuration guide for Kōan's default Claude Code CLI provider, including models, tools, per-role CLI config, MCP, and devcontainer mode."
tags: [providers]
created: 2026-05-28
updated: 2026-09-09
---

# Claude Code CLI Provider

The Claude Code CLI is Koan's default and most capable provider. It gives
the agent full access to Claude's reasoning, tool use, and multi-turn
conversation capabilities.

## Quick Setup

### 1. Install Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
```

Verify the installation:

```bash
claude --version
```

### 2. Authenticate

```bash
claude
```

Follow the interactive login flow. Once authenticated, your credentials
are stored in `~/.claude/` and persist across sessions.

### 3. Configure Koan

Claude is the default provider — no extra configuration is needed.
If you've previously changed the provider, set it back:

In `config.yaml`:

```yaml
cli_provider: "claude"
```

Or via environment variable (in `.env`):

```bash
KOAN_CLI_PROVIDER=claude
```

### 4. Verify

```bash
claude -p "Hello, what model are you?"
```

If this returns a response, you're ready to run Koan.

## Model Configuration

Koan uses different models for different tasks. Configure them in
`config.yaml`:

```yaml
models:
  mission: ""              # Main mission execution (empty = subscription default)
  chat: ""                 # Telegram/dashboard chat responses
  lightweight: "haiku"     # Low-cost calls: formatting, classification
  fallback: "sonnet"       # Fallback when primary model is overloaded
  review_mode: ""          # Override model for REVIEW mode
```

Empty strings use your subscription's default model. Common overrides:

| Use Case | Recommended Model | Why |
|----------|------------------|-----|
| Complex missions | `opus` | Best reasoning for architectural work |
| Cost-efficient missions | `sonnet` | Good balance for routine tasks |
| Chat responses | `haiku` | Fast, cheap for quick answers |
| Code review | `sonnet` | Sufficient for review, saves quota |

### Per-Project Model Overrides

Different projects can use different models. In `projects.yaml`:

```yaml
projects:
  critical-backend:
    path: "/path/to/backend"
    models:
      mission: "opus"         # Use Opus for complex backend work
      review_mode: "sonnet"   # Sonnet for reviews

  small-library:
    path: "/path/to/lib"
    models:
      mission: "sonnet"       # Sonnet is sufficient here
```

## Tool Configuration

Control which tools the agent can use:

```yaml
tools:
  chat: ["Read", "Glob", "Grep"]                          # Read-only for Telegram
  mission: ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]  # Full access for missions
```

Available tools: `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`.

### Per-Project Tool Restrictions

Restrict tools for sensitive repos in `projects.yaml`:

```yaml
projects:
  vendor-lib:
    path: "/path/to/vendor"
    tools:
      mission: ["Read", "Glob", "Grep"]  # Read-only — no modifications
```

## Advanced Configuration

### Custom CLI Binary

You can point Koan at a custom Claude-compatible binary instead of the
default `claude` command. Set `KOAN_CLAUDE_CLI_PATH` in your `.env`:

```bash
KOAN_CLAUDE_CLI_PATH=bin/my-claude-wrapper   # relative to KOAN_ROOT (portable)
# or
KOAN_CLAUDE_CLI_PATH=/path/to/my-claude-wrapper   # absolute
```

A relative value is resolved against `KOAN_ROOT`, so the config stays portable
across installs/copies without a hard-coded path. The custom binary must
accept the same CLI interface as `claude` (e.g., `my-wrapper --model <model>
-p "prompt"`). This is useful for:

- Using a custom `ANTHROPIC_BASE_URL` via a wrapper script
- Adding default arguments or environment variables
- Proxying through a custom API endpoint

When unset or empty, Koan uses the standard `claude` command from PATH.

When a custom path is set, `/status` and the startup banner (`make` / `make restart`)
show the binary name next to the provider (e.g. `claude (ollama-claude)`), so you can
confirm which wrapper is in use.

#### Per-role CLI provider (the `cli:` section)

`KOAN_CLAUDE_CLI_PATH` replaces the Claude binary for **every** call. To use a
**different provider or binary per mission role** — e.g. run routine work on
Codex but reviews on a paid Claude subscription — add a `cli:` section to
`config.yaml`, parallel to `models:`:

```yaml
cli:
  default:
    mission: codex                                  # routine missions → Codex
    chat: codex
    lightweight: codex
    review_mode: claude:/path/to/review-claude      # reviews → a pinned Claude binary
    reflect: claude
  fallback: claude                                  # used only when a role's CLI can't run
```

Each value is a provider **flavor** (`claude`, `codex`, `copilot`, `cline`,
`ollama-launch`) or `flavor:path`, where `path` is an absolute path or a path
relative to `KOAN_ROOT` (same rule as `KOAN_CLAUDE_CLI_PATH`). The roles are the
same as `models:`: `mission`, `chat`, `lightweight`, `review_mode`, `reflect`.

- **`review_mode`** drives `/review`, `/ultrareview`, and every internal review
  call (main pass, reflection, silent-failure hunter, bot-comment triage),
  replacing the old `KOAN_CLAUDE_CLI_FOR_REVIEW_PATH`.
- **Review footer attribution:** the signature Kōan appends to each review
  (`Automated review by Kōan (… · model …)`) names the CLI that actually ran.
  With `review_mode: claude:/root/.local/bin/claude-deep` it reads
  `claude-deep`, not `Claude`; with no custom path it falls back to the
  provider flavor. The same applies when `KOAN_CLAUDE_CLI_PATH` points the
  `claude` flavor at a wrapper.
- **Model coupling:** a role's *model* is read from that role's provider block —
  e.g. with `review_mode: claude`, the review model comes from
  `models.claude.review_mode` (falling back to `models.default.review_mode`). So
  `cli:` and `models:` compose: the provider you pick for a role selects which
  `models.<provider>.<role>` model applies.
- **`fallback`** is a single section-wide provider used only when a role's chosen
  CLI **can't run** — the binary is missing/misconfigured (and, on the mission
  path, when the CLI reports an auth failure) and no work was produced yet. Quota
  exhaustion still pauses Koan; transient errors still use the normal in-place retry.
- **Per-project overrides:** set a flat `cli:` block under a project in
  `projects.yaml` (e.g. `cli: {mission: claude}`) to override per project.
- **Absence = unchanged:** with no `cli:` section, every role uses the global
  provider (`cli_provider` / `KOAN_CLI_PROVIDER`), exactly as before.

`KOAN_CLAUDE_CLI_PATH` remains the default binary for the `claude` flavor when a
role selects `claude` without an explicit path, and `cli_provider` /
`KOAN_CLI_PROVIDER` remain the global default provider.

`cli_provider` additionally accepts the same `flavor:path` grammar as `cli:`, so
`cli_provider: "claude:/root/.local/bin/lmpanel-claude"` makes **every**
un-overridden role spawn that wrapper instead of bare `claude` — including the
paths that do not resolve a role (chat, rituals, outbox formatting). A path here
wins over `KOAN_CLAUDE_CLI_PATH`, matching how a per-role `claude:path` behaves.
`KOAN_CLI_PROVIDER` stays flavor-only; use `KOAN_CLAUDE_CLI_PATH` to pin a binary
from the environment.

> Tip: the ready-made wrappers in `bin/` (`zai-claude`, `oc-claude`,
> `ollama-claude`) make good `claude:path` targets when you want a role to run a
> Claude-compatible backend.

> **Running OpenRouter models through the Claude CLI?** See
> [openrouter.md](openrouter.md) — it uses this wrapper mechanism plus a local
> CCR router to make non-Anthropic OpenRouter models work in `-p` mode.

> **Using an OpenCode Go subscription?** See [opencode.md](opencode.md) — it
> ships a ready-made wrapper, `bin/oc-claude`, that routes the Claude CLI
> through the `ocgo` proxy. Set `KOAN_CLAUDE_CLI_PATH` to that script.

> **Running a local Ollama model through the Claude CLI?** See
> [ollama-wrapper.md](ollama-wrapper.md) — it ships a ready-made wrapper,
> `bin/ollama-claude`, that routes the Claude CLI through
> `ollama launch claude`. Set `KOAN_CLAUDE_CLI_PATH` to that script.

> **Using a Z.ai (GLM) subscription?** See [zai.md](zai.md) — it ships a
> ready-made wrapper, `bin/zai-claude`, that points the Claude CLI at Z.ai's
> Anthropic-compatible endpoint and maps Koan's model tiers (`haiku`/`sonnet`/
> `opus`) to GLM models. Set `KOAN_CLAUDE_CLI_PATH` to that script.

### MCP (Model Context Protocol) Servers

Claude Code supports MCP servers for extended capabilities (browser,
databases, APIs). Add MCP config file paths to `config.yaml`:

```yaml
# config.yaml — global MCP servers for all projects
mcp:
  configs:
    - "/path/to/mcp-config.json"
```

Per-project overrides are supported in `projects.yaml` — a project-level
`mcp` list replaces the global list entirely:

```yaml
# projects.yaml — project-specific MCP servers
projects:
  my-project:
    path: "/home/user/my-project"
    mcp:
      - "/path/to/project-specific-mcp.json"
```

Legacy `mcp: ["/path/to/mcp-config.json"]` syntax remains accepted. Mapping
syntax permits these provider inputs to coexist with Kōan's own opt-in
[MCP server](../operations/mcp-server.md), whose settings are `mcp.enabled` and
`mcp.tools_allow_destructive`.

The MCP config files use the standard Claude Code JSON format (same as
`~/.claude/mcp.json` or `--mcp-config` flag).

#### Permissions for MCP Tools

When Koan runs as a systemd service (or any non-interactive context),
Claude CLI cannot prompt for tool approval. MCP tools will be
**silently denied** unless pre-approved.

> **Note:** `skip_permissions: true` does **not** work when Koan runs
> as root — Claude CLI rejects `--dangerously-skip-permissions` with
> root/sudo privileges. Koan detects this and drops the flag from
> Claude CLI invocations, logging a warning (once per process) so you
> know the config value is not being honored. Other CLI providers are
> unaffected and honor the setting as usual. You must use the allowlist
> approach below.

To pre-approve MCP tools, create a `.claude/settings.local.json` file
**in the target project's root directory** (the `path` from
`projects.yaml`). This file is loaded by Claude CLI when it runs with
that project as its working directory.

Example — allowlisting the Atlassian MCP server's Jira tools:

```json
{
  "permissions": {
    "allow": [
      "mcp__atlassian__getAccessibleAtlassianResources",
      "mcp__atlassian__getJiraIssue",
      "mcp__atlassian__searchJiraIssuesUsingJql",
      "mcp__atlassian__getVisibleJiraProjects",
      "mcp__atlassian__getJiraIssueTypeMetaWithFields",
      "mcp__atlassian__getJiraProjectIssueTypesMetadata",
      "mcp__atlassian__createJiraIssue",
      "mcp__atlassian__editJiraIssue",
      "mcp__atlassian__addCommentToJiraIssue",
      "mcp__atlassian__getTransitionsForJiraIssue",
      "mcp__atlassian__transitionJiraIssue",
      "mcp__atlassian__lookupJiraAccountId",
      "mcp__atlassian__getIssueLinkTypes",
      "mcp__atlassian__createIssueLink",
      "mcp__atlassian__getJiraIssueRemoteIssueLinks",
      "mcp__atlassian__searchAtlassian",
      "mcp__atlassian__fetchAtlassian",
      "mcp__atlassian__atlassianUserInfo"
    ]
  }
}
```

The tool name format is `mcp__<server-name>__<toolName>` where
`<server-name>` matches the key in your MCP config JSON (e.g.,
`"atlassian"` in `~/.claude.json`). To find the exact tool names,
run Claude CLI interactively once — denied tools appear in the JSON
output under `permission_denials`.

**Setup checklist for each project using MCP:**

1. Add the MCP config path to `projects.yaml` (under the project's
   `mcp:` key) or globally under `mcp.configs` in `config.yaml`
2. Create `<project-path>/.claude/settings.local.json` with the
   tool allowlist
3. Restart Koan (`systemctl restart koan.service`)

### Per-role MCP access (`mcp_roles`)

MCP servers listed under `mcp.configs` are loaded per **execution role**, controlled by
`mcp_roles` (default `["mission", "contemplative", "plan"]`). This lets, e.g.,
`@bot plan` consult Jira/internal-docs MCP servers while conversational replies
stay isolated.

- **Kill switch**: `mcp_roles: []` → no runner passes `--mcp-config`.
- **Per-project**: a `mcp_roles` list in projects.yaml replaces the global list.
- **Tools still gated**: loading a server does not grant its tools. With
  `skip_permissions` unset, allow MCP tools by qualified name in the role's
  `tools:` list — e.g. `tools: { plan: [Read, Glob, Grep, "mcp__jira"] }` or a
  single tool `mcp__jira__search_issues`.
- **Excluded by default**: `chat` and `github_reply` handle untrusted input and
  are opt-in only — add them to `mcp_roles` to enable.

Secondary planning sub-agents (critic/improve/review/assumptions) get no MCP by
design; only primary plan generation loads it. Note they are *intended* to be
read-only but are not enforced as such unless the role resolves into
`READ_ONLY_ROLES` — passing a narrow `tools:` list pre-approves, it does not
withhold. See `specs/components/providers.md`.

### Read-only roles

A role in `READ_ONLY_ROLES` (currently `review_mode`, used by `/review`) is
built with:

| Flag | Purpose |
| --- | --- |
| `--tools Read,Glob,Grep,Bash` | Positive allowlist. Everything unnamed — `Write`, `Edit`, `MultiEdit`, `Task`, `Skill` — is withheld by construction. |
| `--disallowedTools Write,Edit,NotebookEdit` | Second layer, for a pinned older binary that ignores `--tools`. `Bash` is excluded here because the allowlist grants it deliberately. |
| `--strict-mcp-config` | `--tools` covers built-ins only; without this the role inherits the operator's user-level MCP servers. |
| `--settings <path>` | Installs the `PreToolUse` gate from `app.review_bash_guard`. `Bash` is never added to `--allowedTools`, so if the gate fails to load there is simply no shell. |
| `--setting-sources user` | The reviewed repository's `.claude/` and `CLAUDE.md` are untrusted and are not loaded. |

`--dangerously-skip-permissions` is never emitted for these roles, regardless of
`skip_permissions`, because it would bypass all of the above.

### Max Turns

The `max_turns` setting controls how many tool-use rounds Claude gets
per invocation. Koan sets sensible defaults per context (missions get
more turns than chat). You generally don't need to change this.

### Output Format

Claude Code supports JSON output (`--output-format json`) which Koan
uses internally for structured mission results. This is handled
automatically.

### Fallback Model

When the primary model is rate-limited or unavailable, Koan falls back
to the configured fallback model:

```yaml
models:
  fallback: "sonnet"  # Used when primary model is overloaded
```

This is a Claude-specific feature — other providers don't support it.

## Project context isolation (KOAN_ROOT sessions)

A deployed instance is a git clone of the Koan repo. Several **runtime**
CLI sessions intentionally use `cwd=KOAN_ROOT` (Telegram chat bridge,
dashboard web chat, contemplative, rituals, outbox formatting). Without
isolation, Claude Code would auto-load **project** scope from that directory:

- root `CLAUDE.md` / `AGENTS.md` (contributor instructions)
- `.claude/skills/` (e.g. `brain`, `speckit-*`)

That leaks contributor-only advice into operator-facing replies (e.g.
“run `/brain sync`”). See issue
[#2379](https://github.com/Anantys-oss/koan/issues/2379).

**Fix (CLI boundary, not filesystem quarantine):** those sessions pass
`project_context=False` into `build_full_command`, which for Claude
becomes:

```bash
claude … --setting-sources user
```

Omitting `project` (and `local`) skips project settings, project
`CLAUDE.md`, and project `.claude` skills. User-level prefs under
`~/.claude/` still apply.

**Mission / project work is unchanged.** When `cwd` is a project under
`workspace/` (normally its own git root), Claude loads **that** project's
`CLAUDE.md` as usual (`project_context` defaults to true). Projects
should be real git roots (or external checkouts); a non-git folder that
is only a subdirectory of the Koan clone can still walk up to KOAN_ROOT
and pick up contributor docs — keep projects as separate repos.

This approach does **not** move files on disk or use `git skip-worktree`
(contrast the alternative explored in PR #2384).

## Troubleshooting

### "claude: command not found"

The CLI is not installed or not in your PATH.

```bash
npm install -g @anthropic-ai/claude-code
```

If installed via a version manager (nvm, fnm), make sure the right
Node.js version is active.

### Authentication expired

Re-authenticate:

```bash
claude
```

Or check your credentials:

```bash
ls ~/.claude/
```

### Rate limiting / quota exhaustion

Koan monitors quota and pauses automatically when limits are approached.
Check your usage:

```bash
# Via Telegram
/quota

# Or check Claude's stats
claude usage
```

### "Reached max turns" errors

If you see this in logs, the agent ran out of allowed tool-use rounds.
This is normal for complex tasks — Koan handles it gracefully and
reports partial results.

---

## Devcontainer Mode

When a project has a `.devcontainer/` setup, Kōan can execute Claude inside
the devcontainer so the agent has access to the full runtime — Ruby, bundled
gems, databases, language toolchains, and anything else the container provides.

### Prerequisites

Install the devcontainer CLI:

```bash
npm install -g @devcontainers/cli
```

Verify:

```bash
devcontainer --version
```

### Configuration

In `projects.yaml`, set `devcontainer: true` for the project:

```yaml
projects:
  my-ruby-app:
    path: "/home/user/workspace/my-ruby-app"
    devcontainer: true
```

You can also set it in `defaults:` to enable it for all projects.

### What Kōan injects

Kōan never writes or modifies any files in your project. Before each mission it
passes CLI flags directly to `devcontainer up`:

- `--additional-features` — adds three features on top of whatever your devcontainer
  already has:
  - `ghcr.io/exciton/devcontainer-features/claude-code-config-bind-mount:latest` — bind-mounts `~/.claude` from the host and creates the in-container symlink (handled at container build time by this feature)
  - `ghcr.io/anthropics/devcontainer-features/claude-code:1` — installs Claude Code CLI
  - `ghcr.io/devcontainers/features/github-cli:1` — installs `gh` CLI (Claude uses it for PRs, issues, CI checks)
- `--mount` (×2) — bind-mounts two host directories into the container:
  - `KOAN_ROOT/instance/` → `/mnt/koan-instance` — agent memory, soul, missions
  - `KOAN_ROOT/devcontainer-tmp/` → `/mnt/koan-tmp` — temp files (system prompts, plugin dirs)

After the container starts, Kōan runs two post-start steps via `devcontainer exec`:

1. If a GitHub token is available and the tmp mount is in place: writes the token to a temp file inside `/mnt/koan-tmp/` and runs `gh auth login --with-token` inside the container to authenticate `gh`. The file is deleted immediately after.
2. Runs `gh auth setup-git` as the container user to configure the git HTTPS credential helper so `git push` works with the host's GitHub token.

The agent prompt uses container-native paths (`/mnt/koan-instance`, `/workspaces/<name>`) so Claude never references host-side paths it can't reach.

### How execution works

Before each mission on a devcontainer-enabled project, Kōan:

1. Creates `KOAN_ROOT/devcontainer-tmp/` if it doesn't exist
2. Runs `devcontainer up` with mounts and features (idempotent — reuses running containers)
3. Runs post-start credential setup: `gh auth login --with-token` (if a token is available) + `gh auth setup-git` (via `devcontainer exec`)
4. Runs the mission as: `devcontainer exec --workspace-folder <path> -- claude <args>`

### If the container was created before Kōan managed it

Mounts are only applied at container creation time. If you previously started the
devcontainer yourself (via VS Code or directly), those mounts won't be present.
Remove the old container to force recreation:

```bash
docker ps -a --filter label=devcontainer.local_folder=<absolute_project_path> --format '{{.ID}}'
docker rm <container_id>
```

The next mission run will recreate the container with the correct mounts.

### Fallback behaviour

If `devcontainer: true` is set but `.devcontainer/devcontainer.json` does not
exist in the project, Kōan logs a warning and falls back to running Claude on
the host. No error is raised — the mission proceeds normally.

### Known limitations

- **`KOAN_PYTHON -m app.issue_cli`** (used for Jira issue fetching) will not
  work inside a devcontainer in v1. Kōan's Python venv is not mounted in the
  container. All other operations (git, gh, project tooling) work normally.

### Out of scope

- Multi-container (docker-compose-based) devcontainer setups
- Parallel sessions (worktrees) inside devcontainers
- Auto-installing `@devcontainers/cli` — Kōan fails fast with a clear install
  hint if the CLI is not found
