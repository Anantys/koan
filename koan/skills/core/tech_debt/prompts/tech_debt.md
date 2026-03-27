You are performing a tech debt analysis of the **{PROJECT_NAME}** project. Your goal is to produce a structured, prioritized report of technical debt.

## Instructions

### Phase 1 — Orientation

1. **Read the project's CLAUDE.md** (if it exists) for architecture overview, conventions, and key file paths.
2. **Explore the directory structure**: Use Glob to understand the project layout — source directories, test directories, config files, build files.

### Phase 2 — Scan for Tech Debt

Systematically scan the codebase for the following categories:

#### A. Code Duplication
- Search for repeated patterns, copy-pasted logic, or near-duplicate functions.
- Look for opportunities to extract shared utilities or base classes.

#### B. Complexity Hotspots
- Identify functions that are excessively long (>80 lines) or deeply nested (>4 levels).
- Look for functions with too many parameters (>6) or too many local variables.
- Check for god classes or modules that handle too many responsibilities.

#### C. Testing Gaps
- Compare source files to test files — identify modules with no corresponding tests.
- Look for critical code paths (error handling, edge cases, security boundaries) that lack coverage.

#### D. Infrastructure & Dependencies
- Check for outdated patterns, deprecated API usage, or TODO/FIXME/HACK comments.
- Look for hardcoded values that should be configurable.
- Identify missing or incomplete type annotations in key interfaces.

### Phase 3 — Produce the Report

Output a structured report in this exact format:

```
Tech Debt Report — {PROJECT_NAME}

## Summary

[2-3 sentence overview of the project's tech debt posture]

**Debt Score**: [1-10]/10

(1 = pristine, 10 = critical debt load)

## Findings

### High Priority

[Numbered list of high-priority findings with file paths and brief descriptions]

### Medium Priority

[Numbered list of medium-priority findings]

### Low Priority

[Numbered list of low-priority findings]

## Suggested Missions

1. [Most impactful improvement — one sentence]
2. [Second most impactful improvement]
3. [Third most impactful improvement]
```

## Rules

- **Read-only.** Do not modify any files. This is a pure analysis task.
- **Be specific.** Always include file paths and line numbers in findings.
- **Be actionable.** Each finding should suggest what to do, not just what's wrong.
- **Prioritize by impact.** High-priority items are those that cause bugs, block features, or slow down development. Low-priority items are cosmetic or minor.
- **Limit scope.** Report at most 5 findings per priority level. Focus on the most impactful issues.
- **Suggested missions must be self-contained.** Each should be achievable in a single focused session.
