# Docker Setup

> Docker provides isolated environments and simplified deployment — ideal for
> VPS/server hosting or keeping Koan sandboxed on your machine. For running
> Koan directly (no container), see [INSTALL.md](../INSTALL.md).

## Prerequisites

- **Docker Engine 20+** and **Docker Compose v2+**
- **Claude authentication** (one of):
  - **ANTHROPIC_API_KEY** in `.env` — for API billing accounts ([console.anthropic.com](https://console.anthropic.com/settings/keys))
  - **`claude setup-token`** — for Claude subscription users (requires Claude CLI on host: `npm install -g @anthropic-ai/claude-code`)
- **GitHub CLI (`gh`)** authenticated on the host (for PR/issue operations)
- A messaging platform configured (**Telegram** or **Slack** — see [INSTALL.md](../INSTALL.md#2-set-up-a-messaging-platform))

## Quick Start

```bash
# 1. Clone and enter the repo
git clone https://github.com/sukria/koan.git
cd koan

# 2. Create your instance directory
cp -r instance.example instance

# 3. Set up credentials
cp env.example .env
# Edit .env — set messaging credentials (Telegram or Slack)
# For API billing: also set ANTHROPIC_API_KEY in .env

# 4. Add projects to the workspace
mkdir -p workspace
ln -s /path/to/your/project workspace/myproject

# 5. Run the setup script (auto-detects host paths, generates mounts)
./setup-docker.sh

# 6. Authenticate Claude CLI (pick one)
# Option A: API key — just set ANTHROPIC_API_KEY in .env (done in step 3)
# Option B: Subscription users (requires Claude CLI on host):
make docker-auth
# Generates an OAuth token from your host's Claude CLI and saves it to .env.
# If you haven't authenticated on the host yet: claude auth login

# 7. Inject GitHub token for container use
make docker-gh-auth
# Extracts your host's gh token and saves it to .env as GH_TOKEN.
# Required because macOS Keychain tokens aren't accessible inside Docker.

# 8. Build and start (runs setup-docker.sh first, then starts detached)
make docker-up

# Or start in the foreground to watch logs directly:
# docker compose up --build
```

Verify it's running:

```bash
# Watch logs
docker compose logs -f

# Send a test message (requires messaging to be configured)
make say m="hello"
```

## Workspace Setup

Koan accesses your project repos through the `workspace/` directory. Each
subdirectory (or symlink) becomes a project the agent can work on.

```bash
mkdir -p workspace

# Symlink existing repos (recommended — no duplication)
ln -s ~/code/myapp workspace/myapp
ln -s ~/code/backend workspace/backend

# Or copy/clone directly
git clone https://github.com/you/myapp.git workspace/myapp
```

When you run `./setup-docker.sh`, it:

1. **Resolves symlinks** — Docker can't follow host symlinks. The script
   resolves each `workspace/<name>` entry to its real path and emits a
   **per-project bind mount**: `<real_path> → /app/workspace/<name>`.
   Each project gets its own mount in `docker-compose.override.yml`.
2. **Generates `projects.docker.yaml`** — maps each workspace entry to its
   container path (e.g. `/app/workspace/<name>`). This file is mounted as
   `/app/projects.docker.yaml` and copied to `/app/projects.yaml` at
   container startup so atomic writes work correctly.
   The script uses **smart merge**: if the file already exists, it only
   adds missing project entries — it never overwrites custom settings you've
   added manually. Safe to re-run at any time.
3. **Generates `docker-compose.override.yml`** — adds the per-project
   workspace mounts, auth directory mounts, and host UID/GID.

To add a new project later, symlink it into `workspace/` and re-run
`./setup-docker.sh` (or `make docker-setup`). Existing project entries in
`projects.docker.yaml` are preserved.

### Editing `projects.docker.yaml`

The generated file works out of the box, but you can edit it for per-project
overrides (auto-merge rules, CLI provider, etc.) — same schema as
`projects.yaml`:

```yaml
defaults:
  git_auto_merge:
    enabled: false
    base_branch: "main"
    strategy: "squash"

projects:
  myapp:
    path: /app/workspace/myapp
  backend:
    path: /app/workspace/backend
    git_auto_merge:
      base_branch: "staging"
```

## Architecture (How It Works)

### Container layout

The Docker image packages everything Koan needs: Python, Node.js, Claude CLI
(installed via npm), GitHub CLI (`gh`), and git. No host binaries are mounted.

Two processes run inside a single container, supervised by the entrypoint
script:

| Process | Purpose |
|---------|---------|
| `awake.py` | Telegram/Slack bridge — polls for messages, flushes outbox |
| `run.py` | Agent loop — picks missions, executes via Claude CLI |

If either process crashes, the entrypoint restarts it automatically.

### Authentication

- **Claude CLI** supports two auth methods:
  - **API key**: Set `ANTHROPIC_API_KEY` in `.env` (for API billing accounts).
  - **OAuth token** (subscription): Run `make docker-auth`. This calls `claude setup-token`
    on the host to generate a token, then saves it to `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.
    Requires Claude CLI installed and authenticated on the host
    (`npm install -g @anthropic-ai/claude-code && claude auth login`).
- **GitHub CLI** supports two auth methods:
  - **GH_TOKEN** (recommended for Docker): Run `make docker-gh-auth` to extract the token from
    the host's `gh` CLI and save it to `.env`. Required on macOS where tokens are in the Keychain.
  - **Mounted config**: `~/.config/gh` is mounted read-only from the host (works when tokens
    are stored as plain text, not in the system Keychain).
- **Git** uses a default identity (`Koan <koan@noreply.github.com>`), overridable
  by mounting `~/.gitconfig`.

### Volume mounts

| Host | Container | Purpose |
|------|-----------|---------|
| `./instance/` | `/app/instance/` | Runtime state (missions, memory, journal) |
| `instance/missions.docker.md` | `/app/instance/missions.md` | Isolated mission queue (see below) |
| *(per-project — see below)* | `/app/workspace/<name>` | Project repositories (one mount per project) |
| `./logs/` | `/app/logs/` | Log files |
| `./claude-auth/` | `/home/koan/.claude/` | Claude CLI auth state (interactive login) |
| `projects.docker.yaml` | `/app/projects.docker.yaml` | Project config template (copied to `projects.yaml` on startup) |
| `~/.config/gh` | `/home/koan/.config/gh` | GitHub CLI auth (read-only) |

> **Workspace mounts are per-project and dynamic.** The base `docker-compose.yml`
> contains no workspace mounts. `setup-docker.sh` resolves each `workspace/<name>`
> symlink to its real host path and writes individual bind mounts into
> `docker-compose.override.yml` (e.g. `/real/path/to/myapp:/app/workspace/myapp`).
> Re-run `./setup-docker.sh` after adding or removing projects.

### Isolated mission queue

The container mounts `instance/missions.docker.md` over
`instance/missions.md`. This means Docker and a native (non-Docker) Koan
instance each have their own mission queue — they won't interfere if you run
both.

### Health check

The entrypoint writes a heartbeat timestamp to `.koan-heartbeat` every 5
seconds. Docker's `HEALTHCHECK` verifies it's less than 120 seconds old:

```bash
docker inspect --format='{{.State.Health.Status}}' koan
```

### UID/GID matching

To avoid permission issues on bind-mounted volumes, `setup-docker.sh` detects
your host UID/GID and passes them as build args. The container user runs with
the same IDs as your host user.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | Credentials: `ANTHROPIC_API_KEY`, messaging tokens, `GITHUB_USER` |
| `projects.docker.yaml` | Project paths and per-project overrides (auto-generated) |
| `instance/config.yaml` | Agent settings (same as native setup) |
| `instance/soul.md` | Agent personality |
| `docker-compose.override.yml` | Volume mounts and build args (auto-generated) |

The two auto-generated files (`docker-compose.override.yml` and
`projects.docker.yaml`) are gitignored. Re-run `./setup-docker.sh` to
regenerate them after changing your workspace layout.

## Usage

### Make targets

```bash
make docker-setup   # Run setup-docker.sh
make docker-up      # Build and start (detached)
make docker-down    # Stop the container
make docker-logs    # Tail container logs
make docker-test    # Run the test suite inside the container
make docker-auth    # Extract Claude OAuth token from host CLI → .env
make docker-gh-auth # Extract GitHub token from host gh CLI → .env
```

### Docker Compose commands

```bash
# Start in foreground (see logs directly)
docker compose up --build

# Generate OAuth token from host CLI (one-time, for subscription users)
make docker-auth

# Interactive shell inside the container
docker compose run --rm koan shell

# Run tests
docker compose run --rm koan test

# Run only the agent loop (no bridge)
docker compose run --rm koan agent

# Run only the bridge (no agent)
docker compose run --rm koan bridge
```

### Dry-run setup

Preview what `setup-docker.sh` would generate without writing files:

```bash
./setup-docker.sh --dry-run
```

## Troubleshooting

### "Claude CLI is not authenticated"

The container needs one of:
- **API key**: Set `ANTHROPIC_API_KEY` in `.env` ([console.anthropic.com](https://console.anthropic.com/settings/keys))
- **OAuth token**: Run `make docker-auth` on the HOST (not inside Docker)

### Permission errors on workspace files

UID/GID mismatch between host and container. Re-run `./setup-docker.sh` to
regenerate with the correct IDs, then rebuild:

```bash
./setup-docker.sh
docker compose up --build
```

### `gh` CLI not authenticated

On macOS, `gh` stores tokens in the system Keychain, which isn't accessible
inside Docker. The fix is to inject the token as an environment variable:

```bash
# Extract token from host and save to .env as GH_TOKEN
make docker-gh-auth

# Restart the container to pick up the new token
docker compose up --build -d
```

The `gh` CLI natively uses `GH_TOKEN` when set — no config file needed.

If you're on Linux (where tokens are stored as plain text in `~/.config/gh`),
the mounted config directory may work directly. Re-run `./setup-docker.sh` to
ensure the mount is set up.

### No projects found or workspace entries missing

If Kōan starts but doesn't see any projects, the `projects.docker.yaml` file
may be missing a `projects:` section or workspace entries. Re-run
`./setup-docker.sh` — it automatically adds the `projects:` section if
absent, and appends any workspace entries that are not yet listed:

```bash
./setup-docker.sh
docker compose up --build -d
```

Existing entries and custom settings in `projects.docker.yaml` are never
overwritten.

### Container keeps restarting

Check the logs for the root cause:

```bash
docker compose logs --tail=50
```

Common causes: missing API key, invalid messaging credentials, no projects
configured.

### `make docker-auth` fails

`make docker-auth` runs `claude setup-token` interactively on your host and captures the
resulting 1-year OAuth token from its output. For this to work:

- **Claude CLI must be installed** on the host: `npm install -g @anthropic-ai/claude-code`
- **Claude CLI must be authenticated** on the host: `claude auth login`
- The `setup-token` command requires `~/.claude.json` with `{"hasCompletedOnboarding": true}`.
  If you've run `claude` at least once, this file should already exist.

If the token extraction fails, you can manually copy the token from the `setup-token`
output and add it to `.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

### Docker and native Koan sharing state

Mission queues are isolated by default (`missions.docker.md` vs
`missions.md`). However, `instance/` state (memory, journal, config) is
shared. If you run both simultaneously, avoid editing `instance/config.yaml`
from both sides.
