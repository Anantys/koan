"""
Kōan -- Pre-flight quota check.

Performs a lightweight CLI probe before each mission to verify that
API quota is available. This catches external quota consumption (manual
Claude usage, other tools) that internal token estimation cannot detect.

Uses ``claude usage`` which consumes zero tokens (no API call).
"""

import sys
from typing import Optional, Tuple


def preflight_quota_check(
    project_path: str,
    instance_dir: str,
    project_name: str = "",
) -> Tuple[bool, Optional[str]]:
    """Check quota availability before starting a mission.

    Args:
        project_path: Working directory for the CLI probe.
        instance_dir: Instance directory (for config access).
        project_name: Project name (for per-project provider lookup).

    Returns:
        (ok, error_message) — ok=True means quota is available,
        ok=False means quota is exhausted (error_message has details).
    """
    # Skip if budget mode is disabled
    try:
        from app.usage_tracker import _get_budget_mode
        if _get_budget_mode() == "disabled":
            return True, None
    except Exception as e:
        print(f"[preflight] Budget mode check failed: {e}", file=sys.stderr)

    # Get the provider for this project (falls back to global)
    try:
        from app.provider import get_provider
        provider = get_provider()
    except Exception as e:
        print(f"[preflight] Provider resolution failed: {e}", file=sys.stderr)
        return True, None

    available, error_detail = provider.check_quota_available(project_path)
    if available:
        return True, None

    return False, error_detail
