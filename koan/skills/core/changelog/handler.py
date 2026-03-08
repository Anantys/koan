"""Koan changelog skill — generate release notes from commits and journals."""

import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Conventional commit prefix to Keep a Changelog section mapping
_SECTION_MAP = {
    "feat": "Added",
    "fix": "Fixed",
    "docs": "Documentation",
    "perf": "Performance",
    "refactor": "Changed",
    "style": "Changed",
    "test": "Other",
    "ci": "Other",
    "build": "Other",
    "chore": "Other",
    "revert": "Removed",
}

# Keyword fallback patterns (when no conventional prefix)
_KEYWORD_PATTERNS = [
    ("Added", re.compile(r"\badd\b|\bimplement\b|\bnew\b|\bsupport\b|\bcreate\b", re.IGNORECASE)),
    ("Fixed", re.compile(r"\bfix\b|\bbug\b|\bpatch\b|\bresolve\b|\bhotfix\b", re.IGNORECASE)),
    ("Changed", re.compile(r"\brefactor\b|\bupdate\b|\bimprove\b|\benhance\b|\brename\b|\bmove\b", re.IGNORECASE)),
    ("Removed", re.compile(r"\bremov\w+\b|\bdelet\w+\b|\bdrop\b|\bdeprecate\b", re.IGNORECASE)),
    ("Performance", re.compile(r"\bperf\w*\b|\boptimi\w+\b|\bspeed\b|\bfast\w*\b|\bcache\b", re.IGNORECASE)),
    ("Documentation", re.compile(r"\bdoc\w*\b|\breadme\b|\bchangelog\b", re.IGNORECASE)),
]

# Conventional commit regex: type(scope)!: description
_CONVENTIONAL_RE = re.compile(r"^(\w+)(?:\([^)]*\))?[!]?:\s+(.+)$")

# Ordered sections per Keep a Changelog
_SECTION_ORDER = ["Added", "Changed", "Fixed", "Removed", "Performance", "Documentation", "Other"]

# Section icons for Telegram output
_SECTION_ICONS = {
    "Added": "+",
    "Changed": "~",
    "Fixed": "!",
    "Removed": "-",
    "Performance": "^",
    "Documentation": "?",
    "Other": ".",
}


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def handle(ctx):
    """Generate changelog from recent commits and journal entries."""
    args = ctx.args.strip() if ctx.args else ""
    project, since_date, output_format = _parse_args(args)

    # Resolve project path
    project_path = _resolve_project(ctx, project)
    if project_path is None:
        return f"Project '{project}' not found." if project else "No project specified and none could be resolved."

    project_name = project or Path(project_path).name

    # Collect commits
    commits = _get_commits(project_path, since_date)
    if not commits:
        since_str = since_date.strftime("%Y-%m-%d")
        return f"No commits found for {project_name} since {since_str}."

    # Categorize commits into sections
    sections = _categorize_commits(commits)

    # Collect journal context
    journal_entries = _get_journal_entries(ctx.instance_dir, project_name, since_date)

    # Format output
    if output_format == "telegram":
        return _format_telegram(project_name, since_date, sections, journal_entries)
    return _format_markdown(project_name, since_date, sections, journal_entries)


def _parse_args(args: str) -> Tuple[str, datetime, str]:
    """Parse command arguments.

    Returns:
        (project_name, since_date, format)
    """
    project = ""
    since_date = datetime.now() - timedelta(days=7)
    output_format = "telegram"

    if not args:
        return project, since_date, output_format

    parts = args.split()
    for part in parts:
        if part.startswith("--since="):
            date_str = part[len("--since="):]
            try:
                since_date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                pass
        elif part.startswith("--format="):
            fmt = part[len("--format="):]
            if fmt in ("md", "markdown"):
                output_format = "md"
            else:
                output_format = "telegram"
        else:
            if not project:
                project = part

    return project, since_date, output_format


def _resolve_project(ctx, project_name: str) -> Optional[str]:
    """Resolve project to a filesystem path."""
    from app.utils import get_known_projects

    projects = get_known_projects()
    if not projects:
        return None

    if project_name:
        # Find matching project (case-insensitive)
        for name, path in projects:
            if name.lower() == project_name.lower():
                return path
        return None

    # No project specified — use first project if only one
    if len(projects) == 1:
        return projects[0][1]

    return None


