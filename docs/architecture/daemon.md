---
type: doc
title: "Daemon Runtime"
description: "Describes how the Koan daemon is assembled: startup/process management, the bridge's chat/bg worker lanes, the agent loop's modular pieces, runtime modes, parallel sessions, and the bounded-memory model for CLI stdout capture."
tags: [architecture]
created: 2026-05-28
updated: 2026-09-02
---

# Daemon Runtime

This page describes how the long-running Koan daemon is assembled today.

## Startup

`make start` delegates to process management code in `koan/app/pid_manager.py`.
The manager starts the bridge, the agent loop, and optional local-model services
depending on provider configuration. PID files and `fcntl.flock()` prevent
duplicate process instances for the same role.

Startup displays the shared hero banner from `koan/app/banners/koan_hero.txt`
using the terminal mint theme. Banner rendering is cosmetic and must not block
process launch if it fails.

`make run` starts only the agent loop. `make awake` starts only the messaging
bridge. `make stop` asks managed processes to exit and escalates only when a
process does not stop cleanly.

When `mcp.enabled: true` and `mcp.transport: http`, `make start` also launches
the MCP Streamable HTTP daemon after the REST API. It owns
`.koan-pid-mcp`, writes process and audit output to `logs/mcp.log`, and is
included in `make stop`, `make status`, and `make logs`. The default stdio
transport is launched by MCP clients and is never daemonized by `make start`.

## Bridge Loop

See `specs/components/bridge.md` for the design contract behind this process
(two-process isolation, crash-safe outbox, invariants on inbound-text trust).

`awake.py` owns user-facing message ingestion. It:

- loads messaging configuration and command registries;
- polls Telegram, Slack, Matrix, GitHub, or Jira integration paths as configured;
- routes slash commands through command handlers and skill dispatch;
- promotes a plain message whose first word names a core skill to its slash form (`time` → `/time`);
- classifies remaining non-command text as chat or mission intent;
- appends missions to `instance/missions.md`;
- drains `instance/outbox.md` back to the messaging provider.

Bridge state that would otherwise create circular imports lives in
`bridge_state.py`. Bridge logging lives in `bridge_log.py`.

### Worker lanes

The bridge runs heavy work off the messaging poll loop in independent
daemon-thread lanes (`awake._run_in_worker(fn, lane=...)`):

- **chat** — interactive replies (`handle_chat`). When busy, a second chat
  message is answered with "⏳ Busy with a previous message."
- **bg** — background tasks: worker skills (Claude/API/GitHub calls typed
  in chat, e.g. `/review`, `/rebase`). When busy, additional bg tasks are dropped
  silently (no chat spam). `_run_in_worker` returns `True`/`False`
  (started vs dropped) so callers can tell. Autonomous background work
  ignores the result and stays silent; **user-initiated** worker skills
  (a `/review`, `/implement`, etc. typed in chat) dispatch on the bg lane
  but surface a "⏳ Busy with a previous task" reply when the lane was full,
  so a typed command never vanishes without feedback.
- **maintenance** — internal housekeeping such as the hourly foreign-worktree
  sweep. It is single-flight and silent when busy; its work never runs on the
  poll loop, so a large repository cannot delay Slack, Telegram, or other
  inbound commands. Each sweep has a fixed two-minute deadline and a 15-second
  cap per Git command; incomplete checks retain worktrees for a later sweep.
  Unlike chat and bg, this lane does **not** hold back the bridge's memory
  watchdog: its work is idempotent and restart-safe (a half-removed worktree is
  pruned on the next sweep), so a sweep wedged in uninterruptible I/O — which
  outlives its Python-level timeout — cannot silently disable the watchdog and
  trade a clean re-exec for an OOM kill.

Because the lanes run concurrently, a long-running background task never
blocks an interactive chat reply, and neither blocks the poll loop. One
in-flight task per lane provides back-pressure (no unbounded fan-out). No
extra OS process is forked — the "dedicated chat channel vs bg tasks" split is
realized with threads inside the existing bridge process.

#### Staying responsive during missions (#1084)

While a mission runs, the agent loop and the bridge invoke the AI CLI
concurrently against the same account (the default provider takes no
cross-invocation lock), so a chat call can come back empty or time out. Two
in-process measures keep chat responsive without adding a third process:

