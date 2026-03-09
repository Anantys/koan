

# TDD Mode — Red-Green-Refactor

This mission is tagged `[tdd]`. You MUST follow strict Test-Driven Development:

## Workflow

1. **RED** — Write failing tests first. Define the expected behavior through test cases before writing any implementation code. Run the tests to confirm they fail.
2. **GREEN** — Write the minimum implementation code to make the failing tests pass. Do not add functionality beyond what the tests require.
3. **REFACTOR** — Clean up both test and implementation code while keeping all tests green. Remove duplication, improve naming, simplify logic.

## Rules

- Never write implementation code without a corresponding failing test first.
- Each test should test one behavior or edge case.
- Tests must be runnable and actually assert expected behavior (no empty tests, no `pass`-only tests).
- Follow the project's existing test conventions (file naming, framework, fixtures).
- Commit tests and implementation together — the final state must have all tests passing.

## Quality Checks

- New test files must follow the project's naming convention (e.g., `test_*.py`, `*_test.py`).
- Tests should cover both happy path and edge cases.
- Avoid testing implementation details — test behavior and public interfaces.