def _get_commits(project_path: str, since: datetime) -> List[Tuple[str, str]]:
    """Get commits from git log since a date.

    Returns:
        List of (hash, message) tuples.
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--no-merges", f"--since={since_str}"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if result.returncode != 0:
        return []

    commits = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: <hash> <message>
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append((parts[0], parts[1]))
    return commits


def _categorize_commits(
    commits: List[Tuple[str, str]],
) -> Dict[str, List[Tuple[str, str]]]:
    """Categorize commits into Keep a Changelog sections.

    Returns:
        Dict mapping section name to list of (hash, description) tuples.
    """
    sections: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    for commit_hash, message in commits:
        section, description = _classify_commit(message)
        sections[section].append((commit_hash, description))

    return dict(sections)


def _classify_commit(message: str) -> Tuple[str, str]:
    """Classify a single commit message.

    Returns:
        (section_name, cleaned_description)
    """
    # Try conventional commit prefix
    match = _CONVENTIONAL_RE.match(message)
    if match:
        prefix = match.group(1).lower()
        description = match.group(2)
        section = _SECTION_MAP.get(prefix, "Other")
        return section, description

    # Fall back to keyword matching
    for section, pattern in _KEYWORD_PATTERNS:
        if pattern.search(message):
            return section, message

    return "Other", message


def _get_journal_entries(
    instance_dir: Path, project_name: str, since: datetime
) -> List[str]:
    """Collect journal entries for context enrichment."""
    journal_dir = instance_dir / "journal"
    if not journal_dir.exists():
        return []

    entries = []
    current = since.date()
    today = datetime.now().date()

    while current <= today:
        date_dir = journal_dir / current.strftime("%Y-%m-%d")
        if date_dir.is_dir():
            # Look for project-specific journal
            project_journal = date_dir / f"{project_name}.md"
            if project_journal.exists():
                try:
                    content = project_journal.read_text().strip()
                    if content:
                        # Extract key lines (skip headers, keep substance)
                        for line in content.splitlines():
                            line = line.strip()
                            if line and not line.startswith("#") and len(line) > 10:
                                entries.append(line)
                except OSError:
                    pass
        current += timedelta(days=1)

    return entries


def _format_markdown(
    project: str,
    since: datetime,
    sections: Dict[str, List[Tuple[str, str]]],
    journal_entries: List[str],
) -> str:
    """Format changelog in Keep a Changelog markdown format."""
    today = datetime.now().strftime("%Y-%m-%d")
    since_str = since.strftime("%Y-%m-%d")

    lines = [
        f"# Changelog — {project}",
        "",
        f"## [{today}] (since {since_str})",
        "",
    ]

    for section in _SECTION_ORDER:
        items = sections.get(section)
        if not items:
            continue
        lines.append(f"### {section}")
        lines.append("")
        for commit_hash, description in items:
            lines.append(f"- {description} ({commit_hash})")
        lines.append("")

    if journal_entries:
        lines.append("### Context (from journal)")
        lines.append("")
        for entry in journal_entries[:10]:
            lines.append(f"- {_truncate(entry, 120)}")
        lines.append("")

    total = sum(len(items) for items in sections.values())
    lines.append(f"*{total} commits across {len(sections)} categories*")

    return "\n".join(lines)


def _format_telegram(
    project: str,
    since: datetime,
    sections: Dict[str, List[Tuple[str, str]]],
    journal_entries: List[str],
) -> str:
    """Format changelog for Telegram (compact, readable)."""
    since_str = since.strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    total = sum(len(items) for items in sections.values())
    lines = [f"Changelog: {project} ({since_str} to {today})", f"{total} commits", ""]

    for section in _SECTION_ORDER:
        items = sections.get(section)
        if not items:
            continue
        icon = _SECTION_ICONS.get(section, ".")
        lines.append(f"{section} ({len(items)}):")
        for _, description in items[:5]:
            lines.append(f"  {icon} {_truncate(description, 80)}")
        if len(items) > 5:
            lines.append(f"  ... and {len(items) - 5} more")
        lines.append("")

    if journal_entries:
        lines.append("Context:")
        for entry in journal_entries[:5]:
            lines.append(f"  {_truncate(entry, 80)}")

    return "\n".join(lines)
