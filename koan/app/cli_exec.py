"""CLI execution helpers — secure prompt passing via temp files.

Prevents prompts from leaking into ``ps`` process listings and avoids OS
``ARG_MAX`` failures by writing them to a temporary file (``0o600``) and
redirecting that file as the subprocess stdin. Providers decide how their
prompt argument is rewritten to consume stdin.

Providers that consume stdin for the prompt (making it unavailable for
the agent's own tool calls) skip this mechanism and pass the prompt
directly as a ``-p`` argument.
"""

import contextlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Callable, List, Optional, Sequence, Tuple

from app.provider.base import CLIProvider
from app.run_log import log_safe as _log_cli, suppress_logged

STDIN_PLACEHOLDER = "@stdin"

# Default timeout for run_cli (seconds).  All current callers pass an
# explicit timeout, but this guards against future callers forgetting.
DEFAULT_TIMEOUT = 600  # 10 minutes

# Granularity of the provider-invocation-lock wait. Contention is unbounded
# (a peer Kōan can hold the lock for a whole mission), so the wait must never
# park the calling thread in an uninterruptible kernel wait — see
# _ProviderInvocationLock.__enter__.
LOCK_POLL_INTERVAL = 0.25

_FALLBACK_PROVIDER = CLIProvider()


def _get_cli_provider() -> CLIProvider:
    try:
        from app.provider import get_provider

        return get_provider()
    except Exception as e:
        print(f"[cli_exec] Provider check failed: {e}", file=sys.stderr)
        return _FALLBACK_PROVIDER


def _uses_stdin_passing(provider: Optional[CLIProvider] = None) -> bool:
    """Check if the current provider supports stdin-based prompt passing.

    Some providers consume stdin when reading the prompt, leaving nothing for
    their internal tool calls. Those providers opt out through their provider
    implementation.
    """
    return (provider or _get_cli_provider()).supports_stdin_prompt_passing()


def _lock_path(lock_name: str) -> str:
    from app.utils import koan_tmp_dir

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", lock_name.strip()).strip(".-")
    if not safe:
        safe = "provider"
    # Per-uid dir already namespaces the lock, so no "koan-" filename prefix
    # is needed. Per-uid (not global /tmp) avoids cross-user permission clashes.
    return os.path.join(koan_tmp_dir(), f"{safe}.lock")


class _ProviderInvocationLock:
    """Optional process-wide file lock for provider CLI invocations.

    Most providers can run concurrently. Providers whose CLIs mutate shared
    auth/session state can opt into serialization by returning a lock name from
    ``CLIProvider.invocation_lock_name()``.
    """

    def __init__(self, lock_name: str):
        self._lock_name = lock_name.strip()
        self._fh = None
        # True only after the exclusive lock is actually held. When False with a
        # non-empty lock name, the invocation runs UNSERIALIZED — callers may
        # inspect this to detect the degraded state.
        self.acquired = False

    def __enter__(self) -> "_ProviderInvocationLock":
        if not self._lock_name:
            return self
        try:
            import fcntl

            lock_path = _lock_path(self._lock_name)
            Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(lock_path, "a+")  # noqa: SIM115
            # LOCK_NB + sleep rather than a blocking LOCK_EX. A blocking flock
            # parks the thread in the kernel, and CPython only runs Python-level
            # signal handlers from the main thread's eval loop: a
            # process-directed kill() may be accepted by any other thread, so
            # the blocked flock never even sees EINTR. With unbounded contention
            # that makes the whole process deaf to SIGUSR1 / SIGUSR2 (/abort,
            # /restart --force) for as long as the peer holds the lock. Polling
            # bounds that deafness to LOCK_POLL_INTERVAL.
            try:
                while True:
                    try:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(LOCK_POLL_INTERVAL)
            except (KeyboardInterrupt, SystemExit):
                # A signal handler firing during the poll sleep (forced restart,
                # CTRL-C) must not leak the lock file descriptor.
                self._close()
                raise
            self.acquired = True
        except OSError as exc:
            # Degrade loudly but do not abort the invocation: failing to lock
            # is better surfaced than crashing every Codex run on a /tmp hiccup.
            print(
                f"[cli_exec] WARNING: serialization disabled for "
                f"{self._lock_name!r} ({exc}); concurrent provider invocations "
                "may race on shared auth/session state",
                file=sys.stderr,
            )
            self._close()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()

    def _close(self) -> None:
        if self._fh:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None

    def release(self) -> None:
        if not self._fh:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            print(f"[cli_exec] Provider lock release failed: {exc}", file=sys.stderr)
        finally:
            self._close()


