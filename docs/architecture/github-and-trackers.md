---
type: doc
title: "GitHub And Trackers"
description: "Covers GitHub/Jira notification flow, PR workflows (footer, receiving-code-review protocol), review issue-tracker enrichment, and the instance/ tracker files used to dedupe work."
tags: [architecture]
created: 2026-05-28
updated: 2026-09-10
---

# GitHub And Trackers

Koan integrates with GitHub for notifications, PR workflows, CI feedback, and
issue-style command routing. Jira can be used as an issue tracker while GitHub
remains the code review and PR surface.

## Notification Flow

GitHub and Jira notification modules fetch events, filter authorized users,
parse commands, deduplicate work, and enqueue missions. GitHub mention handling
can react to comments to mark that a command was accepted.

Context-aware skills can receive issue, PR, branch, project, and URL context
from the originating notification.

For Jira issue URLs used by `/plan`, `/fix`, and `/implement`, Koan requires a
resolved Koan project identity before continuing. Resolution order is:
1) explicit `--project-name`/mission project context, then 2) Jira key mapping
from `projects.yaml` (`projects.<name>.issue_tracker.provider: jira` with
`jira_project`). If neither resolves, the runner fails fast with an actionable
error instead of falling back to directory basename heuristics.

## PR Workflows

See `specs/components/git-github.md` for the design contract behind branch and
PR creation (draft-only, `gh`-only transport, fork-awareness invariants).

Koan-created work normally lands in branch-prefixed draft PRs. PR helpers cover
creation, review, rebasing, recreating, squashing, CI fixing, and PR quality
checks. Auto-merge is configurable and should remain guarded by project config,
security review, and sync state.

Controlled PR creation paths append a shared Kōan footer to PR bodies and
review comments. The footer includes best-effort provider/model attribution,
the submitted HEAD SHA, and elapsed runtime when that metadata is available.

When applying reviewer feedback (`/pr`, `/rebase`, `/recreate`), the prompts inject
a shared **receiving-code-review** protocol fragment
(`koan/system-prompts/_partials/receiving-code-review.md`, pulled in via
`{@include receiving-code-review}`). It directs the agent to evaluate each
substantive comment (READ→UNDERSTAND→VERIFY→EVALUATE→RESPOND→IMPLEMENT) instead of
blindly implementing it: verify the suggestion against the current codebase, apply a
YAGNI check, and push back with technical reasoning (surfaced in the summary) when a
request is incorrect — while complying when the human insists. Trivial/mechanical
feedback takes a fast-path. The review-learning extraction additionally records
pushback outcomes (validated vs. overridden) so the agent learns which pushbacks to
trust.

### Reviewer references never auto-link

A review comment numbers its findings (`1.`, `2.`, … and `warning #3` in the
checklist), and the rebase feedback agent echoes those numbers back in its
summary — "reviewer #5", "#7 (transport fallback)". GitHub auto-links any bare
`#N` in comment prose to the issue or PR with that number, so those references
used to render as links to unrelated repository issues.

