"""Kōan — Schedule Manager

Controls when deep exploration vs. mission-only work happens based on
time-of-day windows defined in config.yaml.

The human can define:
  schedule:
    deep_hours: "0-6"       # prefer deep/contemplative work during these hours
    work_hours: "8-20"      # mission-only during these hours (suppresses exploration)

When in deep_hours:
  - Contemplative chance is boosted (3x normal)
  - Autonomous mode preference shifts toward deep
  - The agent is encouraged to explore freely

When in work_hours:
  - Autonomous exploration is suppressed (same effect as /focus)
  - Only queued missions are executed
  - Contemplative sessions are skipped

Outside both windows (gap hours):
  - Normal behavior (default contemplative chance, standard mode selection)

Time ranges wrap around midnight: "22-6" means 10 PM to 6 AM.
Multiple ranges can be comma-separated: "0-6,22-24" or "8-12,14-18".
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple


@dataclass
class TimeRange:
    """A single hour range (e.g., 0-6 means 00:00 to 05:59)."""

    start: int  # inclusive, 0-23
    end: int  # exclusive, 0-24

    def contains(self, hour: int) -> bool:
        """Check if an hour falls within this range.

        Handles wrap-around: TimeRange(22, 6) means 22,23,0,1,2,3,4,5.
        """
        if self.start <= self.end:
            return self.start <= hour < self.end
        # Wraps around midnight
        return hour >= self.start or hour < self.end


@dataclass
class ScheduleState:
    """The current schedule evaluation result."""

    in_deep_hours: bool
    in_work_hours: bool

    @property
    def mode(self) -> str:
        """Return the schedule mode: 'deep', 'work', or 'normal'."""
        if self.in_deep_hours:
            return "deep"
        if self.in_work_hours:
            return "work"
        return "normal"


def parse_time_ranges(spec: str) -> List[TimeRange]:
    """Parse a time range specification string.

    Formats:
        "0-6"           single range
        "22-6"          wraps around midnight
        "0-6,22-24"     multiple ranges (comma-separated)
        ""              empty = no ranges

    Args:
        spec: Time range specification string.

    Returns:
        List of TimeRange objects.

    Raises:
        ValueError: If the format is invalid.
    """
    spec = spec.strip()
    if not spec:
        return []

    ranges = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" not in part:
            raise ValueError(
                f"Invalid time range '{part}': expected format 'START-END' (e.g., '0-6')"
            )

        pieces = part.split("-", 1)
        try:
            start = int(pieces[0].strip())
            end = int(pieces[1].strip())
        except ValueError:
            raise ValueError(
                f"Invalid time range '{part}': hours must be integers"
            )

        if not (0 <= start <= 23):
            raise ValueError(
                f"Invalid start hour {start}: must be 0-23"
            )
        if not (0 <= end <= 24):
            raise ValueError(
                f"Invalid end hour {end}: must be 0-24"
            )
        if start == end:
            raise ValueError(
                f"Invalid time range '{part}': start and end cannot be equal"
            )

        ranges.append(TimeRange(start=start, end=end))

    return ranges


def check_schedule(
    deep_hours_spec: str = "",
    work_hours_spec: str = "",
    now: Optional[datetime] = None,
) -> ScheduleState:
    """Evaluate the current schedule state.

    Args:
        deep_hours_spec: Time ranges for deep exploration (e.g., "0-6").
        work_hours_spec: Time ranges for mission-only work (e.g., "8-20").
        now: Current time (defaults to datetime.now()).

    Returns:
        ScheduleState indicating which mode is active.
    """
    if now is None:
        now = datetime.now()

    current_hour = now.hour

    in_deep = False
    in_work = False

    try:
        deep_ranges = parse_time_ranges(deep_hours_spec)
        in_deep = any(r.contains(current_hour) for r in deep_ranges)
    except ValueError:
        pass  # Invalid config — treat as no deep hours

    try:
        work_ranges = parse_time_ranges(work_hours_spec)
        in_work = any(r.contains(current_hour) for r in work_ranges)
    except ValueError:
        pass  # Invalid config — treat as no work hours

    # deep_hours takes priority if both overlap
    if in_deep and in_work:
        in_work = False

    return ScheduleState(in_deep_hours=in_deep, in_work_hours=in_work)


def get_schedule_config() -> Tuple[str, str]:
    """Read schedule configuration from config.yaml.

    Returns:
        (deep_hours_spec, work_hours_spec) tuple of strings.
    """
    try:
        from app.utils import load_config

        config = load_config()
        schedule = config.get("schedule", {})
        if not isinstance(schedule, dict):
            return "", ""
        deep = str(schedule.get("deep_hours", ""))
        work = str(schedule.get("work_hours", ""))
        return deep, work
    except (ImportError, OSError, ValueError):
        return "", ""


def get_current_schedule() -> ScheduleState:
    """Get the current schedule state from config.

    Convenience function that reads config and evaluates the current time.

    Returns:
        ScheduleState for the current moment.
    """
    deep_spec, work_spec = get_schedule_config()
    return check_schedule(deep_spec, work_spec)


def adjust_contemplative_chance(base_chance: int, schedule: ScheduleState) -> int:
    """Adjust contemplative chance based on schedule.

    During deep hours: triple the chance (capped at 50%).
    During work hours: zero (no contemplative sessions).
    Normal: unchanged.

    Args:
        base_chance: Base contemplative chance from config (0-100).
        schedule: Current schedule state.

    Returns:
        Adjusted chance (0-100).
    """
    if schedule.in_work_hours:
        return 0
    if schedule.in_deep_hours:
        return min(base_chance * 3, 50)
    return base_chance


def should_relax_pr_limit(schedule: ScheduleState) -> bool:
    """Check if PR limits should be relaxed for exploration.

    During deep_hours, the agent should be free to explore even when
    projects are at their PR limit — it can work in review mode
    (read-only: audit code, find bugs, write reports) without creating
    new PRs.

    Args:
        schedule: Current schedule state.

    Returns:
        True if PR limits should be relaxed (deep_hours active).
    """
    return schedule.in_deep_hours


def should_suppress_exploration(schedule: ScheduleState) -> bool:
    """Check if autonomous exploration should be suppressed.

    During work hours, the agent should only process queued missions,
    similar to /focus mode behavior.

    Args:
        schedule: Current schedule state.

    Returns:
        True if exploration should be suppressed.
    """
    return schedule.in_work_hours


def is_scheduled_active(schedule: Optional[ScheduleState] = None) -> bool:
    """Check if a schedule window (deep_hours or work_hours) is currently active.

    Used to prevent idle auto-pause during scheduled hours. When the human
    configures deep_hours or work_hours, the agent should respect those
    windows and stay active — not give up after an idle timeout.

    Args:
        schedule: Current schedule state (fetched if not provided).

    Returns:
        True if currently in deep_hours or work_hours.
    """
    if schedule is None:
        schedule = get_current_schedule()
    return schedule.in_deep_hours or schedule.in_work_hours


def cap_mode_for_schedule(
    budget_mode: str,
    schedule: ScheduleState,
    deep_hours_configured: bool,
) -> str:
    """Cap the budget-derived mode based on schedule constraints.

    When deep_hours are configured and the current time is NOT within
    deep_hours, the mode is capped at "implement" — preventing the agent
    from entering "deep" mode outside the designated schedule.

    This ensures the human's deep_hours setting is honored: the agent
    only takes deep initiatives (autonomous exploration, deep research
    suggestions, 60+ min work sessions) during the configured window.

    Args:
        budget_mode: Mode decided by usage_tracker ("wait"/"review"/"implement"/"deep").
        schedule: Current schedule state.
        deep_hours_configured: Whether deep_hours is set in config (non-empty).

    Returns:
        Potentially capped mode string.
    """
    if budget_mode != "deep":
        return budget_mode

    # If deep_hours are configured and we're NOT in them, cap to implement
    if deep_hours_configured and not schedule.in_deep_hours:
        return "implement"

    return budget_mode
