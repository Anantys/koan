You are performing a codebase audit of the **{PROJECT_NAME}** project. Your goal is to find optimizations, simplifications, and potential issues — then produce a structured report that will be used to create individual GitHub issues.

{EXTRA_CONTEXT}

## Instructions

### Phase 1 — Orientation

1. **Read the project's CLAUDE.md** (if it exists) for architecture overview, conventions, and key file paths.
2. **Explore the directory structure**: Use Glob to understand the project layout — source directories, test directories, config files, build files.
3. **Read recent git history**: Use `git log --oneline -20` to understand current development focus.

### Phase 2 — Deep Analysis

Systematically scan the codebase. Look for issues across these categories:

#### A. Code Simplification
- Overly complex logic that could be simplified
- Redundant conditions, unnecessary nesting, verbose patterns
- Functions doing too many things that should be split

#### B. Optimization Opportunities
- Inefficient algorithms or data structures
- Redundant I/O operations (file reads, API calls, subprocess spawns)
- Missing caching opportunities
- Unnecessary memory allocations or copies

#### C. Duplication & Refactoring
- Copy-pasted logic that should be extracted
- Near-duplicate functions with minor variations
- Patterns repeated across modules that deserve a shared utility

#### D. Robustness & Edge Cases
- Missing error handling for likely failure modes
- Race conditions or TOCTOU issues
- Silent failures (bare except, swallowed errors)
- Unvalidated inputs at system boundaries

#### E. Dead Code & Cleanup
- Unused imports, variables, functions, or classes
- Commented-out code blocks
- Stale TODO/FIXME/HACK comments
- Obsolete compatibility shims

### Phase 3 — Produce Findings

For EACH finding, produce a block in this exact format. Use `---FINDING---` as separator between findings:

```
---FINDING---
TITLE: <type>: <concise one-line summary>
SEVERITY: <critical|high|medium|low>
CATEGORY: <simplification|optimization|duplication|robustness|cleanup>
LOCATION: <file_path:line_range>
PROBLEM: <2-3 sentences explaining what's wrong and why it matters>
WHY: <1-2 sentences on the impact — bugs, performance, maintainability, readability>
SUGGESTED_FIX: <Concrete description of what to change. Include a brief code sketch if helpful.>
EFFORT: <small|medium|large>
```

### Severity Guide

- **critical**: Security vulnerability, data loss risk, or correctness bug
- **high**: Performance bottleneck, reliability issue, or significant maintainability problem
- **medium**: Code smell, suboptimal pattern, or moderate simplification opportunity
- **low**: Style improvement, minor cleanup, or cosmetic issue

### Effort Guide

- **small**: < 30 minutes, single file, straightforward change
- **medium**: 1-2 hours, possibly multiple files, requires some design thought
- **large**: Half day+, cross-cutting change, may need new tests or migration

## Rules

- **Read-only.** Do not modify any files. This is a pure analysis task.
- **Be specific.** Always include exact file paths and line numbers.
- **Be actionable.** Each finding must have a concrete suggested fix, not just "improve this".
- **Quality over quantity.** Report at most 10 findings. Focus on the most impactful issues.
- **No trivial findings.** Skip style-only issues unless they actively harm readability.
- **Each finding must be self-contained.** A developer should be able to understand and fix it from the issue alone.
- **Use the exact separator format** (`---FINDING---`) so findings can be parsed programmatically.
