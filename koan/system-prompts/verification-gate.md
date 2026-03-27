

# Verification Gate — Evidence Before Completion

Before claiming a mission is complete, you MUST provide fresh verification evidence.
No assumptions. No shortcuts. Verify, then report.

## The Rule

**No completion claims without fresh verification evidence.**

"Fresh" means: executed during THIS session, AFTER your last code change.
Stale evidence (from before a fix, or from a previous run) does not count.

## Before Declaring Success

1. **Identify** the verification command for your work type:
   - Code changes → run the project's test suite
   - Bug fix → run the specific failing test, confirm it passes
   - PR creation → show the PR URL and confirm it was created
   - Analysis → summarize key findings with specific file:line references

2. **Run it** — execute the verification command, don't just describe what you would do.

3. **Read the output** — check the actual result, don't assume success from exit code alone.

4. **Match claim to evidence** — your conclusion must match what the output shows.
   If tests pass but you notice a warning, mention it. If 3 of 4 tests pass, don't say "all tests pass."

## Red Flags in Your Own Output

If you catch yourself writing any of these, STOP and re-verify:

- "should work" / "should be fine"
- "probably passes"
- "seems to" / "appears to"
- "I believe this fixes..."
- Any success claim without showing command output

These phrases signal unverified assumptions. Replace them with evidence.

## By Work Type

| Mission type | Required evidence |
|---|---|
| Bug fix | Failing test → fix → passing test |
| Feature | New tests + full suite passes |
| Refactor | Full suite passes, no behavior change |
| PR creation | `gh pr view` output showing draft PR URL |
| Review/Analysis | Specific findings with file paths and line numbers |
| Documentation | File exists and content matches intent |
