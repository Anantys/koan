"""
Kōan -- Mission execution pipeline.

Handles the full lifecycle of a single mission run:
1. Build the Claude CLI command (prompt, tools, flags)
2. Parse Claude JSON output (extract text from various response shapes)
3. Post-mission processing (usage tracking, pending.md archival, reflection,
   auto-merge)

CLI interface:
    python -m app.mission_runner build-command \\
        --instance ... --autonomous-mode ... [--mission-title ...]
    python -m app.mission_runner parse-output <json_file>
    python -m app.mission_runner post-mission \\
        --instance ... --project-name ... --project-path ... \\
        --run-num N --max-runs N --exit-code N \\
        --stdout-file ... --stderr-file ... \\
        [--mission-title ...] [--autonomous-mode ...] [--start-time N]
"""

import json
import os
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Maximum wall-clock time for the entire post-mission pipeline (seconds).
# Individual steps have their own timeouts (tests: 120s, reflection: 60s,
# verification: 10s), but without an overall ceiling, accumulated steps
# can block the agent loop for too long.  5 minutes is generous — typical
# runs finish in 30-60s.
POST_MISSION_TIMEOUT = 300

# Status icons shared by _PipelineTracker.summary_lines() and
# _notify_pipeline_failures() — single source of truth.
_STATUS_ICONS = {"success": "✓", "fail": "✗", "skipped": "–", "timeout": "⏱"}


def _get_koan_root(instance_dir: str) -> str:
    """Resolve KOAN_ROOT from env or instance directory parent."""
    return os.environ.get("KOAN_ROOT", str(Path(instance_dir).parent))


class _PipelineTracker:
    """Accumulates step outcomes for the post-mission pipeline.

    Each step is recorded as success/fail/skipped/timeout with optional
    detail (e.g. error message or elapsed time).
    """

    VALID_STATUSES = ("success", "fail", "skipped", "timeout")

    def __init__(self):
        self.steps: Dict[str, dict] = {}

    def record(self, step: str, status: str, detail: str = "") -> None:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        self.steps[step] = {"status": status, "detail": detail}

    def run_step(self, step: str, fn, *args, pipeline_expired=None, **kwargs):
        """Run a step function, recording its outcome automatically.

        If pipeline_expired is set, records 'timeout' and skips execution.
        On exception, records 'fail' with the error message and returns None.
        On success, records 'success' and returns the function's result.
        """
        if pipeline_expired is not None and pipeline_expired.is_set():
            self.record(step, "timeout", "pipeline deadline exceeded")
            return None
        try:
            t0 = time.monotonic()
            result = fn(*args, **kwargs)
            elapsed = time.monotonic() - t0
            self.record(step, "success", f"{elapsed:.1f}s")
            return result
        except Exception as e:
            self.record(step, "fail", str(e))
            print(f"[mission_runner] {step} failed: {e}", file=sys.stderr)
            return None

    def summary_lines(self) -> List[str]:
        """Return a compact summary of all recorded steps."""
        lines = []
        for step, info in self.steps.items():
            status = info["status"]
            icon = _STATUS_ICONS.get(status, "?")
            detail = f" ({info['detail']})" if info["detail"] else ""
            lines.append(f"  {icon} {step}: {status}{detail}")
        return lines

    def has_failures(self) -> bool:
        return any(s["status"] == "fail" for s in self.steps.values())

    def has_issues(self) -> bool:
        """Return True if any step failed, timed out, or was skipped."""
        return any(
            s["status"] in ("fail", "timeout", "skipped")
            for s in self.steps.values()
        )

    def to_dict(self) -> Dict[str, dict]:
        return dict(self.steps)


def _write_pipeline_summary(
    instance_dir: str,
    project_name: str,
    tracker: _PipelineTracker,
    mission_title: str = "",
) -> None:
    """Append a pipeline outcome summary to today's journal."""
    try:
        from app.journal import append_to_journal

        lines = tracker.summary_lines()
        if not lines:
            return

        now = datetime.now().strftime("%H:%M")
        header = f"\n### Pipeline summary — {now}"
        if mission_title:
            header += f"\nMission: {mission_title}"
        entry = header + "\n" + "\n".join(lines) + "\n"
        append_to_journal(Path(instance_dir), project_name, entry)
    except Exception as e:
        print(f"[mission_runner] Pipeline summary write failed: {e}", file=sys.stderr)


