---
type: component-spec
title: "Component Spec — Issue Tracking"
description: "Design contract for the provider-neutral issue-tracker abstraction (GitHub/Jira) that routes fetch/comment/create calls through one service layer."
tags: [issue-tracking]
created: 2026-06-27
updated: 2026-09-10
---

# Component Spec — Issue Tracking

**Package:** `koan/app/issue_tracker/` (`base.py`, `config.py`, `github.py`, `jira.py`,
`types.py`, `enrichment.py`, `__init__.py`) + `issue_cli.py`, `notification_config.py`

## Purpose

A provider-neutral abstraction over issue trackers so the rest of Kōan never branches on
"GitHub vs Jira". Skills and prompts call one service layer; routing to the right backend
is config-driven per project.

## Architecture

```
issue_tracker/__init__.py  → service layer: fetch_issue(), add_comment(),
       │                      create_issue(), update_issue(), link_issues(),
       │                      find_existing_plan_issue()
       ├─ base.py    → IssueTracker ABC (fetch/comment/create contract)
       ├─ config.py  → get_tracker_for_project(), Jira-key→project map, repo resolution
       ├─ github.py  → GitHubIssueTracker (gh CLI backend)
       ├─ jira.py    → JiraIssueTracker (REST API backend)
       ├─ types.py   → IssueRef, IssueContent
       └─ enrichment.py → PR-review {ISSUE_CONTEXT} block from tracker refs
issue_cli.py          → CLI entry point (fetch/comment/create) used by prompts/subprocesses
```

## Key types & functions

| Symbol | Contract |
|---|---|
| `IssueTracker` (ABC) | The provider-neutral contract. New backends subclass this. `update_issue`/`link_issues` are **concrete** members with safe defaults (`False`) so existing backends keep working without overriding them. |
| `__init__.fetch_issue/add_comment/create_issue` | **Callers use these, not the backends.** No `gh issue create` / raw Jira calls in skill code. |
| `__init__.update_issue(url, body, ...)` | Rewrite an existing issue's body/description, routed to the client that owns `url`. Returns the backend's success boolean (`False` on unsupported/failed write); **never raises**, so callers degrade non-fatally. GitHub delegates to `app.github.issue_edit`; Jira PUTs an ADF description via `jira_update_issue_description`. |
| `__init__.link_issues(parent_url, child_url, link_type="Relates", ...)` | Create a **native** tracker link `parent → child`, routed to the client that owns `parent_url`. No-op (`False`) for providers that express linkage in body text (GitHub `#N`); Jira POSTs an `/issueLink`. Never raises. |
| `config.get_tracker_for_project()` | Routes a project to its configured tracker (`tracker:` section in `projects.yaml`). |
| `enrichment.py` | Parses `PROJ-123` (Jira) / `owner/repo#123` (GitHub) refs out of a PR body, fetches a capped summary, returns `{ISSUE_CONTEXT}`. Best-effort: every path returns `""` on failure. Gated by `review_issue_context.enabled`. |
| `issue_cli.py` | The subprocess/prompt-facing CLI. Agents create tracker issues via `python3 -m app.issue_cli create ...`, never `gh issue create` directly. |

## Invariants

- **Provider neutrality is the whole point.** Code outside `issue_tracker/` must not know
  whether a project uses GitHub or Jira. Branching on provider type is a design smell.
- **Tracker writes go through the service layer / `issue_cli`**, so routing, fork
  awareness, and Jira-key mapping are applied uniformly.
- **Enrichment is non-fatal.** Issue-context fetching is best-effort and must degrade to
  `""` — it must never block or fail a review.
- **Jira issue *descriptions* are rendered to rich ADF at the transport layer.**
  `jira_create_issue` and `jira_update_issue_description` build the `description`
  field via `jira_notifications.markdown_to_adf()`, which converts brainstorm's
  markdown subset (headings, unordered/ordered lists incl. `- [ ]`/`- [x]`,
  horizontal rules, blockquotes, fenced code, inline `**bold**`/`*em*`/`` `code` ``)
  into native ADF nodes; unmodeled lines degrade to a `paragraph` and empty input
  yields one empty `paragraph`. Jira *comments* (`jira_add_comment`/
  `jira_edit_comment`) render through the **same** `markdown_to_adf` path: a plan
  posted as flattened text loses the headings and code blocks `/implement` needs
  to read back, so comments and descriptions share one renderer. This supersedes
  the earlier FR-009 carve-out that pinned comments to `_text_to_adf`.
- **Native master↔sub linkage is a Jira-only concern expressed through
  `link_issues`.** `brainstorm` links its master tracking issue to each created
  sub-issue via the neutral `link_issues` service; on Jira this creates real
  "Linked issues" relationships, on GitHub it is a no-op (`#N` refs + the master's
  task list already express the relationship). Linking is best-effort — a failed
  link is logged and skipped, never aborting issue creation.
