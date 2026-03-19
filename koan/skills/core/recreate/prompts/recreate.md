# Recreate — Reimplement Feature from Scratch

You are reimplementing a pull request feature from scratch. The original branch
has diverged too far from the target for a clean rebase, so you must recreate
the feature on the current codebase.

## Original Pull Request: {TITLE}

**Original branch**: `{BRANCH}` → `{BASE}`

### Original PR Description

{BODY}

---

## Original Implementation (diff)

Study this diff carefully to understand what the feature does.
Use it as **inspiration**, not as a copy-paste source. The codebase has changed
since this was written — adapt the implementation to the current state.

```diff
{DIFF}
```

---

## Review Comments (inline on code)

{REVIEW_COMMENTS}

## Reviews (top-level)

{REVIEWS}

## Conversation Thread

{ISSUE_COMMENTS}

---

## Your Task

You are working on a **fresh branch** created from the current `{BASE}`.
The codebase may have changed significantly since the original PR was written.

**IMPORTANT: Do NOT create new branches or switch branches with git checkout/switch.
Stay on the current branch. Your changes will be committed and pushed automatically.**

1. **Understand the intent.** Read the PR description, diff, and comments to
   understand what the feature is supposed to do and why.

2. **Study the current codebase.** Before writing code, read the relevant files
   in their current state. The original diff references files that may have been
   moved, renamed, refactored, or deleted.

3. **Reimplement the feature.** Write the code fresh, adapting to the current
   architecture. Follow existing patterns and conventions.
   - If review comments requested changes, incorporate them in your implementation.
   - If the original implementation had issues noted by reviewers, fix them.
   - Do NOT blindly copy the original diff — the codebase has changed.

4. **Write or update tests.** The feature should have test coverage. Tests should validate behavior (inputs → outputs). Mocking dependencies is fine, but never inspect source code to verify code presence or absence.

5. **Keep it focused.** Only implement what the original PR intended.
   No drive-by refactoring, no extra improvements beyond what was requested.

When you are done, output a concise summary of what you implemented and
how it differs from the original (if at all).