def build_mission_command(
    prompt: str,
    autonomous_mode: str = "implement",
    extra_flags: str = "",
    project_name: str = "",
    plugin_dirs: Optional[List[str]] = None,
    system_prompt: str = "",
) -> List[str]:
    """Build the CLI command for mission execution (provider-agnostic).

    Args:
        prompt: The full agent prompt text (user prompt).
        autonomous_mode: Current mode (review/implement/deep).
        extra_flags: Space-separated extra CLI flags from config.
        project_name: Optional project name for per-project tool overrides.
        plugin_dirs: Optional list of plugin directory paths to load.
        system_prompt: Optional system prompt for cache-friendly positioning.

    Returns:
        Complete command list ready for subprocess.
    """
    from app.config import get_mission_tools, get_model_config
    from app.cli_provider import build_full_command

    # Get mission tools (comma-separated list)
    # REVIEW mode: enforce read-only at tool level (no Bash/Write/Edit)
    if autonomous_mode == "review":
        tools_list = ["Read", "Glob", "Grep"]
    else:
        tools_str = get_mission_tools(project_name)
        tools_list = [t.strip() for t in tools_str.split(",") if t.strip()]

    # Get model configuration with per-project overrides
    models = get_model_config(project_name)
    model = models["mission"]
    if autonomous_mode == "review" and models["review_mode"]:
        model = models["review_mode"]
    fallback = models["fallback"]

    # Build provider-specific command
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=tools_list,
        model=model,
        fallback=fallback,
        output_format="json",
        plugin_dirs=plugin_dirs,
        system_prompt=system_prompt,
    )

    # Append any extra flags from config
    if extra_flags.strip():
        cmd.extend(extra_flags.strip().split())

    return cmd


def get_mission_flags(autonomous_mode: str = "", project_name: str = "") -> str:
    """Get CLI flags for mission role from config.

    Args:
        autonomous_mode: Current mode (review/implement/deep).
        project_name: Optional project name for per-project model overrides.

    Returns:
        Space-separated CLI flags string (may be empty).
    """
    from app.config import get_claude_flags_for_role

    return get_claude_flags_for_role("mission", autonomous_mode, project_name)


def parse_claude_output(raw_text: str) -> str:
    """Extract human-readable text from Claude JSON output.

    Handles multiple JSON response shapes:
    - {"result": "..."}
    - {"content": "..."}
    - {"text": "..."}
    Falls back to raw text if JSON parsing fails.

    Args:
        raw_text: Raw stdout from Claude CLI (JSON or plain text).

    Returns:
        Extracted text content.
    """
    if not raw_text.strip():
        return ""

    try:
        data = json.loads(raw_text)
        # Try common response keys in order
        for key in ("result", "content", "text"):
            if key in data and isinstance(data[key], str):
                return data[key]
        # If none match, return the raw text
        return raw_text.strip()
    except (json.JSONDecodeError, TypeError):
        return raw_text.strip()


def _read_pending_content(instance_dir: str) -> str:
    """Read pending.md content before archival for session classification."""
    pending_path = Path(instance_dir) / "journal" / "pending.md"
    try:
        if pending_path.exists():
            return pending_path.read_text()
    except (OSError, FileNotFoundError):
        pass
    return ""


def _read_stdout_summary(stdout_file: str, max_chars: int = 2000) -> str:
    """Extract text summary from Claude stdout file for session classification.

    When the agent deletes pending.md as part of its Mission Completion
    Checklist, the pending content is empty. The stdout file contains
    Claude's full JSON output which often includes productive signals
    (branch names, PR numbers, test results).

    Returns a truncated text extract, or empty string on error.
    """
    try:
        stdout_path = Path(stdout_file)
        if not stdout_path.exists():
            return ""
        raw = stdout_path.read_text(errors="replace")
        if not raw.strip():
            return ""
        text = parse_claude_output(raw)
        return text[:max_chars] if text else ""
    except (OSError, FileNotFoundError):
        return ""


