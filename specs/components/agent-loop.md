---
type: component-spec
title: "Component Spec — Agent Loop Pipeline"
description: "Design contract for the core mission pipeline (iteration manager, mission executor/runner, quota handling, stagnation monitor) that pulls missions, invokes the CLI provider, and finalizes lifecycle state."
tags: [agent-loop]
created: 2026-06-27
updated: 2026-09-03
---

# Component Spec — Agent Loop Pipeline

**Modules:** `run.py`, `iteration_manager.py`, `mission_executor.py`,
`mission_runner.py`, `loop_manager.py`, `contemplative_runner.py`, `quota_handler.py`,
`prompt_builder.py`, `event_scheduler.py`, `stagnation_monitor.py`, `hooks.py`,
`devcontainer.py`, `usage_estimator.py`, `usage_tracker.py`, `burn_rate.py`,
`oauth_usage.py`, `authoritative_usage.py`

## Purpose

The beating heart: a pure-Python loop that pulls a mission, builds a prompt, invokes
the CLI provider as a subprocess, monitors it, and finalizes the mission's lifecycle
state. Everything else exists to feed or observe this loop.

See `docs/architecture/daemon.md`'s Agent Loop section for how this pipeline is wired
into the running daemon (startup, quota pause, parallel sessions).

## Execution flow (one iteration)

```
iteration_manager._decide()        # usage refresh, mode (REVIEW/IMPLEMENT/DEEP/WAIT),
                                    # recurring injection, mission pick, project resolve
        │
mission_executor._run_iteration()  # orchestration: pick → dispatch → execute → finalize
        │
        ├─ skill mission?  → _handle_skill_dispatch()  → skill_dispatch runners
        │                                                (bypass the Claude agent)
        └─ normal mission? → run.run_claude_task()      # CLI subprocess + monitoring
        │
run._finalize_mission()            # lifecycle state machine: Done / Failed / requeue
        │
mission_runner (post-processing)   # usage tracking, pending.md archival, reflection,
                                    # auto-merge
```

## Key types & functions

| Symbol | Contract |
|---|---|
| `run.run_claude_task()` | CLI subprocess invocation + monitoring host. Wires in the stagnation monitor and timeout watchdog. |
| `run._finalize_mission()` | The lifecycle authority — decides Done vs Failed vs requeue. All exits from In Progress funnel here. |
| `run._classify_and_handle_cli_error()` | Maps CLI error text → action. `trust_stdout` flag distinguishes raw CLI output from skill transcripts (skill stdout is DATA, not error signal). |
| `run._probe_exit0_quota()` | False-success detection: exit 0 but the run actually hit quota. |
| `mission_executor._run_iteration()` | Full per-iteration orchestration. |
| `mission_executor._maybe_retry_mission()` | Single transient-error retry. **Any new mission-terminating pathway must add a guard here** (see stagnation retry gap). |
| `mission_runner.build_mission_command()` | CLI prompt + flags assembly. |
| `mission_runner.parse_claude_output()` | JSON → text extraction from `--output-format json` / stream-json. |
| `run._is_ci_check_mission()` | Classifies a mission title as CI-related (`/ci_check …`, ci_dispatch `Fix CI failure: …`). |
| `run._mission_fail_icon()` | **The single source of truth for the emoji prefix on a mission-failure notification** — 🚦 for CI missions, ❌ otherwise. Every failure-notification site MUST call this, never hardcode ❌. |
| `iteration_manager._downgrade_if_burning_fast()` | Burn-rate-driven mode downgrade, next to affordability downgrade. |
| `stagnation_monitor` | Daemon thread hashing last-N stdout lines; kills the subprocess group after K identical hashes; requeues up to `max_retry_on_stagnation`. |
| `stagnation_monitor.get_retry_info()` | Exposes the per-mission retry count and stagnation pattern; consumed by `prompt_builder._get_stagnation_retry_section()` to coach a requeued session away from repeating the same stuck pattern. |
| `quota_handler` | Parses quota exhaustion from CLI output, writes pause state + journal entry. `extract_reset_info` is **bounded** — it stops at JSON/structural delimiters so a single-line CLI result object can't leak its JSON tail into `reset_display`. `quota_debug_snippet` returns a capped, reset-centered window of the raw output for chat debug blocks. |
| `hooks.py` | Lifecycle events: `session_start`, `session_end`, `pre_mission`, `post_mission`, `post_review`, each error-isolated. |
| `prompt_builder._get_koan_md_section()` | Delegates reading to `project_koan.read_general_koan_md()` (root `KOAN.md` + `.koan/KOAN.md`, combined cap `_MAX_KOAN_MD_CHARS` 16k), frames via the `koan-md` template. Returns `""` for absent/blank/unreadable. |
| `project_koan.read_koan_config()` / `get_review_always_check()` / `get_mission_hooks()` | Reads the target repo's optional structured `.koan/config.yaml` (a YAML surface alongside the markdown `.koan/` steering files). Fail-safe: returns `{}` / `[]` on absent/unparseable/malformed config, never raises. `review.always_check` feeds `/review` diff-size pinning; `get_mission_hooks(path, type, phase)` resolves the `pre_hooks`/`post_hooks` list executed by `mission_hooks` — see `specs/components/skills.md` → "Repo config file (`.koan/config.yaml`)". |
| `mission_hooks.hooks_enabled()` / `run_pre_hooks()` / `run_post_hooks()` | Gated executor for repo-config-driven shell hooks around a mission (default off; see "Mission hooks" below). Error-isolated, never raises. |