def prepare_prompt_file(
    cmd: List[str],
    provider: Optional[CLIProvider] = None,
) -> Tuple[List[str], Optional[str], bool]:
    """Extract the prompt from *cmd* and write it to a secure temp file.

    Returns ``(modified_cmd, temp_file_path, use_as_stdin)``.

    - **stdin mode** (default for Claude-like providers): the prompt is
      rewritten to a marker and the temp file is opened as the process
      stdin. ``use_as_stdin`` is True.
    - **prompt-file mode** (e.g. Grok Build): large prompts become
      ``--prompt-file <path>`` on argv; stdin is left alone. ``use_as_stdin``
      is False but the path is still returned so callers can clean it up.

    If no rewrite applies, returns ``(cmd, None, False)``.
    """
    provider = provider or _get_cli_provider()
    from app.utils import koan_tmp_dir

    # Prompt-file path (Grok etc.): write file, put path on argv, no stdin.
    if provider.supports_prompt_file_passing():
        # Probe with a placeholder path; rewrite returns the prompt body only
        # when the provider wants file delivery (e.g. over a size threshold).
        probe_cmd, prompt = provider.rewrite_prompt_for_file(cmd, "__koan_probe__")
        if prompt is not None:
            fd, path = tempfile.mkstemp(
                suffix=".md", prefix="koan-prompt-", dir=koan_tmp_dir(),
            )
            try:
                os.write(fd, prompt.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(path, 0o600)
            new_cmd, _ = provider.rewrite_prompt_for_file(cmd, path)
            return new_cmd, path, False
        # Fall through: short prompts stay on argv; try stdin if supported.

    if not _uses_stdin_passing(provider):
        return cmd, None, False

    new_cmd, prompt = provider.rewrite_prompt_for_stdin(cmd, STDIN_PLACEHOLDER)
    if prompt is None:
        return cmd, None, False

    fd, path = tempfile.mkstemp(suffix=".md", prefix="koan-prompt-", dir=koan_tmp_dir())
    try:
        os.write(fd, prompt.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)

    return new_cmd, path, True


def _cleanup_prompt_file(path: Optional[str]) -> None:
    """Silently remove a temp prompt file if it exists."""
    if path:
        with suppress_logged(_log_cli, "debug", f"Prompt file cleanup failed ({path})", OSError):
            os.unlink(path)


def run_cli(
    cmd,
    provider: Optional[CLIProvider] = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a CLI command with the prompt passed via temp-file stdin.

    Drop-in replacement for ``subprocess.run(cmd, stdin=DEVNULL, ...)``.
    A default timeout of :data:`DEFAULT_TIMEOUT` seconds is applied if
    the caller does not provide one, preventing indefinite hangs.
    """
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    provider = provider or _get_cli_provider()
    cmd, prompt_path, use_as_stdin = prepare_prompt_file(cmd, provider=provider)
    with _ProviderInvocationLock(provider.invocation_lock_name()):
        if prompt_path and use_as_stdin:
            try:
                with open(prompt_path) as f:
                    kwargs.pop("stdin", None)
                    kwargs["stdin"] = f
                    return subprocess.run(cmd, **kwargs)
            finally:
                _cleanup_prompt_file(prompt_path)
        try:
            kwargs.setdefault("stdin", subprocess.DEVNULL)
            return subprocess.run(cmd, **kwargs)
        finally:
            # prompt-file mode: path is on argv; still delete after the run.
            if prompt_path and not use_as_stdin:
                _cleanup_prompt_file(prompt_path)


def acquire_provider_lock(
    provider: Optional[CLIProvider] = None,
) -> _ProviderInvocationLock:
    """Take the provider's invocation lock now, for a later :func:`popen_cli`.

    Callers that must not sit inside ``popen_cli`` for an unbounded time hoist
    the wait out with this and hand the result back via ``cli_lock=``; the
    returned ``cleanup()`` then owns the release. The runner does this so the
    contended wait happens *outside* its forced-restart deferral window (see
    ``app.run._sigusr2_deferred``), where a ``/restart --force`` can still be
    honoured immediately because no child process exists yet.
    """
    provider = provider or _get_cli_provider()
    lock = _ProviderInvocationLock(provider.invocation_lock_name())
    lock.__enter__()
    return lock


def popen_cli(
    cmd,
    provider: Optional[CLIProvider] = None,
    launcher: Optional[List[str]] = None,
    cli_lock: Optional[_ProviderInvocationLock] = None,
    **kwargs,
) -> Tuple[subprocess.Popen, Callable[[], None]]:
    """Start a :class:`~subprocess.Popen` process with the prompt via temp-file stdin.

    Returns ``(proc, cleanup)`` where *cleanup()* **must** be called after
    the process exits to close the file handle and delete the temp file.

    *launcher* is an argv prefix applied **after** the prompt rewrite —
    ``mission_scope`` uses it to wrap the provider CLI in a ``systemd-run
    --scope`` invocation. Prefixing before :func:`prepare_prompt_file` would
    hide the provider's own argv from the rewrite, so the prefix is deliberately
    applied at the last moment, immediately before the spawn.

    *cli_lock* is an already-acquired :func:`acquire_provider_lock` result whose
    ownership transfers here — it is released by ``cleanup()`` (or immediately,
    on failure), exactly like a lock taken internally. The transfer covers
    **every** exit path: the guard below opens before the first statement that
    can fail, so a handed-over lock is never left held by a raising caller.
    """
    provider = provider or _get_cli_provider()
    prompt_path: Optional[str] = None
    # One outer guard so the lock is released on ANY failure — including
    # prepare_prompt_file() (tempfile.mkstemp under koan_tmp_dir() raises
    # OSError on a full/unwritable scratch dir) and open(prompt_path), both of
    # which sit before the Popen try/except. A handed-over lock leaked here
    # self-deadlocks the caller: mission_scope.launch_scoped catches OSError and
    # retries the spawn unscoped, and the fresh flock on a second open-file
    # description can never be granted against one this process already holds.
    # On the success path we return normally (no exception), so the lock stays
    # held until the returned cleanup()/release runs.
    try:
        cmd, prompt_path, use_as_stdin = prepare_prompt_file(cmd, provider=provider)
        if launcher:
            cmd = list(launcher) + list(cmd)
        if cli_lock is None:
            cli_lock = _ProviderInvocationLock(provider.invocation_lock_name())
            cli_lock.__enter__()
        if prompt_path and use_as_stdin:
            stdin_file = open(prompt_path)  # noqa: SIM115
            kwargs.pop("stdin", None)
            kwargs["stdin"] = stdin_file
            try:
                proc = subprocess.Popen(cmd, **kwargs)
            except Exception:
                stdin_file.close()
                raise

            def cleanup():
                stdin_file.close()
                _cleanup_prompt_file(prompt_path)
                cli_lock.release()

            return proc, cleanup

        kwargs.setdefault("stdin", subprocess.DEVNULL)
        proc = subprocess.Popen(cmd, **kwargs)

        def cleanup_prompt_file_only():
            if prompt_path and not use_as_stdin:
                _cleanup_prompt_file(prompt_path)
            cli_lock.release()

        return proc, cleanup_prompt_file_only
    except Exception:
        # Any failure (prepare_prompt_file(), open(), Popen, ...) must release
        # the lock and remove the temp prompt file. _cleanup_prompt_file tolerates
        # a None path (the no-prompt branch); cli_lock is None only when we failed
        # before taking one of our own, and release() is a no-op on an unheld lock.
        _cleanup_prompt_file(prompt_path)
        if cli_lock is not None:
            cli_lock.release()
        raise


class StreamResult:
    """Result of :func:`stream_with_timeout`."""

    __slots__ = ("stdout", "stderr", "timed_out", "timeout_kind")

    def __init__(
        self,
        stdout: str,
        stderr: str,
        timed_out: bool,
        timeout_kind: str = "",
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.timeout_kind = timeout_kind


def stream_with_timeout(
    proc: subprocess.Popen,
    timeout: float,
    on_line: Optional[Callable[[str], None]] = None,
    drain_timeout: float = 30.0,
    idle_timeout: Optional[float] = None,
    max_duration: Optional[float] = None,
) -> StreamResult:
    """Consume ``proc.stdout`` line-by-line with a process-group-kill watchdog.

    Each stdout line is collected into the returned ``stdout`` text and
    optionally forwarded to *on_line*. After stdout EOF, the stderr
    stream is drained and the subprocess is awaited.

    On timeout the entire process group is killed via
    :func:`app.subprocess_runner.force_kill_process_group`. The caller
    must start *proc* with ``start_new_session=True``.

    Both std streams are closed before returning.
    """
    from app.subprocess_runner import (
        LivenessWatchdog,
        ProcessWatchdog,
        force_kill_process_group,
    )

    stdout_lines: List[str] = []
    stderr_text = ""
    drain_timed_out = False
    timeout_kind = ""

    # Backward-compatible default: hard timeout from process start.
    # Callers can override with activity-based policy by setting
    # idle_timeout and/or max_duration explicitly.
    effective_max_duration = timeout if max_duration is None else max_duration
    duration_watchdog = None
    idle_watchdog = None

    if effective_max_duration and effective_max_duration > 0:
        duration_watchdog = ProcessWatchdog(proc, effective_max_duration, graceful=False).start()
    if idle_timeout and idle_timeout > 0:
        idle_watchdog = LivenessWatchdog(proc, idle_timeout).start()

    try:
        try:
            for line in proc.stdout:
                stripped = line.rstrip("\n")
                stdout_lines.append(stripped)
                if on_line is not None:
                    on_line(stripped)
                if idle_watchdog is not None:
                    idle_watchdog.heartbeat()
        finally:
            if duration_watchdog is not None:
                duration_watchdog.mark_completed()
                duration_watchdog.cancel()
            if idle_watchdog is not None:
                idle_watchdog.cancel()

        with suppress_logged(_log_cli, "warning", "Stderr stream read failed", OSError, ValueError):
            if proc.stderr:
                stderr_text = proc.stderr.read()

        try:
            proc.wait(timeout=drain_timeout)
        except subprocess.TimeoutExpired:
            drain_timed_out = True
            timeout_kind = "drain"
            force_kill_process_group(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with suppress_logged(_log_cli, "debug", "Stream close failed", OSError):
                    stream.close()

    duration_fired = bool(duration_watchdog and duration_watchdog.fired)
    idle_fired = bool(idle_watchdog and idle_watchdog.fired)
    timed_out = duration_fired or idle_fired or drain_timed_out
    if timed_out and not timeout_kind:
        if idle_fired:
            timeout_kind = "idle"
        elif duration_fired:
            timeout_kind = "max_duration"
        else:
            timeout_kind = "drain"

    return StreamResult(
        stdout="\n".join(stdout_lines).strip(),
        stderr=stderr_text,
        timed_out=timed_out,
        timeout_kind=timeout_kind,
    )


# Default backoff durations for CLI retries (seconds).
# Higher than retry.py's (1/2/4s) because CLI calls are heavier.
CLI_RETRY_BACKOFF = (2, 5, 10)
CLI_RETRY_MAX_ATTEMPTS = 3


def run_cli_with_retry(
    cmd,
    *,
    max_attempts: int = CLI_RETRY_MAX_ATTEMPTS,
    backoff: Sequence[float] = CLI_RETRY_BACKOFF,
    provider: Optional[CLIProvider] = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a CLI command with automatic retry on transient errors.

    Wraps :func:`run_cli` with error classification: retries on
    ``RETRYABLE`` errors, returns immediately on ``TERMINAL``,
    ``QUOTA``, or ``UNKNOWN`` errors.

    Only suitable for **short-lived** CLI calls (quota probes, format
    commands, reflection invocations).  Do **not** use for long-running
    mission executions managed by the main loop — those use
    :func:`popen_cli` and have their own recovery.

    Args:
        cmd: Command list for subprocess.
        max_attempts: Maximum number of attempts (default 3).
        backoff: Sleep durations between retries.
        provider: Explicit provider instance (per-role CLI). Forwarded to
            :func:`run_cli` for stdin-rewrite / invocation-lock, and its name
            drives provider-specific error classification. ``None`` uses the
            global provider.
        **kwargs: Passed through to :func:`run_cli`.

    Returns:
        The :class:`~subprocess.CompletedProcess` from the last attempt.
    """
    from app.cli_errors import ErrorCategory, classify_cli_error

    # Ensure capture_output so we can classify errors
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)

    provider_name = provider.name if provider is not None else ""

    last_result = None
    for attempt in range(max_attempts):
        result = run_cli(cmd, provider=provider, **kwargs)
        last_result = result

        if result.returncode == 0:
            return result

        category = classify_cli_error(
            result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            provider_name=provider_name,
        )

        if category != ErrorCategory.RETRYABLE:
            return result

        if attempt < max_attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            err_detail = (result.stderr or "").strip()
            if not err_detail:
                err_detail = (result.stdout or "").strip()[-200:]
            else:
                err_detail = err_detail[:200]
            print(
                f"[cli_exec] Retryable CLI error "
                f"(attempt {attempt + 1}/{max_attempts}): "
                f"{err_detail} "
                f"— retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)

    return last_result
