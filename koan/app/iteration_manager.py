"""
Kōan -- Iteration planning for the main run loop.

Consolidates per-iteration decision-making into a single Python call:
1. Refresh usage from accumulated token state
2. Decide autonomous mode (wait/review/implement/deep)
3. Inject due recurring missions
4. Pick next mission (or enter autonomous mode)
5. Resolve project path from mission or round-robin
6. Handle autonomous mode decisions (contemplative, focus, WAIT)
7. Resolve focus area description

CLI interface:
    python -m app.iteration_manager plan-iteration \\
        --instance <dir> --koan-root <dir> \\
        --run-num <int> --count <int> \\
        --projects <semicolon-separated> \\
        --last-project <name> \\
        --usage-state <path>

Output: JSON on stdout with iteration plan.
"""

import argparse
import json
import random
import re
import sys
from collections import namedtuple
from pathlib import Path
from typing import List, Optional, Tuple

from app.loop_manager import resolve_focus_area


def _log_iteration(category: str, message: str):
    """Log iteration events to stderr. Uses stderr to avoid polluting
    stdout when iteration_manager runs as a subprocess (CLI mode outputs JSON)."""
    print(f"[{category}] {message}", file=sys.stderr)


def _refresh_usage(usage_state: Path, usage_md: Path, count: int):
    """Refresh usage.md from accumulated token state.

    Always refreshes — critical after auto-resume so stale usage.md
    is cleared and session resets are detected.
    """
    try:
        from app.usage_estimator import cmd_refresh
        cmd_refresh(usage_state, usage_md)
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Usage refresh error: {e}")


_MODE_DOWNGRADE = {
    "deep": "implement",
    "implement": "review",
    "review": "wait",
}


def _downgrade_if_unaffordable(tracker, mode: str) -> str:
    """Downgrade mode until can_afford_run() passes or we hit wait.

    Called after decide_mode() to ensure the estimated run cost
    actually fits within remaining budget. Prevents launching a deep
    session when budget can only cover a review.
    """
    original = mode
    while mode in _MODE_DOWNGRADE and not tracker.can_afford_run(mode):
        mode = _MODE_DOWNGRADE[mode]
    if mode != original:
        _log_iteration("koan",
            f"Budget check: downgraded {original} → {mode} "
            f"(estimated cost {tracker.estimate_run_cost():.1f}%)")
    return mode


def _get_usage_decision(usage_md: Path, count: int, projects_str: str):
    """Parse usage.md and decide autonomous mode.

    Returns:
        dict with keys: mode, available_pct, reason, display_lines
    """
    try:
        from app.usage_tracker import UsageTracker, _get_budget_mode, _get_budget_thresholds
        budget_mode = _get_budget_mode()
        warn_pct, stop_pct = _get_budget_thresholds()
        tracker = UsageTracker(usage_md, count, budget_mode=budget_mode,
                               warn_pct=warn_pct, stop_pct=stop_pct)
        mode = tracker.decide_mode()

        # Verify the chosen mode is affordable; downgrade if not
        mode = _downgrade_if_unaffordable(tracker, mode)

        session_rem, weekly_rem = tracker.remaining_budget()
        available_pct = int(min(session_rem, weekly_rem))
        reason = tracker.get_decision_reason(mode)

        # Get display lines for console output
        display_lines = []
        if usage_md.exists():
            content = usage_md.read_text()
            session_match = re.search(r'^.*Session.*$', content, re.MULTILINE | re.IGNORECASE)
            weekly_match = re.search(r'^.*Weekly.*$', content, re.MULTILINE | re.IGNORECASE)
            if session_match:
                display_lines.append(session_match.group(0).strip())
            if weekly_match:
                display_lines.append(weekly_match.group(0).strip())

        return {
            "mode": mode,
            "available_pct": available_pct,
            "reason": reason,
            "display_lines": display_lines,
        }
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Usage tracker error: {e}")
        return {
            "mode": "review",
            "available_pct": 0,
            "reason": "Tracker error — safe fallback (review only)",
            "display_lines": [],
            "tracker_error": str(e),
        }


