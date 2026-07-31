---
type: skill-spec
title: "Skill Spec — plan"
description: "Documents the `/plan` skill that deep-thinks an idea (or iterates an existing issue) into a structured tracker-issue plan via a critic→regenerate loop, covered by the deterministic eval harness."
tags: [skill]
created: 2026-06-27
updated: 2026-07-31
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
- A failed comment **lookup** must never trigger a write. `jira_list_comments` degrades
  to `[]` on API failure, which is indistinguishable from "no comments"; the publisher
  uses `jira_list_comments_checked` so a broken read path cannot stack duplicate plan
  comments.
- An unverified publish fails the mission and retains the staged plan, so a later run
  republishes it without spending a model call to regenerate. The stage is dropped once
  it expires or three consecutive runs fail, after which the next `/plan` regenerates —
  a permanently undeliverable plan must not wedge the issue.

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