- **Chat retries the contention symptom.** `handle_chat` treats an empty
  response (clean exit, blank stdout) as retryable — the same class as a
  timeout — and retries with backoff and a lighter context
  (`cli_exec.CLI_RETRY_BACKOFF` / `CLI_RETRY_MAX_ATTEMPTS`) before showing any
  degraded message. The "I didn't get a response" apology now appears only on a
  genuine outage, not on the first contention hit. The chat lane is
  single-flight (`_CHAT_LOCK`), so the retry loop is bounded by a wall-clock
  budget (`CHAT_RETRY_BUDGET`, default 1.5× `CHAT_TIMEOUT`) — one stuck chat
  can't hold the lane for the full retry worst case and starve later chats —
  and the typing indicator wraps only each live CLI call, not the backoff
  sleeps, so a retrying chat doesn't flood Telegram with typing pulses.
- **Outbox formatting yields to active missions.** AI outbox formatting is
  cosmetic and the lowest-value concurrent AI caller. `OutboxManager` skips it
  and uses the instant local `fallback_format()` while a mission is *actively
  executing*, determined by `active_mission.is_mission_active()` (the
  authoritative `.koan-active` provider-liveness signal, #2086 — never the
  free-form `.koan-status` string). Polished AI formatting resumes as soon as no
  mission is executing; the check fail-opens on an absent/corrupt signal.

## Agent Loop

See `specs/components/agent-loop.md` for the design contract behind this
pipeline (execution flow, retry guards, lifecycle invariants).

`run.py` owns background work. Its loop is split across focused modules:

- `iteration_manager.py` refreshes usage, selects mode, injects recurring work,
  chooses a mission, and resolves the project.
- `mission_runner.py` performs lifecycle transitions, builds the execution
  command, runs the provider or direct skill, parses output, records usage, and
  handles completion, failure, reflection, and auto-merge.
- `loop_manager.py` handles focus, pending-file setup, project validation, and
  interruptible sleeps.
- `quota_handler.py` detects quota exhaustion and writes pause state. Hard
  quota hits requeue the active mission, pause until the provider reset time
  plus 10 minutes, or fall back to a 5-hour pause when no reset time is known.
  Claude Code's structured `rate_limit_event` stream events are matched
  status-aware: only a *rejected* status pauses Koan. The newer CLI also emits
  informational `rate_limit_event`s (status `allowed`) on every session, so
  matching the bare event type would otherwise pause Koan on successful runs.
  The rejected status must co-occur with the event on the same stream-json line
  — an unanchored whole-text match would pair the always-present informational
  event with any unrelated `"status":"exceeded"` JSON elsewhere in the output
  (e.g. CI / check-run payloads that `/ci_check` inspects). The informational
  summary line is rendered as `[cli] rate_limit_ok:` (underscored) so it never
  collides with the loose `rate limit` quota pattern.

