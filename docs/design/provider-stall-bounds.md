---
type: doc
title: "Bounding a stalled provider: inactivity, not wall-clock"
description: "Why run_command_streaming's timeout never reached its read loop, why the replacement bound is on inactivity rather than duration, and why session isolation and the group SIGKILL are scoped to the armed watchdog."
tags: [design, providers, decision]
created: 2026-09-10
updated: 2026-09-11
---

# Bounding a stalled provider: inactivity, not wall-clock

The durable contract lives in `specs/components/providers.md` (§ Invariants,
"The streaming read loop must be inactivity-bounded"). This page records the
*why*.

## The symptom

On 2026-09-10 a `/review` mission produced this and nothing else:

```
[18:51:18] [review] Reviewing PR #NNN ...
[cli] Starting claude CLI session
[cli] session init (model=…)
[19:01:29] [error] No output for 600s — killing stuck process (elapsed: 619s)
[19:01:29] [error] Skill runner timed out (liveness: 600s)
```

The provider opened its stdout pipe, printed a session banner, and went silent
for ten minutes. The mission was reported as a bare "Skill runner timed out":
no verdict, no partial result, no indication of *which* pass stalled.

## Why the existing timeout did nothing

`run_command_streaming` takes a `timeout` argument, and it looks like it covers
the run. It does not. The function reads the child with a blocking

```python
for line in proc.stdout:
    ...
proc.wait(timeout=timeout)
```

`timeout` is applied only by that `proc.wait()`, which is reached **after
stdout EOF**. A provider that holds the pipe open and emits nothing never ends
the loop, so the wait — and its timeout — is never reached. The declared bound
was unreachable in exactly the scenario it appears to exist for.

The only thing that ever ended such a run was run.py's *outer* skill-runner
liveness watchdog (`first_output_timeout`, default 600s). That watchdog
SIGKILLs the whole runner, which is a blunt instrument: it kills the pipeline
mid-flight, discards work already completed by earlier passes, and attributes
nothing.

`specs/skills/review.md` already *claimed* the guarantee — "Pairs with the
provider-side per-pass stall watchdog … which makes a stalled enrichment pass
degrade to empty rather than hang" — and cited a `specs/components/providers.md`
section that did not exist. The contract was written; the code never
implemented it. This change makes reality match the declaration.

## Why inactivity and not duration

The obvious fix — actually enforce `timeout` as a wall-clock cap around the
read loop — is wrong, and would have caused a worse regression than the bug.

A healthy review pass streams progress events for many minutes: observed runs
of 5–6 minutes are routine and longer ones are legitimate on large PRs. A hard
600s cap would start killing productive work. What distinguishes a stall from a
long job is not elapsed time, it is **silence**. So the bound is on inactivity:
every consumed stdout line heartbeats a `LivenessWatchdog`, and only a gap
longer than `idle_timeout` ends the pass.

`idle_timeout` defaults to `None`, so every caller that has not opted in keeps
its exact previous behaviour.

## Why the inner bound is derived, not configured

An inner bound is only useful strictly below the outer one. At or above
`first_output_timeout` the outer watchdog fires first, SIGKILLs the runner, and
the inner bound never gets to report anything — it becomes decorative while
looking configured. Rather than add a knob an operator can silently set into
uselessness, `review_runner._review_stall_timeout()` derives the value:

- `first_output_timeout - 60`, when that leaves at least 60s;
- `0` (no inner bound) when the operator disabled the outer watchdog, or when
  the margin would leave less than the 60s floor below which a brief
  legitimate pause reads as a stall.

The margin is a flat 60s rather than half the budget, and that distinction was
caught in review. On this path the inner and outer clocks are the **same
clock**: run.py's watchdog resets on the per-event `print()` inside
`run_command_streaming`'s read loop, which is exactly what heartbeats the
inner watchdog. So `outer // 2` would not have added a bound where none
existed — it would have *halved* the silence a review pass has always been
allowed (600s → 300s). Kōan never passes `--include-partial-messages`, so
plain `stream-json` emits one line per complete message, and a single long
turn — notably the final synthesis turn on a large PR — is silent for its
whole duration. A 350s synthesis turn would have degraded to an empty verdict
where it previously finished: the exact outcome this change set out to
eliminate, reachable at half the previous threshold. All the margin has to buy
is room for the pass to fail and be reported, and `review_runner` prints
immediately afterwards, which resets the outer watchdog — so seconds suffice.