def _inject_recurring(instance_dir: Path):
    """Inject due recurring missions into the pending queue.

    Returns:
        list of injection descriptions (for logging)
    """
    recurring_path = instance_dir / "recurring.json"
    if not recurring_path.exists():
        return []

    try:
        from app.recurring import check_and_inject
        missions_path = instance_dir / "missions.md"
        return check_and_inject(recurring_path, missions_path)
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Recurring injection error: {e}")
        return []


def _fallback_mission_extract(instance_dir: Path, projects_str: str,
                              context_msg: str):
    """Attempt direct mission extraction when the picker fails or returns empty.

    Safety net that bypasses the Claude-based picker and reads missions.md
    directly.  Shared by both the "picker returned nothing" and "picker
    crashed" branches inside ``_pick_mission()``.

    Returns:
        (project_name, mission_title) or (None, None)
    """
    try:
        from app.missions import count_pending
        from app.pick_mission import fallback_extract

        missions_path = instance_dir / "missions.md"
        if not missions_path.exists():
            return None, None

        pending_count = count_pending(missions_path.read_text())
        if pending_count <= 0:
            return None, None

        _log_iteration("error",
            f"{context_msg} — {pending_count} pending mission(s) exist "
            f"— attempting direct extraction")
        project, title = fallback_extract(missions_path, projects_str)
        if project and title:
            _log_iteration("mission",
                f"Direct fallback picked: [{project}] {title[:60]}")
            return project, title

        _log_iteration("error", "Direct fallback also failed to extract a mission")
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Fallback mission extract failed: {e}")
    return None, None


