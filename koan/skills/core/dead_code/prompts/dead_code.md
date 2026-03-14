You are performing a dead code analysis of the **{PROJECT_NAME}** project. Your goal is to produce a structured report of unused code that can be safely removed.

## Instructions

### Phase 1 — Orientation

1. **Read the project's CLAUDE.md** (if it exists) for architecture overview, conventions, and key file paths.
2. **Explore the directory structure**: Use Glob to understand the project layout — source directories, test directories, config files.
3. **Identify the primary language(s)** and any frameworks in use (Django, Flask, React, etc.).

### Phase 2 — Scan for Dead Code

Systematically scan the codebase for the following categories. For each finding, verify it is truly unused by searching for references across the entire codebase (including tests).

#### A. Unused Imports
- Search for imported names that are never referenced in the importing module.
- Check `__init__.py` re-exports — modules re-exported for public API are NOT dead code.
- Skip `__all__` entries and wildcard imports from analysis.

#### B. Unused Functions & Methods
- Identify functions/methods defined but never called anywhere in the codebase.
- **Check test files** — functions used only in tests are NOT dead code.
- **Check framework patterns** — Django views, Flask routes, pytest fixtures, click commands, dataclass methods, and similar framework-registered callables are NOT dead code.
- **Check dynamic dispatch** — functions accessed via `getattr()`, `importlib`, or string-based lookups may appear unused but aren't. Flag these as Low certainty only.

#### C. Unused Classes
- Identify classes that are never instantiated or subclassed anywhere.
- Check for framework magic: Django models, serializers, admin classes, test case classes are NOT dead code.

#### D. Dead Variables
- Find variables that are assigned but never read afterward.
- Skip loop variables, unpacking patterns (e.g. `_, value = ...`), and `__dunder__` assignments.

#### E. Unreachable Code
- Look for code after unconditional `return`, `raise`, `break`, or `continue` statements.
- Identify conditions that are always true or always false (e.g. `if False:`, constants).

#### F. Commented-Out Code
- Flag large blocks (3+ lines) of commented-out code that appear to be disabled functionality.
- These are distinct from documentation comments — look for code syntax patterns.

### Phase 3 — Produce the Report

Output a structured report in this exact format:

```
Dead Code Report — {PROJECT_NAME}

## Summary

[2-3 sentence overview of the project's dead code posture]

**Dead Code Score**: [1-10]/10

(1 = very clean, 10 = significant dead code accumulation)

## Findings

### High Certainty

[Numbered list — code that is definitely unused. Include file paths and line numbers.]

### Medium Certainty

[Numbered list — code that is likely unused but has some ambiguity (e.g. could be called via dynamic dispatch). Include file paths.]

### Low Certainty

[Numbered list — code that might be used via reflection, dynamic imports, or framework magic. Include file paths and why it's flagged.]

## Suggested Missions

1. [Most impactful removal — one sentence describing what to remove and where]
2. [Second most impactful removal]
3. [Third most impactful removal]
```

## Rules

- **Read-only.** Do not modify any files. This is a pure analysis task.
- **Be specific.** Always include file paths and line numbers in findings.
- **Verify before reporting.** For every potential finding, search the codebase for references before declaring it unused. Use Grep to search for the name across all files.
- **Respect frameworks.** Do not flag framework-registered code (routes, views, fixtures, signals, decorators) as dead code.
- **Respect test usage.** Code used only in tests is still live code.
- **Certainty levels matter.** Only flag code as High certainty when you have verified zero references exist. Use Medium/Low for ambiguous cases.
- **Limit scope.** Report at most 5 findings per certainty level. Focus on the most impactful issues.
- **Skip vendored code.** Ignore `vendor/`, `node_modules/`, `.venv/`, `dist/`, `build/` directories.
- **Suggested missions must be self-contained.** Each should be achievable in a single focused session.
