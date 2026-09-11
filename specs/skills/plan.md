---
type: skill-spec
title: "Skill Spec — plan"
description: "Documents the `/plan` skill that deep-thinks an idea (or iterates an existing issue) into a structured tracker-issue plan via a critic→regenerate loop, covered by the deterministic eval harness."
tags: [skill]
created: 2026-06-27
updated: 2026-09-10
---

# Skill Spec — `plan`

## Command(s)

- **Primary:** `/plan [--iterations N] <idea>` · `/plan <project> <idea>` · `/plan <issue-url>`
- **Group:** `code`

## Purpose

Deep-think an idea and produce a structured plan as a tracker issue — or iterate on an
existing issue. Plans become the contract `implement`/`fix` work against.

See `docs/users/skills.md` for the end-user `/plan` reference and
`docs/users/user-manual.md` for the fuller walkthrough.

## Inputs

| Input | Source | Required | Notes |
|---|---|---|---|
| idea text | command arg | yes (or issue URL) | free-form |
| project name | command arg | no | scopes the plan |
| issue URL | command arg | alt | iterate on an existing plan |
| `--iterations N` | flag | no | 1–5, default 1; critic→regenerate loop, only final posted |

## Outputs / side effects

- Creates (or updates) a tracker issue via `issue_tracker.create_issue()` /
  `find_existing_plan_issue()`.
- Multi-iteration runs cost ~5× a single plan at `--iterations 3` (token-linear).

## Error cases

| Condition | Behavior |
|---|---|
| no idea/URL | reply with usage |
| unknown project | alias resolution then skip if unknown |
| `--iterations` out of 1–5 | clamp/validate |

## Integration hooks

- **Handler:** `handler.py`. **GitHub/Jira:** `github_enabled` + `github_context_aware`.
- **Combo:** paired with `implement` in `plan_implement` (`/planit`, `/doit`).

## Invariants

- Only the final iteration is posted — intermediate critic passes are internal.
- `find_existing_plan_issue()` is consulted before creating a duplicate plan issue.
- The final plan is assumption-audited before posting (`_apply_assumptions_audit`,
  gated by `plan_review.assumptions_check`): unverified-critical findings are folded
  into the plan's `### Open Questions` section so humans can resolve them on the
  tracker before `/implement`. The audit is **advisory and fail-open** — auditor
  errors leave the plan unchanged; it never blocks or suppresses posting.
- Jira issue plans keep a single **current-plan** comment, identified by a trailing
  `Koan current plan (rev <digest>)` footer whose revision is a digest of the plan
  body. Jira renders ADF text literally, so the footer is deliberately human-readable
  rather than an HTML comment. The body is staged on disk before posting and the write
  is retried three times; it counts as posted only once a read-back returns a comment
  carrying that revision — Jira's write endpoints report success for writes that never
  became a visible comment.
- **Only Koan's own comments may be written to.** Every plan comment is stamped with a
  `koan.jira.plan` entity property, supplied atomically with the create or update. A
  human-readable footer is reproducible by anyone quoting the tail of a plan, and a
  comment matched on the footer alone is a comment the next revision would *overwrite*
  and the retirement pass would blank; the property cannot be produced from Jira's
  comment editor, so it is what distinguishes Koan's parts from a reviewer's. Lookup
  and retirement therefore consider only property-carrying comments — falling back to
  footer-only matching solely when no comment on the issue carries the property, since
  a deployment that drops properties (or ignores `expand=properties`) must still be
  able to update the plan it published. In that fallback the comment's Jira author is
  the remaining guard: a comment Jira attributes to an account other than Koan's own is
  never matched, and the **retirement pass demands positive proof** — the property, or
  Jira naming Koan's account as author. A stale part left standing is recoverable; a
  human's comment blanked by the retirement pass is not.
