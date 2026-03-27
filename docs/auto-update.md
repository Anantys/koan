# Auto-Update

Kōan can automatically keep itself up to date by periodically checking for new upstream commits and pulling them.

## Overview

When enabled, the auto-update feature:

1. **At startup** — checks for upstream updates before the first iteration
2. **Periodically** — checks every N iterations during the main loop
3. **On detection** — pulls new commits, notifies via Telegram, and restarts

The check is lightweight: a `git fetch` followed by a `rev-list --count` comparison between local `main` and the upstream remote. A 2-minute cache prevents excessive fetching even if iterations run faster than expected.

## Configuration

Add the `auto_update` section to your `instance/config.yaml`:

```yaml
auto_update:
  enabled: true           # Master switch (default: false)
  check_interval: 10      # Check every N iterations (default: 10)
  notify: true            # Notify on Telegram before/after update (default: true)
```

### Options

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable or disable automatic updates. Opt-in only. |
| `check_interval` | `10` | How often to check, in number of loop iterations. With the default `interval_seconds: 300`, a value of 10 means roughly every 50 minutes. |
| `notify` | `true` | Send Telegram notifications before pulling and after success/failure. |

## How It Works

1. Kōan identifies the upstream remote (looks for a remote named `upstream`, or falls back to `origin`)
2. Runs `git fetch <remote> --quiet` to update remote refs
3. Compares `main...<remote>/main` to count how many commits are ahead
4. If new commits are found:
   - Sends a Telegram notification (if `notify: true`)
   - Stashes any dirty working tree changes
   - Pulls from upstream into local `main`
   - Signals a restart via the restart manager
   - Sends a success notification with a summary of changes
5. The run loop wrapper detects the restart signal and relaunches Kōan with the updated code

## Safety

- **Disabled by default** — must be explicitly opted in via `enabled: true`
- **Stash protection** — dirty working tree changes are auto-stashed before pulling (a warning is included in the notification)
- **Failure resilience** — if the pull fails, Kōan logs the error, notifies via Telegram, and continues running on the current version
- **Rate limiting** — a 2-minute cache ensures checks never happen more than once every 120 seconds, regardless of iteration speed
- **Non-destructive** — uses the same `pull_upstream()` mechanism as the manual `/update` command

## Manual Alternative

You can always update manually via the `/update` Telegram command, which performs the same pull + restart flow on demand.