def _pick_mission(instance_dir: Path, projects_str: str, run_num: int,
                  autonomous_mode: str, last_project: str):
    """Pick next mission from the queue.

    Returns:
        (project_name, mission_title) or (None, None) for autonomous mode
    """
    try:
        from app.pick_mission import pick_mission
        result = pick_mission(
            str(instance_dir), projects_str,
            str(run_num), autonomous_mode, last_project,
        )
        if result:
            parts = result.split(":", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
        # pick_mission returned empty — safety net for silent picker failures
        return _fallback_mission_extract(
            instance_dir, projects_str,
            "Mission picker returned nothing but")
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Mission picker error: {e}")
        return _fallback_mission_extract(
            instance_dir, projects_str,
            "Picker crashed but")


def _projects_to_str(projects: List[Tuple[str, str]]) -> str:
    """Convert a list of (name, path) tuples to semicolon-separated string.

    This is used for downstream functions that still expect the string format
    (pick_mission).
    """
    return ";".join(f"{name}:{path}" for name, path in projects)


def _resolve_project_path(
    project_name: str, projects: List[Tuple[str, str]],
) -> Optional[str]:
    """Find the path for a project name.

    Returns:
        Path string or None if not found
    """
    for name, path in projects:
        if name == project_name:
            return path
    return None


def _get_project_by_index(projects: List[Tuple[str, str]], idx: int):
    """Get (name, path) for project at given index.

    Returns:
        (name, path) tuple
    """
    if not projects:
        return "default", ""
    idx = max(0, min(idx, len(projects) - 1))
    return projects[idx]


def _get_known_project_names(projects: List[Tuple[str, str]]) -> list:
    """Extract sorted list of project names."""
    return sorted(name for name, _ in projects)


def _should_contemplate(autonomous_mode: str, focus_active: bool,
                        contemplative_chance: int,
                        schedule_state=None) -> bool:
    """Check if this iteration should be a contemplative session.

    Contemplative sessions only trigger when:
    - Mode is deep or implement (need budget for Claude call)
    - Focus mode is NOT active
    - Schedule is not in work_hours
    - Random roll succeeds (chance boosted during deep_hours)

    Returns:
        True if should run a contemplative session
    """
    if autonomous_mode not in ("deep", "implement"):
        return False

    if focus_active:
        return False

    # Adjust chance based on schedule (work hours → 0, deep hours → 3x)
    if schedule_state is not None:
        from app.schedule_manager import adjust_contemplative_chance
        contemplative_chance = adjust_contemplative_chance(
            contemplative_chance, schedule_state
        )

    return random.randint(0, 99) < contemplative_chance


def _check_focus(koan_root: str):
    """Check focus mode state.

    Returns:
        Focus state object if active, None if not active.
        Gracefully returns None if focus_manager module is not available.
    """
    try:
        from app.focus_manager import check_focus
        return check_focus(koan_root)
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Focus check failed: {e}")
        return None


def _select_random_exploration_project(
    projects: List[Tuple[str, str]],
    last_project: str = "",
    instance_dir: str = "",
) -> Tuple[str, str]:
    """Randomly select a project for autonomous exploration.

    Uses session outcome history to weight selection: fresh projects
    (recently productive) are preferred over stale ones (consecutive
    empty sessions).  Also avoids repeating the last explored project.

    Args:
        projects: List of eligible (name, path) tuples (must be non-empty).
        last_project: Name of the project used in the previous iteration.
        instance_dir: Path to instance directory (for freshness lookup).

    Returns:
        (name, path) tuple of the selected project.
    """
    if len(projects) == 1:
        return projects[0]

    # Load session outcomes once for both freshness and drift lookups
    # (avoids 2N file reads — one per project per function)
    weights = None
    drift = None
    success_rates = None
    if instance_dir:
        try:
            from app.session_tracker import (
                _load_outcomes, get_project_freshness, get_project_drift,
            )
            from pathlib import Path as _Path
            outcomes_path = _Path(instance_dir) / "session_outcomes.json"
            all_outcomes = _load_outcomes(outcomes_path)

            weights = get_project_freshness(instance_dir, projects,
                                             _all_outcomes=all_outcomes)
            drift = get_project_drift(instance_dir, projects,
                                       _all_outcomes=all_outcomes)
        except (ImportError, OSError, ValueError) as e:
            _log_iteration("error", f"Freshness/drift lookup failed: {e}")

        try:
            from app.mission_metrics import get_project_success_rates
            project_names = [n for n, _ in projects]
            success_rates = get_project_success_rates(
                instance_dir, project_names, days=30,
            )
        except (ImportError, OSError, ValueError) as e:
            _log_iteration("error", f"Success rate lookup failed: {e}")

    # Filter out last project when possible
    candidates = projects
    if last_project and len(projects) > 1:
        filtered = [(n, p) for n, p in projects if n != last_project]
        if filtered:
            candidates = filtered

    # Weighted random selection combining freshness, drift, and success rate
    if (weights or drift or success_rates) and len(candidates) > 1:
        candidate_weights = []
        for name, _ in candidates:
            base = weights.get(name, 10) if weights else 10
            # Drift boost: projects with significant new commits get a bonus
            if drift:
                d = drift.get(name, 0)
                if d >= 15:
                    base += 6  # High drift — strong pull
                elif d >= 5:
                    base += 3  # Moderate drift
                elif d >= 3:
                    base += 1  # Minor drift
            # Success rate adjustment: deprioritize projects with low success
            # Only applies when we have enough data (rate != 0.5 neutral)
            if success_rates:
                rate = success_rates.get(name, 0.5)
                if rate < 0.3:
                    base = max(1, base - 3)  # Low success — reduce weight
                elif rate >= 0.7:
                    base += 2  # High success — boost
            candidate_weights.append(base)

        total = sum(candidate_weights)
        if total > 0:
            selected = random.choices(candidates, weights=candidate_weights, k=1)[0]
            extra_info = []
            if weights:
                staleness = 10 - weights.get(selected[0], 10)
                if staleness > 0:
                    extra_info.append(f"staleness={staleness}")
            if drift:
                d = drift.get(selected[0], 0)
                if d > 0:
                    extra_info.append(f"drift={d} commits")
            if success_rates:
                rate = success_rates.get(selected[0], 0.5)
                if rate != 0.5:
                    extra_info.append(f"success={rate:.0%}")
            suffix = f" ({', '.join(extra_info)})" if extra_info else ""
            _log_iteration("koan",
                f"Weighted selection: '{selected[0]}'{suffix} "
                f"from {len(candidates)} candidate(s)")
            return selected

    return random.choice(candidates)


FilterResult = namedtuple("FilterResult", ["projects", "pr_limited"])
AutonomousDecision = namedtuple("AutonomousDecision", ["action", "focus_remaining"])


def _filter_exploration_projects(
    projects: List[Tuple[str, str]], koan_root: str,
    schedule_state=None,
) -> FilterResult:
    """Filter projects to only those eligible for exploration.

    Checks two gates in order:
    1. ``exploration`` flag — projects with ``exploration: false`` are excluded.
    2. ``max_open_prs`` limit — projects at or over their PR limit are excluded.

    Returns a FilterResult with:
    - ``projects``: list of (name, path) tuples eligible for exploration
    - ``pr_limited``: list of project names excluded due to PR limit
    """
    from app.projects_config import (
        load_projects_config, get_project_exploration,
        get_project_max_open_prs,
    )

    try:
        config = load_projects_config(koan_root)
    except (OSError, ValueError) as e:
        print(f"[iteration_manager] Could not load projects config: {e}", file=sys.stderr)
        return FilterResult(projects=projects, pr_limited=[])

    if config is None:
        return FilterResult(projects=projects, pr_limited=[])

    # Gate 1: exploration flag
    exploration_enabled = [
        (name, path) for name, path in projects
        if get_project_exploration(config, name)
    ]

    # Gate 2: max_open_prs limit
    # During deep_hours, relax PR limits — allow exploration in review mode
    skip_pr_limit = False
    if schedule_state is not None:
        from app.schedule_manager import should_relax_pr_limit
        skip_pr_limit = should_relax_pr_limit(schedule_state)

    if skip_pr_limit:
        return FilterResult(projects=exploration_enabled, pr_limited=[])

    from app.github import get_gh_username, batch_count_open_prs, cached_count_open_prs
    author = get_gh_username()

    # Phase 1: Collect all repos that need PR counts
    # Projects with limit=0, no author, or no URLs skip the PR check entirely
    projects_needing_check = {}  # name -> (path, limit, urls_to_check)
    filtered = []
    pr_limited = []

    for name, path in exploration_enabled:
        limit = get_project_max_open_prs(config, name)
        if limit == 0:
            filtered.append((name, path))
            continue

        if not author:
            filtered.append((name, path))
            continue

        project_cfg = config.get("projects", {}).get(name, {}) or {}
        urls_to_check = set()
        primary_url = project_cfg.get("github_url", "")
        if primary_url:
            urls_to_check.add(primary_url)
        for url in project_cfg.get("github_urls", []):
            if url:
                urls_to_check.add(url)

        if not urls_to_check:
            _log_iteration("debug",
                f"Project '{name}' has max_open_prs={limit} but no github_url — skipping PR check")
            filtered.append((name, path))
            continue

        projects_needing_check[name] = (path, limit, urls_to_check)

    if not projects_needing_check:
        return FilterResult(projects=filtered, pr_limited=pr_limited)

    # Phase 2: Batch-fetch PR counts for all repos in one GraphQL call
    all_repos = []
    for _, (_, _, urls) in projects_needing_check.items():
        all_repos.extend(urls)
    all_repos = list(dict.fromkeys(all_repos))  # deduplicate, preserve order

    batch_results = batch_count_open_prs(all_repos, author)

    # Phase 3: Evaluate limits using batch results (fall back to sequential on miss)
    for name, (path, limit, urls_to_check) in projects_needing_check.items():
        total_open = 0
        any_error = True

        for url in urls_to_check:
            if url in batch_results:
                count = batch_results[url]
            else:
                # Batch missed this repo — fall back to individual query
                count = cached_count_open_prs(url, author)
            if count >= 0:
                total_open += count
                any_error = False

        if any_error:
            filtered.append((name, path))
            continue

        if total_open >= limit:
            _log_iteration("koan",
                f"Project '{name}' at PR limit ({total_open}/{limit}) — excluding from exploration")
            pr_limited.append(name)
        else:
            filtered.append((name, path))

    return FilterResult(projects=filtered, pr_limited=pr_limited)


def _check_schedule():
    """Check schedule state (time-of-day windows from config).

    Returns:
        ScheduleState object, or None if schedule is not configured
        or module is unavailable.
    """
    try:
        from app.schedule_manager import get_current_schedule
        return get_current_schedule()
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Schedule check failed: {e}")
        return None


def _make_result(*, action, project_name, project_path="",
                 mission_title="", autonomous_mode, focus_area="",
                 available_pct, decision_reason, display_lines,
                 recurring_injected, focus_remaining=None,
                 schedule_mode="normal", error=None,
                 tracker_error=None):
    """Build a standardised iteration-plan result dict."""
    return {
        "action": action,
        "project_name": project_name,
        "project_path": project_path or "",
        "mission_title": mission_title,
        "autonomous_mode": autonomous_mode,
        "focus_area": focus_area,
        "available_pct": available_pct,
        "decision_reason": decision_reason,
        "display_lines": display_lines,
        "recurring_injected": recurring_injected,
        "focus_remaining": focus_remaining,
        "schedule_mode": schedule_mode,
        "error": error,
        "tracker_error": tracker_error,
    }


def _decide_autonomous_action(
    autonomous_mode: str,
    koan_root: str,
    schedule_state,
    contemplative_chance: int = 10,
) -> "AutonomousDecision":
    """Decide autonomous action via a linear priority chain.

    Called when no mission is pending and WAIT mode has already been
    handled upstream (before exploration filtering).

    Priority (first match wins):
    1. Contemplative session — random roll, requires deep/implement + no focus
    2. Focus wait — focus mode active, skip exploration
    3. Schedule wait — work_hours active, skip exploration
    4. Autonomous exploration — default fallback

    Returns:
        AutonomousDecision(action, focus_remaining)
    """
    focus_state = _check_focus(koan_root)
    focus_active = focus_state is not None
    _log_iteration("koan",
        f"Evaluating autonomous action "
        f"(mode={autonomous_mode}, focus_active={focus_active})")

    # 1. Contemplative session (random reflection)
    if _should_contemplate(autonomous_mode, focus_active,
                           contemplative_chance, schedule_state):
        return AutonomousDecision(action="contemplative", focus_remaining=None)

    # 2. Focus mode active → wait for missions
    if focus_state is not None:
        try:
            focus_remaining = focus_state.remaining_display()
        except (ValueError, OSError) as e:
            _log_iteration("error", f"Focus state display error: {e}")
            focus_remaining = "unknown"
        return AutonomousDecision(action="focus_wait",
                                 focus_remaining=focus_remaining)

    # 3. Schedule work_hours → suppress exploration
    if schedule_state is not None and schedule_state.in_work_hours:
        return AutonomousDecision(action="schedule_wait", focus_remaining=None)

    # 4. Default: autonomous exploration
    return AutonomousDecision(action="autonomous", focus_remaining=None)


def plan_iteration(
    instance_dir: str,
    koan_root: str,
    run_num: int,
    count: int,
    projects: List[Tuple[str, str]],
    last_project: str = "",
    usage_state_path: str = "",
) -> dict:
    """Plan a single iteration of the run loop.

    This is the main entry point. It consolidates all per-iteration
    decision-making into a single call.

    Args:
        instance_dir: Path to instance directory
        koan_root: Path to KOAN_ROOT
        run_num: Current run number (1-based)
        count: Completed runs count
        projects: List of (name, path) tuples
        last_project: Last project name (for rotation)
        usage_state_path: Path to usage_state.json (defaults to instance/usage_state.json)

    Returns:
        dict with iteration plan:
        {
            "action": "mission" | "autonomous" | "contemplative" | "focus_wait" | "schedule_wait" | "exploration_wait" | "pr_limit_wait" | "wait_pause" | "error",
            "project_name": str,
            "project_path": str,
            "mission_title": str (empty for autonomous/contemplative),
            "autonomous_mode": str (wait/review/implement/deep),
            "focus_area": str,
            "available_pct": int,
            "decision_reason": str,
            "display_lines": list[str] (usage status lines for console),
            "recurring_injected": list[str] (injected recurring missions),
            "focus_remaining": str | None (if focus mode active),
            "schedule_mode": str (deep/work/normal from schedule config),
            "error": str | None (project validation error),
        }
    """
    instance = Path(instance_dir)
    if usage_state_path:
        usage_state = Path(usage_state_path)
    else:
        usage_state = instance / "usage_state.json"
    usage_md = instance / "usage.md"

    # Convert projects to string format for downstream functions
    projects_str = _projects_to_str(projects)

    # Step 1: Refresh usage
    _refresh_usage(usage_state, usage_md, count)

    # Step 2: Get usage decision (mode, available%, reason, project idx)
    decision = _get_usage_decision(usage_md, count, projects_str)
    autonomous_mode = decision["mode"]
    available_pct = decision["available_pct"]
    decision_reason = decision["reason"]
    display_lines = decision["display_lines"]
    tracker_error = decision.get("tracker_error")
    _log_iteration("koan", f"Usage decision: mode={autonomous_mode}, available={available_pct}%")

    # Step 2b: Check schedule and cap mode based on deep_hours config.
    # This runs early (before mission pick) so the capped mode affects
    # everything downstream — including the prompt sent for missions.
    schedule_state = _check_schedule()
    deep_hours_configured = False
    try:
        from app.schedule_manager import get_schedule_config, cap_mode_for_schedule
        deep_spec, _ = get_schedule_config()
        deep_hours_configured = bool(deep_spec.strip())
        if schedule_state is not None:
            original_mode = autonomous_mode
            autonomous_mode = cap_mode_for_schedule(
                autonomous_mode, schedule_state, deep_hours_configured,
            )
            if autonomous_mode != original_mode:
                decision_reason = (
                    f"{decision_reason} (capped from {original_mode}: "
                    f"outside deep_hours schedule)"
                )
    except (ImportError, OSError, ValueError) as e:
        _log_iteration("error", f"Schedule mode cap check failed: {e}")

    # Step 3: Inject recurring missions
    recurring_injected = _inject_recurring(instance)

    # Step 4: Pick mission
    mission_project, mission_title = _pick_mission(
        instance, projects_str, run_num, autonomous_mode, last_project,
    )
    if mission_project and mission_title:
        _log_iteration("mission", f"Mission picked: [{mission_project}] {mission_title[:80]}")
    else:
        _log_iteration("koan", "No pending mission — entering autonomous mode")

    # Step 5: Resolve project
    if mission_project and mission_title:
        # Mission picked — resolve project path
        project_name = mission_project
        project_path = _resolve_project_path(project_name, projects)

        if project_path is None:
            known = _get_known_project_names(projects)
            return _make_result(
                action="error",
                project_name=project_name,
                mission_title=mission_title,
                autonomous_mode=autonomous_mode,
                available_pct=available_pct,
                decision_reason=decision_reason,
                display_lines=display_lines,
                recurring_injected=recurring_injected,
                schedule_mode=schedule_state.mode if schedule_state else "normal",
                error=f"Unknown project '{project_name}'. Known: {', '.join(known)}",
                tracker_error=tracker_error,
            )
    else:
        # No mission — autonomous mode
        mission_title = ""

        # Short-circuit: WAIT mode means budget is exhausted — skip
        # exploration filtering entirely to avoid wasted gh API calls.
        if autonomous_mode == "wait":
            focus_area = resolve_focus_area(autonomous_mode, has_mission=False)
            return _make_result(
                action="wait_pause",
                project_name=projects[0][0] if projects else "default",
                project_path=projects[0][1] if projects else "",
                autonomous_mode=autonomous_mode,
                focus_area=focus_area,
                available_pct=available_pct,
                decision_reason=decision_reason,
                display_lines=display_lines,
                recurring_injected=recurring_injected,
                schedule_mode=schedule_state.mode if schedule_state else "normal",
                tracker_error=tracker_error,
            )

        # Filter to exploration-enabled projects only
        filter_result = _filter_exploration_projects(projects, koan_root,
                                                     schedule_state=schedule_state)
        exploration_projects = filter_result.projects
        if not exploration_projects:
            # Determine whether this is exploration-disabled or PR-limited
            if filter_result.pr_limited:
                _log_iteration("koan", "All exploration projects at PR limit — waiting for reviews")
                wait_action = "pr_limit_wait"
                wait_reason = (
                    f"PR limit reached for: {', '.join(filter_result.pr_limited)} "
                    f"— waiting for reviews"
                )
            else:
                _log_iteration("koan", "All projects have exploration disabled — waiting for missions")
                wait_action = "exploration_wait"
                wait_reason = "All projects have exploration disabled — waiting for missions"

            focus_area = resolve_focus_area(autonomous_mode, has_mission=False)
            return _make_result(
                action=wait_action,
                project_name=projects[0][0] if projects else "default",
                project_path=projects[0][1] if projects else "",
                autonomous_mode=autonomous_mode,
                focus_area=focus_area,
                available_pct=available_pct,
                decision_reason=wait_reason,
                display_lines=display_lines,
                recurring_injected=recurring_injected,
                schedule_mode=schedule_state.mode if schedule_state else "normal",
                tracker_error=tracker_error,
            )

        project_name, project_path = _select_random_exploration_project(
            exploration_projects, last_project,
            instance_dir=instance_dir,
        )
        _log_iteration("koan",
            f"Exploration: selected '{project_name}' "
            f"from {len(exploration_projects)} eligible project(s)"
            f"{' (avoiding last: ' + last_project + ')' if last_project and last_project != project_name else ''}")

    # Step 6: Determine action for autonomous mode
    if mission_title:
        action = "mission"
    else:
        # No mission — decide autonomous action via priority chain
        try:
            from app.utils import get_contemplative_chance
            contemplative_chance = get_contemplative_chance()
        except (ImportError, OSError, ValueError) as e:
            _log_iteration("error", f"Contemplative chance load error: {e}")
            contemplative_chance = 10

        autonomous_decision = _decide_autonomous_action(
            autonomous_mode, koan_root, schedule_state, contemplative_chance,
        )
        action = autonomous_decision.action

        if action == "focus_wait":
            focus_area = resolve_focus_area(autonomous_mode, has_mission=False)
            return _make_result(
                action=action,
                project_name=project_name,
                project_path=project_path,
                autonomous_mode=autonomous_mode,
                focus_area=focus_area,
                available_pct=available_pct,
                decision_reason=decision_reason,
                display_lines=display_lines,
                recurring_injected=recurring_injected,
                focus_remaining=autonomous_decision.focus_remaining,
                schedule_mode=schedule_state.mode if schedule_state else "normal",
                tracker_error=tracker_error,
            )

        if action == "schedule_wait":
            focus_area = resolve_focus_area(autonomous_mode, has_mission=False)
            return _make_result(
                action=action,
                project_name=project_name,
                project_path=project_path,
                autonomous_mode=autonomous_mode,
                focus_area=focus_area,
                available_pct=available_pct,
                decision_reason=decision_reason,
                display_lines=display_lines,
                recurring_injected=recurring_injected,
                schedule_mode="work",
                tracker_error=tracker_error,
            )

    # Step 7: Resolve focus area
    has_mission = bool(mission_title)
    focus_area = resolve_focus_area(autonomous_mode, has_mission=has_mission)

    return _make_result(
        action=action,
        project_name=project_name,
        project_path=project_path,
        mission_title=mission_title,
        autonomous_mode=autonomous_mode,
        focus_area=focus_area,
        available_pct=available_pct,
        decision_reason=decision_reason,
        display_lines=display_lines,
        recurring_injected=recurring_injected,
        schedule_mode=schedule_state.mode if schedule_state else "normal",
        tracker_error=tracker_error,
    )


def main():
    """CLI entry point for iteration_manager."""
    parser = argparse.ArgumentParser(description="Kōan iteration planner")
    subparsers = parser.add_subparsers(dest="command")

    plan_parser = subparsers.add_parser("plan-iteration",
                                        help="Plan next loop iteration")
    plan_parser.add_argument("--instance", required=True, help="Instance directory")
    plan_parser.add_argument("--koan-root", required=True, help="KOAN_ROOT directory")
    plan_parser.add_argument("--run-num", type=int, required=True, help="Current run number (1-based)")
    plan_parser.add_argument("--count", type=int, required=True, help="Completed runs count")
    plan_parser.add_argument("--projects", required=True, help="Projects string (name:path;...)")
    plan_parser.add_argument("--last-project", default="", help="Last project name")
    plan_parser.add_argument("--usage-state", required=True, help="Path to usage_state.json")

    args = parser.parse_args()

    if args.command == "plan-iteration":
        # Convert CLI string format to tuples
        projects = []
        for pair in args.projects.split(";"):
            pair = pair.strip()
            if pair:
                parts = pair.split(":", 1)
                if len(parts) == 2:
                    projects.append((parts[0].strip(), parts[1].strip()))
        result = plan_iteration(
            instance_dir=args.instance,
            koan_root=args.koan_root,
            run_num=args.run_num,
            count=args.count,
            projects=projects,
            last_project=args.last_project,
            usage_state_path=args.usage_state,
        )
        print(json.dumps(result))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
