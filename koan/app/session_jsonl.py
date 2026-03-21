"""JSONL session file reader for Claude Code internal session data.

Reads the tail of Claude Code's JSONL session files to extract cost,
token, and activity data after mission execution. Session files live
at ~/.claude/projects/{encoded-path}/*.jsonl.

This module is **Claude provider-specific**. Callers must guard invocations
with a ``get_provider_name() == "claude"`` check so it is never called
when running under other providers (e.g. Copilot).
"""

import json
import sys
from pathlib import Path
from typing import Optional


def _encode_project_path(project_path: str) -> str:
    """Encode a project path the same way Claude Code does.

    Claude Code uses ``/`` → ``-`` for directory names under
    ``~/.claude/projects/``.
    """
    return project_path.replace("/", "-")


def find_session_jsonl(project_path: str) -> Optional[Path]:
    """Locate the most recently modified JSONL session file for *project_path*.

    Returns ``None`` when the Claude projects directory doesn't exist or
    contains no ``.jsonl`` files for the given project.
    """
    try:
        encoded = _encode_project_path(project_path)
        projects_dir = Path.home() / ".claude" / "projects" / encoded
        if not projects_dir.is_dir():
            return None

        jsonl_files = list(projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return None

        # Most recently modified file is the active session
        return max(jsonl_files, key=lambda p: p.stat().st_mtime)
    except Exception as e:
        print(f"[session_jsonl] find_session_jsonl failed: {e}", file=sys.stderr)
        return None


def read_tail_bytes(path: Path, max_bytes: int = 131072) -> bytes:
    """Read the last *max_bytes* of *path*, or the entire file if smaller."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read()
    except (FileNotFoundError, IOError) as e:
        print(f"[session_jsonl] read_tail_bytes failed: {e}", file=sys.stderr)
        return b""


def parse_session_tail(path: Path) -> dict:
    """Parse the tail of a JSONL session file for cost and activity data.

    Returns a dict with keys: ``cost_usd``, ``input_tokens``,
    ``output_tokens``, ``last_action``, ``session_id``.  Missing fields
    are omitted.  Returns ``{}`` on error or empty file.
    """
    raw = read_tail_bytes(path)
    if not raw:
        return {}

    lines = raw.decode("utf-8", errors="replace").split("\n")

    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    last_action = ""
    session_id = ""
    found_any = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # Truncated first line from tail window — skip
            continue

        found_any = True

        # Session ID from first parseable line
        if not session_id:
            session_id = obj.get("sessionId", "")

        # Cost — take the last seen value (most up-to-date)
        if "costUSD" in obj:
            cost_usd = obj["costUSD"]

        # Token counts — sum across lines
        input_tokens += obj.get("inputTokens", 0)
        output_tokens += obj.get("outputTokens", 0)

        # Last tool action — update on each toolUse entry
        if obj.get("type") == "tool_use" or "toolName" in obj:
            tool_name = obj.get("toolName", "")
            if tool_name:
                last_action = tool_name

    if not found_any:
        return {}

    result = {}
    if cost_usd:
        result["cost_usd"] = cost_usd
    if input_tokens:
        result["input_tokens"] = input_tokens
    if output_tokens:
        result["output_tokens"] = output_tokens
    if last_action:
        result["last_action"] = last_action
    if session_id:
        result["session_id"] = session_id

    return result


def collect_jsonl_tokens(project_path: str) -> Optional[dict]:
    """Find and parse the JSONL session file for *project_path*.

    Composes :func:`find_session_jsonl` and :func:`parse_session_tail`
    into a single call.  Returns ``None`` if the file wasn't found or
    parsing yielded no data.  Never raises.
    """
    try:
        path = find_session_jsonl(project_path)
        if path is None:
            return None

        data = parse_session_tail(path)
        return data if data else None
    except Exception as e:
        print(f"[session_jsonl] collect_jsonl_tokens failed: {e}", file=sys.stderr)
        return None
