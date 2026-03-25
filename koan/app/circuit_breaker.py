"""
Kōan -- Circuit breaker for fire-and-forget subsystem calls.

Tracks consecutive failures per named subsystem. After a configurable
threshold, the circuit "opens" and further calls are skipped (returning
a caller-supplied default) until the breaker is explicitly reset or the
process restarts.

Designed for the post-mission pipeline in mission_runner.py where
multiple independent subsystems (usage tracking, reflection, quality
gates, etc.) are called in sequence. If a subsystem is broken, there's
no point hammering it every iteration — log once that the circuit opened
and move on.

Usage:

    breaker = CircuitBreaker(threshold=2)

    @breaker.guard("cost_tracker")
    def record_cost(...):
        ...

    # Or inline:
    if breaker.is_open("cost_tracker"):
        return None
    try:
        do_thing()
        breaker.record_success("cost_tracker")
    except Exception as e:
        breaker.record_failure("cost_tracker", e)
"""

import sys
import time
from copy import copy
from functools import wraps
from typing import Any, Callable, Dict, Optional, Union

# Sentinel for "no default provided" (distinct from None)
_SENTINEL = object()


def _default_log(msg: str) -> None:
    """Default log function — writes to stderr without using print()."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


class CircuitBreaker:
    """Per-process circuit breaker for fire-and-forget subsystems.

    Thread-safety: not guaranteed — designed for single-threaded
    sequential pipelines (like run_post_mission).
    """

    def __init__(
        self,
        threshold: int = 2,
        reset_after: float = 0,
        log_prefix: str = "circuit_breaker",
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            threshold: Consecutive failures before the circuit opens.
            reset_after: Seconds after which an open circuit resets to
                half-open (0 = never auto-reset, manual only).
            log_prefix: Prefix for log messages (e.g. "mission_runner").
            log_fn: Callable for log output. Receives a formatted string.
                Defaults to writing to stderr.
        """
        self.threshold = threshold
        self.reset_after = reset_after
        self.log_prefix = log_prefix
        self._log = log_fn or _default_log
        self._failures: Dict[str, int] = {}
        self._open_since: Dict[str, float] = {}
        self._last_error: Dict[str, str] = {}

    def is_open(self, name: str) -> bool:
        """Check if the circuit for *name* is open (tripped)."""
        if name not in self._open_since:
            return False
        if self.reset_after > 0:
            elapsed = time.monotonic() - self._open_since[name]
            if elapsed >= self.reset_after:
                # Half-open: allow one attempt
                del self._open_since[name]
                return False
        return True

    def record_success(self, name: str) -> None:
        """Record a successful call — resets the failure counter."""
        self._failures.pop(name, None)
        self._open_since.pop(name, None)
        self._last_error.pop(name, None)

    def record_failure(self, name: str, error: Exception) -> None:
        """Record a failure.  Opens the circuit when threshold is reached."""
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        self._last_error[name] = str(error)
        if count >= self.threshold and name not in self._open_since:
            self._open_since[name] = time.monotonic()
            self._log(
                f"[{self.log_prefix}] {name}: circuit OPEN after "
                f"{count} failures (last: {error})"
            )

    def reset(self, name: Optional[str] = None) -> None:
        """Reset one or all circuits."""
        if name is None:
            self._failures.clear()
            self._open_since.clear()
            self._last_error.clear()
        else:
            self._failures.pop(name, None)
            self._open_since.pop(name, None)
            self._last_error.pop(name, None)

    def guard(
        self,
        name: str,
        default: Any = _SENTINEL,
        default_factory: Optional[Callable[[], Any]] = None,
    ) -> Callable:
        """Decorator that wraps a function with circuit-breaker logic.

        When the circuit is open, the decorated function is skipped and
        the default value is returned.  Exceptions are caught, recorded
        as failures, logged to stderr, and the default is returned.

        For mutable defaults (dicts, lists), use ``default_factory``
        instead of ``default`` to avoid shared-instance bugs::

            @breaker.guard("pipeline", default_factory=dict)
            def run_pipeline(...) -> dict: ...

        Args:
            name: Subsystem identifier (used for tracking).
            default: Value returned on failure or when circuit is open.
                Must be immutable or explicitly safe to share.
            default_factory: Callable returning a fresh default value.
                Mutually exclusive with ``default``.
        """
        if default is not _SENTINEL and default_factory is not None:
            raise ValueError("Cannot specify both default and default_factory")

        def _get_default() -> Any:
            if default_factory is not None:
                return default_factory()
            if default is _SENTINEL:
                return None
            # Copy mutable defaults to prevent shared-instance bugs
            if isinstance(default, (dict, list, set)):
                return copy(default)
            return default

        log_fn = self._log
        log_prefix = self.log_prefix

        def decorator(fn: Callable) -> Callable:
            @wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if self.is_open(name):
                    return _get_default()
                try:
                    result = fn(*args, **kwargs)
                    self.record_success(name)
                    return result
                except Exception as e:
                    self.record_failure(name, e)
                    log_fn(f"[{log_prefix}] {name} failed: {e}")
                    return _get_default()

            return wrapper

        return decorator

    @property
    def open_circuits(self) -> Dict[str, str]:
        """Return dict of currently open circuits {name: last_error}."""
        return {
            name: self._last_error.get(name, "unknown")
            for name in self._open_since
        }


def get_open_circuits() -> Dict[str, str]:
    """Public API: return open circuits from the mission_runner breaker.

    Lazily imports the breaker instance to avoid circular imports.
    Returns an empty dict if the breaker hasn't been initialized.
    """
    try:
        from app.mission_runner import _breaker
        return _breaker.open_circuits
    except (ImportError, AttributeError):
        return {}
