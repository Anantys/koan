"""Outbox manager — handles outbox flush, format, and delivery.

Extracted from awake.py as the first step of the OO migration.
Encapsulates all outbox state (thread, lock, staging file) in a class
instead of module-level globals.
"""

import fcntl
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from app.bridge_log import log
from app.conversation_history import save_conversation_message
from app.format_outbox import (
    fallback_format,
    format_message,
    load_human_prefs,
    load_memory_context,
    load_soul,
)
from app.notify import NotificationPriority, NOTIFICATION_SUPPRESSED, send_telegram
from app.outbox_scanner import scan_and_log


# Pre-compiled regex for outbox priority header parsing
_OUTBOX_PRIORITY_RE = re.compile(
    r'^\[priority:(urgent|action|warning|info)\]\n?', re.MULTILINE,
)

_OUTBOX_PRIORITY_MAP = {
    "urgent": NotificationPriority.URGENT,
    "action": NotificationPriority.ACTION,
    "warning": NotificationPriority.WARNING,
    "info": NotificationPriority.INFO,
}


def parse_outbox_priority(content: str) -> Tuple[NotificationPriority, str]:
    """Parse priority headers from outbox content and strip them.

    Scans the content for any [priority:name] headers (from append_to_outbox),
    returns the highest-priority value found (most urgent wins) and the content
    with all priority headers removed for clean formatting.

    Legacy outbox entries (no header) default to ACTION.

    Args:
        content: Raw outbox content, possibly containing [priority:name] headers

    Returns:
        Tuple of (NotificationPriority, cleaned_content_str)
    """
    matches = _OUTBOX_PRIORITY_RE.findall(content)
    if not matches:
        return NotificationPriority.ACTION, content

    # Find the highest-priority level across all blocks.
    max_priority = _OUTBOX_PRIORITY_MAP.get(matches[0], NotificationPriority.ACTION)
    for name in matches[1:]:
        p = _OUTBOX_PRIORITY_MAP.get(name, NotificationPriority.ACTION)
        if p.value > max_priority.value:
            max_priority = p

    cleaned = _OUTBOX_PRIORITY_RE.sub("", content).strip()
    return max_priority, cleaned


