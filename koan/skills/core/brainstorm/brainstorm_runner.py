"""
Koan -- Brainstorm runner.

Decomposes a broad topic into multiple GitHub issues grouped under a
master tracking issue. Uses Claude CLI to analyze the codebase and
produce structured sub-issue decomposition.

CLI:
    python3 -m skills.core.brainstorm.brainstorm_runner \
        --project-path <path> --topic "Improve caching strategy"
    python3 -m skills.core.brainstorm.brainstorm_runner \
        --project-path <path> --topic "Improve caching" --tag prompt-caching
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

from app.github import run_gh, issue_create, issue_edit
from app.prompts import load_prompt_or_skill


def run_brainstorm(
    project_path: str,
    topic: str,
    tag: Optional[str] = None,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute the brainstorm pipeline.

    1. Generate a tag if not provided.
    2. Invoke Claude to decompose the topic into sub-issues (JSON).
    3. Ensure the GitHub label exists.
    4. Create sub-issues on GitHub.
    5. Create a master tracking issue linking all sub-issues.

    Returns:
        (success, summary) tuple.
    """
    if notify_fn is None:
        from app.notify import send_telegram
        notify_fn = send_telegram

    # Generate tag if not provided
    if not tag:
        tag = _generate_tag(topic)
    notify_fn(
        f"\U0001f9e0 Brainstorming: {topic[:100]}"
        f"{'...' if len(topic) > 100 else ''} (tag: {tag})"
    )

    # Get repo info
    owner, repo = _get_repo_info(project_path)
    if not owner or not repo:
        return False, "No GitHub repository found at project path."

    # Decompose via Claude
    try:
        decomposition = _decompose_topic(project_path, topic, skill_dir)
    except Exception as e:
        return False, f"Decomposition failed: {str(e)[:300]}"

    if not decomposition:
        return False, "Claude returned empty decomposition."

    # Parse the JSON output
    try:
        data = _parse_decomposition(decomposition)
    except ValueError as e:
        return False, f"Failed to parse decomposition: {e}"

    master_summary = data["master_summary"]
    issues = data["issues"]

    # Ensure label exists
    _ensure_label(tag, project_path)

    # Create sub-issues
    created_issues = []
    for i, issue in enumerate(issues, 1):
        try:
            url = issue_create(
                issue["title"],
                issue["body"],
                labels=[tag],
                cwd=project_path,
            )
            # Extract issue number from URL
            number = url.strip().rstrip("/").split("/")[-1]
            created_issues.append((number, issue["title"], url.strip()))
            notify_fn(f"  \u2705 #{number}: {issue['title'][:60]}")
        except (RuntimeError, OSError) as e:
            # Retry without label if label creation failed silently
            try:
                url = issue_create(
                    issue["title"], issue["body"], cwd=project_path,
                )
                number = url.strip().rstrip("/").split("/")[-1]
                created_issues.append((number, issue["title"], url.strip()))
                notify_fn(f"  \u2705 #{number}: {issue['title'][:60]} (no label)")
            except (RuntimeError, OSError) as e2:
                notify_fn(f"  \u274c Failed to create issue {i}: {e2}")

    if not created_issues:
        return False, "No issues were created."

    # Replace SUB-N placeholders in issue bodies with real GitHub numbers
    _replace_sub_placeholders(created_issues, issues, project_path)

    # Build master issue
    master_title = f"[{tag}] {_extract_master_title(topic)}"
    master_body = _build_master_body(
        topic, master_summary, created_issues, owner, repo
    )

    try:
        master_url = issue_create(
            master_title, master_body, labels=[tag], cwd=project_path,
        )
    except (RuntimeError, OSError):
        try:
            master_url = issue_create(
                master_title, master_body, cwd=project_path,
            )
        except (RuntimeError, OSError) as e:
            return True, (
                f"Created {len(created_issues)} sub-issues but "
                f"master issue failed: {e}"
            )

    master_url = master_url.strip()
    summary = (
        f"Created {len(created_issues)} sub-issues + master issue: {master_url}"
    )
    notify_fn(f"\U0001f3af {summary}")
    return True, summary


def _replace_sub_placeholders(created_issues, original_issues, project_path):
    """Replace SUB-N placeholders in created issue bodies with real #numbers.

    After all sub-issues are created on GitHub, we know each ordinal position's
    real issue number. This function patches each issue body to replace
    ``SUB-1``, ``SUB-2``, etc. with ``#42``, ``#43``, etc.
    """
    # Build ordinal → real number mapping
    ordinal_to_number = {}
    for idx, (number, _title, _url) in enumerate(created_issues, 1):
        ordinal_to_number[idx] = number

    for idx, (number, _title, _url) in enumerate(created_issues, 1):
        body = original_issues[idx - 1]["body"]
        updated = _apply_sub_replacements(body, ordinal_to_number)
        if updated != body:
            try:
                issue_edit(number, updated, cwd=project_path)
            except (RuntimeError, OSError) as e:
                print(
                    f"[brainstorm_runner] Failed to update issue #{number}: {e}",
                    file=sys.stderr,
                )