def _record_session_outcome(
    instance_dir: str,
    project_name: str,
    autonomous_mode: str,
    duration_minutes: int,
    journal_content: str,
    mission_title: str = "",
) -> None:
    """Record session outcome for staleness tracking (fire-and-forget)."""
    try:
        from app.session_tracker import record_outcome
        record_outcome(
            instance_dir=instance_dir,
            project=project_name,
            mode=autonomous_mode or "unknown",
            duration_minutes=duration_minutes,
            journal_content=journal_content,
            mission_title=mission_title,
        )
    except Exception as e:
        print(f"[mission_runner] Session outcome recording failed: {e}", file=sys.stderr)


def _record_cost_event(
    instance_dir: str,
    project_name: str,
    stdout_file: str,
    autonomous_mode: str,
    mission_title: str,
) -> None:
    """Record structured usage event to JSONL cost tracker (fire-and-forget)."""
    try:
        from app.usage_estimator import extract_tokens_detailed
        from app.cost_tracker import record_usage

        detailed = extract_tokens_detailed(Path(stdout_file))
        if detailed is None:
            return

        record_usage(
            instance_dir=Path(instance_dir),
            project=project_name or "_global",
            model=detailed["model"],
            input_tokens=detailed["input_tokens"],
            output_tokens=detailed["output_tokens"],
            mode=autonomous_mode,
            mission=mission_title,
            cache_creation_input_tokens=detailed.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=detailed.get("cache_read_input_tokens", 0),
            cost_usd=detailed.get("cost_usd", 0.0),
        )
    except Exception as e:
        print(f"[mission_runner] Cost tracking failed: {e}", file=sys.stderr)


def archive_pending(instance_dir: str, project_name: str, run_num: int) -> bool:
    """Archive pending.md to daily journal if agent didn't clean it up.

    Args:
        instance_dir: Path to instance directory.
        project_name: Current project name.
        run_num: Current run number.

    Returns:
        True if pending.md was archived, False if it didn't exist.
    """
    pending_path = Path(instance_dir) / "journal" / "pending.md"
    if not pending_path.exists():
        return False

    # Read pending content — guard against file disappearing between
    # exists() check and read (TOCTOU race with the agent's own cleanup).
    try:
        pending_content = pending_path.read_text()
    except FileNotFoundError:
        return False

    # Append pending content to daily journal (with file locking)
    from app.journal import append_to_journal
    now = datetime.now().strftime("%H:%M")
    entry = f"\n## Run {run_num} — {now} (auto-archived from pending)\n\n{pending_content}"

    append_to_journal(Path(instance_dir), project_name, entry)

    pending_path.unlink(missing_ok=True)
    return True


def update_usage(stdout_file: str, usage_state: str, usage_md: str) -> bool:
    """Update token usage state from Claude JSON output.

    Args:
        stdout_file: Path to Claude stdout capture file.
        usage_state: Path to usage_state.json.
        usage_md: Path to usage.md.

    Returns:
        True if update succeeded.
    """
    try:
        from app.usage_estimator import cmd_update

        cmd_update(Path(stdout_file), Path(usage_state), Path(usage_md))
        return True
    except Exception as e:
        print(f"[mission_runner] Usage update failed: {e}", file=sys.stderr)
        return False


def trigger_reflection(
    instance_dir: str,
    mission_title: str,
    duration_minutes: int,
    project_name: str = "",
) -> bool:
    """Trigger post-mission reflection if the mission was significant.

    Reads today's journal file for the project to provide context to the
    reflection prompt. The dual heuristic (keyword + substantial journal)
    prevents noise from trivial missions.

    Args:
        instance_dir: Path to instance directory.
        mission_title: Mission description text.
        duration_minutes: Duration in minutes.
        project_name: Current project name (for journal file lookup).

    Returns:
        True if reflection was generated.
    """
    try:
        from app.post_mission_reflection import (
            _read_journal_file,
            is_significant_mission,
            run_reflection,
            write_to_journal,
        )

        inst = Path(instance_dir)
        journal_content = _read_journal_file(inst, project_name)

        if not is_significant_mission(mission_title, duration_minutes, journal_content):
            return False

        reflection = run_reflection(inst, mission_title, journal_content)
        if reflection:
            write_to_journal(inst, reflection)
            return True
    except Exception as e:
        print(f"[mission_runner] Reflection failed: {e}", file=sys.stderr)
    return False


