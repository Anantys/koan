---
type: skill-spec
title: "Skill Spec — rebase"
description: "Documents the `/rebase` skill that rebases a PR onto its current base by default and, with `--fix` (or any trailing context), also addresses review feedback, including its already-solved detection JSON scored by the eval harness."
tags: [skill]
created: 2026-06-27
updated: 2026-09-10
---

# Skill Spec — `rebase`

## Command(s)

- **Primary:** `/rebase [--now] [--fix] <pr-url> [context]`
- **Aliases:** `rb`
- **Group:** `pr`

## Purpose

Rebase a PR onto its current base — the standing workflow for keeping a Kōan PR
current and merge-ready. **By default `/rebase` performs only the rebase**
(rebase onto the base branch, resolving conflicts). The review-feedback leg
(read PR comments and apply requested changes) is **opt-in via `--fix`** — a
plain `/rebase` no longer applies feedback.

`--fix` is **implied by any trailing text after the URL** (a focus area or a
severity keyword), so `/rebase <url> address the auth bug` and
`/rebase <url> critical` both address feedback; only a bare `/rebase <url>`
rebases without touching feedback. `/fix` on a PR URL redirects here **with
`--fix`**.

See `docs/users/skills.md` for the end-user `/rebase` reference and
`docs/users/user-manual.md` for the fuller walkthrough.

## Inputs

| Input | Source | Required | Notes |
|---|---|---|---|
| PR URL | command arg | yes | parsed by `github_url_parser` |
| `--now` | flag | no | queue at top |
| `--fix` | flag | no | also address review feedback; implied by any trailing context |
| trailing context | command arg | no | threaded into the queued mission; implies `--fix` |

## Outputs / side effects

- Queues a rebase mission (`model_key: mission`); runs via `rebase_pr.py`.
- Updates the PR branch (force-push with multi-account token resolution if needed).
- After the pipeline's final push, the force-push content-preservation guard
  (`force_push_guard.py`, see `specs/components/git-github.md`) compares the
  pre-rebase PR head against what was pushed; if original PR content was dropped
  or modified, or a concurrent push was clobbered/raced, the PR comment opens
  with one CAUTION/WARNING callout naming the recoverable pre-rebase SHA.
- Commit messages shaped by `commit_conventions.py`.

## Error cases

| Condition | Behavior |
|---|---|
| invalid PR URL | reply with usage |
| force-push 403 (fork owned by other account) | recovery via `claude_step._force_push` using `gh auth token --user <owner>` |

## Integration hooks

- **Handler:** `handler.py` (also the redirect target of `fix`, which injects `--fix`).
- **GitHub:** `github_enabled` + `github_context_aware`.
- **Combo:** second leg of `review_rebase` (`/rr`), which passes `--fix` so the
  rebase leg addresses the review it just generated (`sub_commands: [review,
  "rebase --fix"]`).

## Invariants

- Post-URL context must thread into the queued mission and feedback prompt as a
  fenced explicit user request.
- Multi-account pushes resolve the remote owner's token; tokens redacted in logs.
- **Feedback leg is opt-in.** A bare `/rebase` rebases only; the feedback leg
  runs only when `--fix` is present or trailing text follows the URL. Callers
  that rely on feedback (`/fix` on a PR, `/rr`, autoreview) must pass `--fix`.
  The single decision point is `skill_dispatch._build_rebase_cmd`; the runner
  gates step 4 on `apply_feedback = fix or _FEEDBACK_ON_BY_DEFAULT`.
- A feedback run that makes no commit must return a structured `SKIPPED:`
  disposition. Otherwise it fails before force-pushing and cannot be reported
  as a simple rebase.
- **"Makes no commit" means HEAD did not move, not merely that the worktree is
  clean.** A feedback agent may commit its own work — and for a
  commit-message-only request (a wrong ticket key in the subject) `git commit
  --amend` on the current branch is the only route, since there is nothing left
  in the worktree for the runner to stage. `run_claude_step` credits that
  agent-authored HEAD move as a commit (see `specs/components/git-github.md`),
  so it reaches the push instead of failing as `feedback_no_disposition`.