`app.github.escape_issue_refs` neutralizes them: every bare `#N` (including the
`owner/repo#N` cross-repo form) becomes a fullwidth `＃N` (U+FF03), which
reads the same but is not a GitHub reference. Fenced code blocks, inline code
spans, and URLs are left verbatim, so a `…/pull/12#issuecomment-9` fragment or a
`#1f2937` hex colour survives untouched. The rebase PR comment
(`rebase_pr._build_rebase_comment`) runs its whole rendered body through it —
nothing that comment emits is ever a deliberate issue link. Comments that *do*
link real issues on purpose (the already-solved close notice's "linked to PR
#N") are not passed through it.

### Force-push content-preservation guard

A `/rebase` ends in a force-push, and a force-push can silently lose work: a
bad rebase can drop or reshape commits the PR previously carried, and a commit
someone pushes to the branch *while* the rebase is running gets erased outright.
After confirmed incidents of both, every rebase-pipeline force-push is now
followed by a detection harness (`koan/app/force_push_guard.py`; contract in
`specs/components/git-github.md`):

- **Before every pipeline push** (the main push *and* any private-gate
  re-push, whose observations are fed back via `push_state`) the remote branch
  tip is snapshotted (`git ls-remote`) and fetched, so a concurrent human push
  is known — and recoverable — before it gets overwritten. The fetch also
  refreshes the tracking ref, making the subsequent `--force-with-lease`
  compare against the freshest observation. If that lease is *rejected*, the
  remote moved since the snapshot and the plain `--force` fallback is about to
  overwrite whatever moved it — so the tip is observed again first, otherwise
  the clobbered commits could never be named. Observations belong to the remote
  they were taken on: the push tries several remotes in turn, and tips seen on
  one whose push then failed are dropped, since that branch was never
  overwritten and reporting it would be a false alarm.
- **After the pipeline's last push** the guard compares the pre-rebase PR head
  against the last SHA actually pushed. That SHA is captured before the push
  and kept only on success; with no confirmed value the guard skips rather
  than falling back to local HEAD, which may hold commits the remote never
  received. The comparison is `git cherry` patch-id screening — plus a
  separate pass over the PR's **merge commits**, which `git cherry` never
  reports and a plain rebase replays away, so a conflict resolution living
  only inside a merge would otherwise vanish unseen (their combined `--cc`
  diff is exactly the content unique to the merge) — refined by content-level
  survival checks: a commit whose files are byte-identical at both heads
  (upstream squash-merge) or whose added lines are not *removed* by the
  old→pushed diff (context-line drift) counts as preserved. Checking the
  removed side, rather than looking each added line up anywhere in the file,
  is what stops an identical line elsewhere in the same file from masking a
  reverted change. Anything the guard cannot evaluate — an unreadable file
  list, a deletion-only commit, any git failure — is reported as unverified
  rather than cleared. On top of that come a file-level cross-check for
  changes that vanished from the PR diff entirely, the list of clobbered
  concurrent commits, and a
  final `ls-remote` to detect a push that raced in right after ours — the
  window that cannot be closed client-side, only detected. Per-commit analysis
  is capped and the cap is reported, never silent.
- **Findings render as one alert** (`build_alert`) at the top of the rebase PR
  comment — CAUTION (red) when content was lost or a concurrent push was
  overwritten, WARNING (amber) when patches were only reshaped in-flight or
  the remote raced us — plus a Telegram/Slack notification sent through the
  un-gated outcome channel, so a safety finding still reaches you in normal
  messaging mode. The alert always names the pre-rebase head SHA with a
  copy-pasteable recovery command. Because that command is meant to be pasted
  into a shell and git ref names may legally contain `$()`, backticks, `;` and
  `&`, every argument is shell-quoted and the backup branch is derived from the
  SHA — no branch-controlled text ever reaches it.

A check that could not run at all — an `ls-remote` outage before or after the
push — is listed in the alert as an explicitly **unverified** check. "Could
not look" is not "nothing was there", and a safety check that silently did not
happen is worse than one that says so.

The guard **detects and warns; it never blocks or reverts**. It is best-effort
by contract: any internal failure logs, appends a "guard skipped" action, and
lets the rebase finish normally. That boundary covers the whole call site, not
just the git operations — by the time the guard runs the branch has already
been rewritten, so an unexpected bug inside it must never turn a completed
rebase into a failed mission or skip the PR comment that follows. It also
covers the part of the guard that runs *on* the push path: a failed pre-push
observation degrades to "clobber check unverified", never to a push that
silently does not happen or lands on a fallback remote instead.

## Mission status indicators (`koan/mission`)

While a GitHub-linked mission runs, Kōan surfaces a live "Running" indicator on
GitHub with no GitHub App — a `koan:working` issue label plus a `koan/mission`
commit status — reusing the existing `gh` auth. See
`specs/components/git-github.md` (Mission status indicators) for the contract
and [GitHub commands](../messaging/github-commands.md#running-indicator-koanmission)
for the user-facing config.

The orchestration lives in `koan/app/mission_status.py` and hangs off the
mission lifecycle:

1. **Start** — `_start_mission_in_file` (`run.py`) confirms the Pending→In
   Progress transition, then `start_indicator` resolves the linked issue/repo
   (from an issue URL in the mission text, else the project's `github_url`),
   adds the `koan:working` label, and records a tracker entry.
2. **First push** — `on_branch_pushed` (called from `pr_submit.py` right after
   the branch is pushed) fills the head SHA into the tracker and posts the
   `pending` commit status. The commit status is complementary: koan pushes
   late, so the label is the primary live signal.
3. **Finalize** — `resolve_indicator` (called from `_finalize_mission`) posts
   the final `success`/`failure` status and removes the label on every terminal
   path. A **stagnation requeue** deliberately leaves the indicator up (the
   mission returns to Pending and is still "running").
4. **Crash recovery** — a hard crash skips finalize, stranding a yellow
   `pending`. `startup_manager.reconcile_running_indicators` (run right after
   crash recovery) resolves any tracked mission no longer Pending/In Progress
   as `error` and removes its label.

Cross-stage state lives in `instance/.running-indicator.json`, keyed by mission
title (mirroring `.stagnation-retries.json`). It carries `{repo, issue, sha,
branch, project}` across the start → push → finalize gap. Local-only missions
(no issue URL, no `github_url`) write nothing. Every entrypoint is best-effort:
a `gh` failure is logged and never blocks the mission.

## Review Issue-Tracker Enrichment

See `specs/components/issue-tracking.md` for the design contract behind the
provider-neutral tracker abstraction this enrichment sits on top of.

When `/review` builds a PR review prompt, it can enrich the prompt with the
referenced tracker issue so Claude reviews the change against its stated intent.
References are parsed out of the PR body:

- **Jira** — keys like `PROJ-123`. Fetched via the existing Jira credentials in
  `config.yaml` (`jira:` section). Only runs for projects whose `issue_tracker`
  in `projects.yaml` maps a `jira_project`.
- **GitHub** — cross-repo refs like `owner/repo#123`, fetched via the existing
  `gh` CLI auth. In-repo `#123` refs are intentionally ignored (ambiguous
  without the current repo).

The backend is selected by the project's `issue_tracker.provider`. Output is
best-effort (any failure is silently skipped), with per-ticket excerpts capped
at 500 chars and the whole injected block at 1000 chars. At most the first 5
references are fetched (each costs a network/subprocess round-trip), so a PR
body listing dozens of tickets cannot balloon review latency or burn API quota.
Disable globally with `review_issue_context.enabled: false` in `config.yaml`
(default enabled). The fetched block — third-party text from possibly unrelated
repos/tickets — is wrapped with `fence_external_data()` (injection scanning on)
before being injected into the standard `review` prompt as `{ISSUE_CONTEXT}`, so
the reviewer agent treats it as data, not instructions. Implemented in
`koan/app/issue_tracker/enrichment.py`.

## Trackers

Tracker files in `instance/` prevent duplicate work across daemon iterations.
Examples include:

- GitHub notification and reaction tracking.
- Review comment dispatch fingerprints (`.review-dispatch-tracker.json`).
  Fetch failures (timeout/OS/`gh` error) return `None` rather than `[]`, so
  the dispatch loop skips that PR without clearing its fingerprint — a
  transient GitHub timeout must not re-dispatch the same unresolved comments
  as a duplicate mission on the next successful poll.
- CI dispatch fingerprints keyed by PR, SHA, and job.
- Remote rename and default-branch tracking.
- Burn-rate and quota-related state.
- Running-indicator state (`.running-indicator.json`), keyed by mission title,
  carrying the linked issue/repo/SHA across a mission's lifecycle. Stale entries
  from a crashed run are reconciled at startup.

Use the existing tracker module for a behavior when one exists. If a new tracker
is needed, keep its state local to `instance/`, make keys stable, and document
the deduplication rule.

User setup lives in [GitHub commands](../messaging/github-commands.md) and
[Jira integration](../messaging/jira-integration.md).
