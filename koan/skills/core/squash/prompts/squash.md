You are analyzing the final state of a pull request to generate a clean commit message, PR title, and PR description.

## PR Context

- **Current title**: {{TITLE}}
- **Current description**: {{BODY}}
- **Branch**: `{{BRANCH}}` → `{{BASE}}`

## Final diff (after squash)

```diff
{{DIFF}}
```

## Instructions

Based on the final diff above, produce THREE outputs separated by the exact markers shown:

### 1. Commit message

A conventional commit message. First line is the subject (max 72 chars, imperative mood).
If the change is substantial, add a blank line then a body explaining the what and why.
Do NOT include Co-Authored-By or other trailers.

### 2. PR title

Short (under 70 chars), describes the change. Use the same style as the commit subject.

### 3. PR description

A concise markdown description (5-15 lines) structured as:
- **What**: One sentence summary
- **Why**: The problem or value
- **How**: Key implementation details worth noting

---

Output format (use these exact markers):

```
===COMMIT_MESSAGE===
<commit message here>
===PR_TITLE===
<title here>
===PR_DESCRIPTION===
<description here>
===END===
```