class OutboxManager:
    """Manages the outbox file lifecycle: read, format, send, recover.

    Encapsulates the outbox thread state and file locking that were
    previously module-level globals in awake.py.

    Args:
        outbox_file: Path to the outbox.md file.
        instance_dir: Path to the instance directory.
        conversation_history_file: Path to the conversation history JSONL.
    """

    def __init__(
        self,
        outbox_file: Path,
        instance_dir: Path,
        conversation_history_file: Path,
    ):
        self._outbox_file = outbox_file
        self._instance_dir = instance_dir
        self._conversation_history_file = conversation_history_file
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def outbox_file(self) -> Path:
        return self._outbox_file

    @property
    def staging_path(self) -> Path:
        """Return path of the outbox staging file (crash-recovery backup)."""
        return self._outbox_file.parent / "outbox-sending.md"

    def recover_staged(self):
        """Recover content from a staging file left by a previous crash.

        If outbox-sending.md exists, a previous flush() was interrupted
        between truncation and send completion. Re-queue the content so it
        gets retried on the next cycle.
        """
        staging = self.staging_path
        if not staging.exists():
            return
        try:
            content = staging.read_text().strip()
            if content:
                log("outbox", "Recovering staged outbox content from interrupted flush")
                self.requeue(content)
            staging.unlink(missing_ok=True)
        except Exception as e:
            log("error", f"Staged outbox recovery failed: {e}")

    def flush(self):
        """Relay messages from the run loop outbox.

        Uses file locking for concurrency. All outbox messages are formatted
        via Claude before sending to Telegram. The lock is held only during
        read+clear (microseconds), not during the slow Claude formatting call.

        Crash safety: content is written to a staging file before truncation.
        """
        self.recover_staged()

        if not self._outbox_file.exists():
            return

        # Phase 1: Read, stage, and clear under lock (fast)
        content = None
        staging = self.staging_path
        try:
            with open(self._outbox_file, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    content = f.read().strip()
                    if content:
                        staging.write_text(content)
                        f.seek(0)
                        f.truncate()
                        f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            log("error", f"Outbox read error: {e}")
            return

        if not content:
            return

        # Phase 2: Scan, format, and send (slow — outside lock)
        scan_result = scan_and_log(content)
        if scan_result.blocked:
            quarantine = self._instance_dir / "outbox-quarantine.md"
            try:
                with open(quarantine, "a") as qf:
                    qf.write(
                        f"\n---\n[{datetime.now().isoformat()}] BLOCKED: "
                        f"{scan_result.reason}\n"
                    )
                    qf.write(content[:500])
                    qf.write("\n")
            except OSError as e:
                log("error", f"Quarantine write error: {e}")
            log("outbox", f"Outbox BLOCKED by scanner: {scan_result.reason}")
            staging.unlink(missing_ok=True)
            return

        priority, clean_content = parse_outbox_priority(content)
        formatted = self._format_message(clean_content)
        formatted = self._expand_github_refs(formatted, clean_content)
        result = send_telegram(formatted, priority=priority)

        if result is NOTIFICATION_SUPPRESSED:
            preview = formatted[:150].replace("\n", " ")
            if len(formatted) > 150:
                preview += "..."
            log("outbox", f"Outbox suppressed (priority below threshold): {preview}")
            staging.unlink(missing_ok=True)
        elif result:
            msg_id = self._get_last_message_id()
            save_conversation_message(
                self._conversation_history_file, "assistant", formatted,
                message_id=msg_id, message_type="notification",
            )
            preview = formatted[:150].replace("\n", " ")
            if len(formatted) > 150:
                preview += "..."
            log("outbox", f"Outbox flushed: {preview}")
            staging.unlink(missing_ok=True)
        else:
            log("error", "Outbox send failed — re-queuing for retry")
            self.requeue(content)
            staging.unlink(missing_ok=True)

    def requeue(self, content: str):
        """Re-append content to outbox.md after a failed send attempt.

        If re-appending fails, writes to outbox-failed.md as a last resort.
        """
        try:
            with open(self._outbox_file, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(content + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            log("error", f"Failed to re-queue outbox message: {e}")
            self._write_failed(content, e)

    def _write_failed(self, content: str, original_error: Exception):
        """Last-resort persistence: write lost content to outbox-failed.md."""
        failed_file = self._outbox_file.parent / "outbox-failed.md"
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = f"<!-- lost {timestamp} — {original_error} -->\n{content}\n"
            with open(failed_file, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(entry)
                    f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            log("warn", f"Lost outbox content saved to {failed_file.name}")
        except Exception as e2:
            log("error",
                f"Failed to write outbox-failed.md: {e2} — content lost: {content[:120]}")

    def flush_async(self):
        """Run flush() in a background thread if not already running.

        flush() calls Claude CLI for message formatting (up to 30s).
        Running it synchronously blocks Telegram polling.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return  # Previous flush still running — skip this cycle
            self._thread = threading.Thread(target=self._flush_safe, daemon=True)
            self._thread.start()

    def _flush_safe(self):
        """Wrapper that catches exceptions so the thread exits cleanly."""
        try:
            self.flush()
        except Exception as e:
            log("error", f"Background flush_outbox failed: {e}")

    def _format_message(self, raw_content: str) -> str:
        """Format outbox content via Claude with full personality context."""
        try:
            soul = load_soul(self._instance_dir)
            prefs = load_human_prefs(self._instance_dir)
            memory = load_memory_context(self._instance_dir)
            return format_message(raw_content, soul, prefs, memory)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            log("error", f"Format error, sending fallback: {e}")
            return fallback_format(raw_content)
        except Exception as e:
            log("error", f"Unexpected format error, sending fallback: {e}")
            return fallback_format(raw_content)

    @staticmethod
    def _expand_github_refs(formatted: str, raw_content: str) -> str:
        """Expand bare #123 GitHub refs to full URLs.

        Uses the raw (pre-formatted) content to detect the project context,
        then applies expansion to the formatted output.
        """
        from app.text_utils import expand_github_refs, extract_project_from_message

        project_name = extract_project_from_message(raw_content)
        if not project_name:
            project_name = extract_project_from_message(formatted)
        if not project_name:
            return formatted

        try:
            from app.projects_merged import get_github_url
            github_url = get_github_url(project_name)
        except Exception as e:
            log("error", f"GitHub URL lookup failed for {project_name}: {e}")
            return formatted

        if not github_url:
            return formatted

        return expand_github_refs(formatted, github_url)

    @staticmethod
    def _get_last_message_id() -> int:
        """Get the message_id from the last send_telegram() call."""
        try:
            from app.messaging import get_messaging_provider
            provider = get_messaging_provider()
            ids = provider.get_last_message_ids()
            return ids[-1] if ids else 0
        except (SystemExit, Exception):
            return 0
