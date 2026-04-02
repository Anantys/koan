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

### MCP (Model Context Protocol) Servers

Claude Code supports MCP servers for extended capabilities (browser,
databases, APIs). Add MCP config file paths to `config.yaml`:

```yaml
# config.yaml — global MCP servers for all projects
mcp:
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

The MCP config files use the standard Claude Code JSON format (same as
`~/.claude/mcp.json` or `--mcp-config` flag).

#### Permissions for MCP Tools

When Koan runs as a systemd service (or any non-interactive context),
Claude CLI cannot prompt for tool approval. MCP tools will be
**silently denied** unless pre-approved.

> **Note:** `skip_permissions: true` does **not** work when Koan runs
> as root — Claude CLI rejects `--dangerously-skip-permissions` with
> root/sudo privileges. You must use the allowlist approach below.

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
   `mcp:` key) or globally in `config.yaml`
2. Create `<project-path>/.claude/settings.local.json` with the
   tool allowlist
3. Restart Koan (`systemctl restart koan.service`)

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
