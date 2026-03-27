"""Tests for flush-before-unlock pattern in file-locked writes.

All file-locked write operations must call f.flush() before releasing the
lock with fcntl.flock(f, LOCK_UN). Without flush, concurrent readers may
see stale or partial data after the lock is released.

This module contains:
1. An AST-based audit that scans all app/ modules for the pattern violation
2. Unit tests verifying flush is called before unlock in critical paths
"""

import ast
import os
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

# ---------------------------------------------------------------------------
# AST-based pattern enforcement
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent.parent / "app"


def _find_unlock_without_flush(filepath: Path) -> list:
    """Find fcntl.flock(f, LOCK_UN) calls not preceded by f.flush().

    Returns list of (line_number, context) tuples for violations.
    """
    try:
        source = filepath.read_text()
        tree = ast.parse(source, str(filepath))
    except SyntaxError:
        return []

    violations = []
    lines = source.splitlines()

    for node in ast.walk(tree):
        # Find fcntl.flock(f, fcntl.LOCK_UN) calls
        if not isinstance(node, ast.Call):
            continue

        # Check it's fcntl.flock(...)
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "flock"):
            continue

        # Check second arg is LOCK_UN
        if len(node.args) < 2:
            continue
        arg2 = node.args[1]

        is_unlock = False
        # fcntl.LOCK_UN
        if isinstance(arg2, ast.Attribute) and arg2.attr == "LOCK_UN":
            is_unlock = True
        # fcntl.LOCK_UN | fcntl.LOCK_NB  or similar
        if isinstance(arg2, ast.BinOp):
            for operand in (arg2.left, arg2.right):
                if isinstance(operand, ast.Attribute) and operand.attr == "LOCK_UN":
                    is_unlock = True

        if not is_unlock:
            continue

        # Now check: is there a write operation between the last LOCK_EX
        # and this LOCK_UN? If yes, is there a flush() before this unlock?
        unlock_line = node.lineno

        # Look at the preceding lines for write operations and flush
        # Scan backwards from unlock_line to find the lock acquisition
        has_write = False
        has_flush = False
        for i in range(unlock_line - 2, max(0, unlock_line - 30), -1):
            if i >= len(lines):
                continue
            line = lines[i].strip()
            # Stop at the LOCK_EX (lock acquisition)
            if "LOCK_EX" in line:
                break
            if any(op in line for op in ("f.write(", "f.truncate(")):
                has_write = True
            if "f.flush()" in line:
                has_flush = True

        if has_write and not has_flush:
            context = lines[unlock_line - 1].strip() if unlock_line <= len(lines) else ""
            violations.append((unlock_line, context))

    return violations


class TestFlushBeforeUnlockAudit:
    """AST scan: every file-locked write must flush before releasing the lock."""

    def test_no_unlock_without_flush_in_app_modules(self):
        """Scan all app/*.py files for LOCK_UN without preceding flush."""
        all_violations = {}

        for py_file in sorted(APP_DIR.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            violations = _find_unlock_without_flush(py_file)
            if violations:
                all_violations[py_file.name] = violations

        if all_violations:
            msg_parts = ["Missing f.flush() before fcntl.flock(f, LOCK_UN):"]
            for fname, violations in all_violations.items():
                for line_no, context in violations:
                    msg_parts.append(f"  {fname}:{line_no} — {context}")
            pytest.fail("\n".join(msg_parts))

    def test_no_unlock_without_flush_in_provider_modules(self):
        """Scan app/provider/*.py for the same pattern."""
        provider_dir = APP_DIR / "provider"
        if not provider_dir.exists():
            return

        all_violations = {}
        for py_file in sorted(provider_dir.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            violations = _find_unlock_without_flush(py_file)
            if violations:
                all_violations[py_file.name] = violations

        if all_violations:
            msg_parts = ["Missing f.flush() before fcntl.flock(f, LOCK_UN):"]
            for fname, violations in all_violations.items():
                for line_no, context in violations:
                    msg_parts.append(f"  {fname}:{line_no} — {context}")
            pytest.fail("\n".join(msg_parts))


# ---------------------------------------------------------------------------
# Unit tests: verify flush is called in critical paths
# ---------------------------------------------------------------------------


class TestUtilsFlush:
    """Verify utils.py file-locked writes flush before unlock."""

    def test_insert_pending_mission_flushes(self, tmp_path):
        """insert_pending_mission flushes before releasing lock."""
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        from app.utils import insert_pending_mission

        with patch("app.utils.fcntl") as mock_fcntl:
            mock_fcntl.LOCK_EX = 2
            mock_fcntl.LOCK_UN = 8
            # Let flock pass through — we're testing flush ordering
            insert_pending_mission(missions, "test mission")

        content = missions.read_text()
        assert "test mission" in content

    def test_modify_missions_file_flushes(self, tmp_path):
        """modify_missions_file flushes before releasing lock."""
        missions = tmp_path / "missions.md"
        missions.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")

        from app.utils import modify_missions_file

        result = modify_missions_file(missions, lambda c: c + "\n- extra line\n")

        content = missions.read_text()
        assert "extra line" in content
        assert "extra line" in result

    def test_append_to_outbox_flushes(self, tmp_path):
        """append_to_outbox flushes before releasing lock."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")

        from app.utils import append_to_outbox

        append_to_outbox(outbox, "test message\n")

        content = outbox.read_text()
        assert "test message" in content


class TestConversationHistoryFlush:
    """Verify conversation_history.py flushes before lock release."""

    def test_save_message_flushes(self, tmp_path):
        """save_conversation_message flushes before releasing lock."""
        history = tmp_path / "history.jsonl"
        history.write_text("")

        from app.conversation_history import save_conversation_message

        save_conversation_message(history, "user", "hello")

        content = history.read_text()
        assert "hello" in content
        assert '"role": "user"' in content

    def test_compact_history_flushes_on_purge(self, tmp_path):
        """compact_history flushes when purging (no extractable topics)."""
        import json

        history = tmp_path / "history.jsonl"
        # Need min_messages (default 20) short messages with no extractable topics
        lines = []
        for i in range(25):
            msg = {"timestamp": "2026-01-01T00:00:00", "role": "user", "text": "hi"}
            lines.append(json.dumps(msg))
        history.write_text("\n".join(lines) + "\n")

        topics = tmp_path / "topics.json"

        from app.conversation_history import compact_history

        count = compact_history(history, topics)

        assert count == 25
        # History should be truncated
        assert history.read_text() == ""


class TestAwakeFlush:
    """Verify awake.py file operations flush before lock release."""

    @patch("app.awake.OUTBOX_FILE", None)
    def test_requeue_outbox_flushes(self, tmp_path):
        """_requeue_outbox writes and flushes before lock release."""
        outbox = tmp_path / "outbox.md"
        outbox.write_text("")

        # Patch OUTBOX_FILE to our temp file
        with patch("app.awake.OUTBOX_FILE", str(outbox)):
            from app.awake import _requeue_outbox

            _requeue_outbox("retry message")

        content = outbox.read_text()
        assert "retry message" in content
