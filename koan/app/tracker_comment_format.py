"""Provider-aware comment formatting for tracker updates."""

from __future__ import annotations

import re
from typing import Dict, List, Optional


_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")
_ORDERED_RE = re.compile(r"^\s*\d+\.\s+")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# GitHub collapsible code blocks (<details>/<summary>) render as literal text on
# Jira, so flatten them: drop the <details> wrappers and turn the summary label
# into a plain "Label:" line above the (always-visible) code.
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.IGNORECASE)
_DETAILS_TAG_RE = re.compile(r"</?details\s*>", re.IGNORECASE)
# GitHub-flavored alert blocks (`> [!WARNING]` etc.) render as literal
# unrendered text on Jira. Detect the EXACT canonical opener (5 kinds only,
# nothing trailing) so ordinary human blockquotes are never mangled.
_ALERT_OPEN_RE = re.compile(
    r"^>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$",
    re.IGNORECASE,
)
_BLOCKQUOTE_PREFIX_RE = re.compile(r"^>\s?")


def _flatten_github_alerts(text: str) -> str:
    """Fold GitHub ``> [!TYPE]`` alert blocks into plain ``TYPE: ...`` text.

    The opener must be exactly ``> [!TYPE]`` (one of the 5 canonical kinds);
    the block body is the contiguous run of ``>``-prefixed lines below it,
    stopping at the next alert opener so back-to-back blocks stay separate.
    The first body line is labelled ``TYPE: <text>`` and the rest follow as
    plain lines. Alert syntax inside a fenced code block is left untouched.
    This is the single definition of the plain-text degradation for a GitHub
    alert on a markdown-less tracker; keep it in sync with the native-alert
    helper once #2301's ``build_alert()`` lands.
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    in_fence = False
    while i < n:
        if lines[i].strip().startswith("```"):
            in_fence = not in_fence
            out.append(lines[i])
            i += 1
            continue
        match = None if in_fence else _ALERT_OPEN_RE.match(lines[i].strip())
        if not match:
            out.append(lines[i])
            i += 1
            continue
        kind = match.group(1).upper()
        body: List[str] = []
        i += 1
        while (
            i < n
            and lines[i].lstrip().startswith(">")
            and not _ALERT_OPEN_RE.match(lines[i].strip())
        ):
            body.append(_BLOCKQUOTE_PREFIX_RE.sub("", lines[i].lstrip()).rstrip())
            i += 1
        content = [line for line in body if line.strip()]
        if content:
            out.append(f"{kind}: {content[0]}")
            out.extend(content[1:])
        else:
            out.append(f"{kind}:")
    return "\n".join(out)


def flatten_github_markdown_for_jira(text: str) -> str:
    """Keep standard Markdown while flattening GitHub-only extensions.

    The result is intentionally still Markdown: Jira's ADF renderer can turn
    headings, lists, code fences, and marks into native nodes.  Only GitHub's
    ``details`` and alert extensions are rewritten because Jira has no
    equivalent representation for them.
    """
    if not text:
        return ""

    flattened = _flatten_github_alerts(text)
    lines: List[str] = []
    in_fence = False
    for raw_line in flattened.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.strip().startswith("```"):
            in_fence = not in_fence
            lines.append(raw_line.rstrip())
            continue
        if in_fence:
            # `/plan` posts code examples verbatim; rewriting details/summary
            # inside a fence corrupts the very content it is meant to convey.
            # Scoped to ``` fences on purpose: markdown_to_adf also treats a
            # 4-space-indented block as code, but guarding that too would leave
            # a genuine <details> wrapper nested under a list item unflattened.
            lines.append(raw_line.rstrip())
            continue
        line = _SUMMARY_RE.sub(lambda m: f"**{m.group(1).strip()}**", raw_line)
        line = _DETAILS_TAG_RE.sub("", line)
        lines.append(line.rstrip())
    return "\n".join(lines)


def _parse_markdown_sections(markdown: str) -> Dict[str, List[str]]:
    """Parse ``##``-style markdown sections into lowercase section keys."""
    sections: Dict[str, List[str]] = {}
    current = "__root__"
    sections[current] = []
    for raw_line in (markdown or "").splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _collect_section_lines(sections: Dict[str, List[str]], *keys: str) -> List[str]:
    lines: List[str] = []
    for key in keys:
        lines.extend(sections.get(key, []))
    return lines


