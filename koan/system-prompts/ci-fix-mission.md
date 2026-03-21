Fix the CI failure on PR #{pr_number} in project {project_name}.

PR: {pr_url}

## Failure Summary

{error_summary}

## Instructions

1. Read the CI failure logs above carefully to identify the root cause.
2. Explore the affected files in the project.
3. Fix the issue causing the CI failure.
4. Commit with a clear message explaining what was fixed.
5. Push the fix to the existing PR branch.

## Constraints

- Only fix what is failing. Do not refactor unrelated code.
- Do not change the PR title or description.
- If the failure is a flaky test (intermittent, unrelated to the change), note it in a comment but do not retry.
