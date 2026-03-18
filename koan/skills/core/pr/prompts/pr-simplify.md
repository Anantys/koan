# Simplify Pass — Post-Review Readability

You are performing a readability-only simplification pass on recently changed files in this project.

Working directory: `{PROJECT_PATH}`

## Your Task

1. Run `git diff HEAD~5..HEAD --name-only` to identify files changed in recent commits.
2. Read each changed file and look for **readability issues only**:
   - Unclear variable or function names that could be more descriptive
   - Nested ternary operators (replace with if/else chains)
   - Unnecessary comments that describe what the code obviously does
   - Magic values that could use a nearby named constant
   - Dead code branches that can never execute
3. Apply **readability-only fixes** — each change must make code clearer without altering structure.
4. Do NOT: move code, change function signatures, extract new helpers, restructure control flow.
5. Do NOT: change any line that isn't directly related to a clarity improvement.
6. Prefer no change over a change that might be controversial.

Output a brief summary of what you simplified (or "No simplifications needed" if clean).
