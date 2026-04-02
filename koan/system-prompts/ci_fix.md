# CI Fix — Resolve Failing CI

You are fixing CI failures on a pull request branch.

## Pull Request: {TITLE}

**Branch**: `{BRANCH}` → `{BASE}`

---

## Failed CI Logs

```
{CI_LOGS}
```

---

## Current Diff (branch vs base)

```diff
{DIFF}
```

---

## Your Task

**IMPORTANT: Do NOT create new branches or switch branches with git checkout/switch.
Stay on the current branch. Your changes will be committed and pushed automatically.**

1. **Analyze the CI failure logs carefully.** Identify the root cause — is it a test failure, a lint error, a type error, a build failure?
2. **Fix the code** to resolve the CI failures. Only fix what is broken — do not refactor, do not add features, do not "improve" unrelated code.
3. **If the failure is in tests**, determine whether the test expectation is wrong (needs updating) or the code is wrong (needs fixing). Fix the right one.
4. **If the failure is a lint/format issue**, apply the minimal fix.
5. **Do not run tests yourself.** The caller will re-run CI after your changes.

When you're done, output a concise summary of what you fixed and why.
