"""Koan /audit skill -- queue a codebase audit mission."""

import re

# Matches limit=N anywhere in the args string
_LIMIT_RE = re.compile(r"\blimit=(\d+)\b", re.IGNORECASE)

DEFAULT_MAX_ISSUES = 5


def _extract_limit(text):
    """Extract limit=N from text, return (limit, cleaned_text)."""
    m = _LIMIT_RE.search(text)
    if not m:
        return DEFAULT_MAX_ISSUES, text
    limit = int(m.group(1))
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    # Collapse double spaces left by removal
    cleaned = re.sub(r"  +", " ", cleaned)
    return max(1, limit), cleaned


def handle(ctx):
    """Handle /audit command -- queue a codebase audit mission.

    Usage:
        /audit <project>                          -- audit (top 5 findings)
        /audit <project> <extra context>          -- audit with focus guidance
        /audit <project> <focus> limit=N          -- override max findings
    """
    args = ctx.args.strip()

    if args in ("-h", "--help"):
        return (
            "Usage: /audit <project-name> [extra context] [limit=N]\n\n"
            "Audits a project for optimizations, simplifications, "
            "and potential issues. Creates a GitHub issue for each finding.\n\n"
            f"Default: top {DEFAULT_MAX_ISSUES} most important findings. "
            "Use limit=N to override.\n\n"
            "Examples:\n"
            "  /audit koan\n"
            "  /audit myapp focus on the auth module\n"
            "  /audit webapp look for performance bottlenecks limit=10"
        )

    if not args:
        return (
            "\u274c Usage: /audit <project-name> [extra context] [limit=N]\n"
            "Example: /audit koan focus on error handling"
        )

    # Extract limit=N before splitting
    max_issues, args = _extract_limit(args)

    # First word is project name, rest is extra context
    parts = args.split(None, 1)
    project_name = parts[0]
    extra_context = parts[1] if len(parts) > 1 else ""

    return _queue_audit(ctx, project_name, extra_context, max_issues)


def _queue_audit(ctx, project_name, extra_context, max_issues=DEFAULT_MAX_ISSUES):
    """Queue an audit mission."""
    from app.utils import insert_pending_mission, resolve_project_path

    path = resolve_project_path(project_name)
    if not path:
        from app.utils import get_known_projects

        known = ", ".join(n for n, _ in get_known_projects()) or "none"
        return (
            f"\u274c Unknown project '{project_name}'.\n"
            f"Known projects: {known}"
        )

    suffix = f" {extra_context}" if extra_context else ""
    limit_suffix = f" limit={max_issues}" if max_issues != DEFAULT_MAX_ISSUES else ""
    mission_entry = f"- [project:{project_name}] /audit{suffix}{limit_suffix}"
    missions_path = ctx.instance_dir / "missions.md"
    insert_pending_mission(missions_path, mission_entry)

    context_hint = f" (focus: {extra_context})" if extra_context else ""
    limit_hint = f", limit={max_issues}" if max_issues != DEFAULT_MAX_ISSUES else ""
    return f"\U0001f50e Audit queued for {project_name}{context_hint}{limit_hint}"