### KOAN.md injection

`prompt_builder._get_koan_md_section(project_path)` delegates file reading to
`project_koan.read_general_koan_md(project_path)`, which reads **both**
`<project>/KOAN.md` and `<project>/.koan/KOAN.md` (root first, `.koan/KOAN.md`
behind a `# .koan/KOAN.md` marker), strips and concatenates them, and caps the
*combined* length at `_MAX_KOAN_MD_CHARS`. When the result is non-empty it is
appended (framed via the `koan-md` system-prompt template) as a **Tier-1 stable
system-prompt section** — placed right after the submit-PR section so the
prompt-cache prefix stays intact. Root `KOAN.md` stays fully backward-compatible:
a project with no `.koan/` sees byte-identical output. `build_agent_prompt_parts()` /
`build_agent_prompt()` take an optional `host_project_path`; the reader uses it
in preference to `project_path` so the on-disk files are read from the host even
when `project_path` is the devcontainer workspace. Invariant: both sources
absent/blank leaves the system prompt unchanged. KOAN.md is koan-only — Claude
Code auto-loads `CLAUDE.md` but never `KOAN.md`, so interactive sessions never
see it.

**Steering-context visibility (`make logs`).** Every steering file that shapes a
mission prompt is announced on stderr (→ `logs/run.log`) as `Detected <label>,
loaded N chars (~ M tokens)` via `project_koan.log_context_load`. The agent loop
surfaces `KOAN.md` when `_get_koan_md_section` reads it, and — detection-only —
`CLAUDE.md (auto-loaded by CLI)` via `_log_claude_md_detected(project_path)`,
which reads the project-root `CLAUDE.md` solely to report its size (koan never
injects it; the CLI loads it from `cwd`). Best-effort: any read/stream failure is
swallowed and never blocks prompt assembly.

### Mission hooks (`mission_hooks`) — repo-config-driven shell around missions

`mission_hooks.py` executes the repo-owner shell commands declared in a target
repo's `.koan/config.yaml` under `pre_hooks`/`post_hooks` (resolver + schema:
`specs/components/skills.md` → "Mission hooks"). This is **distinct from `hooks.py`**:
`hooks.py` runs operator-authored Python from the trusted `instance/` tree; this runs
shell from a **repo-controlled** file, so it is a gated, separately-audited surface.

**Operator opt-in gate (default off).** Nothing runs unless the operator opts in.
`mission_hooks.hooks_enabled(project_name)` = per-project override
(`projects_config.get_project_mission_hooks` — the `mission_hooks:` bool in
`projects.yaml`) if set, else the global `config.is_mission_hooks_enabled()`
(`mission_hooks.enabled` in `instance/config.yaml`, **default `False`**). Mirrors the
`review_dispatch`/`ci_dispatch` opt-in pattern. When disabled, `run_pre_hooks` /
`run_post_hooks` no-op and log one "skipped (not enabled)" diagnostic.

