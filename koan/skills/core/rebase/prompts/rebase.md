# Rebase — Apply Review Feedback

You are rebasing a pull request and applying changes requested by reviewers.

## Pull Request: {TITLE}

**Branch**: `{BRANCH}` → `{BASE}`

### PR Description

{BODY}

---

## Current Diff

```diff
{DIFF}
```

---

## CI Failures

{CI_FAILURES}

## Review Comments (inline on code)

{REVIEW_COMMENTS}

## Reviews (top-level)

{REVIEWS}

## Conversation Thread

{ISSUE_COMMENTS}

---

## Your Task

**IMPORTANT: Do NOT create new branches or switch branches with git checkout/switch.
Stay on the current branch. Your changes will be committed and pushed automatically.**

1. **Fix CI failures first.** If there are CI failures listed above, diagnose and fix them before addressing review comments.
2. **Read all review comments carefully.** Identify actionable change requests vs. discussion or questions.
3. **Implement the requested changes.** Edit the code to address each actionable review comment.
   - Skip comments that are questions, acknowledgments, or discussion (not change requests).
   - If a reviewer requested a specific change, implement it as described.
4. **Be focused.** Only change what was requested — no drive-by refactoring, no extra improvements.
5. **Do not run tests.** The caller handles testing separately.

When you're done, output a concise summary of what you changed and why.
This summary will be used in the git commit message and PR comment, so it must
clearly explain each change and justify it by referencing the reviewer's request.
Use bullet points if you made multiple changes. Be specific about what was
modified (e.g. "Renamed `get_user()` to `fetch_user()` per reviewer request")
rather than vague (e.g. "Applied feedback").