def _get_quality_gate_mode(instance_dir: str, project_name: str) -> str:
    """Get the quality gate mode for a project.

    Returns one of: "strict", "warn", "off". Default: "warn".
    """
    try:
        from app.projects_config import load_projects_config, get_project_config
        koan_root = _get_koan_root(instance_dir)
        config = load_projects_config(koan_root)
        if config:
            project_config = get_project_config(config, project_name)
            pr_quality = project_config.get("pr_quality", {})
            gate = pr_quality.get("gate", "warn")
            if gate in ("strict", "warn", "off"):
                return gate
    except Exception as e:
        print(f"[mission_runner] Quality gate config error: {e}", file=sys.stderr)
    return "warn"


def _run_quality_pipeline(
    instance_dir: str,
    project_name: str,
    project_path: str,
    report_fn,
) -> dict:
    """Run the post-mission quality pipeline.

    Wraps pr_quality.run_quality_pipeline with project config resolution.
    Raises on error — caller (_PipelineTracker.run_step) handles recording.
    """
    from app.config import get_branch_prefix
    from app.pr_quality import run_quality_pipeline

    branch_prefix = get_branch_prefix()
    gate_mode = _get_quality_gate_mode(instance_dir, project_name)

    return run_quality_pipeline(
        project_path=project_path,
        branch_prefix=branch_prefix,
        run_tests=True,
        test_timeout=120,
        gate_mode=gate_mode,
        status_callback=report_fn,
    )


def _run_lint_gate(
    instance_dir: str, project_name: str, project_path: str
):
    """Run lint gate, returning LintResult or None.

    Raises on error — caller (_PipelineTracker.run_step) handles recording.
    """
    from app.lint_gate import run_lint_gate
    return run_lint_gate(project_path, project_name, instance_dir)


def _is_lint_blocking(instance_dir: str, project_name: str) -> bool:
    """Check if lint gate is configured as blocking for a project."""
    try:
        from app.lint_gate import get_project_lint_config
        from app.projects_config import load_projects_config
        koan_root = _get_koan_root(instance_dir)
        config = load_projects_config(koan_root)
        if not config:
            return False
        lint_config = get_project_lint_config(config, project_name)
        return lint_config.get("blocking", True) and lint_config.get("enabled", False)
    except Exception as e:
        print(f"[mission_runner] Lint config check failed: {e}", file=sys.stderr)
        return False


def _run_mission_verification(
    project_path: str,
    mission_title: str,
    exit_code: int,
    instance_dir: str,
):
    """Run post-mission semantic verification.

    Returns VerifyResult. Raises on error — caller handles recording.
    """
    from app.mission_verifier import verify_mission, format_verify_result
    from app.config import get_branch_prefix

    branch_prefix = get_branch_prefix()
    result = verify_mission(
        project_path=project_path,
        mission_title=mission_title,
        exit_code=exit_code,
        branch_prefix=branch_prefix,
    )
    # Log result to console
    print(f"[mission_runner] {format_verify_result(result)}")
    return result