**Executor.** `run_pre_hooks(project_path, project_name, mission_type)` and
`run_post_hooks(project_path, project_name, mission_type, success)` gate-check, then
run each resolved command with `subprocess.run(cmd, shell=True, cwd=project_path,
env={**os.environ, KOAN_MISSION_TYPE, [KOAN_MISSION_STATUS]}, capture_output=True,
text=True, timeout=MISSION_HOOK_TIMEOUT)`. Post-hooks set `KOAN_MISSION_STATUS` =
`success`/`failure`. Best-effort: each command is error-isolated (a non-zero exit,
`TimeoutExpired`, or launch error is logged with bounded output and never aborts the
mission, blocks later commands, or raises). `mission_type =
skill_dispatch.mission_command_name(mission_title)`.

**Call sites (no double-fire).** Skill-dispatched missions return from
`_handle_skill_dispatch` before the agent-loop `pre_mission` fire, so the **pre**
sites are mutually exclusive. For **post**, note that skill dispatch *does* flow
through `mission_runner.run_post_mission` (`run._run_skill_mission` calls it with
`is_skill_dispatch=True`) and therefore reaches `_fire_post_mission_hook` — so that
function fires the repo post-hooks **only for the agent-loop path** (guarded on
`not is_skill_dispatch`); skill-dispatch post-hooks fire in
`_handle_skill_dispatch`'s `finally` (which also covers skill exit paths that never
reach `run_post_mission`, e.g. a runner exception). Exactly one post-fire per
mission on either path:

| Path | Pre | Post |
|---|---|---|
| Skill dispatch | `mission_executor._handle_skill_dispatch`, before the `_run_skill_mission` `try` | same function's `finally`, `success = exit_code == 0` (fires on success, failure, early-return, `KeyboardInterrupt`). `_fire_post_mission_hook` suppresses the repo post-hook here via `is_skill_dispatch`. |
| Agent loop | `mission_executor._run_iteration`, at the `pre_mission` fire site | `mission_runner._fire_post_mission_hook` (`is_skill_dispatch=False`), `success = exit_code == 0` |

### Usage source selection (authoritative OAuth anchor)

`usage.md` — the session/weekly percentages `usage_tracker` parses to pick a mode
— is produced by `usage_estimator._write_usage_md`. By default those percentages
come from the **local heuristic**: an accumulated token counter divided by
configured `session_token_limit` / `weekly_token_limit`, with wall-clock reset
windows. Issue #2455 adds an **optional authoritative anchor** in front of that
heuristic:

- `oauth_usage.py` reads the Claude Code CLI's OAuth **access** token (from
  `~/.claude/.credentials.json`, then the macOS keychain service
  `Claude Code-credentials`; field `claudeAiOauth.accessToken`) and GETs the
  **undocumented** endpoint `https://api.anthropic.com/api/oauth/usage` with the
  **unstable** `anthropic-beta: oauth-2025-04-20` header. It maps `five_hour` →
  session and `seven_day` → weekly (per-model buckets are preserved but unused by
  the shim), honours 429 `Retry-After`/backoff, and on 401 re-reads the
  (CLI-rotated) access token **once** — it never spends or rotates the refresh
  token.
- `authoritative_usage.py` is the source-selection shim. When a poll succeeds it
  stores an **anchor** (`instance/.oauth-usage.json`): the account-wide
  percentages + real reset timestamps + the local token-counter values at poll
  time. Between polls the **local token counter interpolates** on top of the
  anchor (`anchor_pct + Δtokens/limit·100`), so per-run attribution and burn-rate
  stay on the local counter (the OAuth figures are account-wide). Polling is
  periodic (`authoritative_poll_seconds`, default 300s), not per-run.

**Invariants this feature MUST uphold:**

- **Augment, never replace.** `decide_mode`, `burn_rate`, and `quota_handler` are
  unchanged. `quota_handler` remains the last-resort reactive safety net. The
  anchor only changes the *percentages/resets written into `usage.md`*, which the
  unchanged tracker then reads.
- **Mandatory graceful degradation.** Any of: `authoritative_source: off`, a
  non-Claude provider or one with `has_api_quota()` false (Codex/Copilot/Ollama),
  an API-key user with no OAuth token, an HTTP error, or an anchor older than
  `authoritative_max_staleness_seconds` (default 900s) / past its window's reset
  → fall back to the heuristic. Session and weekly windows degrade independently.
- **Percentages stay integers** in `usage.md` (the tracker parser matches
  `(\d+)%`); the shim rounds before writing and records the chosen source in an
  HTML comment (`<!-- Usage source: oauth_usage|heuristic -->`).
