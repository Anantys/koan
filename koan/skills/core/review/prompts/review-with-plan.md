# Code Review with Plan Alignment

You are performing a code review on a pull request that was created from a
structured plan. Your task has two parts:

1. **Plan alignment** — verify that the implementation matches the plan's intent
2. **Code quality** — standard review for correctness, security, and maintainability

## Pull Request: {TITLE}

**Author**: @{AUTHOR}
**Branch**: `{BRANCH}` -> `{BASE}`

### PR Description

{BODY}

---

## Original Plan

This PR was created to implement the following plan. Use it as the source of
truth for what *should* be built. **Do not trust the PR description** — verify
each plan requirement independently against the actual diff.

{PLAN}

---

## Current Diff

```diff
{DIFF}
```

---

## Existing Reviews

{REVIEWS}

## Existing Comments

{REVIEW_COMMENTS}

{ISSUE_COMMENTS}

## Repliable Comments (with IDs)

{REPLIABLE_COMMENTS}

---

## Your Task

### Part 1: Plan Alignment

Read the plan carefully, then inspect the diff. For each requirement described
in the plan's `### Implementation Phases` section, determine:

- **Met**: The diff implements this requirement. Be specific — name the file and
  what was added.
- **Missing**: The requirement is not present in the diff, or only partially
  implemented. Be specific about what is absent.
- **Out of scope**: Changes in the diff that are not mentioned in the plan
  (neutral — neither good nor bad, but worth noting).

**Critical rule**: Do NOT trust the PR description's claims about what was
implemented. Verify each claim against the actual diff.

### Part 2: Code Quality

Analyze the code changes and produce a structured review. Focus on:

1. **Correctness** — Logic bugs, edge cases, off-by-one errors, race conditions
2. **Security** — Injection, authentication gaps, data exposure, unsafe operations
3. **Architecture** — Design issues, coupling, abstraction level, naming
4. **Maintainability** — Readability, complexity, test coverage gaps

### Review Checklist

Use the following checklist to guide your review. Check each item *if applicable* to the
files in the diff — skip items that don't apply to the changes under review.

**Security**
- Check for SQL/command injection, shell interpolation of user input
- Check for hardcoded secrets, API keys, or credentials
- Check for unsafe deserialization (`pickle.loads`, `yaml.load` without `SafeLoader`)
- Check for path traversal (unsanitized user input in file paths)
- Check for missing input validation at system boundaries (API endpoints, CLI args)

**Error Handling**
- Check for bare `except:` or `except Exception` that swallows errors silently
- Check for missing cleanup in error paths (unclosed files, unreleased locks)
- Check for resource leaks (sockets, file handles, database connections)
- Check for error messages that expose internal details to end users

**Performance**
- Check for N+1 queries or repeated I/O in loops
- Check for unbounded collections that grow without limit
- Check for missing pagination on list endpoints or queries
- Check for unnecessary copies of large data structures

**Testing**
- Check for untested code branches introduced by the changes
- Check for missing edge case coverage (empty input, boundary values, None)
- Check for test isolation issues (shared state, order-dependent tests)

**Python-specific** (apply only when Python files are in the diff)
- Check for mutable default arguments (`def f(x=[])`)
- Check for `is` vs `==` misuse with literals
- Check for unsafe `eval()`/`exec()` usage
- Check for missing `with` statement for resource management

### Replying to Comments

If there are repliable comments listed above, review each one and decide whether a reply
would add value. Reply when:

- A user asks a question (about design decisions, implementation choices, trade-offs)
- A user raises a concern that you can address with technical detail
- A comment contains a misconception you can clarify
- A reviewer requests changes and you can explain the rationale or suggest a path forward

Do NOT reply when:
- The comment is purely informational with nothing to add
- A simple acknowledgement ("thanks", "will fix") would suffice
- The comment is from the PR author to themselves
- Replying would just repeat what your review already covers

When you do reply, be **complete and detailed** — explain the **why** and **how**, not just
the what. Reference specific code, line numbers, or documentation to support your argument.

### Rules

- Be specific: reference file names and line ranges from the diff.
- Prioritize: separate blocking issues from minor suggestions.
- Skip praise — focus on what needs attention.
- If the code is solid, say so briefly. Don't invent problems.
- Do NOT modify any files. This is a read-only review.

### Output Format

Your ENTIRE response must be a single valid JSON object (no markdown, no code fences, no text before or after). The JSON must conform to this schema:

```json
{
  "plan_alignment": {
    "requirements_met": [
      "Phase 1: _detect_plan_url() added in review_runner.py (lines 42-52)"
    ],
    "requirements_missing": [
      "Phase 3: --plan-url CLI flag not found in main() argument parser"
    ],
    "out_of_scope": [
      "review_schema.py: added PLAN_ALIGNMENT_SCHEMA (not mentioned in plan but consistent)"
    ]
  },
  "file_comments": [
    {
      "file": "path/to/file.py",
      "line_start": 42,
      "line_end": 42,
      "severity": "critical",
      "title": "Short issue title",
      "comment": "Detailed explanation of the issue and suggested fix.",
      "code_snippet": "relevant code or empty string"
    }
  ],
  "review_summary": {
    "lgtm": false,
    "summary": "Final assessment paragraph.",
    "checklist": [
      {
        "item": "No hardcoded secrets",
        "passed": true,
        "finding_ref": ""
      },
      {
        "item": "Input validation at boundaries",
        "passed": false,
        "finding_ref": "critical #1"
      }
    ]
  },
  "comment_replies": [
    {
      "comment_id": 12345,
      "reply": "Detailed reply explaining why and how."
    }
  ]
}
```

Field rules:
- **plan_alignment**: Required in this prompt. List each plan phase/requirement individually.
  - `requirements_met`: Things the plan asked for that are present in the diff.
  - `requirements_missing`: Things the plan asked for that are absent or incomplete.
  - `out_of_scope`: Diff changes not mentioned in the plan (neutral observation).
- **file_comments**: Array of per-file inline comments. Empty array `[]` if no issues found.
- **file**: File path as shown in the diff (e.g. `src/auth.py`).
- **line_start** / **line_end**: Line numbers from the diff. Same value for single-line issues. Use `0` for whole-file comments.
- **severity**: Must be exactly one of: `"critical"` (blocking, must fix), `"warning"` (important, should fix), `"suggestion"` (nice to have).
- **title**: Short title for the issue.
- **comment**: Detailed explanation with suggested fix.
- **code_snippet**: Relevant code illustrating the issue. Empty string `""` if not needed.
- **lgtm**: `true` if the PR is merge-ready with no blocking issues, `false` otherwise.
- **summary**: Final assessment — what's good, what needs fixing, merge readiness.
- **checklist**: Review checklist results. Empty array `[]` for trivial changes.
- **comment_replies**: Optional. Omit or use `[]` if no replies are warranted.

All fields in `file_comments` and `review_summary` are required. Use empty strings `""`, empty arrays `[]`, or `false` as sentinel values — never omit a field.

IMPORTANT: Output ONLY the JSON object. No markdown formatting, no explanatory text, no code fences around the JSON.