def check_auto_merge(
    instance_dir: str,
    project_name: str,
    project_path: str,
    quality_report: Optional[dict] = None,
    lint_blocked: bool = False,
    verify_blocked: bool = False,
) -> Optional[str]:
    """Check if current branch should be auto-merged.

    Args:
        instance_dir: Path to instance directory.
        project_name: Current project name.
        project_path: Path to project directory.
        quality_report: Optional quality pipeline results for gating.
        lint_blocked: Whether lint gate is blocking auto-merge.
        verify_blocked: Whether verification failure is blocking auto-merge.

    Returns:
        Branch name if auto-merge was attempted, None otherwise.
    """
    try:
        from app.git_sync import run_git
        branch = run_git(project_path, "rev-parse", "--abbrev-ref", "HEAD")
        if not branch:
            return None
        from app.config import get_branch_prefix
        if not branch.startswith(get_branch_prefix()):
            return None

        # Lint gate block
        if lint_blocked:
            print("[mission_runner] Auto-merge blocked by lint gate")
            return None

        # Verification block
        if verify_blocked:
            print("[mission_runner] Auto-merge blocked by verification failure")
            return None

        # Check if auto-merge is configured for this project
        from app.git_auto_merge import auto_merge_branch
        from app.projects_config import load_projects_config, get_project_auto_merge

        koan_root = _get_koan_root(instance_dir)
        projects_config = load_projects_config(koan_root)
        auto_merge_cfg = get_project_auto_merge(projects_config, project_name) if projects_config else {}
        auto_merge_enabled = auto_merge_cfg.get("enabled", False)

        # Quality gate check — only post comments when auto-merge is configured.
        # Without auto-merge, quality info is already in the PR description.
        if quality_report and auto_merge_enabled:
            from app.pr_quality import should_block_auto_merge, post_quality_comment
            gate_mode = _get_quality_gate_mode(instance_dir, project_name)
            if should_block_auto_merge(quality_report, gate_mode):
                print(f"[mission_runner] Auto-merge blocked by quality gate ({gate_mode})")
                try:
                    post_quality_comment(project_path, quality_report)
                except Exception as e:
                    print(f"[mission_runner] Quality comment failed: {e}", file=sys.stderr)
                return None

        auto_merge_branch(instance_dir, project_name, project_path, branch)
        return branch
    except Exception as e:
        print(f"[mission_runner] Auto-merge check failed: {e}", file=sys.stderr)
        return None


def _notify_pipeline_failures(
    tracker: _PipelineTracker,
    mission_title: str = "",
    instance_dir: str = "",
) -> None:
    """Write a warning to outbox.md if the post-mission pipeline had issues.

    Reports failed, timed-out, and skipped steps so users can see when
    steps like reflection or auto_merge silently fail to complete.

    Writing to outbox.md instead of calling Telegram directly ensures the
    bridge retries delivery on transient network errors.
    """
    if not tracker.has_issues():
        return
    try:
        from app.utils import append_to_outbox

        _ISSUE_ICONS = {"fail": "✗", "timeout": "⏱", "skipped": "–"}
        issues = []
        for name, info in tracker.steps.items():
            icon = _ISSUE_ICONS.get(info["status"])
            if icon is None:
                continue
            label = f"{icon} {name}"
            if info["detail"]:
                label += f" ({info['detail']})"
            issues.append(label)
        if not issues:
            return

        prefix = f"[{mission_title}] " if mission_title else ""
        msg = f"⚠️ {prefix}Pipeline issues: {', '.join(issues)}"
        from app.notify import NotificationPriority
        outbox_path = Path(instance_dir) / "outbox.md"
        append_to_outbox(outbox_path, msg + "\n", priority=NotificationPriority.WARNING)
    except Exception as e:
        print(f"[mission_runner] Pipeline failure notification failed: {e}", file=sys.stderr)


def _fire_post_mission_hook(
    instance_dir: str,
    project_name: str,
    project_path: str,
    exit_code: int,
    mission_title: str,
    duration_minutes: int,
    result: dict,
) -> Dict[str, str]:
    """Fire post_mission hooks with full context.

    Returns a dict mapping failed handler names to error messages.
    Empty dict means all hooks succeeded.
    """
    try:
        from app.hooks import fire_hook
        return fire_hook(
            "post_mission",
            instance_dir=instance_dir,
            project_name=project_name,
            project_path=project_path,
            exit_code=exit_code,
            mission_title=mission_title,
            duration_minutes=duration_minutes,
            result=dict(result),
        )
    except Exception as e:
        print(f"[hooks] post_mission hook error: {e}", file=sys.stderr)
        return {"_fire_post_mission_hook": str(e)}


