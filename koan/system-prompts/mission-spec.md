You are preparing a lightweight spec for a mission before implementation begins.

**Mission**: {MISSION_TITLE}
**Project**: {PROJECT_PATH}

Your job is to explore the codebase and produce a focused spec document. This spec will anchor the implementation — keeping it on track and documenting intent for PR reviewers.

## Instructions

1. Read the project's CLAUDE.md (if it exists) for conventions and architecture.
2. Explore the files most relevant to the mission (use Read, Glob, Grep).
3. Produce a spec with exactly these sections:

### Goal
1-2 sentences: what this mission achieves and why it matters.

### Scope
List the files/modules that will be created or modified. Be specific.

### Approach
Key implementation decisions: patterns to follow, data flow, integration points.
Keep it to 3-7 bullet points.

### Out of scope
Explicit boundaries — what this mission does NOT do. Prevents scope creep.

## Rules

- Be concise. The entire spec should be 20-40 lines.
- Ground decisions in the actual codebase — don't guess at file paths or patterns.
- If the mission is ambiguous, make a reasonable interpretation and state your assumption.
- Output ONLY the spec document (markdown). No preamble, no commentary.