def _lines_to_bullets(lines: List[str]) -> List[str]:
    bullets: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _BULLET_RE.match(stripped):
            bullets.append(_BULLET_RE.sub("", stripped, count=1).strip())
            continue
        if _ORDERED_RE.match(stripped):
            bullets.append(_ORDERED_RE.sub("", stripped, count=1).strip())
    return bullets


def _first_nonempty_line(lines: List[str]) -> str:
    for line in lines:
        if line.strip():
            return line.strip()
    return ""


def _strip_markdown_for_jira(text: str) -> str:
    """Make markdown text human-friendly for Jira plain ADF paragraphs."""
    if not text:
        return ""

    # Degrade GitHub alert blocks before line-by-line stripping so a
    # `> [!WARNING]` opener never survives as literal Jira text.
    text = flatten_github_markdown_for_jira(text)

    out: List[str] = []
    in_fence = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(f"    {line}")
            continue

        # The rich normalizer represents a details summary as bold Markdown;
        # this legacy plain-text path keeps its historical ``Label:`` form.
        line = re.sub(
            r"^(\s*)\*\*(.+?)\*\*\s*$",
            lambda m: f"{m.group(1)}{m.group(2)}:",
            line,
        )
        if not line.strip():
            out.append("")
            continue

        line = _HEADING_RE.sub("", line)
        line = _LINK_RE.sub(r"\1 (\2)", line)
        line = _INLINE_CODE_RE.sub(r"\1", line)
        line = line.replace("**", "").replace("__", "")
        line = _ORDERED_RE.sub("- ", line)
        line = _BULLET_RE.sub("- ", line)
        line = re.sub(r"^\s*---+\s*$", "", line)
        out.append(line)

    # Collapse excessive blank lines, preserve section spacing.
    collapsed: List[str] = []
    blank = 0
    for line in out:
        if line.strip():
            blank = 0
            collapsed.append(line)
            continue
        blank += 1
        if blank <= 1:
            collapsed.append("")
    return "\n".join(collapsed).strip()


def build_pr_comment_success(
    provider: str,
    pr_url: str,
    pr_title: str,
    pr_body: str,
    skill_name: str = "",
    base_branch: Optional[str] = None,
) -> str:
    """Build a mission-completion comment when draft PR creation succeeds."""
    # Jira has no native alert rendering: flatten before section extraction so
    # a `> [!TYPE]` line pulled into the Why/What summary never leaks literally.
    body_for_sections = (
        _flatten_github_alerts(pr_body) if provider == "jira" else pr_body
    )
    sections = _parse_markdown_sections(body_for_sections)
    what_bullets = _lines_to_bullets(
        _collect_section_lines(sections, "summary", "changes"),
    )
    how_bullets = _lines_to_bullets(_collect_section_lines(sections, "how"))
    why_text = _first_nonempty_line(_collect_section_lines(sections, "why"))
    testing_bullets = _lines_to_bullets(_collect_section_lines(sections, "testing"))
    mission = f"/{skill_name}" if skill_name else "(unknown)"
    target_branch = (base_branch or "").strip()

    if provider == "jira":
        lines: List[str] = [
            "Koan update: Draft pull request created.",
            "",
            f"Mission: {mission}",
            f"Pull request: {pr_url}",
        ]
        if pr_title:
            lines.append(f"PR title: {pr_title}")
        if target_branch:
            lines.append(f"Target branch: {target_branch}")
        if what_bullets:
            lines.extend(["", "What changed:"])
            lines.extend(f"- {item}" for item in what_bullets[:8])
        if why_text:
            lines.extend(["", f"Why: {why_text}"])
        if how_bullets:
            lines.extend(["", "How it was implemented:"])
            lines.extend(f"- {item}" for item in how_bullets[:8])
        if testing_bullets:
            lines.extend(["", "Validation:"])
            lines.extend(f"- {item}" for item in testing_bullets[:8])
        lines.extend(["", "Next:", "- Review the draft PR and merge when ready."])
        return "\n".join(lines)

    # GitHub / generic markdown-capable trackers.
    lines = [
        "## Draft PR Created",
        "",
        f"- Mission: `{mission}`",
        f"- PR: {pr_url}",
    ]
    if target_branch:
        lines.append(f"- Target branch: `{target_branch}`")
    if what_bullets:
        lines.extend(["", "### What"])
        lines.extend(f"- {item}" for item in what_bullets[:8])
    if why_text:
        lines.extend(["", "### Why", why_text])
    if how_bullets:
        lines.extend(["", "### How"])
        lines.extend(f"- {item}" for item in how_bullets[:8])
    if testing_bullets:
        lines.extend(["", "### Validation"])
        lines.extend(f"- {item}" for item in testing_bullets[:8])
    return "\n".join(lines)