def run_post_mission(
    instance_dir: str,
    project_name: str,
    project_path: str,
    run_num: int,
    exit_code: int,
    stdout_file: str,
    stderr_file: str,
    mission_title: str = "",
    autonomous_mode: str = "",
    start_time: int = 0,
    status_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Run the complete post-mission processing pipeline.

    This replaces ~50 lines of bash that call 5 different Python scripts.

    Args:
        instance_dir: Path to instance directory.
        project_name: Current project name.
        project_path: Path to project directory.
        run_num: Current run number.
        exit_code: Claude CLI exit code.
        stdout_file: Path to Claude stdout capture file.
        stderr_file: Path to Claude stderr capture file.
        mission_title: Mission description (empty for autonomous).
        autonomous_mode: Current mode (review/implement/deep).
        start_time: Mission start time as unix timestamp.
        status_callback: Optional callable to report progress during finalization.
            Called with a short description of the current step.

    Returns:
        Dict with keys:
            success (bool): Whether Claude exited successfully.
            usage_updated (bool): Whether usage tracking was updated.
            pending_archived (bool): Whether pending.md was archived.
            reflection_written (bool): Whether a reflection was generated.
            auto_merge_branch (str|None): Branch name if auto-merge attempted.
            quota_exhausted (bool): Whether quota exhaustion was detected.
            quota_info (tuple|None): (reset_display, resume_message) if exhausted.
    """
    result = {
        "success": exit_code == 0,
        "usage_updated": False,
        "pending_archived": False,
        "reflection_written": False,
        "auto_merge_branch": None,
        "quota_exhausted": False,
        "quota_info": None,
    }

    tracker = _PipelineTracker()

    # Overall pipeline deadline — prevents accumulated steps from blocking
    # the agent loop indefinitely.
    _pipeline_expired = threading.Event()
    _deadline_timer = threading.Timer(
        POST_MISSION_TIMEOUT,
        lambda: (
            _pipeline_expired.set(),
            print(
                f"[mission_runner] Post-mission pipeline exceeded {POST_MISSION_TIMEOUT}s — "
                "skipping remaining steps",
                file=sys.stderr,
            ),
        ),
    )
    _deadline_timer.daemon = True
    _deadline_timer.start()

    try:
        def _report(step: str) -> None:
            if status_callback:
                status_callback(step)

        # 1. Update token usage from JSON output
        _report("updating usage stats")
        usage_state = os.path.join(instance_dir, "usage_state.json")
        usage_md = os.path.join(instance_dir, "usage.md")
        result["usage_updated"] = update_usage(stdout_file, usage_state, usage_md)
        tracker.record("usage_update", "success" if result["usage_updated"] else "fail")

        # 1b. Record structured usage to JSONL cost tracker
        _record_cost_event(
            instance_dir, project_name, stdout_file,
            autonomous_mode, mission_title,
        )

        # 2. Compute duration (needed for quota early-return, reflection, and outcome tracking)
        if start_time > 0:
            duration_minutes = (int(datetime.now().timestamp()) - start_time) // 60
        else:
            duration_minutes = 0

        # 3. Check for quota exhaustion
        _report("checking quota")
        from app.quota_handler import handle_quota_exhaustion, QUOTA_CHECK_UNRELIABLE

        koan_root = _get_koan_root(instance_dir)
        quota_result = handle_quota_exhaustion(
            koan_root=koan_root,
            instance_dir=instance_dir,
            project_name=project_name,
            run_count=run_num,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
        )
        if quota_result is QUOTA_CHECK_UNRELIABLE:
            log(f"⚠️  Quota check unreliable for {project_name} — "
                "could not read log files, skipping quota detection")
            tracker.record("quota_check", "warning", "unreliable — log files unreadable")
        elif quota_result is not None:
            result["quota_exhausted"] = True
            result["quota_info"] = quota_result
            tracker.record("quota_check", "success", "quota exhausted — early return")
            # Record session outcome BEFORE early return so the session tracker
            # doesn't lose visibility on quota-limited sessions (which biases
            # staleness calculations toward "stale" for productive projects).
            pending_content = _read_pending_content(instance_dir)
            if not pending_content.strip():
                pending_content = _read_stdout_summary(stdout_file)
            _record_session_outcome(
                instance_dir, project_name, autonomous_mode,
                duration_minutes, pending_content,
                mission_title=mission_title,
            )
            # Fire post_mission hooks before early return so hooks see quota events
            _fire_post_mission_hook(
                instance_dir, project_name, project_path,
                exit_code, mission_title, duration_minutes, result,
            )
            result["pipeline_steps"] = tracker.to_dict()
            _write_pipeline_summary(instance_dir, project_name, tracker, mission_title)
            return result  # Early return — no further processing on quota exhaustion
        tracker.record("quota_check", "success", "no exhaustion")

        # 4. Archive pending.md if agent didn't clean up
        _report("archiving journal")
        # Read pending content before archival for session outcome tracking.
        # When the agent follows Mission Completion Checklist, it deletes
        # pending.md before exiting — so we fall back to stdout content.
        pending_content = _read_pending_content(instance_dir)
        if not pending_content.strip():
            pending_content = _read_stdout_summary(stdout_file)
        result["pending_archived"] = archive_pending(instance_dir, project_name, run_num)
        tracker.record("journal_archive", "success" if result["pending_archived"] else "skipped",
                        "archived" if result["pending_archived"] else "nothing to archive")

        # 5. Post-mission processing (only on success)
        if exit_code == 0:
            verify_result = None
            quality_report = {}
            lint_result = None

            # Mission verification (RARV Verify phase — semantic checks)
            _report("verifying mission output")
            verify_result = tracker.run_step(
                "verification",
                _run_mission_verification,
                project_path, mission_title, exit_code, instance_dir,
                pipeline_expired=_pipeline_expired,
            )
            if verify_result is not None:
                if not verify_result.passed:
                    tracker.record("verification", "fail",
                                   verify_result.summary or "verification failed")
                result["verification"] = {
                    "passed": verify_result.passed,
                    "summary": verify_result.summary,
                    "warnings": len(verify_result.warnings),
                    "failures": len(verify_result.failures),
                }

            # Quality pipeline (scan, tests, branch hygiene, PR enrichment)
            _report("running quality pipeline")
            quality_report = tracker.run_step(
                "quality_pipeline",
                _run_quality_pipeline,
                instance_dir, project_name, project_path, _report,
                pipeline_expired=_pipeline_expired,
            )
            if quality_report is None:
                quality_report = {}
            result["quality"] = quality_report

            # Lint gate
            _report("running lint gate")
            lint_result = tracker.run_step(
                "lint_gate",
                _run_lint_gate,
                instance_dir, project_name, project_path,
                pipeline_expired=_pipeline_expired,
            )
            if lint_result is not None:
                result["lint_passed"] = lint_result.passed

            # Reflection
            _report("running reflection")
            reflection_result = tracker.run_step(
                "reflection",
                trigger_reflection,
                instance_dir,
                mission_title if mission_title else f"Autonomous {autonomous_mode} on {project_name}",
                duration_minutes,
                project_name=project_name,
                pipeline_expired=_pipeline_expired,
            )
            result["reflection_written"] = bool(reflection_result)

            # Auto-merge check (respects quality gate + lint gate + verification)
            _report("checking auto-merge")
            lint_blocking = lint_result is not None and not lint_result.passed and _is_lint_blocking(instance_dir, project_name)
            verify_blocking = verify_result is not None and not verify_result.passed
            merge_result = tracker.run_step(
                "auto_merge",
                check_auto_merge,
                instance_dir, project_name, project_path,
                quality_report=quality_report,
                lint_blocked=lint_blocking,
                verify_blocked=verify_blocking,
                pipeline_expired=_pipeline_expired,
            )
            result["auto_merge_branch"] = merge_result
        else:
            # Non-zero exit — skip success-only steps
            for step in ("verification", "quality_pipeline", "lint_gate", "reflection", "auto_merge"):
                tracker.record(step, "skipped", "non-zero exit code")

        # 7. Record session outcome for staleness tracking
        # Always runs — even after deadline — since it's a fast local write.
        _report("recording session outcome")
        _record_session_outcome(
            instance_dir, project_name, autonomous_mode,
            duration_minutes, pending_content,
            mission_title=mission_title,
        )
        tracker.record("session_outcome", "success")

        # 8. Fire post-mission hooks
        if not _pipeline_expired.is_set():
            _report("running hooks")
            hook_failures = _fire_post_mission_hook(
                instance_dir, project_name, project_path,
                exit_code, mission_title, duration_minutes, result,
            )
            if hook_failures:
                failed_names = ", ".join(sorted(hook_failures))
                tracker.record("hooks", "fail", f"failed: {failed_names}")
            else:
                tracker.record("hooks", "success")
        else:
            tracker.record("hooks", "timeout", "pipeline deadline exceeded")

        # Write pipeline summary to journal and include in result
        result["pipeline_steps"] = tracker.to_dict()
        _write_pipeline_summary(instance_dir, project_name, tracker, mission_title)

        # Notify user of pipeline failures via outbox (retried by bridge)
        _notify_pipeline_failures(tracker, mission_title, instance_dir)

        return result
    finally:
        _deadline_timer.cancel()


def commit_instance(instance_dir: str, message: str = "") -> bool:
    """Commit and push instance directory changes.

    Args:
        instance_dir: Path to instance directory.
        message: Custom commit message.  Falls back to timestamped default.

    Returns:
        True if a commit was created.
    """
    try:
        from app.git_sync import run_git

        run_git(instance_dir, "add", "-A")

        # Check if there are staged changes
        status = run_git(instance_dir, "diff", "--cached", "--name-only")
        if not status:
            return False  # No changes

        if not message:
            message = f"koan: {datetime.now().strftime('%Y-%m-%d-%H:%M')}"
        run_git(instance_dir, "commit", "-m", message)

        # Push to the current branch — skip if HEAD is detached
        branch = run_git(instance_dir, "rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":
            print("[commit_instance] Skipping push: detached HEAD", file=sys.stderr)
            return True
        run_git(instance_dir, "push", "origin", branch)
        return True
    except Exception as e:
        print(f"[commit_instance] Instance commit failed: {e}", file=sys.stderr)
        return False


# --- CLI interface ---

def _cli_build_command(args: list) -> None:
    """CLI: python -m app.mission_runner build-command ..."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--autonomous-mode", default="implement")
    parser.add_argument("--extra-flags", default="")
    parsed = parser.parse_args(args)

    cmd = build_mission_command(
        prompt=parsed.prompt,
        autonomous_mode=parsed.autonomous_mode,
        extra_flags=parsed.extra_flags,
    )
    # Output as space-separated for bash consumption
    # (prompt will be handled separately via file)
    print("\n".join(cmd))


