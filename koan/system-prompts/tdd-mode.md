

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
- **Test behavior, not implementation.** Tests should validate what code does (inputs → outputs, observable state changes, side effects), not how it does it internally. Mocking internal dependencies of the unit under test is fine, but tests must never read or inspect actual source code to verify whether specific code is present or absent — that couples tests to implementation text rather than behavior. Prefer asserting on return values, raised exceptions, written files, or other observable outcomes. Unless the project's own test conventions explicitly say otherwise, this is the default.