- **GitHub-only markdown extensions are folded at the transport layer, inside the
  renderer.** `tracker_comment_format.flatten_github_markdown_for_jira()` runs from
  `markdown_to_adf`, so every Jira-bound comment gets the same treatment however it
  was built: `> [!TYPE]` alert blocks fold to plain `TYPE: text`, and GitHub
  `<details>` wrappers are removed with their `<summary>` rendered as a visible
  label. Both folds are **fence-aware** — alert syntax and `<details>` markup
  inside a fenced code block are left verbatim, because there they are example
  text the plan is trying to convey, not wrappers. Alert folding stops each
  block's body run at the next opener (adjacent blocks degrade independently
  instead of merging).
- **Jira-bound text is sanitized and linked centrally.**
  `markdown_to_adf()` removes HTML comments outside inline and fenced code,
  preserves comment syntax inside those code scopes, and adds ADF `link`
  marks to bare HTTP(S) URLs as well as explicit Markdown links. Outcome
  templates express their heading, metadata list, section labels, code
  values, and labelled PR link in Markdown; the transport remains the single
  owner of ADF construction.
- **Jira outcome identity survives a hostile transport.**
  Mission-outcome comments carry the stable `(issue, command)` digest twice:
  in the `koan.jira.outcome` comment property supplied atomically with comment
  creation or update, and in a trailing visible `Kōan status · <digest>`
  footer. Upsert matches on **either**. The property is the preferred key and
  comment listing must return normalized properties so upsert can find the
  existing status without inspecting prose — but it is an optimization, not
  the identity. The footer is, because it is the only carrier the transport
  cannot silently drop: HTML comments are stripped by the shared renderer, and
  a Jira deployment that does not persist comment properties (or does not
  honour `expand=properties` on the comment-list endpoint) would otherwise
  leave the comment unfindable and stack one duplicate per mission. This is
  the same reason `/plan` comments carry a visible `Koan current plan (rev …)`
  footer.
  Because that footer is plain text anyone can reproduce by quoting the tail of
  a status, it identifies the status comment only together with **authorship**,
  under the same rule the `/plan` path applies: the `koan.jira.outcome` property
  when any comment on the issue carries one, otherwise Jira's own attribution
  excluding foreign accounts. An upsert overwrites a comment body outright, so a
  reviewer's quoted footer must never be what it edits.
  Legacy `<!-- koan-jira-outcome:… -->` markers are lookup-only migration
  inputs subject to the same authorship guard: the next update removes the
  marker and writes the footer plus the property. Lookup failure remains
  fail-closed and must never authorize creation of a potentially duplicate
  comment. A write is only reported as published once a read-back observes one
  of the two identities on a comment Koan could have written; an accepted write
  that left neither is reported as unverified rather than as success, and a
  quoted footer elsewhere on the issue does not satisfy it.
- **Every Jira comment read carries its authorship evidence.** Both comment-read
  paths — the notification-side listing and the full issue fetch — normalize
  `expand=properties` into a `{key: value}` map and surface the author's
  account id and email alongside the body. Readers decide whether a comment is
  Koan's own before acting on its contents, and a fetch shape that drops that
  evidence silently downgrades every such decision to trusting prose.

## Integration points

- `__init__.create_issue` backs `audit`, `security_audit`, `plan`, `brainstorm`, `fix`.
  `brainstorm` additionally uses `update_issue` (resolving `SUB-N` placeholders to
  real refs) and `link_issues` (master↔sub native links) — both provider-neutral.
- `enrichment.py` wired into `review_runner.build_review_prompt()`.
- Polling cadence resolved via `notification_config.py` (shared GitHub/Jira interval).
- Project routing from `projects_config` (`tracker:` override).

## Known debt / watch-outs

- Jira and GitHub have different identity/permission models; `config.py` carries the
  mapping glue (Jira key → project, code-repo resolution) — keep it the single source.
- `enrichment.py` caps context size; raising the cap risks prompt bloat in reviews.
- `_flatten_github_alerts()` defines the plain-text form of a GitHub alert
  (`TYPE: text`) independently of #2301's future `build_alert()`. When #2301
  lands, `build_alert()`'s non-GitHub degradation path should import/reuse this
  helper so there is one definition of "what alert kind X looks like in plain
  text."

## Change protocol

A new tracker backend subclasses `IssueTracker`, registers in `config.py` routing, and
updates this spec + a `docs/messaging/` page. Service-layer signature changes ripple to
every skill that creates/fetches issues — review all callers.