- **Config flag** `usage.authoritative_source`: `auto` (default) | `oauth_usage`
  | `off`. `auto` and `oauth_usage` share the same provider/token availability
  gating; `off` disables entirely.

## Invariants

- **One-shot headless invocation, in-turn completion.** Missions run via
  `claude -p --output-format json` — a single non-interactive turn with no
  post-turn event loop. Deferred re-invocation (background monitors, scheduled
  wake-ups, "report later") is NOT available; such work is dropped and the child
  is killed. Result-bearing work MUST complete before the model ends its turn —
  enforced at the prompt layer (`_partials/cli-execution-model.md`) and supported
  by a raised Bash foreground timeout (`get_bash_foreground_timeout_ms()`,
  injected into the mission subprocess env as `BASH_DEFAULT_TIMEOUT_MS` /
  `BASH_MAX_TIMEOUT_MS` for the Claude provider only, clamped below
  `mission_timeout`). `max_turns` is
  orthogonal: default missions impose no `--max-turns` cap (`build_mission_command`
  passes `0` unless `complexity_routing` assigns a tier), and a cap-hit is
  classified as failure (`subtype: "error_max_turns"`), not a clean success.
- **Mission scratch is bounded across missions (#2354 follow-up).** A long-lived
  container's memory graph must not ratchet up from test-suite tmp leftovers. Each
  mission subprocess runs with a per-mission `TMPDIR` (reaped in the outer `finally`)
  **and** `PYTEST_ADDOPTS=--basetemp=$TMPDIR/pytest` (appended, never clobbering an
  existing value via `pytest_addopts_with_basetemp`) so pytest tmp trees land inside
  the reaped dir. As a safety net for tools that ignore `$TMPDIR`, the post-mission
  step sweeps stray `/tmp` trees (`sweep_stray_tmp_dirs`, globs from
  `cleanup.extra_tmp_globs`). The sweep MUST only remove paths directly under `/tmp`
  matching a glob, MUST NOT follow symlinks, MUST NOT remove the live `koan_tmp_dir()`
  scratch/lock dir (even though it matches `/tmp/koan-*`), and MUST skip paths owned
  by another uid. Because same-uid `/tmp/test-koan*` (KOAN_ROOT) trees written by a
  concurrent parallel session (`session_manager.spawn_session`) are covered by none of
  those guards, the sweep MUST additionally be **age-gated**
  (`cleanup.min_tmp_age_seconds`, default 600s): a tree is skipped if the newest mtime
  anywhere in it (the whole subtree, not just the top-level dir) is within the window,
  so a session mid-`make test` is never `rmtree`d out from under itself. Triage rule:
  `anon` / per-process RSS is the leak signal, not cgroup
  `memory.current` (which counts reclaimable page cache + slab); the cgroup breakdown
  is surfaced via `get_memory_status`/`health_check` when `/sys/fs/cgroup/memory.stat`
  is readable. See `docs/operations/memory-footprint.md`.
- **Kernel page cache is reclaimed universally, not just RSS (#2374).** Bounding
  `anon` (per-mission `TMPDIR` reap + stray-tmp sweep, above) does not return the
  reclaimable page cache (`file`) that mission file I/O leaves warm, and Railway's
  `/sys/fs/cgroup/memory.reclaim` is read-only. The agent loop therefore runs a
  single reclaim primitive (`app/page_cache.reclaim_page_cache`, over
  `default_reclaim_roots()` = project workdirs + `instance/` + venv + scratch dir +
  the stray per-mission `/tmp` trees matched by `cleanup.extra_tmp_globs`, own-uid
  only — those hold big *out-of-root* page-cache residuals that the standard roots
  never cover, observed live 2026-07-19 as ~570 MB of `file` pinned for ~4.5h until
  the age-gated sweep deleted the files; reclaiming their clean pages decouples the
  billed baseline from that deletion latency)
  at exactly two non-bypassable choke points: the `run_claude_task` outer `finally`
  (post-mission, after the CLI subprocess has exited) and **inside
  `loop_manager.interruptible_sleep()` itself** — the single sleep primitive every
  idle path shares (between-runs sleep, contemplative sleep, and the whole
  `_IDLE_WAIT_CONFIG` family: `focus_wait`, `passive_wait`, `schedule_wait`,
  `exploration_wait`, `pr_limit_wait`, `branch_saturated_wait`) — throttled to
  `page_cache_reclaim.idle_interval_s` (default 180s) by `maybe_reclaim_page_cache_idle()`'s
  module-level timestamp, which makes the call idempotent per tick. Wiring the
  idle hook at individual call-sites instead of inside the primitive is a
  contract violation: it is exactly how `focus_wait` shipped with no reclaim at
  all (observed live 2026-07-14: 850 MB flat billed `memory.current` on an idle
  focus-mode instance). The sweep is `posix_fadvise(DONTNEED)` on
  regular files only — strictly read-only, symlink- and non-regular-file-skipped,
  time-budgeted (`time_budget_s`) so it never stalls the loop, and a no-op where
  `os.posix_fadvise` is absent (macOS). It reuses `read_cgroup_memory_stat()` for
  the before/after `file` delta; no new cgroup parser. No per-feature opt-in exists
  by construction — a new provider or mission type inherits both hooks. Default on
  (`page_cache_reclaim.enabled: true`); `idle_interval_s: 0` keeps only the
  post-mission hook. See `docs/operations/memory-footprint.md`.
- **Every mission is contained in a cgroup scope, and the scope is torn down on
  every exit path — success included.** Bounding scratch and page cache (above) does
  nothing about *processes*: a mission that leaves a build daemon running raises the
  host's idle baseline until someone kills it by hand. A process group is
  structurally insufficient, because Gradle's build daemon (3-hour idle timeout)
  detaches to `PPID 1` with its own session and has left the mission's group by the
  time the mission ends — `os.killpg` can never reach it, while a cgroup catches
  every descendant however often it double-forks. Killing that daemon is also what
  releases the Testcontainers `ryuk` client socket it was holding, so ryuk reaps the
  containers itself. `app/mission_scope.py` is the single containment primitive:
  `launch_scoped()` wraps the spawn in `systemd-run --scope --collect
  --unit=koan-mission-<uuid>.scope --property=MemoryMax=<n>
  --property=MemoryHigh=<90% of n>` and `teardown()` MUST be called from the same
  `finally` that reaps `TMPDIR`. The `.scope` suffix is part of the contract:
  `systemctl` appends `.service` to an abbreviated unit name, so a bare name would
  make every stop, kill and property read address a unit that never existed.
  **All three** mission spawn sites go through it (`run_claude_task`,
  `_run_skill_mission`, and `session_manager.spawn_session` for parallel sessions —
  whose teardown lives in `poll_sessions` on completion and `kill_session` on abort);
  the inner `provider/__init__.py` spawn intentionally shares `review_runner`'s
  process group and so inherits the same cgroup. A spawn path added without
  `launch_scoped` is a containment hole, not merely a gap in coverage: parallel
  sessions shipped that way and were caught in production running a Gradle daemon at
  `PPID 1` (822 MB, inside Kōan's own SSH login scope) while
  `systemctl list-units 'koan-mission-*'` listed nothing. Cleanup MUST touch
  only the mission's own descendants — fleet hosts are shared, so there is no
  name-based sweep (no `pkill`, no `gradlew --stop`, no `docker prune`) and **no
  container sweep at all**. A container is a child of the Docker daemon, not of the
  mission, so no observable property distinguishes this mission's containers from a
  co-tenant's: a creation time inside the mission's window proves overlap, never
  ownership, and `docker rm -f` on that basis can destroy a live unrelated workload.
  Containers MUST therefore be left to ryuk, which reaps them when the scope
  teardown drops its client socket; a project that disables ryuk owns its own
  container cleanup.
  Teardown MUST judge the manager by result, not by reachability: `_systemctl` runs
  with `check=False`, so a refusal is a non-zero `CompletedProcess`, and treating it
  as success reports containment that never happened. A non-zero `systemctl stop` is
  disambiguated with `systemctl show -p LoadState` — `not-found` means `--collect`
  already reaped the scope (the ordinary clean ending), anything else escalates to
  `systemctl kill -s SIGKILL`. `mission_scope.stop_scope_unit()` is the single lever
  for this and `make stop` MUST use it too, keeping a registry entry whose scope it
  could not confirm stopped — that record is the only durable handle on a live scope,
  and its descendants have left the daemon's process group. A destructive action MUST
  verify its target first: a fallback `pid-<n>` record names a PID, and a PID is
  recycled, so `make stop` MUST signal its process group only after the PID's real
  start time matches the record's `started_at` — dropping a stale record afterwards
  does not undo a SIGKILL already sent to a stranger's group. The rule binds every
  escalation from a stored PID to a `killpg`, the daemon `.koan-pid-*` files
  included: `check_pidfile` verifies identity only via the flock probe, so a
  non-Python daemon (or one that died without cleaning up) falls through to a bare
  liveness check that a recycled PID passes. `stop_processes` MUST therefore
  escalate to the process group only when the PID's real start time is consistent
  with the pid file that named it, and degrade to a single-PID `os.kill` when it
  cannot be confirmed — a stale pid file after a reboot must cost one wrong signal,
  not a stranger's whole session. For the same reason
  "cannot tell" is never "contained": only `FileNotFoundError` on `cgroup.events`
  proves the cgroup is empty, and any other read failure MUST fall through to the
  manager's own confirmation. The fallback path is held to the same standard — only
  a `ProcessLookupError` from `killpg` proves the group is empty, so an EPERM
  refusal, a group that outlived SIGKILL, or a pgid that was never captured MUST
  keep the registry record rather than report a clean sweep. The rule is
  symmetric, and its mirror binds just as hard: **known-negative evidence MUST NOT
  be overridden by a guess.** `memory.events`' `oom_kill 0` is the kernel saying the
  cap did not fire, and it is readable in exactly the case this contract targets (a
  leaked daemon keeps the scope populated, so `--collect` has not reaped the
  cgroup), so the exit-status heuristic MUST be gated on evidence that is genuinely
  *unreadable*. Otherwise an unrelated SIGKILL — a co-tenant exhausting RAM and the
  kernel's *global* OOM killer taking the CLI — is relabelled a cap hit and, since
  a cap hit is never retried, permanently suppresses both retry paths. Every kill
  Kōan issues itself MUST be attributable for the same reason, the double-tap
  CTRL-C included: `_on_sigint` records it as an abort exactly as `/abort` does.
  The cap is `max(memory_min, MemTotal - memory_reserve)` with an explicit
  `memory_max` winning verbatim — a reserve **with a floor**, never a percentage of
  RAM, because Kōan's baseline is roughly constant while the fleet spans 1.9–7.7 GiB
  with no swap. A cap hit is a distinct mission result (`memory_cap_exceeded` /
  `memory_cap_detail` in the `post_mission` hook context) and MUST NOT be retried.
  The verdict belongs to the mission that produced it: it is passed *into*
  `run_post_mission` by that mission's own owner (the sequential loop's
  `_last_mission_memory_cap`, or a parallel session's own `ScopedProcess` carried on
  `SessionResult`), never read from a process-global by the pipeline — a session
  reaped in a later iteration would otherwise inherit a previous mission's flag and
  never report its own.
  A scope also outlives a hard crash of `run.py`, which never reaches `teardown()`,
  so startup MUST reconcile the registry (`stop_registered_scopes` from
  `startup_manager`, alongside the stale-`TMPDIR` sweep): a record under this
  `KOAN_ROOT` can only be a previous incarnation of this instance, and both record
  kinds are already verified before they are acted on.
  Where no scope can be created (macOS, no systemd manager) the loop falls back to
  `start_new_session=True` + a kill of the process group captured at launch — the
  pgid, not the `Popen`, because `kill_process_group()` returns at its
  `poll()` guard once the mission process is reaped and would signal nothing on the
  success path. No host is left unable to run missions. The probe MUST establish
  that a scope can actually be *created*, not merely that `systemd-run` is on PATH
  with a live manager: a manager that rejects the transient scope or its
  `MemoryMax`/`MemoryHigh` properties (an undelegated memory controller, a manager
  that refuses resource control) lets `systemd-run` start, exit non-zero and never
  exec the provider — which `Popen` reports as success, so no exception-based
  fallback can fire and *every* mission on that host would fail with empty output.
  The probe therefore creates a throwaway resource-controlled scope once per
  process and caches the verdict beside the binary lookup. The degraded-mode warning is
  once per process for the *probe* verdict (a host does not grow a `systemd-run`
  mid-run, so repeating it says nothing new) but **every occurrence** for a scope
  that fails to *start*: a manager that goes away mid-run leaves every later mission
  uncontained, and one shared one-shot budget would hide exactly the invisible leak
  this contract exists to end. Default on
  (`mission_limits.enabled: true`); `enabled: false` is a true off switch and MUST
  restore the pre-containment behaviour exactly — no scope, no registry record, and
  **no teardown sweep at all**. A disabled feature that still SIGTERM/SIGKILLs the
  mission's whole process group on every exit path leaves an operator whose mission
  deliberately backgrounds a process no configuration that turns the reaping off,
  which is the one thing a master switch on a default-on destructive feature is for.
  See `docs/operations/memory-footprint.md`.
- **`run.py` never commits to main and never merges.** This is a hard safety boundary
  enforced by prompt + convention; the loop's job is to host the subprocess, not to
  alter git state itself.
- **A missing CLI binary at startup enters an in-memory degraded (no-mission) mode.**
  `startup_manager.check_cli_binary()` probes the primary provider once via
  `cli_health.check_primary_cli()` (which wraps `CLIProvider.is_available()` /
  `shutil.which(binary())`, honoring absolute / bare-PATH / `KOAN_ROOT`-relative paths and
  the `KOAN_CLAUDE_CLI_PATH` / `cli.<role>` overrides). On a miss it logs, sends ONE ⚠️
  operator warning (all messaging backends, via `send_telegram`), and sets the in-memory
  `cli_health` flag — **never a hard stop** (chat/inbox must keep working) and **no on-disk
  signal**: the flag lives for the process lifetime and clears only on restart (PATH must
  be fixed properly). `iteration_manager.plan_iteration` gates on `cli_health.is_unavailable()`
  **before** mission/autonomous/contemplative selection (next to the passive gate),
  returning the `cli_unavailable_wait` idle action so **no** execution starts, missions stay
  Pending, and GitHub/Jira notification polling (which runs before planning) still queues
  work. The loop reminder is throttled (`cli_health.should_warn`/`mark_warned`, ~6h) so the
  operator is never flooded. Defense-in-depth for a mid-session vanish: `run.run_claude_task`
  converts a provider-binary `FileNotFoundError` into an actionable exit-127 failure (shared
  `provider.missing_binary_message`, also used by `run_command_streaming`), routing through
  the normal failure/fallback path rather than crashing the loop. `cli_unavailable_wait` sets
  `wake_on_mission=False` (like `passive_wait`) so a queued mission cannot tight-loop the gate.
- **Skill-dispatch stdout is DATA, not CLI error output.** `_classify_and_handle_cli_error`
  is called with `trust_stdout=False` for skill dispatches so a transcript is not
  mistaken for a quota/auth message. Keep that default for new dispatch pathways.
- **Every termination pathway needs a retry guard.** Stagnation kill, timeout kill,
  and CLI error all route through `_maybe_retry_mission`'s RETRYABLE check. The
  forced-restart exit (below) is the ONE sanctioned exception: it never returns to
  the retry guard at all, and hands the mission to crash recovery instead.
- **Forced restart (`/restart --force`) unwinds the process; it does not return an
  exit code.** `run_claude_task` is normally the only thing that decides a mission's
  outcome, and every other kill path returns to `_finalize_mission`. The forced path
  does not: `_force_restart_now` kills the provider process group and raises
  `SystemExit(RESTART_EXIT_CODE)` from the main thread, so `run_claude_task` **never
  returns** and `_maybe_retry_mission`/`_finalize_mission` never run. The mission is
  deliberately left In Progress and owned by `recover.py`, which re-queues it to
  Pending on the next startup — or escalates it to Failed once the mission has
  exhausted `max_crash_retries`, since the forced kill counts as a crash. Code added
  after `run_claude_task` returns is therefore NOT reached on this path; anything that
  must happen on a forced restart belongs in a `finally` (which the `SystemExit`
  unwind does run) or in recovery.
  Two mechanisms drive it, and both are load-bearing: (1) SIGUSR2, handled by
  `_on_sigusr2`, installed in `main_loop`; (2) the `force` line in `.koan-restart-run`,
  re-read every `MISSION_POLL_INTERVAL` inside the mission wait loop as the fallback
  for a lost signal, filtered by `_runner_start_time` so a marker from a previous
  incarnation cannot force a restart. SIGUSR2 delivery is blocked (`_sigusr2_deferred`)
  across the window between spawning the CLI subprocess and publishing it on
  `_sig.claude_proc`, otherwise a forced restart in that window would kill nothing and
  orphan the just-spawned session.
- **SIGUSR2 is capability-gated, never sent optimistically.** Its default disposition
  is *terminate*, so a runner without `_on_sigusr2` is hard-killed by it: no `finally`
  runs, and the provider subprocess — spawned `start_new_session=True`, hence outside
  the runner's process group — survives as an orphan mutating the worktree while the
  relaunched runner starts the next mission in the same repo. A PID/cmdline check
  (`pid_manager.signal_process`) cannot detect this, because the stale runner *is*
  `run.py`. The runner therefore publishes `.koan-run-caps` (its PID + a `sigusr2`
  line) immediately after installing the handler and clears it in `main_loop`'s exit
  `finally`; the `/restart` skill signals only when `runner_supports_force_signal`
  matches that marker to the live PID, and otherwise degrades to the polite restart
  and reports the degradation. This is not hypothetical: `/update` re-execs the bridge
  at once but lets the runner finish its mission (`CYCLE_FILE` → exit 42), so a new
  bridge routinely drives a pre-upgrade runner. Any future runner-directed signal
  whose default disposition is lethal MUST be gated the same way.
- **Mission-failure notifications route their emoji through `_mission_fail_icon()`.**
  CI-related missions (`/ci_check`, ci_dispatch `Fix CI failure:`) surface 🚦 — a
  status signal, not an alarm, per operator preference; all other failures surface ❌.
  This was scattered across call sites and kept regressing (start-transition and
  devcontainer-setup failures still hardcoded ❌). New failure-notify sites must call
  `_mission_fail_icon(mission_title)`, never inline the emoji.
- **Contemplative failures must surface, not swallow.** `_handle_contemplative`
  captures the CLI exit code and runs `_notify_contemplative_failure`, which classifies
  the outcome (529 overload, quota, auth, transient, exit-code) and sends ONE throttled
  message per outage episode (`.contemplative-failure-notify.json`, 6h cooldown). Without
  it a failed contemplative session is invisible and the agent emits generic
  "Run failed / went sideways" text. The contemplative path does NOT retry — it sleeps
  and the next iteration retries naturally.
- **Provider gateway overloads (HTTP 5xx via `API Error: NNN`) are RETRYABLE.**
  OpenAI-compatible gateways behind the Claude CLI surface 529 as
  `API Error: 529 [..][The service may be temporarily overloaded...]`; `cli_errors`
  matches `api error: 5\d\d` and `temporarily overloaded` so these classify as
  RETRYABLE, not UNKNOWN.
- **Quota signals come from the summary stream, not assistant text.** Clean `output`
  must never carry quota signals; read `stream_summary` (`cli_runtime_quota_signal`).
- **`reset_display` is shared by the chat warning, `.koan-pause` (`/status`), and the
  journal — it must stay clean.** `_RESET_RE` is bounded so a one-line CLI JSON result
  can't dump its tail into it; `parse_reset_time` handles minute-precision times (`8:40am`).
  Quota chat warnings go through `_notify_raw` (`_notify_quota_warning`) with the raw
  output fenced in a code block — `_notify` runs the Claude reformatter, which strips
  markdown fences, so a code block would never render that way.

## Integration points

- Reads missions via `missions.py`; writes status to `.koan-status`.
- Mode + affordability from `usage_tracker.py` / `burn_rate.py`.
- Provider invocation through `provider/` (subprocess, lock under `koan_tmp_dir()`).
- Skill missions handed to `skill_dispatch.py`.
- Post-mission: `git_auto_merge.py`, `security_review.py`, memory + journal writes.

## Known debt / watch-outs

- **Silent timeouts are the dominant failure mode** — CLI can hang with zero stdout
  until the 7200s watchdog. A resettable-deadline timer would catch stuck sessions far
  faster than post-kill JSON-completeness checks.
- Retry-guard gaps: introducing a new kill/abort mechanism without a `_maybe_retry_mission`
  guard silently drops retryable missions. The forced-restart exit is the single
  sanctioned exception (see the invariant above) — it bypasses the guard by design and
  delegates to `recover.py`. Do not read it as precedent: any *other* new kill path
  still needs the guard.
- `_run_iteration` is large; the dispatch layer was extracted to `mission_executor` to
  keep `run.py` focused on the execution host. Resist re-merging them.

## Change protocol

Changes to the lifecycle state machine, error classification, or subprocess monitoring
must update this spec and add tests via `test_run.py` (drives `run._run_iteration`) plus
`mission_executor` patch points (`app.skill_dispatch.*`, `app.run.*`).
