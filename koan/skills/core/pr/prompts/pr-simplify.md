# Simplify Pass — Post-Review Readability

You are performing a readability-only simplification pass on recently changed files in this project.

Working directory: `{PROJECT_PATH}`

## Your Task

1. Run `git diff $(git merge-base HEAD origin/main)..HEAD --name-only` to identify files changed on this branch since it diverged from the upstream target branch.
2. Read each changed file and look for **readability issues only**:
   - Unclear variable or function names that could be more descriptive
   - Nested ternary operators (replace with if/else chains)
   - Unnecessary comments that describe what the code obviously does
   - Magic values that could use a nearby named constant
   - Dead code branches that can never execute
   - Boilerplate code that duplicates logic already available in existing helpers — replace with calls to those helpers
   - Opportunities to reuse existing helpers from the codebase rather than reimplementing similar logic
   - Cases where adding a parameter to an existing helper would eliminate duplication, rather than creating a new helper
3. Apply **readability-only fixes** — each change must make code clearer without altering structure.
4. Do NOT: change any line that isn't directly related to a clarity improvement.
5. Prefer no change over a change that might be controversial.

Output a brief summary of what you simplified (or "No simplifications needed" if clean).