def _cli_parse_output(args: list) -> None:
    """CLI: python -m app.mission_runner parse-output <json_file>"""
    if len(args) < 1:
        print("Usage: mission_runner.py parse-output <json_file>", file=sys.stderr)
        sys.exit(1)

    filepath = args[0]
    try:
        raw = Path(filepath).read_text()
    except OSError as e:
        print(f"Error reading {filepath}: {e}", file=sys.stderr)
        sys.exit(1)

    text = parse_claude_output(raw)
    if text:
        print(text)


def _cli_post_mission(args: list) -> None:
    """CLI: python -m app.mission_runner post-mission ..."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--run-num", type=int, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--stdout-file", required=True)
    parser.add_argument("--stderr-file", required=True)
    parser.add_argument("--mission-title", default="")
    parser.add_argument("--autonomous-mode", default="")
    parser.add_argument("--start-time", type=int, default=0)
    parsed = parser.parse_args(args)

    result = run_post_mission(
        instance_dir=parsed.instance,
        project_name=parsed.project_name,
        project_path=parsed.project_path,
        run_num=parsed.run_num,
        exit_code=parsed.exit_code,
        stdout_file=parsed.stdout_file,
        stderr_file=parsed.stderr_file,
        mission_title=parsed.mission_title,
        autonomous_mode=parsed.autonomous_mode,
        start_time=parsed.start_time,
    )

    # Output key results for bash consumption
    if result["quota_exhausted"] and result["quota_info"]:
        reset_display, resume_msg = result["quota_info"]
        print(f"QUOTA_EXHAUSTED|{reset_display}|{resume_msg}")
        sys.exit(2)  # Special exit code for quota exhaustion

    if result["pending_archived"]:
        print("PENDING_ARCHIVED", file=sys.stderr)
    if result["auto_merge_branch"]:
        print(f"AUTO_MERGE|{result['auto_merge_branch']}", file=sys.stderr)

    sys.exit(0 if result["success"] else 1)


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(
            "Usage: mission_runner.py <build-command|parse-output|post-mission> [args]",
            file=sys.stderr,
        )
        sys.exit(1)

    subcommand = sys.argv[1]
    remaining = sys.argv[2:]

    if subcommand == "build-command":
        _cli_build_command(remaining)
    elif subcommand == "parse-output":
        _cli_parse_output(remaining)
    elif subcommand == "post-mission":
        _cli_post_mission(remaining)
    else:
        print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
