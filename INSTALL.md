# Installation

## Quick Start (Wizard)

The easiest way to set up Kōan is with the interactive wizard:

```bash
git clone https://github.com/sukria/koan.git
cd koan
make setup
make install
```

This launches a web-based wizard that guides you through Telegram setup, project configuration, and validation. If you prefer manual setup, continue below.

## Docker

To run Koan in a Docker container (for server deployment or local isolation), see the [Docker Setup Guide](docs/docker.md).

## Prerequisites

- At least one supported CLI provider installed and authenticated:
  - [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
  - [OpenAI Codex CLI](https://github.com/openai/codex)
  - [GitHub Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli)
  - Local provider dependencies (see [docs/provider-local.md](docs/provider-local.md))
- Python 3.8+
- A Telegram account or a Slack workspace (for messaging)

## Recommended

- GitHub CLI `gh` installed and authenticated — see [Dedicated GitHub Identity](#dedicated-github-identity-recommended) for setting up a bot account

## LLM Providers

Koan supports multiple LLM providers. Claude Code CLI is the default and
most capable option. You can also use OpenAI Codex, GitHub Copilot, or a
local LLM server.

| Provider | Setup Guide | Best For |
|----------|------------|----------|
| **Claude Code** (default) | [docs/provider-claude.md](docs/provider-claude.md) | Full-featured agent with best reasoning |
| **OpenAI Codex** | [docs/provider-codex.md](docs/provider-codex.md) | ChatGPT users who want Codex models |
| **GitHub Copilot** | [docs/provider-copilot.md](docs/provider-copilot.md) | Teams with existing Copilot subscriptions |
| **Local LLM** | [docs/provider-local.md](docs/provider-local.md) | Offline use, privacy, zero API cost |

You can mix providers per project — see the individual guides for
per-project configuration via `projects.yaml`.

## Manual Setup

### 1. Clone and create your instance

```bash
git clone https://github.com/sukria/koan.git
cd koan
cp -r instance.example instance
```

The `instance/` directory is your private data — it's gitignored and never pushed to the Kōan repo. You can version it in a separate private repo if you want persistence.

### 2. Set up a messaging platform

Kōan supports **Telegram** (default) and **Slack** for communication. Follow the setup guide for your preferred platform:

| Platform | Setup Guide | Best For |
|----------|-------------|----------|
| **Telegram** (default) | [docs/messaging-telegram.md](docs/messaging-telegram.md) | Quick setup, works from any network |
| **Slack** | [docs/messaging-slack.md](docs/messaging-slack.md) | Team collaboration, workspace integration |

Both platforms are fully supported with the same feature set. Telegram is recommended for personal use (simpler setup), while Slack is ideal for team environments.

### 3. Set environment variables

```bash
cp env.example .env
```

Edit `.env` and fill in the credentials for your chosen messaging provider.

**For Telegram:**
```bash
KOAN_TELEGRAM_TOKEN=123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
KOAN_TELEGRAM_CHAT_ID=987654321
```

**For Slack:**
```bash
KOAN_MESSAGING_PROVIDER=slack
KOAN_SLACK_BOT_TOKEN=xoxb-your-bot-token
KOAN_SLACK_APP_TOKEN=xapp-your-app-token
KOAN_SLACK_CHANNEL_ID=C01234ABCD
```

The `.env` file is gitignored — your secrets stay local. See the provider-specific setup guides above for detailed instructions on obtaining these credentials.

### 4. Configure projects

**Recommended:** Use `projects.yaml` at your koan root:

```bash
cp projects.example.yaml projects.yaml
```

Edit `projects.yaml`:

```yaml
defaults:
  git_auto_merge:
    enabled: false
    base_branch: "main"
    strategy: "squash"

projects:
  myapp:
    path: "/Users/yourname/myapp"
  backend:
    path: "/Users/yourname/backend"
```

Each project only needs a `path`. All other fields are optional and inherit from `defaults`.

**Fallback:** You can also use the `KOAN_PROJECTS` env var in `.env`:

```bash
KOAN_PROJECTS=myapp:/Users/yourname/myapp;backend:/Users/yourname/backend
```

If `projects.yaml` exists, the env var is ignored. On first startup, Kōan will auto-migrate env vars to `projects.yaml`.

### 5. Set up project memory (optional but recommended)

If you're using multi-project mode, create a memory folder for each project:

```bash
# Copy the template for each project
cp -r instance/memory/projects/_template instance/memory/projects/myapp
```

Edit the files in `instance/memory/projects/myapp/` to describe your project's architecture. This helps Kōan understand your codebase faster.

### 6. Customize your agent (optional)

```bash
$EDITOR instance/soul.md    # Write your agent's personality
```

### 7. Email notifications (optional)

Koan can email you session digests when budget is exhausted. This uses standard SMTP — no extra dependencies.

**Step 1:** Enable email in `instance/config.yaml`:

```yaml
email:
  enabled: true          # Turn on email notifications
  max_per_day: 5         # Rate limit (rolling 24h window)
```

**Step 2:** Add SMTP credentials to `.env`:

```bash
# SMTP server (e.g., Gmail with an app password)
KOAN_SMTP_HOST=smtp.gmail.com
KOAN_SMTP_PORT=587
KOAN_SMTP_USER=koan-bot@gmail.com
KOAN_SMTP_PASSWORD=your-app-password-here

# Your email address (single recipient)
EMAIL_KOAN_OWNER=you@example.com
```

> **Gmail users:** You need an [App Password](https://support.google.com/accounts/answer/185833), not your regular password. Enable 2FA first, then generate an app password under Security → App Passwords.

**Step 3:** Test with the `/email test` command in Telegram.

Use `/email` (or `/email status`) to check configuration and sending stats at any time.

### 8. Install dependencies

```bash
make setup
```

This creates a `.venv/` and installs Python dependencies.

### 9. Run

```bash
# Terminal 1: Telegram bridge
make awake

# Terminal 2: Agent loop
make run
```

## Dedicated GitHub Identity (Recommended)

> **Linux users:** See [Running as a systemd service](#running-as-a-systemd-service-linux) below for automatic startup, restart-on-failure, and proper service management.

For full autonomy, Kōan should have its own GitHub account. This gives it a distinct identity for PRs, commits, and @mention commands — clearly separated from your personal account.

### 1. Create a GitHub account for the bot

- Go to [github.com/signup](https://github.com/signup) and create a new account (e.g., `yourname-koan`)
- Use a dedicated email address (or a Gmail alias like `yourname+koan@gmail.com`)
- Pick a short, recognizable username — this will appear in @mentions and commit history

### 2. Generate a classic Personal Access Token

> **Important:** Use a **classic** token, not a fine-grained token. Fine-grained tokens do not support the `notifications` scope, which is required for GitHub @mention commands.

On the bot's GitHub account:

1. **Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Click **"Generate new token (classic)"**
3. Set an expiration (or "No expiration" for convenience)
4. Select these scopes:

| Scope | Required | Purpose |
|-------|----------|---------|
| `repo` | Yes | Push branches, create PRs and issues |
| `notifications` | Yes | Poll @mention notifications |
| `workflow` | Optional | Trigger GitHub Actions on push |

5. Click **Generate token** and copy the `ghp_...` token

### 3. Authenticate `gh` CLI with the bot identity

```bash
echo "ghp_YOUR_TOKEN_HERE" | gh auth login --user yourname-koan --with-token
```

No output means success. Verify both accounts are registered:

```bash
gh auth status
```

You should see both your personal account and the bot account:

```
github.com
  ✓ Logged in to github.com account yourname-koan (keyring)
  - Active account: true
  - Token: ghp_****
  - Token scopes: 'notifications', 'repo'

  ✓ Logged in to github.com account yourname (keyring)
  - Active account: false
  - Token: gho_****
```

### 4. Invite the bot as a collaborator

On each repository Kōan should work on:

1. **Settings → Collaborators → Add people**
2. Invite the bot account with **Write** access (or **Maintain** if you want it to merge PRs)
3. Accept the invitation from the bot account

### 5. Configure Kōan

**In `.env`:**

```bash
GITHUB_USER=yourname-koan
KOAN_EMAIL=yourname-koan@users.noreply.github.com
```

- `GITHUB_USER` tells Kōan which `gh` identity to use for API calls
- `KOAN_EMAIL` sets the git author/committer on the bot's commits

**In `instance/config.yaml`:**

```yaml
github:
  nickname: "yourname-koan"        # The @mention name (must match the GitHub username)
  commands_enabled: true            # Enable @mention commands
  authorized_users: ["yourname"]   # Your personal account (who can command the bot)
```

**In `projects.yaml` (optional per-project override):**

```yaml
projects:
  sensitive-repo:
    path: "/path/to/sensitive-repo"
    github:
      authorized_users: ["alice", "bob"]  # Restrict who can command the bot on this repo
```

### 6. Verify the setup

```bash
# Confirm the bot identity resolves
GH_TOKEN=$(gh auth token --user yourname-koan) gh api user --jq '.login'
# → yourname-koan

# Confirm notifications access (should return [] not 403)
GH_TOKEN=$(gh auth token --user yourname-koan) gh api notifications
# → []

# Confirm repo access
GH_TOKEN=$(gh auth token --user yourname-koan) gh api repos/OWNER/REPO/collaborators/yourname-koan/permission --jq '.permission'
# → write (or admin/maintain)
```

### 7. Start Kōan

```bash
make start
```

You should see in the logs:

```
[init] GitHub CLI authenticated as yourname-koan
```

### How it works

At startup, Kōan calls `gh auth token --user <GITHUB_USER>` to retrieve the bot's token and sets it as `GH_TOKEN` in the environment. All subsequent `gh` API calls use this token, so the bot operates under its own identity regardless of which `gh` account is "active" on your machine.

During the sleep cycle between missions, Kōan polls the bot's GitHub notifications. When someone posts `@yourname-koan rebase` on a PR, the bot detects the mention, verifies the user's permissions, and queues a mission.

See [docs/github-commands.md](docs/github-commands.md) for the full list of supported @mention commands and the security model.

## Running as a systemd Service (Linux)

On Linux systems with systemd, Kōan can run as a native system service with automatic restart on failure, journal integration, and boot-time startup.

> **Note:** macOS users should use launchd instead — see [Running as a launchd Service (macOS)](#running-as-a-launchd-service-macos) below. systemd is Linux-only.

### Quick setup

```bash
make install-systemctl-service   # One-time: install + enable services
make start                       # Start via systemctl
```

Or simply run `make start` — on Linux with systemd, it auto-installs the service on first run.

### What it does

The install creates two systemd services:

| Service | Process | Description |
|---------|---------|-------------|
| `koan.service` | `run.py` | Agent loop (missions, execution, reflection) |
| `koan-awake.service` | `awake.py` | Messaging bridge (Telegram/Slack) |

The services are linked: stopping one stops both (`BindsTo=` relationship).

### How `make start/stop/status` work with systemd

On Linux with systemd available, the Makefile **automatically delegates** to `systemctl`:

| Command | Without systemd | With systemd |
|---------|----------------|--------------|
| `make start` | Python PID manager | `systemctl start koan` |
| `make stop` | Python PID manager | `systemctl stop koan` |
| `make status` | Python PID manager | `systemctl status koan koan-awake` |

The detection is automatic — no configuration needed. On macOS or Linux without systemd, the original PID-manager behavior is preserved.

### Viewing logs

Logs are written to both the systemd journal and the `logs/` directory:

```bash
# systemd journal (quick checks)
sudo journalctl -u koan -f
sudo journalctl -u koan-awake -f

# File-based logs (same as non-systemd)
make logs
```

### Managing the service

```bash
# Standard systemctl commands work
sudo systemctl restart koan        # Restart both services
sudo systemctl enable koan         # Start on boot (done by install)
sudo systemctl disable koan        # Don't start on boot

# Uninstall completely (reverts to PID-manager mode)
make uninstall-systemctl-service
```

### Uninstalling

```bash
make uninstall-systemctl-service
```

This stops the services, disables them, removes the unit files, and reloads systemd. After uninstalling, `make start` will use the Python PID manager again.

## Running as a launchd Service (macOS)

On macOS, Kōan can run as a native launchd user agent with automatic restart on failure and login-time startup — no `sudo` required.

> **Tip:** To keep your Mac awake while Kōan runs, see [Preventing macOS sleep](#preventing-macos-sleep) below.

### Quick setup

```bash
make install-launchd-service   # One-time: render plists + load services
make start                     # Start via launchctl
```

Or simply run `make start` — on macOS, it auto-installs the launchd service on first run.

### What it does

The install creates two launchd user agents in `~/Library/LaunchAgents/`:

| Plist | Process | Description |
|-------|---------|-------------|
| `com.koan.run.plist` | `run.py` | Agent loop (missions, execution, reflection) |
| `com.koan.awake.plist` | `awake.py` | Messaging bridge (Telegram/Slack) |

Both services are configured with:
- **`RunAtLoad`** — start automatically at login
- **`KeepAlive`** (on non-zero exit) — restart on failure
- **`ThrottleInterval`** (10s) — prevent rapid restart loops

A wrapper script (`koan/launchd/koan-wrapper.sh`) sources your `.env` file and sets up the environment (`KOAN_ROOT`, `PYTHONPATH`, `SSH_AUTH_SOCK`) before launching Python.

### How `make start/stop/status` work with launchd

On macOS with launchctl available, the Makefile **automatically delegates** to launchd:

| Command | Without launchd | With launchd |
|---------|----------------|--------------|
| `make start` | Python PID manager | `launchctl kickstart` |
| `make stop` | Python PID manager | `launchctl kill SIGTERM` |
| `make status` | Python PID manager | `launchctl print` |

The detection is automatic — no configuration needed. On Linux or systems without launchctl, the original PID-manager behavior is preserved.

### Viewing logs

Logs are written to the `logs/` directory:

```bash
# Watch live logs
make logs

# Or tail directly
tail -f logs/run.log
tail -f logs/awake.log
```

### SSH agent forwarding

If you use SSH-based git remotes, `make start` automatically forwards your SSH agent socket so launchd-managed processes can access it:

```bash
# This is done automatically by `make start`, but you can also do it manually:
ln -sf "$SSH_AUTH_SOCK" /path/to/koan/.ssh-agent-sock
```

### Uninstalling

```bash
make uninstall-launchd-service
```

This stops the services, removes the plist files from `~/Library/LaunchAgents/`, and unloads them from launchd. After uninstalling, `make start` will use the Python PID manager again.

## Troubleshooting

### "KOAN_ROOT environment variable is not set"

This happens if you run Python scripts directly instead of using `make` commands.

**Fix:** Always use the Makefile targets (`make run`, `make awake`, `make test`). They set `KOAN_ROOT` automatically.

If you need to run scripts directly:
```bash
KOAN_ROOT=/path/to/koan python3 koan/app/awake.py
```

### "Project 'example' not found" or similar

Your `missions.md` file references a project name that doesn't match your configuration.

**Fix:** Either:
1. Remove project tags from missions: `- My task` instead of `- [project:example] My task`
2. Or ensure the project name matches your `projects.yaml` config

### Messaging provider not responding

**For Telegram:**
1. Verify your token: `curl "https://api.telegram.org/botYOUR_TOKEN/getMe"` should return your bot info
2. Verify your chat ID: Make sure `KOAN_TELEGRAM_CHAT_ID` matches the ID from the `getUpdates` call
3. Check `make awake` is running without errors

**For Slack:**
1. Verify Socket Mode is enabled and the app token starts with `xapp-`
2. Check that the bot is invited to the channel (`/invite @koan`)
3. Review the logs for connection errors (`make logs`)

### CLI provider errors

Make sure your configured provider CLI is installed and authenticated.

**Claude Code:**
```bash
claude --version   # Should show version
claude             # Should start interactive mode (exit with /exit)
```

**OpenAI Codex:**
```bash
codex --version    # Should show version
codex login --device-auth
```

For Copilot and local setups, see:
- [docs/provider-copilot.md](docs/provider-copilot.md)
- [docs/provider-local.md](docs/provider-local.md)

## Preventing macOS sleep

Kōan runs in the background — if your Mac goes to sleep, everything stops. You need to prevent sleep while keeping the screen off.

### Using `caffeinate` (recommended)

The simplest approach — run this before launching Kōan:

```bash
# Prevent system sleep indefinitely (Ctrl+C to stop)
caffeinate -s &
```

Or wrap your entire session:

```bash
caffeinate -s make run
```

The `-s` flag prevents system sleep even when the display is off. The display will still turn off normally (saving power), but the CPU keeps running.

### Using `pmset` (persistent)

To make the setting survive reboots:

```bash
# Disable sleep on AC power
sudo pmset -c sleep 0
sudo pmset -c disablesleep 1
```

To revert:

```bash
sudo pmset -c sleep 10
sudo pmset -c disablesleep 0
```

### macOS System Settings

Alternatively: **System Settings → Energy → Prevent automatic sleeping when the display is off** — toggle ON.

> **Tip:** `caffeinate -s` is the safest option — it only affects the current session and stops automatically when you kill it.

## Environment Variables Reference

### Required (Provider-Specific)

**For Telegram** (default):

| Variable | Description |
|----------|-------------|
| `KOAN_TELEGRAM_TOKEN` | Telegram bot token from @BotFather |
| `KOAN_TELEGRAM_CHAT_ID` | Your Telegram chat ID |

**For Slack**:

| Variable | Description |
|----------|-------------|
| `KOAN_MESSAGING_PROVIDER` | Must be set to `slack` |
| `KOAN_SLACK_BOT_TOKEN` | Bot User OAuth Token (starts with `xoxb-`) |
| `KOAN_SLACK_APP_TOKEN` | App-Level Token (starts with `xapp-`) |
| `KOAN_SLACK_CHANNEL_ID` | Channel ID where Kōan operates |

> **Note:** Project paths are configured in `projects.yaml` (see step 4 above). The `KOAN_PROJECTS` env var is supported as a fallback.

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `KOAN_ROOT` | (auto-detected) | Path to koan repo (set by Makefile) |
| `KOAN_EMAIL` | — | Git author email for koan's commits |
| `KOAN_BRIDGE_INTERVAL` | 3 | Telegram poll interval (seconds) |
| `KOAN_CHAT_TIMEOUT` | 180 | Claude CLI timeout for chat responses (seconds) |
| `KOAN_GIT_SYNC_INTERVAL` | 5 | Runs between git sync checks |
| `KOAN_SMTP_HOST` | — | SMTP server hostname (e.g., `smtp.gmail.com`) |
| `KOAN_SMTP_PORT` | 587 | SMTP server port |
| `KOAN_SMTP_USER` | — | SMTP login username |
| `KOAN_SMTP_PASSWORD` | — | SMTP login password |
| `EMAIL_KOAN_OWNER` | — | Recipient email for notifications |

> **Note:** `max_runs_per_day` and `interval_seconds` are now configured in `config.yaml`, not `.env`.
> The env vars `KOAN_MAX_RUNS` and `KOAN_INTERVAL` are deprecated and ignored.

## Multi-Project Setup

Kōan can work on up to 50 projects, rotating between them.

Configure in `projects.yaml`:

```yaml
projects:
  myapp:
    path: "/Users/yourname/myapp"
  backend:
    path: "/Users/yourname/backend"
    git_auto_merge:
      base_branch: "staging"
```

Per-project `git_auto_merge` overrides are defined directly in `projects.yaml`. See `projects.example.yaml` for the full schema.

For each project, create a memory folder:
```bash
cp -r instance/memory/projects/_template instance/memory/projects/myapp
cp -r instance/memory/projects/_template instance/memory/projects/backend
```

Tag missions with the target project:
```
- [project:myapp] Add user authentication
- [project:backend] Fix CSS bug on homepage
```
