#!/usr/bin/env python3
"""
Kōan Usage Tracker — Parse usage.md and decide autonomous mode

Parses session/weekly usage percentages, estimates run costs,
and decides which autonomous mode to use (review/implement/deep/wait).

Keeps 10% safety margin to avoid quota exhaustion.

Usage:
    usage_tracker.py <usage.md> <run_count>

Output:
    mode:available%:reason
    Example: implement:45:Normal budget
"""

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Tuple

# If usage.md is older than this, widen safety margin (data may be stale)
STALENESS_THRESHOLD_SECONDS = 6 * 3600  # 6 hours
STALE_SAFETY_MARGIN = 15.0  # vs normal 10%

# When usage.md exists but is malformed, assume this usage % to avoid
# accidentally running in unlimited/DEEP mode on bad data.
MALFORMED_DEFAULT_PCT = 75.0

logger = logging.getLogger(__name__)


class UsageTracker:
    """Track Claude usage and decide autonomous mode based on remaining budget."""

    def __init__(self, usage_file: Path, runs_completed: int = 0,
                 budget_mode: str = "full",
                 warn_pct: int = 70, stop_pct: int = 85):
        """Initialize tracker by parsing usage.md file.

        Args:
            usage_file: Path to instance/usage.md
            runs_completed: Number of runs completed in current session
            budget_mode: Controls which internal budget gates are active.
                "full" (default): both session and weekly gates.
                "session_only": ignore weekly limit — only session budget matters.
                    Useful when the internal weekly token estimate drifts from
                    the real Claude API quota.
                "disabled": no internal budget gating — only real API quota
                    exhaustion errors (from quota_handler.py) will pause.
            warn_pct: Usage percentage at which to enter conservative mode
                (default 70 → review when <30% remaining).
            stop_pct: Usage percentage at which to pause entirely
                (default 85 → wait when <15% remaining).

        Raises:
            ValueError: If usage file cannot be parsed
        """
        self.session_pct = 0.0
        self.session_reset = "unknown"
        self.weekly_pct = 0.0
        self.weekly_reset = "unknown"
        self.runs_this_session = runs_completed
        self.safety_margin = 10.0  # Keep 10% buffer
        self.budget_mode = budget_mode
        self.warn_pct = warn_pct
        self.stop_pct = stop_pct

        if usage_file.exists():
            self._parse_usage_file(usage_file)
            # Widen safety margin if data is stale
            try:
                age = time.time() - os.path.getmtime(usage_file)
                if age > STALENESS_THRESHOLD_SECONDS:
                    self.safety_margin = STALE_SAFETY_MARGIN
            except OSError:
                pass

    def _parse_usage_file(self, usage_file: Path):
        """Parse usage.md to extract session and weekly percentages.

        Format examples:
            Session (5hr) : 25% (reset in 3h)
            Weekly (7 day) : 60% (Resets in 3d)
        """
        content = usage_file.read_text()

        # Parse session line
        session_match = re.search(
            r'Session\s*\([^)]+\)\s*:\s*(\d+)%\s*\((?:reset|resets)\s+in\s+([^)]+)\)',
            content,
            re.IGNORECASE
        )
        if session_match:
            self.session_pct = float(session_match.group(1))
            self.session_reset = session_match.group(2).strip()

        # Parse weekly line
        weekly_match = re.search(
            r'Weekly\s*\([^)]+\)\s*:\s*(\d+)%\s*\((?:reset|resets)\s+in\s+([^)]+)\)',
            content,
            re.IGNORECASE
        )
        if weekly_match:
            self.weekly_pct = float(weekly_match.group(1))
            self.weekly_reset = weekly_match.group(2).strip()

        # If file has content but neither regex matched, the format is
        # malformed.  Default to conservative usage to avoid accidentally
        # granting unlimited/DEEP mode on bad data.
        if content.strip() and not session_match and not weekly_match:
            logger.warning(
                "usage.md exists but could not parse session or weekly "
                "percentages — defaulting to %s%% used",
                MALFORMED_DEFAULT_PCT,
            )
            self.session_pct = MALFORMED_DEFAULT_PCT
            self.weekly_pct = MALFORMED_DEFAULT_PCT

    def remaining_budget(self) -> Tuple[float, float]:
        """Calculate remaining budget after safety margin.

        Respects budget_mode:
        - "disabled": always returns (90, 90) — effectively unlimited
        - "session_only": weekly_remaining is always 90 (ignored)
        - "full": both limits active

        Returns:
            (session_remaining, weekly_remaining) in percentage points
        """
        if self.budget_mode == "disabled":
            return 90.0, 90.0

        session_remaining = max(0, 100 - self.session_pct - self.safety_margin)

        if self.budget_mode == "session_only":
            return session_remaining, 90.0

        weekly_remaining = max(0, 100 - self.weekly_pct - self.safety_margin)
        return session_remaining, weekly_remaining

    def estimate_run_cost(self) -> float:
        """Estimate usage cost of one run in percentage points.

        V1: Simple average (current_usage / runs_completed)
        V2: Could track per-mode costs separately

        Returns:
            Estimated cost in percentage points (default 5.0 if no history)
        """
        if self.runs_this_session > 0 and self.session_pct > 0:
            return self.session_pct / self.runs_this_session
        return 5.0  # Conservative default for first run

    def can_afford_run(self, mode: str) -> bool:
        """Check if budget allows a run in the given mode.

        Args:
            mode: One of "review", "implement", "deep"

        Returns:
            True if estimated cost fits within available budget
        """
        cost_multipliers = {
            "review": 0.5,      # Low-cost: read-only activities
            "implement": 1.0,   # Medium-cost: normal development
            "deep": 2.0,        # High-cost: intensive work
        }

        base_cost = self.estimate_run_cost()
        estimated_cost = base_cost * cost_multipliers.get(mode, 1.0)

        session_rem, weekly_rem = self.remaining_budget()
        available = min(session_rem, weekly_rem)

        return estimated_cost <= available

    def decide_mode(self) -> str:
        """Decide autonomous mode based on remaining budget.

        Budget thresholds (derived from config):
        - < (100 - stop_pct)%: wait (too close to limit)
        - < (100 - warn_pct)%: review (low-cost only)
        - < 40%: implement (medium-cost)
        - >= 40%: deep (high-cost allowed)

        With defaults (warn_pct=70, stop_pct=85):
        - < 15%: wait
        - < 30%: review
        - < 40%: implement
        - >= 40%: deep

        Returns:
            One of: "wait", "review", "implement", "deep"
        """
        session_rem, weekly_rem = self.remaining_budget()
        available = min(session_rem, weekly_rem)

        stop_remaining = 100 - self.stop_pct  # default: 15
        warn_remaining = 100 - self.warn_pct  # default: 30

        if available < stop_remaining:
            return "wait"
        elif available < warn_remaining:
            return "review"
        elif available < 40:
            return "implement"
        else:
            return "deep"

    def get_decision_reason(self, mode: str) -> str:
        """Generate human-readable reason for mode decision.

        Args:
            mode: Decided mode (wait/review/implement/deep)

        Returns:
            Explanation string
        """
        session_rem, weekly_rem = self.remaining_budget()
        available = min(session_rem, weekly_rem)

        if mode == "wait":
            return f"Budget exhausted ({available:.0f}% remaining)"
        elif mode == "review":
            return f"Low budget ({available:.0f}% remaining) - conservative mode"
        elif mode == "implement":
            return f"Normal budget ({available:.0f}% remaining)"
        else:  # deep
            return f"Ample budget ({available:.0f}% remaining) - full capability"

    def format_output(self, mode: str) -> str:
        """Format decision output for bash consumption.

        Args:
            mode: Decided autonomous mode

        Returns:
            Colon-separated string: "mode:available%:reason"
        """
        session_rem, weekly_rem = self.remaining_budget()
        available = min(session_rem, weekly_rem)
        reason = self.get_decision_reason(mode)

        return f"{mode}:{available:.0f}:{reason}"


