# Running as a launchd Service (macOS)

Kōan can run as a native **launchd** user agent on macOS — automatic restart on failure, login-time startup, and no `sudo` required.

## Quick Setup

```bash
make install-launchd-service   # One-time: render plists + load services
make start                     # Start via launchctl
```

Or simply run `make start` — on macOS, it auto-installs the launchd service on first run.

## What It Does

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

## How `make start/stop/status` Work

On macOS with `launchctl` available, the Makefile **automatically delegates** to launchd:

| Command | Without launchd | With launchd |
|---------|----------------|--------------|
| `make start` | Python PID manager | `launchctl kickstart` |
| `make stop` | Python PID manager | `launchctl kill SIGTERM` |
| `make status` | Python PID manager | `launchctl print` |

The detection is automatic — no configuration needed. On Linux or systems without `launchctl`, the original PID-manager behavior is preserved.

## Viewing Logs

Logs are written to the `logs/` directory:

```bash
# Watch live logs
make logs

# Or tail directly
tail -f logs/run.log
tail -f logs/awake.log
```

## SSH Agent Forwarding

If you use SSH-based git remotes, `make start` automatically forwards your SSH agent socket so launchd-managed processes can access it:

```bash
# This is done automatically by `make start`, but you can also do it manually:
ln -sf "$SSH_AUTH_SOCK" /path/to/koan/.ssh-agent-sock
```

See [ssh-setup.md](ssh-setup.md) for full SSH authentication details.

## Preventing macOS Sleep

To keep your Mac awake while Kōan runs, use `caffeinate`:

```bash
caffeinate -i -w $(pgrep -f "koan/app/run.py")
```

Or add a `KeepAlive`-style approach via a caffeinate wrapper in your shell profile.

## Uninstalling

```bash
make uninstall-launchd-service
```

This stops the services, removes the plist files from `~/Library/LaunchAgents/`, and unloads them from launchd. After uninstalling, `make start` will use the Python PID manager again.

## Troubleshooting

### Services not starting after install

Check that the plists were rendered correctly:

```bash
plutil -lint ~/Library/LaunchAgents/com.koan.run.plist
plutil -lint ~/Library/LaunchAgents/com.koan.awake.plist
```

### Services fail immediately after login

Check logs for environment issues:

```bash
tail -50 logs/run.log
tail -50 logs/awake.log
```

Common causes:
- Missing `.env` file — copy from `instance.example/.env.example`
- Invalid `KOAN_ROOT` — the wrapper script expects the Kōan repo at the path baked into the plist

### Manually loading/unloading services

```bash
# Load (start)
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.koan.run.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.koan.awake.plist

# Unload (stop)
launchctl bootout "gui/$(id -u)/com.koan.run"
launchctl bootout "gui/$(id -u)/com.koan.awake"
```

### Checking service status

```bash
launchctl print "gui/$(id -u)/com.koan.run"
launchctl print "gui/$(id -u)/com.koan.awake"
```