Usage percentages that drive mode selection come from `usage_estimator`, which by
default uses a **local token-count heuristic**. When available, an **optional
authoritative anchor** (`oauth_usage.py` + `authoritative_usage.py`, issue #2455)
reads real session/weekly utilization from Anthropic's *undocumented* OAuth usage
endpoint and anchors the heuristic — the local counter still interpolates between
periodic polls. It degrades to the heuristic for API-key users, non-Claude
providers, or any endpoint failure, and never changes `decide_mode`, `burn_rate`,
or `quota_handler` (the reactive safety net stays authoritative on real
exhaustion). Configured via `usage.authoritative_source: auto | oauth_usage | off`.
See `specs/components/agent-loop.md` § "Usage source selection".

Idle actions use the same interruptible sleep path even when `auto_pause` is
disabled. If `interval_seconds` is set to `0`, the runner waits until the next
configured GitHub/Jira notification poll is due, or a small minimum breath when
notification polling is disabled, so always-on instances do not hot-loop.
During those idle waits, the runner only wakes for the run-targeted restart
marker (`.koan-restart-run`); stale legacy `.koan-restart` markers are ignored.

The loop writes real-time state to status files so the bridge, dashboard, and
commands can report progress without directly controlling the runner.

## One-shot execution model (no post-turn event loop)

Koan invokes the CLI in headless print mode (`claude -p --output-format json`).
Each mission is a **single non-interactive turn**: the CLI loops through tool
calls internally until the model emits a final message with no further tool
call, then exits. `run_claude_task()` blocks in the subprocess wait; the instant
the CLI exits `0`, `_run_iteration()` runs the post-mission pipeline and
`_finalize_mission()` marks the mission Done. **There is no event loop after the
turn.** Any work the model defers to "after" its turn — armed monitors, "I'll
report when it finishes", scheduled wake-ups — is silently dropped and the
backgrounded child is killed with the process group.

Consequences and safeguards:

- **Result-bearing work must complete in-turn.** The agent is instructed (see
  the `cli-execution-model` prompt partial) to block or poll within the turn
  until a command finishes, then read its result before concluding.
- **Foreground headroom.** For the Claude provider, Koan sets
  `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` (from
  `bash_foreground_timeout`, default 15 min, clamped
  below `mission_timeout` with a 120s reporting buffer) so a long-but-bounded
  command can block in the foreground rather than being backgrounded and
  orphaned. Set `bash_foreground_timeout: 0` to keep the CLI's built-in default.
- **Not a `max_turns` issue.** Default missions pass no `--max-turns` flag; and a
  genuine turn-cap hit surfaces as `subtype: "error_max_turns"`, which
  `check_json_success()` treats as failure — not the clean "Done" this class of
  bug produced. The trigger is a *natural* turn-end after backgrounding.

## Runtime Modes And Guards

- Pause mode uses `.koan-pause` state and can be time-bounded.
- Focus mode narrows work to a project or focus area.
- Passive mode keeps Koan alive but blocks execution.
- CLI-unavailable degraded mode: if the primary provider binary is missing from
  `PATH` at startup, the loop stays alive (chat and the GitHub/Jira inbox still
  work) but starts **no** missions — running one would crash the provider
  subprocess. Detected once at startup (`startup_manager.check_cli_binary` →
  `cli_health.check_primary_cli`), advertised to the operator with one ⚠️ warning,
  and held **in memory** (`app.cli_health`) with no signal file, so it clears only
  on restart. Fix `PATH` / install the CLI, then `make stop && make start`. See
  [troubleshooting](../operations/troubleshooting.md).
- Restart signaling uses a file so the bridge can ask the runner to restart.
- The stagnation monitor watches provider output, kills stuck subprocess groups,
  and requeues missions up to the configured retry limit.

New daemon behavior should prefer these existing state files and managers over
adding direct process coupling.

## Parallel Sessions

When `max_parallel_sessions` is set to 2 or higher in `config.yaml`, the agent
loop can run multiple missions concurrently. Each session gets its own git
worktree so there are no branch conflicts.

The parallel path has two phases wired into `_run_iteration` in `run.py`:

1. **Reap** (`_parallel_reap_sessions`) — polls active sessions for completion,
   runs the post-mission pipeline, transitions `missions.md` state, and sends
   notifications. Quota exhaustion in any session halts new dispatches.
2. **Dispatch** (`_parallel_dispatch_sessions`) — spawns the primary mission
   plus fills remaining free slots from the pending queue. A same-project guard
   prevents two sessions from running on the same project simultaneously.

Session state is tracked in-memory via `_live_sessions` and persisted via
`SessionRegistry` (`instance/sessions.json`). `session_manager.py` owns
`spawn_session`, `poll_sessions`, and `kill_session`. `worktree_manager.py`
handles git worktree create/teardown.

Skill-dispatched missions (`/rebase`, `/plan`, etc.) always use the sequential
path because they depend on git prep and specialised post-mission handling.

Single-slot installations (`max_parallel_sessions: 1`, the default) skip all
parallel logic with zero overhead.

## CLI stdout memory model

Claude/skill CLI output can reach hundreds of MB per mission, multiplied per
concurrent session when `max_parallel_sessions > 1`. To keep peak RAM bounded,
no path holds the full transcript in a Python list while the mission runs:

- **Main mission path** (`run_claude_task`): the child's stdout is wired
  straight to a temp file (`subprocess.Popen(stdout=out_f)`); no Python-side
  line list is ever built. Downstream consumers (`run_post_mission`, token
  parsing, PR-URL extraction) read that file.
- **Skill-dispatch path** (`_run_skill_mission`): each line is streamed via
  `_pump_skill_stdout` to the same on-disk capture and to `pending.md` (for
  `/live`) as it arrives; only a 200-line tail deque is kept in RAM for timeout
  diagnostics. The full transcript is read back from disk once, at
  end-of-mission, when a caller needs it (`_extract_pr_url`, the `— skipping`
  check, error classification).
- **Provider stream runner** (`run_command_streaming`): the error/max-turns
  accumulator (`raw_lines`) is a bounded `deque`; the assistant-text
  accumulator (`text_lines`) is the actual return value and is intentionally
  unbounded so long sessions never silently lose output.

Operators running on constrained hosts (e.g. Railway) should also pin
`max_parallel_sessions: 1` so per-session cost is not multiplied.