def _get_budget_thresholds() -> tuple:
    """Read budget thresholds from config.yaml → budget.warn_at_percent / stop_at_percent.

    Returns:
        (warn_at_percent, stop_at_percent) with defaults (70, 85).
    """
    try:
        from app.utils import load_config
        config = load_config()
        budget = config.get("budget", {})
        warn = int(budget.get("warn_at_percent", 70))
        stop = int(budget.get("stop_at_percent", 85))
        # Sanity bounds
        warn = max(0, min(100, warn))
        stop = max(0, min(100, stop))
        return warn, stop
    except (ImportError, OSError, ValueError, TypeError):
        return 70, 85


def _get_budget_mode() -> str:
    """Read budget_mode from config.yaml → usage.budget_mode.

    Valid values: "full" (default), "session_only", "disabled".
    """
    try:
        from app.utils import load_config
        config = load_config()
        mode = config.get("usage", {}).get("budget_mode", "session_only")
        if mode in ("full", "session_only", "disabled"):
            return mode
    except (ImportError, OSError, ValueError):
        pass
    return "session_only"


def main():
    """CLI entry point for usage_tracker.py"""
    if len(sys.argv) < 3:
        print("Usage: usage_tracker.py <usage.md> <run_count>", file=sys.stderr)
        sys.exit(1)

    usage_file = Path(sys.argv[1])
    run_count = int(sys.argv[2])

    budget_mode = _get_budget_mode()
    warn_pct, stop_pct = _get_budget_thresholds()

    try:
        tracker = UsageTracker(usage_file, run_count, budget_mode=budget_mode,
                               warn_pct=warn_pct, stop_pct=stop_pct)
        mode = tracker.decide_mode()
        output = tracker.format_output(mode)
        print(output)
    except Exception as e:
        # Fallback to safe defaults on error
        print(f"[usage_tracker] Error: {e}", file=sys.stderr)
        print("review:50:Fallback mode")
        sys.exit(0)  # Don't break run loop on tracker errors


if __name__ == "__main__":
    main()