The postcondition is exact and directly tested: the result is either `0`, or a
value strictly below `first_output_timeout`.

## Why session isolation is scoped to the armed watchdog

A fired watchdog group-kills, and `run_command_streaming` previously spawned
the child **without** `start_new_session=True` — the child shared Kōan's
process group, so the kill would have sent `SIGKILL` to the daemon itself. An
armed watchdog therefore *requires* session isolation.

The first version of this change applied it unconditionally, on the reasoning
that a future caller opting in should not have to remember it. That was wrong,
and PR review caught it. Isolation is not free in the other direction:
`run.py`'s skill-runner teardown and `mission_scope`'s fallback path both reap
by **process group**, and a child in its own session is outside both. Applying
it to every call would have put each non-opted-in caller's provider beyond that
teardown while buying nothing — there is no watchdog there to protect — so a
stuck provider could outlive a skill timeout, an abort, or the outer liveness
kill, and keep burning quota after its parent was gone.

So `start_new_session=True` is passed only when `idle_timeout` is set: exactly
where a group kill can happen, and nowhere else.

**Residual, accepted.** An opted-in child *is* outside the outer group
teardown, so an abort or `skill_timeout` firing while the provider is actively
streaming leaves it running. What makes the trade worth taking is that such a
child carries its own, strictly tighter, idle bound. Closing the gap fully
would mean teaching the outer teardown to track isolated provider sessions —
worth doing, but a larger change than this fix.

## Why the kill is SIGKILL-to-the-group, not SIGTERM-first

`LivenessWatchdog` defaulted to `kill_process_group`, which SIGTERMs the group
and escalates to SIGKILL only if the **leader** is still alive after the grace
period. A descendant that handles or ignores SIGTERM survives that — and if it
inherited the stdout write end, the reader never sees EOF. The read loop stays
blocked, never reaches the `fired` check, and never reports the stall: the hang
the watchdog was armed to end, re-entered through the kill path itself.

`LivenessWatchdog` therefore gained a `graceful` flag mirroring the one
`ProcessWatchdog` already had. The class default is unchanged, but both
existing call sites now pass `graceful=False`: this one, and
`cli_exec.stream_with_timeout` (the `/rebase` review and CI phases), which
reads an inherited pipe in exactly the same shape. Leaving that one graceful
would have made `rebase_review_idle_timeout` silently ineffective against a
SIGTERM-surviving descendant, with the resulting hang misattributed to the
much looser `rebase_review_max_duration` — the invariant documented here,
violated by a caller in the same file.

The regression test spawns a child that forks a SIGTERM-ignoring grandchild
holding the same stdout, then goes silent. Under the graceful kill it blocks
for the full 30s; under the group SIGKILL it returns in about two.

## Why the watchdog is disarmed the moment stdout ends

`proc.stderr.read()` and `proc.wait()` run after stdout EOF and emit no
heartbeats. A watchdog left armed across them would SIGKILL a run that had
already streamed everything, and — since the `fired` check has already been
evaluated by then — the kill would surface as an opaque `exit -9` rather than
a stall: a successful review reported as a provider crash. So the watchdog is
disarmed at loop exit, before the stderr drain, which is what
`cli_exec.stream_with_timeout` already did.

`cancel()` is not sufficient on its own. `threading.Timer.cancel()` is a no-op
once `_fire` has begun running, and the `graceful=False` path deliberately has
no `poll()` guard — so a late fire would reach `os.killpg` on a possibly
recycled PID. `LivenessWatchdog` therefore also gained `ProcessWatchdog`'s
`mark_completed()` flag, checked under the lock at the top of `_fire()`, and
both call sites set it alongside `cancel()`.

## Related

- Contract: `specs/components/providers.md`
- Review-side pairing: `specs/skills/review.md`, `docs/design/review-consistency.md`
- Outer watchdog knob: `first_output_timeout` in `docs/users/user-manual.md`
