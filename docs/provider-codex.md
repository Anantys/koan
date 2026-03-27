# OpenAI Codex CLI Provider

The Codex provider lets Kōan use OpenAI's Codex CLI as the underlying
AI agent. This is useful if you have a ChatGPT Pro (or Plus/Business/
Enterprise) subscription and want to use Codex models (GPT-5.4,
GPT-5.3-Codex, etc.) for planning and autonomous work.

## Quick Setup

### 1. Install Codex CLI

```bash
# npm (all platforms)
npm install -g @openai/codex

# macOS (Homebrew)
brew install --cask codex

# Verify
codex --version
```

### 2. Authenticate

```bash
# Browser-based login (default)
codex

# API key login
printenv OPENAI_API_KEY | codex login --with-api-key

# Headless / SSH
codex login --device-auth
```

You need a ChatGPT account with an active subscription that includes
Codex access (Plus, Pro, Business, Edu, or Enterprise).

### 3. Configure Kōan

**Option A: config.yaml** (persistent)

```yaml
cli_provider: "codex"
```

**Option B: Environment variable** (per-session)

```bash
export KOAN_CLI_PROVIDER=codex
```

The env var overrides config.yaml if both are set.

### 4. Model Selection

Set the model in your config.yaml `models:` section. Codex models use
their full names:

```yaml
models:
  mission: "gpt-5.4"           # Main mission execution
  chat: "gpt-5.4-mini"         # Chat responses (faster, cheaper)
  lightweight: "gpt-5.4-mini"  # Low-cost calls
  fallback: ""                  # Not supported by Codex (ignored)
```

Available models (as of March 2026):
- `gpt-5.4` — Flagship frontier model (recommended)
- `gpt-5.4-mini` — Fast, cost-effective for lighter tasks
- `gpt-5.3-codex` — Industry-leading coding model
- `gpt-5.3-codex-spark` — Near-instant iteration (Pro only)

## How It Works

Kōan invokes Codex in **non-interactive mode** via `codex exec`:

```
codex --full-auto --model gpt-5.4 exec "Your prompt here"
```

This runs Codex as a scripted agent that reads the project, generates
a plan, executes it, and streams the result to stdout.

### Execution Modes

| Kōan Setting          | Codex Flag       | Behavior                        |
|-----------------------|------------------|---------------------------------|
| `skip_permissions: false` | `--full-auto`   | Workspace writes + on-request approvals |
| `skip_permissions: true`  | `--yolo`        | No approvals, no sandbox        |

### Feature Mapping

| Kōan Feature           | Codex Support | Notes                                   |
|------------------------|---------------|-----------------------------------------|
| Model selection        | ✅            | `--model` flag                          |
| Fallback model         | ❌            | Silently ignored                        |
| System prompt          | ⚠️            | Prepended to user prompt (no native flag) |
| Per-tool allow/disallow| ❌            | Codex uses sandbox policies instead     |
| Max turns              | ❌            | Codex exec runs to completion           |
| MCP servers            | ⚠️            | Configure in `~/.codex/config.toml`     |
| Plugin directories     | ❌            | Codex uses skills instead               |
| Output format (JSON)   | ⚠️            | Available but not used (Kōan expects text) |
| Quota check            | ✅            | Minimal probe via `codex exec "ok"`     |

## Per-Project Override

You can use Codex for specific projects while keeping Claude as the
default. In `projects.yaml`:

```yaml
projects:
  my-openai-project:
    path: "/path/to/project"
    cli_provider: "codex"
    models:
      mission: "gpt-5.4"
      chat: "gpt-5.4-mini"
```

## MCP Configuration

Codex configures MCP servers via `~/.codex/config.toml` (not CLI flags):

```toml
[mcp_servers.github]
command = ["npx", "-y", "@modelcontextprotocol/server-github"]
```

Kōan's `--mcp-config` flags are silently ignored when using the Codex
provider. Configure MCP servers directly in Codex's config.

## AGENTS.md

Codex reads `AGENTS.md` files from the project root (similar to
Claude's `CLAUDE.md`). If your project already has a `CLAUDE.md`,
consider symlinking or adapting it:

```bash
ln -s CLAUDE.md AGENTS.md
```

## Troubleshooting

### "codex: command not found"

Install the CLI: `npm install -g @openai/codex`

### Authentication errors

Re-authenticate: `codex login --device-auth`

### Rate limits

Codex shares quota with your ChatGPT subscription. If you hit limits,
Kōan's quota detection will pause and notify you.

### Tool restrictions not working

Codex does not support per-tool allow/disallow flags. Tool access is
controlled by sandbox policies. Use `skip_permissions: true` (maps to
`--yolo`) for full access, or the default `--full-auto` for workspace-
scoped writes.

### System prompt not taking effect

Codex does not have a `--append-system-prompt` flag. System prompts
are prepended to the user prompt as a workaround. This means they
don't benefit from Codex's separate instruction caching.
