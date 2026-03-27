"""CLI output journal streamer — tail thread for real-time visibility.

Provides a lightweight tail thread that polls a subprocess stdout temp file
and appends new content to the project's daily journal file. This gives
users real-time visibility via ``tail -f`` on the journal without changing
the subprocess I/O path at all.

Usage::

    stream = start_journal_stream(stdout_file, instance_dir, project_name, run_num)
    # ... run subprocess ...
    stop_journal_stream(stream, exit_code, stderr_file)
"""

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple


_POLL_INTERVAL = 1.0  # seconds between tail polls
_CHUNK_SIZE = 8192    # bytes per read


def _decode_safe(data: bytes) -> tuple:
    """Decode bytes to str, preserving incomplete trailing UTF-8 sequences.

    Returns ``(decoded_text, leftover_bytes)`` where *leftover_bytes* are
    0–3 trailing bytes that form an incomplete multi-byte character.
    """
    # Try full decode first (fast path — no split)
    try:
        return data.decode("utf-8"), b""
    except UnicodeDecodeError:
        pass
    # Walk back up to 3 bytes to find the split point
    for i in range(1, min(4, len(data)) + 1):
        try:
            return data[:-i].decode("utf-8"), data[-i:]
        except UnicodeDecodeError:
            continue
    # Fallback: replace all invalid bytes
    return data.decode("utf-8", errors="replace"), b""


def _get_append_fn():
    """Lazy import of append_to_journal to avoid circular imports."""
    from app.journal import append_to_journal
    return append_to_journal


def _journal_write(instance_dir: Path, project_name: str, content: str) -> None:
    """Append content to journal, logging errors to stderr."""
    try:
        _get_append_fn()(instance_dir, project_name, content)
    except Exception as e:
        print(f"[cli-journal] write error: {e}", file=sys.stderr)


def _tail_loop(
    stdout_file: str,
    instance_dir: Path,
    project_name: str,
    stop_event: threading.Event,
) -> None:
    """Poll *stdout_file* for new bytes and append them to the journal."""
    append = _get_append_fn()
    pos = 0
    leftover = b""  # incomplete UTF-8 trailing bytes from previous read

    while not stop_event.is_set():
        try:
            size = os.path.getsize(stdout_file)
        except OSError:
            stop_event.wait(_POLL_INTERVAL)
            continue

        if size <= pos:
            stop_event.wait(_POLL_INTERVAL)
            continue

        try:
            with open(stdout_file, "rb") as f:
                f.seek(pos)
                raw = f.read(_CHUNK_SIZE)
                if raw:
                    pos += len(raw)
                    chunk = leftover + raw
                    text, leftover = _decode_safe(chunk)
                    if text:
                        try:
                            append(instance_dir, project_name, text)
                        except OSError:
                            pass  # non-critical; avoid log spam in tight loop
        except OSError:
            pass  # file may not exist yet

        stop_event.wait(_POLL_INTERVAL)

    # Final flush: pick up anything written since last poll
    try:
        size = os.path.getsize(stdout_file)
        if size > pos:
            with open(stdout_file, "rb") as f:
                f.seek(pos)
                raw = f.read()
                if raw:
                    chunk = leftover + raw
                    leftover = b""
                    append(instance_dir, project_name, chunk.decode("utf-8", errors="replace"))
        elif leftover:
            append(instance_dir, project_name, leftover.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"[cli-journal] final flush error: {e}", file=sys.stderr)


def start_tail_thread(
    stdout_file: str,
    instance_dir: str,
    project_name: str,
    run_num: int,
) -> Tuple[threading.Thread, threading.Event]:
    """Start a background thread that tails *stdout_file* into the journal.

    Writes a header line to the journal immediately, then polls for new
    content every ~1 s and appends it.

    Args:
        stdout_file: Path to the subprocess stdout temp file.
        instance_dir: Path to the instance directory.
        project_name: Current project name.
        run_num: Current run number (for the header).

    Returns:
        ``(thread, stop_event)`` — call :func:`stop_tail_thread` when done.
    """
    inst = Path(instance_dir)

    header = (
        f"\n---\n### 🖥️ CLI Output — Run {run_num} "
        f"({time.strftime('%H:%M')})\n\n"
    )
    _journal_write(inst, project_name, header)

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_tail_loop,
        args=(stdout_file, inst, project_name, stop_event),
        name="cli-journal-tail",
        daemon=True,
    )
    thread.start()
    return thread, stop_event


def stop_tail_thread(
    thread: threading.Thread,
    stop_event: threading.Event,
    timeout: float = 5.0,
) -> None:
    """Signal the tail thread to stop and wait for it to finish.

    Args:
        thread: The tail thread returned by :func:`start_tail_thread`.
        stop_event: The stop event returned by :func:`start_tail_thread`.
        timeout: Max seconds to wait for thread join.
    """
    stop_event.set()
    thread.join(timeout=timeout)


def append_stderr_to_journal(
    stderr_file: str,
    instance_dir: str,
    project_name: str,
    run_num: int,
) -> None:
    """Append stderr content to the journal on error.

    Only call this when the subprocess exited with a non-zero code.

    Args:
        stderr_file: Path to the subprocess stderr temp file.
        instance_dir: Path to the instance directory.
        project_name: Current project name.
        run_num: Current run number.
    """
    try:
        content = Path(stderr_file).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return
    if not content:
        return

    entry = (
        f"\n---\n### ⚠️ CLI Errors — Run {run_num}\n\n"
        f"```\n{content}\n```\n"
    )
    _journal_write(Path(instance_dir), project_name, entry)


# ---------------------------------------------------------------------------
# High-level lifecycle helpers (used by run.py)
# ---------------------------------------------------------------------------

# Opaque handle returned by start_journal_stream
_StreamHandle = Optional[Tuple[threading.Thread, threading.Event]]


def start_journal_stream(
    stdout_file: str,
    instance_dir: str,
    project_name: str,
    run_num: int,
) -> _StreamHandle:
    """Start journal streaming if ``cli_output_journal`` is enabled.

    Returns an opaque handle to pass to :func:`stop_journal_stream`,
    or ``None`` if streaming is disabled or setup fails.
    """
    try:
        from app.config import get_cli_output_journal
        if not get_cli_output_journal():
            return None
        thread, stop_event = start_tail_thread(
            stdout_file, instance_dir, project_name, run_num,
        )
        return (thread, stop_event)
    except Exception as e:
        print(f"[cli-journal] start error: {e}", file=sys.stderr)
        return None


def stop_journal_stream(
    handle: _StreamHandle,
    exit_code: int,
    stderr_file: str,
    instance_dir: str,
    project_name: str,
    run_num: int,
) -> None:
    """Stop journal streaming and append stderr on error.

    Safe to call with ``handle=None`` (no-op).
    """
    if handle is None:
        return
    thread, stop_event = handle
    try:
        stop_tail_thread(thread, stop_event)
        if exit_code != 0:
            append_stderr_to_journal(
                stderr_file, instance_dir, project_name, run_num,
            )
    except Exception as e:
        print(f"[cli-journal] stop error: {e}", file=sys.stderr)
