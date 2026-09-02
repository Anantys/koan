---
type: doc
title: "Troubleshooting"
description: "Catalogs common operational issues (agent loop, git/worktrees, memory, bridge, GitHub, CLI provider, parallel sessions, config) and their fixes."
tags: [operations]
created: 2026-06-04
updated: 2026-09-08
---

# Troubleshooting

Common operational issues and how to resolve them.

## Quick Diagnostic

Run `/doctor` first — it catches many common problems and can auto-repair with `/doctor --fix`. Use `/doctor --full` to include connectivity checks (Telegram, GitHub, CLI provider).

## Agent Loop Issues

### Agent not picking up missions

1. **Check if paused** — `/pause` or quota exhaustion pauses the agent. Run `/status` to see the current state. Use `/resume` to unpause.
2. **Check quota** — `/quota` shows remaining budget. If below `stop_at_percent` (default 5%), the agent waits for quota reset. Override with `/quota 50` if the estimate is wrong.
3. **Check focus mode** — `/focus` locks the agent to a specific project. If a mission is queued for a different project, it won't be picked up. Use `/unfocus` to release.
4. **Check passive mode** — `/passive` blocks all execution. Use `/active` to resume.
5. **Check schedule** — if `schedule.active_hours` is configured, the agent only works during those hours.
6. **Check `missions.md`** — missions stuck under a non-canonical section header won't be picked up. Run `/doctor --fix` to reconcile malformed sections.

### Agent stuck on a mission (looping / not making progress)

1. **Stagnation detection** — if enabled, the agent auto-kills sessions stuck in a loop. Check the stagnation config in `config.yaml`: `stagnation.check_interval_seconds`, `abort_after_cycles`, and `max_retry_on_stagnation`.
2. **Manual abort** — `/abort` kills the current mission and marks it Failed. The next mission in the queue picks up.
3. **Check `/live`** — see if the agent is producing output or silently hung.
4. **Forced restart** — `/restart --force` kills the in-flight mission and restarts both daemons immediately, instead of waiting for the mission to end like plain `/restart`. The mission is re-queued to Pending by crash recovery on the next startup — unless it has exhausted its crash retries (`max_crash_retries`), in which case recovery moves it to Failed. Reach for it when `/abort` doesn't land or the runner ignores a polite restart.
5. **Bridge unresponsive? Break-glass CLI.** When the loop is truly stuck the Telegram bridge may stop answering `/list`, `/cancel`, and `/abort`. Interrogate and edit the queue straight from the terminal — no bridge, no run loop needed — with `make missions` (list) and `make mission-rm sel=i1` (abort the stuck one). See [Mission-queue break-glass CLI](mission-cli.md). Then `make stop && make start` to restart the loop clean (recovery will skip the aborted mission).

### "Quota exhausted" when quota is actually available

The internal usage estimate can drift from reality. Use `/quota <N>` to override (e.g., `/quota 50` tells the agent it has 50% remaining). This clears any quota-related pause.

