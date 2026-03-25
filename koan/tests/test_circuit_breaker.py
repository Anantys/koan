"""Tests for circuit_breaker.py — circuit breaker for fire-and-forget subsystems."""

import time
from unittest.mock import patch

import pytest

from app.circuit_breaker import CircuitBreaker, get_open_circuits


class TestCircuitBreakerBasics:
    """Test basic state transitions."""

    def test_new_breaker_is_closed(self):
        cb = CircuitBreaker(threshold=2)
        assert not cb.is_open("foo")

    def test_single_failure_stays_closed(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_failure("foo", Exception("err"))
        assert not cb.is_open("foo")

    def test_threshold_failures_opens_circuit(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_failure("foo", Exception("err1"))
        cb.record_failure("foo", Exception("err2"))
        assert cb.is_open("foo")

    def test_threshold_one_opens_on_first_failure(self):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err"))
        assert cb.is_open("foo")

    def test_success_resets_counter(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_failure("foo", Exception("err1"))
        cb.record_success("foo")
        cb.record_failure("foo", Exception("err2"))
        # Only one failure since reset — still closed
        assert not cb.is_open("foo")

    def test_success_closes_open_circuit(self):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err"))
        assert cb.is_open("foo")
        cb.record_success("foo")
        assert not cb.is_open("foo")

    def test_independent_circuits(self):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err"))
        assert cb.is_open("foo")
        assert not cb.is_open("bar")

    def test_extra_failures_keep_circuit_open(self):
        cb = CircuitBreaker(threshold=2)
        for i in range(5):
            cb.record_failure("foo", Exception(f"err{i}"))
        assert cb.is_open("foo")


class TestCircuitBreakerReset:
    """Test reset behavior."""

    def test_reset_single_circuit(self):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err"))
        cb.record_failure("bar", Exception("err"))
        cb.reset("foo")
        assert not cb.is_open("foo")
        assert cb.is_open("bar")

    def test_reset_all_circuits(self):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err"))
        cb.record_failure("bar", Exception("err"))
        cb.reset()
        assert not cb.is_open("foo")
        assert not cb.is_open("bar")

    def test_reset_nonexistent_is_noop(self):
        cb = CircuitBreaker(threshold=1)
        cb.reset("nonexistent")  # Should not raise


class TestCircuitBreakerAutoReset:
    """Test time-based auto-reset."""

    def test_auto_reset_after_elapsed(self):
        cb = CircuitBreaker(threshold=1, reset_after=0.01)
        cb.record_failure("foo", Exception("err"))
        assert cb.is_open("foo")
        time.sleep(0.02)
        # After reset_after elapsed, circuit should be half-open
        assert not cb.is_open("foo")

    def test_no_auto_reset_when_zero(self):
        cb = CircuitBreaker(threshold=1, reset_after=0)
        cb.record_failure("foo", Exception("err"))
        # Can't really wait forever, just verify it stays open
        assert cb.is_open("foo")

    def test_half_open_reopens_on_failure(self):
        cb = CircuitBreaker(threshold=1, reset_after=0.01)
        cb.record_failure("foo", Exception("err1"))
        time.sleep(0.02)
        # Half-open now
        assert not cb.is_open("foo")
        # Another failure reopens
        cb.record_failure("foo", Exception("err2"))
        assert cb.is_open("foo")


class TestGuardDecorator:
    """Test the @guard decorator."""

    def test_guard_passes_through_on_success(self):
        cb = CircuitBreaker(threshold=2)

        @cb.guard("adder")
        def add(a, b):
            return a + b

        assert add(1, 2) == 3

    def test_guard_returns_default_on_exception(self):
        cb = CircuitBreaker(threshold=2)

        @cb.guard("broken", default=-1)
        def broken():
            raise RuntimeError("boom")

        assert broken() == -1

    def test_guard_returns_none_default(self):
        cb = CircuitBreaker(threshold=2)

        @cb.guard("broken")
        def broken():
            raise RuntimeError("boom")

        assert broken() is None

    def test_guard_skips_when_circuit_open(self):
        cb = CircuitBreaker(threshold=1)
        call_count = 0

        @cb.guard("counter", default=0)
        def counter():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        counter()  # First call: executes, fails, opens circuit
        assert call_count == 1
        counter()  # Second call: skipped (circuit open)
        assert call_count == 1

    def test_guard_records_success(self):
        cb = CircuitBreaker(threshold=2)

        @cb.guard("ok")
        def ok():
            return "fine"

        ok()
        assert cb._failures.get("ok", 0) == 0

    def test_guard_records_failure(self):
        cb = CircuitBreaker(threshold=3)

        @cb.guard("flaky")
        def flaky():
            raise RuntimeError("flake")

        flaky()
        assert cb._failures["flaky"] == 1

    def test_guard_logs_failure_to_stderr(self, capsys):
        cb = CircuitBreaker(threshold=2, log_prefix="test")

        @cb.guard("noisy")
        def noisy():
            raise RuntimeError("loud error")

        noisy()
        captured = capsys.readouterr()
        assert "noisy failed: loud error" in captured.err
        assert "[test]" in captured.err

    def test_guard_logs_circuit_open_to_stderr(self, capsys):
        cb = CircuitBreaker(threshold=1, log_prefix="test")

        @cb.guard("opener")
        def opener():
            raise RuntimeError("fatal")

        opener()
        captured = capsys.readouterr()
        assert "circuit OPEN" in captured.err
        assert "opener" in captured.err

    def test_guard_default_factory(self):
        cb = CircuitBreaker(threshold=1)

        @cb.guard("dict_maker", default_factory=dict)
        def dict_maker():
            raise RuntimeError("boom")

        r1 = dict_maker()
        r2 = dict_maker()
        assert r1 == {}
        assert r2 == {}
        assert r1 is not r2  # Fresh instances each time

    def test_guard_default_factory_list(self):
        cb = CircuitBreaker(threshold=1)

        @cb.guard("list_maker", default_factory=list)
        def list_maker():
            raise RuntimeError("boom")

        r1 = list_maker()
        r1.append("mutated")
        r2 = list_maker()
        assert r2 == []  # Not affected by mutation of r1

    def test_guard_rejects_both_default_and_factory(self):
        cb = CircuitBreaker(threshold=2)
        with pytest.raises(ValueError, match="Cannot specify both"):
            @cb.guard("bad", default={}, default_factory=dict)
            def bad():
                pass

    def test_guard_preserves_function_name(self):
        cb = CircuitBreaker(threshold=2)

        @cb.guard("named")
        def my_function():
            pass

        assert my_function.__name__ == "my_function"

    def test_guard_passes_args_and_kwargs(self):
        cb = CircuitBreaker(threshold=2)

        @cb.guard("calc")
        def calc(x, y, op="add"):
            if op == "add":
                return x + y
            return x * y

        assert calc(3, 4) == 7
        assert calc(3, 4, op="mul") == 12


class TestOpenCircuits:
    """Test the open_circuits property."""

    def test_empty_when_all_closed(self):
        cb = CircuitBreaker(threshold=2)
        assert cb.open_circuits == {}

    def test_reports_open_circuits(self):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err_foo"))
        cb.record_failure("bar", Exception("err_bar"))
        circuits = cb.open_circuits
        assert "foo" in circuits
        assert "bar" in circuits
        assert circuits["foo"] == "err_foo"
        assert circuits["bar"] == "err_bar"

    def test_excludes_closed_circuits(self):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err"))
        cb.record_success("foo")
        assert "foo" not in cb.open_circuits

    def test_last_error_tracks_most_recent(self):
        cb = CircuitBreaker(threshold=3)
        cb.record_failure("foo", Exception("first"))
        cb.record_failure("foo", Exception("second"))
        cb.record_failure("foo", Exception("third"))
        assert cb.open_circuits["foo"] == "third"


class TestGetOpenCircuits:
    """Test the public get_open_circuits() API."""

    def test_returns_empty_when_no_open_circuits(self):
        with patch("app.mission_runner._breaker") as mock_breaker:
            mock_breaker.open_circuits = {}
            assert get_open_circuits() == {}

    def test_returns_open_circuits(self):
        with patch("app.mission_runner._breaker") as mock_breaker:
            mock_breaker.open_circuits = {"foo": "err"}
            assert get_open_circuits() == {"foo": "err"}

    def test_returns_empty_on_import_error(self):
        with patch("app.circuit_breaker.get_open_circuits", wraps=get_open_circuits):
            with patch.dict("sys.modules", {"app.mission_runner": None}):
                # When mission_runner can't be imported, return empty
                result = get_open_circuits()
                assert result == {}


class TestLogPrefix:
    """Test configurable log prefix."""

    def test_default_prefix(self, capsys):
        cb = CircuitBreaker(threshold=1)
        cb.record_failure("foo", Exception("err"))
        captured = capsys.readouterr()
        assert "[circuit_breaker]" in captured.err

    def test_custom_prefix(self, capsys):
        cb = CircuitBreaker(threshold=1, log_prefix="my_module")
        cb.record_failure("foo", Exception("err"))
        captured = capsys.readouterr()
        assert "[my_module]" in captured.err

    def test_guard_uses_breaker_prefix(self, capsys):
        cb = CircuitBreaker(threshold=2, log_prefix="pipeline")

        @cb.guard("step")
        def step():
            raise RuntimeError("oops")

        step()
        captured = capsys.readouterr()
        assert "[pipeline]" in captured.err