def _apply_sub_replacements(text, ordinal_to_number):
    """Replace all SUB-N placeholders in *text* with #<real_number>."""
    def _replace(match):
        idx = int(match.group(1))
        real = ordinal_to_number.get(idx)
        if real is not None:
            return f"#{real}"
        return match.group(0)  # leave unknown placeholders as-is

    return re.sub(r'SUB-(\d+)', _replace, text)


def _generate_tag(topic: str) -> str:
    """Generate a kebab-case tag from the topic description."""
    # Extract meaningful words, skip filler
    stop_words = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "to", "of", "in",
        "for", "on", "with", "at", "by", "from", "as", "into", "about",
        "and", "but", "or", "not", "no", "so", "if", "then", "that",
        "this", "it", "its", "we", "our", "i", "my", "me", "you",
        "your", "they", "them", "their", "let", "need", "want", "how",
        "what", "why", "when", "where", "which", "who",
    }
    words = re.findall(r'\b[a-zA-Z]{2,}\b', topic.lower())
    keywords = [w for w in words if w not in stop_words][:4]
    if not keywords:
        keywords = ["brainstorm"]
    return "-".join(keywords)


def _decompose_topic(project_path, topic, skill_dir=None):
    """Run Claude to decompose the topic into sub-issues."""
    prompt = load_prompt_or_skill(skill_dir, "decompose", TOPIC=topic)

    from app.cli_provider import run_command_streaming
    from app.config import get_skill_timeout
    output = run_command_streaming(
        prompt, project_path,
        allowed_tools=["Read", "Glob", "Grep", "WebFetch"],
        max_turns=25, timeout=get_skill_timeout(),
    )
    return output


def _parse_decomposition(raw_output: str) -> dict:
    """Parse Claude's JSON output into structured data.

    Handles common issues: markdown fences, preamble text before JSON.
    """
    if not raw_output:
        raise ValueError("Empty output")

    text = raw_output.strip()

    # Strip markdown code fences if present
    text = re.sub(r'^```(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?```\s*$', '', text)

    # Try to find JSON object in the output
    # Claude sometimes adds preamble text before the JSON
    json_match = re.search(r'\{[\s\S]*\}', text)
    if not json_match:
        raise ValueError("No JSON object found in output")

    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    # Validate structure
    if "issues" not in data:
        raise ValueError("Missing 'issues' key in decomposition")
    if not isinstance(data["issues"], list):
        raise ValueError("'issues' must be a list")
    if len(data["issues"]) < 1:
        raise ValueError("At least 1 issue required")

    # Validate each issue has title and body
    for i, issue in enumerate(data["issues"]):
        if "title" not in issue or "body" not in issue:
            raise ValueError(f"Issue {i+1} missing 'title' or 'body'")

    if "master_summary" not in data:
        data["master_summary"] = ""

    return data


def _ensure_label(tag, project_path):
    """Create the GitHub label if it doesn't exist."""
    try:
        run_gh(
            "label", "create", tag,
            "--description", f"Brainstorm: {tag}",
            "--force",
            cwd=project_path, timeout=15,
        )
    except (RuntimeError, OSError):
        # Label creation failed — issues will be created without it
        pass


def _extract_master_title(topic: str) -> str:
    """Extract a concise title from the topic for the master issue."""
    # Take first sentence or first 100 chars
    first_sentence = re.split(r'[.!?]', topic)[0].strip()
    if len(first_sentence) > 100:
        first_sentence = first_sentence[:97] + "..."
    return first_sentence or "Brainstorm"


def _build_master_body(topic, master_summary, created_issues, owner, repo):
    """Build the master tracking issue body."""
    parts = []

    # Original topic
    parts.append("## Problem Statement\n")
    parts.append(topic)
    parts.append("")

    # Summary
    if master_summary:
        parts.append("## Summary\n")
        parts.append(master_summary)
        parts.append("")

    # Task list with links to sub-issues
    parts.append("## Sub-Issues\n")
    for number, title, _url in created_issues:
        parts.append(f"- [ ] #{number} — {title}")
    parts.append("")

    # Footer
    parts.append("---")
    parts.append(
        f"*Created by Koan /brainstorm — "
        f"{len(created_issues)} sub-issues*"
    )

    return "\n".join(parts)


def _get_repo_info(project_path):
    """Get GitHub owner/repo from a local git repo."""
    try:
        output = run_gh(
            "repo", "view", "--json", "owner,name",
            cwd=project_path, timeout=15,
        )
        data = json.loads(output)
        owner = data.get("owner", {}).get("login", "")
        repo = data.get("name", "")
        if owner and repo:
            return owner, repo
    except Exception as e:
        print(
            f"[brainstorm_runner] Repo info fetch failed: {e}",
            file=sys.stderr,
        )
    return None, None


# ---------------------------------------------------------------------------
# CLI entry point -- python3 -m skills.core.brainstorm.brainstorm_runner
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for brainstorm_runner."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Decompose a topic into linked GitHub issues."
    )
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    parser.add_argument(
        "--topic", required=True,
        help="Topic to brainstorm and decompose",
    )
    parser.add_argument(
        "--tag",
        help="GitHub label for grouping issues (auto-generated if omitted)",
    )
    cli_args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parent

    success, summary = run_brainstorm(
        project_path=cli_args.project_path,
        topic=cli_args.topic,
        tag=cli_args.tag,
        skill_dir=skill_dir,
    )
    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