- Conflict resolution treats `HEAD`/`ours` as the current target branch and
  `theirs` as the replayed PR commit. It verifies no unmerged paths remain
  before continuing. Its per-round agent budget is `rebase_conflict_timeout`
  (600 seconds by default); exhaustion aborts the rebase and preserves the
  existing recreate fallback.
- **The rebase PR comment never auto-links a reviewer reference.** The feedback
  agent numbers reviewer points after the review comment's own finding IDs
  ("reviewer #5", "#7"). The rendered body is passed through
  `app.github.escape_issue_refs`, which rewrites every bare `#N` (and the
  `owner/repo#N` form) to a fullwidth `＃N` outside code spans, fenced blocks and
  URLs — so a reviewer point never renders as a link to an unrelated repository
  issue or PR. Nothing this comment emits is ever a deliberate issue link.
- **Force pushes are guarded, not gated.** The content-preservation guard runs
  after the pipeline's last push (private-gate re-pushes included — the gate
  feeds its own pre-push observations and pushed SHA back into the guard's
  `push_state`), is best-effort — both the whole post-push call site and the
  pre-push observations are wrapped, so not even an unexpected guard bug can
  fail a rebase whose push already landed, nor stop or divert the push it only
  observes — and
  reports via the PR comment + the un-gated outcome channel (`notify_outcome`,
  so the finding is not swallowed by normal messaging mode) — it never blocks
  or reverts a push. Detection scope: dropped/modified original PR content
  (patch-id screening plus a separate merge-commit pass, refined by
  content-level survival checks, so upstream squash-merges and context-line
  drift never warn), clobbered mid-pipeline pushes by others (including a tip
  that only surfaces when `--force-with-lease` is rejected; observations of a
  remote whose push then failed are discarded, since that branch was never
  overwritten), and a post-push remote mismatch against the recorded pushed
  SHA (race). Without a confirmed pushed SHA the guard skips rather than
  comparing against local HEAD, and any check that could not run is listed as
  unverified instead of passing silently. The
  warning always includes the full pre-rebase head SHA and a shell-quoted
  recovery command targeting the actual push remote.

## Transition (temporary)

The default flipped from "rebase + feedback" to "rebase only" on 2026-07-17. To
avoid a silent surprise, a bare `/rebase` surfaces a temporary notice (chat reply
+ PR comment via `build_alert("NOTE", …)`) pointing users to `/fix` (or
alternatively `/rebase --fix`).
The notice is date-gated by `rebase_transition.FIX_NOTICE_DEADLINE`
(2026-08-17) and disappears automatically; the behavior change is permanent.
After the deadline the notice code + `_FEEDBACK_ON_BY_DEFAULT` are removed
(`apply_feedback = fix`).

## Evaluation

The `rebase` skill is covered by the eval harness (`koan/app/skill_evals.py`;
design in `specs/003-core-skill-evals/`).

- **What's scored:** the already-solved decision JSON
  (`{already_solved, confidence, resolved_by, reasoning}`) from
  `_check_if_already_solved` — JSON validity, decision correctness honoring the
  production rule (`already_solved && confidence == "high"`), confidence
  validity, reasoning presence.
- **Golden dataset:** `koan/skills/core/rebase/evals/cases/*.json` —
  `already_solved` (high-confidence positive), `not_solved` (negative),
  `ambiguous` (precision trap: tangential commit, must not skip).
- **CI:** offline scorer + dataset-validity tests run in the `fast` group and
  never call the Claude subprocess.
- **Live:** `KOAN_EVAL_LIVE=1 python -m app.skill_evals rebase --live` runs the
  real already-solved check over the dataset and compares to
  `evals/baseline.json`.

**Contract:** changing the already-solved decision shape (`rebase_pr.py`) or its
prompt MUST be reflected in the golden cases / baseline.

## Known debt / watch-outs

- Order-sensitive combo `/rr` (review→rebase) must insert both sub-missions in one
  atomic locked write to preserve order and avoid TOCTOU.
