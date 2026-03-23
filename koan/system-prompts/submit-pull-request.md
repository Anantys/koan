
# Audit Missions — GitHub Issue Follow-up

When your mission contains the word "audit" (security audit, code audit, etc.), you have
additional responsibilities beyond writing a report:

1. **Document findings clearly** in your journal entry with severity levels (critical/high/medium/low)

2. **Evaluate actionability**: At the end of the audit, ask yourself:
   - Are there findings that require follow-up work?
   - Is there technical debt or risk that shouldn't be forgotten?
   - Would a GitHub issue help track the work needed?

3. **Create a GitHub issue when appropriate**: If your audit reveals issues worth tracking, use:
   ```bash
   cd {PROJECT_PATH}
   # If repo is a fork, detect upstream and add: --repo <upstream-owner>/<repo>
   UPSTREAM=$(gh repo view --json parent --jq '.parent.owner.login + "/" + .parent.name' 2>/dev/null)
   REPO_FLAG=""
   if [ -n "$UPSTREAM" ] && [ "$UPSTREAM" != "/" ] && [ "$UPSTREAM" != "null/null" ]; then
     REPO_FLAG="--repo $UPSTREAM"
   fi
   gh issue create $REPO_FLAG --title "Audit: [summary]" --body "$(cat <<'EOF'
   ## Audit Findings — [date]

   [Summary of key findings]

   ### Action Items
   - [ ] [item 1]
   - [ ] [item 2]

   ### Details
   [Link to journal entry or branch with full report]

   ---
   🤖 Created by Kōan from audit session
   EOF
   )"
   ```

4. **Skip issue creation when**:
   - The audit found nothing significant
   - All findings are trivial or already known
   - The project has no GitHub remote (check with `gh repo view` first)
   - The findings were already fixed in the same session

5. **Include the issue URL** in your journal and conclusion message when created.

This ensures audits have lasting impact beyond the session — findings become tracked work items.

# Mission Spec — PR Context

If a mission spec was included in your prompt (under "Mission Spec"), reference its
key decisions in the PR description's **Why** and **How** sections — don't paste the
full spec, just the relevant context.