**Repeated false pauses right after `/resume` (especially fixed in a newer version):** if the agent keeps pausing for "quota" on every resume even though quota is fine — and a `git log` shows a relevant fix already landed — the running daemon may be executing **stale code from before the fix**. The run loop is long-lived; `/update` re-execs the interpreter to load new code (see [auto-update](auto-update.md)), but a daemon started on a much older version (or one whose update didn't complete) can keep running the old modules in memory. Do a full restart so a fresh process loads the current code:

```bash
make stop && make start          # run as the account that owns the daemon
```

If `make stop` reports processes "stopped" but `pgrep -fl app/run.py` still shows a `run.py` (and `cat instance/.koan-run.pid` is missing), you have an **orphaned daemon** that escaped PID tracking — likely owned by a different user (e.g. a dedicated bot account). Kill it from that account (`kill -9 <pid>`) before `make start`.

### Agent loop process not running

1. Check `make status` to see which processes are alive.
2. Check log files: `make logs` or `tail -100 instance/logs/run.log`.
3. If the process crashed, check for a stale PID file — `/doctor --fix` removes orphaned PID files.

> `make logs` now renders the agent's `[cli]` activity stream in a compact,
> human-readable form (see `docs/operations/log-formatting.md`). To inspect the
> exact unmodified log bytes, run `make logs raw=1`.

## Git & Worktree Issues

### Orphaned or stale worktrees

Parallel sessions create worktrees under `.worktrees/`. If a session crashes, worktrees can linger:

```bash
# From the project directory
git worktree list          # See all worktrees
git worktree prune         # Remove stale references
```

Kōan cleans up worktrees on startup during crash recovery. If you see stale worktrees, restarting the agent loop should clear them.

The bridge also starts an hourly background sweep for leaked worktrees, such as temporary
review checkouts. The sweep does not block messaging.

**What is in scope** — every worktree registered on the project that lives *outside* the
project directory (`/tmp/review-<sha>`), plus scratch checkouts under `<project>/tmp/`.
Nothing else on the host reclaims either, so the sweep can delete them once they go idle.
Everything else inside the project is out of scope: the main checkout,
`<project>/.worktrees/` and `<project>/.claude/worktrees/` (other code owns those), and
any worktree you made by hand elsewhere in the project (`<project>/wt/feature-x`). The
guards below all read git, and git says nothing about the gitignored files — `.env.local`,
`dev.db` — a workspace like that keeps, so it is kept whole by scope rather than by the
dirty check.

**What it keeps** — locked worktrees, worktrees with recent non-ignored file activity
(idle for less than two days), **any worktree with uncommitted changes** (removal is
`--force`, so a dirty tree is never touched, however durable its branch is), tracking
branches with commits missing upstream, and detached commits that no local branch, tag,
or remote-tracking ref reaches. That last check is a single revision walk rather than a
per-ref scan, so it stays fast in repositories with many refs. Each sweep is capped at two
minutes, with a 15-second limit per Git command; deferred worktrees are retried in a later
hourly sweep, and every retained worktree logs its reason (`skip (locked)`, `skip
(uncommitted changes)`, `retain (activity check failed)`, …).

So a leaked review checkout that left an edit behind is *kept*, not reclaimed — commit and
discard it (or remove the worktree by hand) if you want the disk back. That is the
deliberate trade: disk is recoverable, an unattended `--force` on someone's WIP is not.

**Opting out** — `git worktree lock <path>` marks a worktree as a live workspace. The
sweep never removes a locked worktree, and git prep never detaches one (see below).

### Branch conflicts (can't checkout, can't push)

1. **Force-push protection** — Kōan uses `koan/*` branches (configurable via `branch_prefix`). If the branch already exists on remote from another instance, the agent may fail to push.
2. **Shared project repos** — if multiple Kōan instances target the same repo, ensure each uses a distinct `branch_prefix` in `config.yaml`.
3. **Another worktree holds the base branch** — git allows a branch in at most one
   worktree, so a second worktree sitting on `main` blocks the project's own checkout and
   every mission fails in git prep. Kōan self-heals this before each mission: it runs
   `git checkout --detach` in the holding worktree, which frees the branch while leaving
   that worktree, its files and any uncommitted changes exactly where they are (nothing is
   removed). The heal is reported in the prep result and the log. `/doctor` reports the
   same collision, and `/doctor --fix` performs the same detach. To keep a worktree on the
   base branch, `git worktree lock <path>` — a locked holder is never detached, and the
   collision is reported instead: `/doctor` flags it as a non-fixable error, and the fix
   is yours to run (`git worktree unlock <path> && git -C <path> checkout --detach`).
   If `/doctor` instead reports that the collision check was *skipped*, the project's
   base branch could not be resolved without the network — set
   `git_auto_merge.base_branch` in `projects.yaml`, or run `git remote set-head origin -a`
   in the project.

### SSH authentication failures

See the [SSH Setup Guide](../setup/ssh-setup.md) for detailed scenarios. Quick checks:

```bash
ssh -T git@github.com          # Test basic SSH connectivity
ssh-add -l                      # Are keys loaded in the agent?
make ssh-forward                # Refresh agent socket (systemd deployments)
```

## Memory & Journal Issues

### Memory files growing too large

Memory compaction runs automatically every 24 hours (configurable). To force compaction immediately:

```bash
python3 koan/app/memory_manager.py <instance_dir> compact-learnings [project-name]
```

Configure thresholds in `config.yaml`:
```yaml
memory:
  learnings_max_lines: 100      # Target after semantic compaction
  learnings_hard_cap: 200       # Absolute max (safety net)
  compaction_interval_hours: 24
```

### Missing memory or journal directories

Run `/doctor --fix` — it recreates missing `memory/` and `journal/` directories.

## Bridge (Telegram/Slack) Issues

### Bot not responding to Telegram messages

1. **Check bridge is running** — `make status` shows if the awake/bridge process is alive.
2. **Check Telegram token** — verify `KOAN_TELEGRAM_TOKEN` is set in `.env`.
3. **Check logs** — `/logs awake` or `tail -100 instance/logs/awake.log`.
4. **Polling interval** — the bridge polls Telegram every 3 seconds. Messages should appear within 3s.

### Outbox messages not being delivered

Messages queue in `instance/outbox.md`. The bridge flushes this to Telegram on each poll cycle. If messages are stuck:

1. Check the bridge is running (`make status`).
2. Check for Telegram API rate limiting in `awake.log`.
3. If the outbox has grown large, restart the bridge (`make stop && make start`).

## GitHub Integration Issues

### GitHub @mentions not triggering missions

1. **Bot nickname** — verify `github_nickname` in `config.yaml` matches the bot's GitHub username.
2. **Authorized users** — check `authorized_users` in `projects.yaml` for the project.
3. **Notification polling** — by default polls every 60-300s. Use `/check_notifications` to force an immediate check.
4. **Permissions** — the bot's GitHub token must have read access to the repository.

### GitHub webhook not receiving events

1. Verify the webhook secret matches `KOAN_GITHUB_WEBHOOK_SECRET`.
2. Check the webhook endpoint is reachable from GitHub's servers.
3. See [GitHub Webhooks](../messaging/github-webhooks.md) for setup details.

## CLI Provider Issues

### Claude Code not found or not working

1. Verify the CLI is installed: `which claude` or `claude --version`.
2. Check `KOAN_CLI_PROVIDER` in `.env` (default: `claude`).
3. See [Provider Setup](../providers/) for provider-specific configuration.

### CLI binary missing from PATH — "missions blocked" degraded mode

If the daemon started without the provider binary on its `PATH` (a common cause is
launching from a service manager or cron with a minimal environment), Kōan enters a
**degraded, no-mission mode**: it stays alive so chat and the GitHub/Jira inbox keep
working, but it starts **no** missions (running one would crash the provider). You'll
see a one-time ⚠️ warning on Telegram/Slack at startup, and `/status` shows
`⚠️ CLI unavailable — '<binary>' not on PATH; missions blocked`.

This state is held **in memory** and clears **only on restart** — fixing `PATH` while
the daemon runs is not picked up automatically (the `PATH` a long-lived process
inherited cannot be changed from outside). To recover:

1. Run `/doctor --full` (or `which claude`) to confirm which binary is missing.
2. Fix the environment so the binary resolves — ensure the CLI is installed and its
   directory is on the `PATH` the daemon runs with. For service-manager launches, see
   [systemd PATH preservation](../setup/systemd-user.md) and
   [launchd](../setup/launchd.md).
3. Restart the stack: `make stop && make start`.

`/doctor` resolves the *actual* configured provider binary (Claude, Codex, Grok, Haze,
Cline, per-role `cli:` overrides, and `KOAN_CLAUDE_CLI_PATH`), not just `claude`.

### Provider quota / rate limit errors

Kōan detects quota-limit messages from CLI output and auto-pauses. The pause lifts 10 minutes after the reported reset time. If the reset time can't be parsed, Kōan pauses for 5 hours. Use `/quota <N>` to override the estimate and `/resume` to unpause early.

### Provider blocked when another user runs Kōan on the same host

Symptom: the provider CLI (e.g. Codex) fails to start, or you see a warning that
"serialization is disabled" for the provider lock, only when a second user is
running Kōan on the same machine.

Cause: Kōan's scratch files and the provider invocation lock live under a
**per-uid** directory — `$XDG_RUNTIME_DIR/koan` when set, otherwise
`/tmp/koan-<uid>/` (mode `0700`). Each user gets their own, so they never clash.
The old behavior used fixed global names like `/tmp/koan-<provider>.lock`, which
a second user could not lock because the file was owned by the first user.

Fixes / checks:
1. Confirm the per-uid dir exists and is yours: `ls -ld /tmp/koan-$(id -u)` (or
   `ls -ld "$XDG_RUNTIME_DIR/koan"`). It should be owned by you with `drwx------`.
2. To pin a specific location (e.g. a fast tmpfs, or to separate two instances
   you run yourself), set `KOAN_TMP_DIR=/path/to/dir` in `.env`.
3. Stale `koan-*` files left in the shared `/tmp` root by older versions are
   harmless and can be removed once no Kōan process is using them.

## Parallel Session Issues

### Sessions not running in parallel

1. Check `max_parallel_sessions` in `config.yaml` (default: 2).
2. Only one mission can target the same project at once (per-project serialization).
3. GitHub operations (`gh` CLI) may rate-limit parallel requests.

### Shared deps causing conflicts

If a mission's build step modifies dependencies (e.g., `npm install`), it can affect other sessions sharing the same directory via `shared_deps`. Consider removing the dependency directory from `shared_deps` if this is a recurring issue.

## Configuration Issues

### Config drift after update

Run `/config_check` to detect keys missing from or extra to the template. The same check runs as part of `/doctor`. Review the reported diff and update your `config.yaml` accordingly.

### Config changes not taking effect

Most config is read on each iteration. A few settings (process-level: API bind, ports) require a restart. Use `/restart` if a config change doesn't seem to take effect.

## When Nothing Else Works

1. **Collect diagnostics**: `/doctor --full` output, recent logs (`/logs all`), and `make status`.
2. **Restart the stack**: `make stop && make start`.
3. **Check upstream issues**: review the [GitHub repository](https://github.com/Anantys-oss/koan) for known issues.
4. **Export a snapshot**: `/snapshot` before making destructive changes — it backs up memory state.
