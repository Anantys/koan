# Rebase — Resolve Merge Conflicts

You are resolving merge conflicts that occurred while rebasing a pull request branch onto its target.

## Pull Request: {TITLE}

**Branch**: `{BRANCH}` → `{BASE}`

### PR Description

{BODY}

---

## Conflicted Files

{CONFLICTED_FILES}

---

## Your Task

**IMPORTANT: Do NOT create new branches, switch branches, or run git rebase/merge commands.
Stay on the current branch. You are in the middle of a rebase — your job is to resolve the conflicts
in the files listed above so the rebase can continue.**

1. **Read each conflicted file.** Look for conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`).
2. **Resolve conflicts intelligently:**
   - Preserve the intent of the PR changes (the `HEAD`/`ours` side) while incorporating
     any upstream changes that don't conflict with the PR's purpose.
   - If the PR modified a section that upstream also modified, prefer the PR's version
     unless the upstream change is clearly a critical fix (security, crash fix, etc.).
   - Remove ALL conflict markers — the file must be valid, compilable code after resolution.
3. **Stage each resolved file** with `git add <file>` after editing it.
4. **Do not run `git rebase --continue`** — the caller handles that.
5. **Do not run tests.** The caller handles testing separately.

When you're done, output a concise summary of how you resolved each conflict.
