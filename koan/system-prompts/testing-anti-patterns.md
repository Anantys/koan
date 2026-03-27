

# Testing Anti-Patterns Reference

This mission involves writing or modifying tests. Before committing, review these common testing anti-patterns and check your work against the self-check at the bottom.

> **Note**: This reference covers *what makes a good test*. The [TDD mode] section (if present) covers *workflow* (red-green-refactor). The [Verification Gate] covers *evidence requirements* before claiming completion.

---

## Anti-Pattern 1: Testing mock behavior instead of real code

**Description**: Writing tests that only verify mocks were called, without testing that the real code actually does the right thing.

**Bad example**:
```python
def test_process_data():
    with patch("app.processor.transform") as mock_transform:
        process_data(raw)
    mock_transform.assert_called_once()  # Only proves the mock was called
```

**Why it's dangerous**: The test passes even if `process_data` passes the wrong arguments, ignores the return value, or calls `transform` at the wrong time. The mock is a stand-in for behavior — testing the stand-in proves nothing about the real behavior.

**How to fix**: Assert on the observable outcome — return value, file written, state changed — not on how the mock was called.
```python
def test_process_data():
    with patch("app.processor.transform", return_value={"ok": True}):
        result = process_data(raw)
    assert result["ok"] is True  # Proves the return value flows through correctly
```

**Red flags**: Tests that only contain `.assert_called()`, `.assert_called_once()`, or `.assert_called_with()` with no assertion on outputs or state.

---

## Anti-Pattern 2: Test-only code paths in production

**Description**: Adding methods, flags, or branches to production code solely to make testing easier.

**Bad example**:
```python
class MissionRunner:
    def __init__(self, test_mode=False):  # Only used in tests
        self.test_mode = test_mode

    def run(self):
        if self.test_mode:
            return  # Skip real work in tests
        self._do_real_work()
```

**Why it's dangerous**: Production code accumulates dead-weight for tests. The `test_mode` flag is never exercised in production, and the test verifies the skip path, not the real path.

**How to fix**: Make dependencies injectable (e.g., pass a callable or interface) so tests can substitute real behavior without modifying production logic.
```python
class MissionRunner:
    def __init__(self, executor=None):
        self._executor = executor or default_executor

    def run(self):
        self._executor()  # Tests inject a fake executor; production uses default
```

**Red flags**: `if testing:`, `if os.environ.get("TEST")`, `test_mode` parameters, or any branch that only activates in the test environment.

---

## Anti-Pattern 3: Mocking without understanding the dependency

**Description**: Patching a dependency because it's hard to use in tests, without verifying the mock accurately reflects the real dependency's behavior.

**Bad example**:
```python
def test_send_notification():
    with patch("app.notify.send") as mock_send:
        notify_user("hello")
    mock_send.assert_called_once_with("hello")
    # But real send() raises on empty token — mock never raises
```

**Why it's dangerous**: Tests pass because the mock is too permissive. When the real code runs, it encounters behaviors the mock silently swallowed (exceptions, return types, side effects).

**How to fix**: Make mocks match real behavior for the cases you care about. If the real function raises on bad input, configure the mock to raise too. If it returns a specific type, return that type.
```python
def test_send_notification_missing_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        notify_user("hello")
```

**Red flags**: Mocks that always return `None` when the real function returns structured data; mocks that never raise when the real function has error paths you care about.

---

## Anti-Pattern 4: Incomplete mocks hiding structural assumptions

**Description**: Patching at the wrong level — too high (hides branching logic) or too low (leaks internal structure into tests).

**Bad example** (patching too high):
```python
def test_mission_fails_on_bad_input():
    with patch("app.mission_runner.run_mission", return_value={"status": "failed"}):
        result = handle_mission(bad_input)
    assert result["status"] == "failed"
    # But handle_mission's own validation logic is never tested
```

**Bad example** (patching too low — couples test to implementation):
```python
def test_atomic_write():
    with patch("fcntl.flock"):  # Internal implementation detail
        atomic_write(path, content)
    # Test breaks if implementation changes locking strategy
```

**How to fix**: Patch at the *boundary* — external I/O, network calls, subprocesses, and system calls. Leave the unit under test's own logic intact.
```python
def test_mission_fails_on_bad_input():
    # Don't mock the function under test — mock its external dependency
    with patch("app.claude_cli.run", side_effect=RuntimeError("bad input")):
        result = handle_mission(bad_input)
    assert result["status"] == "failed"
```

**Red flags**: Patching the exact function being tested; patching stdlib internals like `os.path.exists` when you could use `tmp_path` instead.

---

## Anti-Pattern 5: Integration tests as an afterthought

**Description**: Writing only unit tests for a feature, then discovering integration issues only in production because the pieces were never tested together.

**Why it's dangerous**: Unit tests can pass while the integration breaks — wrong argument ordering across module boundaries, incompatible data shapes, missing env vars, or config not loaded correctly.

**How to fix**: For each meaningful integration point (module A calling module B with real I/O), write at least one integration test that exercises the actual path end-to-end, even if slower. In Kōan's test suite: use `tmp_path` for real files, use `monkeypatch` for env vars, and avoid mocking anything that isn't a network call or subprocess.

**Red flags**: A new feature with 10 unit tests and 0 integration coverage; tests that mock every import in the module under test.

---

## Self-Check Before Committing Tests

Run through this checklist before marking tests complete:

- [ ] **Assertions on outcomes**: Every test asserts on return values, raised exceptions, file contents, or observable state — not just on mock call counts.
- [ ] **No test-only production code**: I did not add `test_mode` flags, skip branches, or unused methods to production code to make tests easier.
- [ ] **Mocks match real behavior**: Where I've mocked a dependency, the mock's return type and error behavior match what the real function does.
- [ ] **Boundary mocking**: Mocks are at external boundaries (subprocess, network, filesystem), not at internal function calls within the unit under test.
- [ ] **At least one integration path**: If adding a new module integration point, there is at least one test that exercises the path without mocking the integration boundary itself.
- [ ] **No source-code inspection**: Tests do not read source files to check if specific code is present or absent — they test behavior, not implementation text.