def build_pr_comment_failure(
    provider: str,
    reason: str,
    branch: str = "",
    base_branch: Optional[str] = None,
    skill_name: str = "",
) -> str:
    """Build a tracker comment when draft PR creation fails."""
    mission = f"/{skill_name}" if skill_name else "(unknown)"
    target_branch = (base_branch or "").strip()
    branch_text = (branch or "").strip()
    reason_text = (reason or "Unknown error").strip()

    if provider == "jira":
        reason_text = _flatten_github_alerts(reason_text).strip()
        lines = [
            "Koan update: Pull request creation failed.",
            "",
            f"Mission: {mission}",
            f"Reason: {reason_text}",
        ]
        if branch_text:
            lines.append(f"Current branch: {branch_text}")
        if target_branch:
            lines.append(f"Target branch: {target_branch}")
        lines.extend(
            [
                "",
                "Next:",
                "- Check branch state and repository permissions.",
                "- Re-run the mission after fixing the blocking issue.",
            ],
        )
        return "\n".join(lines)

    lines = [
        "## PR Creation Failed",
        "",
        f"- Mission: `{mission}`",
        f"- Reason: {reason_text}",
    ]
    if branch_text:
        lines.append(f"- Current branch: `{branch_text}`")
    if target_branch:
        lines.append(f"- Target branch: `{target_branch}`")
    return "\n".join(lines)


def build_plan_comment_success(provider: str, title: str, body: str) -> str:
    """Format the `/plan` iteration comment for a target tracker."""
    if provider == "jira":
        return (
            f"## {title}\n\n"
            f"{body}\n\n"
            "---\n\n"
            "Generated by Koan."
        ).strip()

    from app.pr_footer import build_koan_footer
    return (
        f"## {title}\n\n{body}\n\n---\n"
        f"{build_koan_footer()}"
    )


def build_plan_comment_failure(provider: str, reason: str) -> str:
    """Format a `/plan` failure status comment."""
    reason_text = (reason or "Unknown error").strip()
    if provider == "jira":
        return (
            "Koan plan update failed.\n\n"
            f"Reason: {reason_text}\n\n"
            "Next:\n"
            "- Re-run /plan after resolving the issue above."
        )
    return (
        "## Plan Update Failed\n\n"
        f"- Reason: {reason_text}\n\n"
        "Re-run `/plan` after addressing the issue."
    )


def jira_readable_markdown(text: str) -> str:
    """Expose markdown-to-readable conversion for Jira issue bodies/comments."""
    return _strip_markdown_for_jira(text or "")