- **The same authorship rule binds the reader.** `/implement` reassembles a multipart
  plan by footer, and the later comment claiming a part number wins it outright — so a
  reviewer who ends their reply with a quoted footer would *substitute* their prose for
  that part rather than appear to be missing one, and the incompleteness banner would
  stay silent. A comment may therefore occupy a part slot only under the same test the
  write side applies: the `koan.jira.plan` property when any comment on the issue
  carries one, otherwise Jira's authorship excluding foreign accounts. This obliges the
  issue-fetch path to carry that evidence — a comment listing that returns only author
  display name and body cannot answer the question.
- **A create that was *attempted* is never repeated.** Jira's comment listing is not
  read-your-writes, and its write path cannot tell "rejected" from "created, response
  lost" — a POST that times out is reported as a failure for a comment that exists. So
  the attempt itself, not its reported result, disqualifies a second create; that is the
  duplicate this whole path exists to prevent. Later attempts may only re-verify, or
  update in place once the comment does appear; if it never does, the publish reports
  `created_unverified` and leaves the stage for the next run. A genuinely rejected
  create therefore also stops retrying within the run, which costs nothing: the stage is
  kept and the next mission run re-verifies before writing.
- A failed comment **lookup** must never trigger a write. An empty comment list is
  indistinguishable from a failed read, so every upsert path reads through
  `jira_list_comments_checked`, which raises instead of degrading to `[]`. There is
  deliberately no lenient variant — a broken read path must not be able to stack
  duplicate plan comments.
- An unverified publish fails the mission and retains the staged plan, so a later run
  republishes it without spending a model call to regenerate. The replay only applies
  when the later run adds nothing: a `/plan` carrying user instructions or a base
  branch **must regenerate**, because the stage predates those instructions and
  republishing it would drop them while reporting success. A replayed publish says so
  in its outcome instead of reading as a freshly generated plan. The stage is dropped
  once it expires or three consecutive runs fail, after which the next `/plan`
  regenerates — a permanently undeliverable plan must not wedge the issue.
- A plan exceeding one Jira comment is split at paragraph (then line, then word)
  boundaries into sequential parts, each footered `(rev <digest>, part N/M)` and
  verified independently. Parts are located by **part number, not revision**, so a new
  revision updates the comments in place instead of posting a second set; parts left
  over when a plan shrinks are retired. Retirement is scoped to comments this publish
  did **not** write: a part just written and read-back verified is never an orphan,
  however stale the listing that drives the pass looks. Jira's comment read path is
  not read-your-writes, so that listing can still be serving the pre-edit body —
  old revision, plan property intact — and retiring on that evidence would destroy
  the plan just published while reporting success. Jira's public REST API exposes no
  reply-to-comment operation, so parts carry `?focusedCommentId=` previous/next links
  rather than being threaded — those links are attached in a second pass, once every
  part has an id.

## Evaluation

The `plan` skill is covered by the eval harness (`koan/app/skill_evals.py`;
design in `specs/003-core-skill-evals/`).

- **What's scored:** the plan markdown — required-section presence (`### Summary`,
  `### Alternatives Considered`, `### File Map`, `### Verification Criteria`),
  min-phase count via `parse_plan_progress` (`#### Phase N:`), banned-placeholder
  absence (`TODO`/`TBD`/`FIXME`/…), and a title first line.
- **Golden dataset:** `koan/skills/core/plan/evals/cases/*.json` —
  `dashboard_feature`, `refactor`, `bugfix_plan`.
- **CI:** offline scorer + dataset-validity tests run in the `fast` group and
  never call the Claude subprocess.
- **Live:** `KOAN_EVAL_LIVE=1 python -m app.skill_evals plan --live` builds the
  plan prompt and runs it over the dataset, comparing to `evals/baseline.json`.

**Contract:** changing the plan output format (`prompts/plan.md` or the
`_partials/plan-*` sections) MUST be reflected in the golden cases / baseline.

## Known debt / watch-outs

- Iteration cost scales linearly; surface the cost expectation to users.
