"""
Kōan -- Pull Request rebase workflow.

Rebases a PR branch onto its target branch, analyzing review comments
and applying requested changes via Claude before pushing.

Pipeline:
1. Fetch PR metadata + comments from GitHub
2. Checkout the PR branch locally
3. Rebase onto the upstream target branch (resolving conflicts via Claude if needed)
4. Analyze review comments and apply changes (Claude-powered, if feedback exists)
5. Force-push to the existing branch (never creates a new PR)
6. Run the backend-only private review gate and push any fixes
7. Comment on the PR with a summary
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from app.claude_step import (
    CI_STATUS_BLOCKED_APPROVAL,
    _build_pr_prompt,
    _fetch_branch,
    _fetch_failed_logs,
    _force_push,
    _get_current_branch,
    _get_diffstat,
    _rebase_onto_target,
    _run_git,
    _safe_checkout,
    check_existing_ci,
    has_rebase_in_progress,
    resolve_pr_location,
    run_ci_fix_loop,
    run_claude,
    run_claude_step,
    wait_for_ci,
)
from app import force_push_guard
from app.github_alerts import build_alert
from app.config import (
    get_rebase_include_bot_feedback,
    get_rebase_ci_idle_timeout,
    get_rebase_ci_max_duration,
    get_rebase_conflict_timeout,
    get_rebase_max_conflict_rounds,
    get_rebase_review_idle_timeout,
    get_rebase_review_max_duration,
    get_skill_max_turns,
    get_skill_timeout,
)
from app.git_utils import ordered_remotes as _ordered_remotes
from app.github import run_gh, sanitize_github_comment
from app.prompts import load_prompt, load_prompt_or_skill, load_skill_prompt  # noqa: F401 — safety import
from app.retry import retry_with_backoff
from app.utils import (
    _GITHUB_REMOTE_RE,
    truncate_diff,
    truncate_diff_with_skips,
    truncate_text,
)


def _cli_label(role: str = "mission") -> str:
    """Return a ``"<provider> (<model>)"`` tag for ``[rebase]`` log lines.

    Names the CLI provider that ``build_full_command`` / ``run_claude_step``
    will actually invoke (the configured global provider) plus the model for
    *role*, so the logs stay accurate when the configured CLI is not Claude —
    instead of a hardcoded "Claude" the user sees e.g. ``codex (gpt-5-codex)``.
    ``role`` falls back to the ``mission`` model when unset (matches the
    already-solved check's ``review``→``mission`` fallback). When the model is
    empty (default config = "use provider default"), the parenthetical is
    omitted so the line reads ``claude`` instead of a dangling ``claude ()``.
    """
    from app.config import get_model_config
    from app.provider import get_provider_display

    models = get_model_config()
    model = models.get(role) or models["mission"]
    disp = get_provider_display()
    return f"{disp} ({model})" if model else disp


def _resolve_own_login() -> str:
    """Resolve our own GitHub login (the configured ``github.nickname``).

    This identity is exempt from bot-comment filtering so feedback Kōan left
    on a previous review/rebase iteration is preserved. Returns empty string
    if not configured.
    """
    try:
        from app.utils import load_config
        config = load_config()
        github = config.get("github") or {}
        return str(github.get("nickname", "")).strip()
    except Exception as e:
        print(f"[rebase_pr] could not resolve own login: {e}", file=sys.stderr)
        return ""


def _is_bot_login(login: str, own_login: str = "") -> bool:
    """Return True when *login* is a third-party bot whose comments may be
    filtered from rebase feedback.

    Our own identity (*own_login*, the configured ``github.nickname``) is
    never treated as a bot: comments Kōan authored on a previous review or
    rebase iteration are preserved so a combined review+rebase flow can act
    on its own earlier feedback — even if that identity is a GitHub App whose
    login ends in ``[bot]``.
    """
    normalized = (login or "").strip().lower()
    if not normalized:
        return False
    own = (own_login or "").strip().lower()
    if own and normalized == own:
        return False
    return normalized.endswith("[bot]")


def _extract_issue_comment_author(line: str) -> Optional[str]:
    """Extract ``@author`` from issue-comment formatted lines."""
    if not line.startswith("@") or ": " not in line:
        return None
    return line[1:].split(":", 1)[0].strip()


def _extract_review_author(line: str) -> Optional[str]:
    """Extract ``@author`` from PR review summary formatted lines."""
    match = re.match(r"^@([^\s:(]+)\s+\([^)]*\):\s", line)
    if match:
        return match.group(1)
    return None


def _extract_inline_review_author(line: str) -> Optional[str]:
    """Extract ``@author`` from inline review-comment formatted lines."""
    match = re.match(r"^\[[^\]]+\]\s+@([^:\s]+):\s", line)
    if match:
        return match.group(1)
    return None


def _filter_bot_comment_blocks(
    raw: str,
    author_extractor,
) -> str:
    """Remove bot-authored multi-line comment blocks from formatted text."""
    if not raw:
        return raw

    own_login = _resolve_own_login()
    lines = raw.split("\n")
    filtered: list = []
    skip = False
    for line in lines:
        author = author_extractor(line)
        if author is not None:
            skip = _is_bot_login(author, own_login)
        if not skip:
            filtered.append(line)
    return "\n".join(filtered)


def _filter_bot_issue_comments(raw: str) -> str:
    """Remove bot-authored comments from the issue_comments string.

    Each comment starts with ``@<login>: `` on its own conceptual line.
    Bot comments (rebase summaries, review results) are verbose and push
    human feedback out of the truncation window.
    """
    return _filter_bot_comment_blocks(raw, _extract_issue_comment_author)


def _filter_bot_reviews(raw: str) -> str:
    """Remove bot-authored PR review summaries."""
    return _filter_bot_comment_blocks(raw, _extract_review_author)


def _filter_bot_review_comments(raw: str) -> str:
    """Remove bot-authored inline PR review comments."""
    return _filter_bot_comment_blocks(raw, _extract_inline_review_author)


def _truncate_recent(text: str, max_chars: int) -> str:
    """Truncate text keeping the most recent content (tail).

    For conversation threads, the most recent comments are the most
    relevant — they contain the latest feedback that triggered the
    current rebase.
    """
    if len(text) <= max_chars:
        return text
    return "(earlier comments truncated)...\n" + text[-(max_chars - 40):]


# Ordered from highest to lowest severity.  The review prompt emits exactly
# these three values; user-facing aliases are resolved by parse_severity().
SEVERITY_LEVELS = ("critical", "warning", "suggestion")

# User-friendly aliases → canonical severity name.
_SEVERITY_ALIASES = {
    "critical": "critical",
    "blocking": "critical",
    "warning": "warning",
    "important": "warning",
    "suggestion": "suggestion",
    "suggestions": "suggestion",
    "all": "suggestion",
}


def parse_severity(token: str) -> Optional[str]:
    """Resolve a user-supplied severity token to a canonical level.

    Strips leading dashes (``-``, ``--``, ``—``) so that all of these
    are equivalent: ``critical``, ``-critical``, ``--critical``, ``—critical``.

    Returns the canonical severity name (``"critical"``, ``"warning"``, or
    ``"suggestion"``), or ``None`` if the token is not recognised.
    """
    # lstrip strips individual chars, not substrings — handles any mix of -, —, –
    cleaned = token.lstrip("-\u2014\u2013").strip().lower()
    return _SEVERITY_ALIASES.get(cleaned)


def severity_at_or_above(min_severity: str) -> List[str]:
    """Return the list of severity levels at or above *min_severity*.

    >>> severity_at_or_above("warning")
    ['critical', 'warning']
    """
    try:
        idx = SEVERITY_LEVELS.index(min_severity)
    except ValueError:
        return list(SEVERITY_LEVELS)
    return list(SEVERITY_LEVELS[: idx + 1])


_DIFF_TOO_LARGE_MARKERS = ("HTTP 406", "too_large", "exceeded the maximum")
_REBASE_FEEDBACK_HEARTBEAT_SECONDS = 45
_REBASE_CI_FIX_HEARTBEAT_SECONDS = 45
_REBASE_CI_FIX_TIMEOUT_RETRIES = 1
_REBASE_CI_FIX_TIGHT_RETRY_SUFFIX = (
    "\n\n## Retry Constraints\n"
    "- Keep edits minimal and focused only on failing checks.\n"
    "- Prefer direct file fixes over broad refactors.\n"
    "- Do not spend tokens on long explanations.\n"
    "- Stop after implementing the smallest viable patch."
)


def _diff_too_large(error_message: str) -> bool:
    """Return True if a gh-pr-diff error matches the > 300 files signature."""
    return any(marker in error_message for marker in _DIFF_TOO_LARGE_MARKERS)


def _token_fetch_url(
    owner: str, repo: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Build an authenticated HTTPS fetch URL from ``gh auth token``.

    Returns ``(url, token)``, or ``(None, None)`` when no token is
    available. ``gh`` resolves the token from ``GH_TOKEN`` / ``GITHUB_TOKEN``
    or its keyring; plain ``git`` reads none of those, so the token must be
    embedded in the URL for HTTPS fetches to authenticate.
    """
    try:
        token = run_gh("auth", "token").strip()
    except (RuntimeError, OSError):
        token = ""
    if token:
        return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git", token
    return None, None


def _resolve_fetch_source(
    owner: str, repo: str, project_path: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a git fetch source for ``owner/repo`` from the local checkout.

    Returns ``(source, secret)`` where *source* is a git remote name or URL
    and *secret* is a token that must be redacted from logs (or ``None``).

    Prefers the local remote whose URL matches ``owner/repo`` — its
    credentials already work, since that is how Kōan fetches and pushes
    (SSH key or git credential helper). Only when no matching remote exists
    does it fall back to an authenticated HTTPS URL built from
    ``gh auth token`` (a fresh ``https://github.com/...`` URL has no
    credentials and prompts for a username on private repos).
    """
    remote = _find_remote_for_repo(owner, repo, project_path)
    if remote:
        return remote, None
    return _token_fetch_url(owner, repo)


def _fetch_diff_locally(
    project_path: str,
    owner: str,
    repo: str,
    pr_number: str,
    base_branch: str,
    timeout: int = 180,
) -> str:
    """Fetch a PR diff from the local checkout when GitHub's API caps out.

    Fetches the PR head (``pull/<N>/head``) and the base branch into
    temporary refs, then runs ``git diff base...head``. This bypasses the
    300-file cap on ``gh pr diff`` because git itself has no such limit.

    The fetch source is the local remote matching ``owner/repo`` (whose
    credentials already work); see :func:`_resolve_fetch_source`. If that
    remote fetch fails — e.g. an HTTPS remote with no credential helper,
    which dies with "could not read Username" — it retries once using an
    authenticated ``gh auth token`` URL so the token in the environment is
    actually used.

    Returns the raw diff text on success, or an empty string on any
    failure (network, missing branch, etc.). Temp refs are always cleaned
    up, even on failure.
    """
    head_ref = f"refs/koan-tmp/pr-{pr_number}-head"
    base_ref = f"refs/koan-tmp/pr-{pr_number}-base"

    def _git(args: list, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=project_path,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            **kwargs,
        )

    def _attempt(source: str, secret: Optional[str]) -> Optional[str]:
        """Fetch head + base from *source* and return the diff, or None on failure."""
        def _redact(text: str) -> str:
            return text.replace(secret, "***") if secret else text

        head_fetch = _git(
            ["fetch", "--no-tags", source, f"pull/{pr_number}/head:{head_ref}"],
        )
        if head_fetch.returncode != 0:
            stderr = head_fetch.stderr.decode("utf-8", errors="replace")
            print(
                f"[rebase_pr] local diff fallback: fetch of pull/{pr_number}/head "
                f"failed: {_redact(stderr)[:200]}",
                file=sys.stderr,
            )
            return None

        base_fetch = _git(
            ["fetch", "--no-tags", source, f"{base_branch}:{base_ref}"],
        )
        if base_fetch.returncode != 0:
            stderr = base_fetch.stderr.decode("utf-8", errors="replace")
            print(
                f"[rebase_pr] local diff fallback: fetch of base {base_branch} "
                f"failed: {_redact(stderr)[:200]}",
                file=sys.stderr,
            )
            return None

        diff_result = _git(
            ["diff", f"{base_ref}...{head_ref}"],
            text=True, encoding="utf-8", errors="replace",
        )
        if diff_result.returncode != 0:
            print(
                f"[rebase_pr] local diff fallback: git diff failed: "
                f"{_redact(diff_result.stderr)[:200]}",
                file=sys.stderr,
            )
            return None
        return diff_result.stdout

    source, secret = _resolve_fetch_source(owner, repo, project_path)
    if not source:
        print(
            f"[rebase_pr] local diff fallback: no usable fetch source for "
            f"{owner}/{repo} (no matching remote and no gh token)",
            file=sys.stderr,
        )
        return ""

    try:
        diff = _attempt(source, secret)
        if diff is not None:
            return diff

        # The first source was a plain remote name (secret is None) whose
        # transport failed — e.g. an HTTPS remote with no credential helper.
        # Retry once with an authenticated token URL so GH_TOKEN is used.
        if secret is None:
            token_url, token = _token_fetch_url(owner, repo)
            if token_url:
                print(
                    f"[rebase_pr] local diff fallback: remote fetch failed for "
                    f"{owner}/{repo}; retrying with gh token URL",
                    file=sys.stderr,
                )
                diff = _attempt(token_url, token)
                if diff is not None:
                    return diff
        return ""
    except (subprocess.TimeoutExpired, OSError) as e:
        msg = str(e).replace(secret, "***") if secret else str(e)
        print(
            f"[rebase_pr] local diff fallback errored: {msg}",
            file=sys.stderr,
        )
        return ""
    finally:
        for ref in (head_ref, base_ref):
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                _git(["update-ref", "-d", ref])


def fetch_pr_context(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: Optional[str] = None,
    max_diff_chars: int = 32000,
) -> dict:
    """Fetch PR details, diff, and all comments via gh CLI.

    Returns a dict with keys: title, body, branch, base, state, author, url,
    diff, diff_skipped_files, review_comments, reviews, issue_comments.

    ``max_diff_chars`` caps the fetch-time diff size. The legacy 32K default
    preserves behaviour for rebase/squash/recreate/ci_queue callers; ``/review``
    passes a large budget-derived cap so the diff compressor (its real coverage
    guardrail) receives the whole diff. Any files cut here are reported via the
    returned ``diff_skipped_files`` list.

    When ``project_path`` is provided, oversized-PR diff failures
    (GitHub HTTP 406: > 300 files) trigger a local ``git fetch`` +
    ``git diff`` fallback. Without ``project_path``, the diff is left
    empty and a warning is logged.
    """
    full_repo = f"{owner}/{repo}"

    # Fetch PR metadata
    pr_json = run_gh(
        "pr", "view", pr_number, "--repo", full_repo, "--json",
        "title,body,headRefName,baseRefName,state,author,url,headRepositoryOwner",
    )

    # Parse metadata up front — needed for the local-diff fallback so we
    # know the base branch name before attempting the fetch.
    try:
        metadata = json.loads(pr_json)
    except (json.JSONDecodeError, TypeError):
        metadata = {}

    # Fetch review comment count from REST API for pending review detection.
    # GitHub counts pending (unsubmitted) review comments in PR metadata but
    # the comments endpoints don't return them to other users.
    # Retry once on transient failures — falling back to 0 incorrectly hides
    # pending reviews, causing the bot to miss unsubmitted review feedback.
    def _fetch_review_comment_count() -> int:
        count_json = run_gh(
            "api", f"repos/{full_repo}/pulls/{pr_number}",
            "--jq", ".review_comments",
        )
        return int(count_json.strip()) if count_json.strip() else 0

    try:
        api_review_comment_count = retry_with_backoff(
            _fetch_review_comment_count,
            max_attempts=2,
            backoff=(1,),
            retryable=(RuntimeError, ValueError),
        )
    except (RuntimeError, ValueError):
        api_review_comment_count = 0

    # Fetch PR diff. May fail for very large PRs — GitHub returns HTTP 406
    # when a diff would contain more than 300 changed files. When that
    # happens and we have a local checkout, fall back to ``git diff`` from
    # the local repo, which has no such cap.
    diff = ""
    diff_error = ""
    try:
        diff = run_gh("pr", "diff", pr_number, "--repo", full_repo)
    except RuntimeError as e:
        err_msg = str(e)
        diff_error = err_msg
        too_large = _diff_too_large(err_msg)
        print(
            f"[rebase_pr] PR diff fetch failed for #{pr_number} "
            f"({'oversized — > 300 files' if too_large else 'gh error'}): "
            f"{err_msg[:300]}",
            file=sys.stderr,
        )
        if too_large and project_path:
            base_branch = metadata.get("baseRefName") or "main"
            diff = _fetch_diff_locally(
                project_path, owner, repo, pr_number, base_branch,
            )
            if diff:
                diff_error = ""
                print(
                    f"[rebase_pr] PR #{pr_number} diff fetched locally "
                    f"({len(diff)} chars)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[rebase_pr] PR #{pr_number} local diff fallback "
                    f"produced no output",
                    file=sys.stderr,
                )

    # Fetch review comments (inline code comments)
    try:
        comments_json = run_gh(
            "api", f"repos/{full_repo}/pulls/{pr_number}/comments",
            "--paginate", "--jq",
            r'.[] | "[\(.path):\(.line // .original_line)] @\(.user.login): \(.body)"',
        )
    except RuntimeError:
        comments_json = ""

    # Fetch PR-level review comments (top-level reviews)
    try:
        reviews_json = run_gh(
            "api", f"repos/{full_repo}/pulls/{pr_number}/reviews",
            "--paginate", "--jq",
            r'.[] | select(.body != "") | "@\(.user.login) (\(.state)): \(.body)"',
        )
    except RuntimeError:
        reviews_json = ""

    # Fetch issue-level comments (conversation thread)
    try:
        issue_comments = run_gh(
            "api", f"repos/{full_repo}/issues/{pr_number}/comments",
            "--paginate", "--jq",
            r'.[] | "@\(.user.login): \(.body)"',
        )
    except RuntimeError:
        issue_comments = ""

    # Count inline comments BEFORE bot-filtering: pending-review detection
    # compares against the API's total review-comment count (which includes
    # bot comments), so filtering here would skew it into false positives.
    fetched_comment_count = len(comments_json.strip().splitlines()) if comments_json.strip() else 0

    # By default bot comments are included; when disabled, drop them so noisy
    # CI/bot output does not inflate prompt size and stall the feedback phase.
    if not get_rebase_include_bot_feedback():
        comments_json = _filter_bot_review_comments(comments_json)
        reviews_json = _filter_bot_reviews(reviews_json)
        issue_comments = _filter_bot_issue_comments(issue_comments)

    # Detect pending (unsubmitted) reviews: GitHub counts pending review
    # comments in the PR metadata but the API doesn't return them to other
    # users.  When the count is positive but fetched comments are empty,
    # there are invisible pending reviews.
    has_pending_reviews = api_review_comment_count > 0 and fetched_comment_count == 0

    truncated_diff, diff_skipped_files = truncate_diff_with_skips(diff, max_diff_chars)

    return {
        "title": metadata.get("title", ""),
        "body": metadata.get("body", ""),
        "branch": metadata.get("headRefName", ""),
        "base": metadata.get("baseRefName", "main"),
        "state": metadata.get("state", ""),
        "author": metadata.get("author", {}).get("login", ""),
        "head_owner": metadata.get("headRepositoryOwner", {}).get("login", ""),
        "url": metadata.get("url", ""),
        "diff": truncated_diff,
        "diff_skipped_files": diff_skipped_files,
        "diff_error": truncate_text(diff_error, 1000),
        "review_comments": truncate_text(comments_json, 4000),
        "reviews": truncate_text(reviews_json, 3000),
        "issue_comments": _truncate_recent(issue_comments, 4000),
        "has_pending_reviews": has_pending_reviews,
    }


def _find_remote_for_repo(
    owner: str, repo: str, project_path: str,
) -> Optional[str]:
    """Find the local git remote name that matches a GitHub owner/repo.

    Compares each remote's URL against the target ``owner/repo`` (case-insensitive).
    Returns the remote name (e.g. ``"upstream"``) or ``None`` if no match.
    """
    target = f"{owner}/{repo}".lower()
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, cwd=project_path, timeout=5,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        remote_name, url = parts[0], parts[1]
        match = _GITHUB_REMOTE_RE.search(url)
        if match:
            slug = f"{match.group(1)}/{match.group(2)}".lower()
            if slug == target:
                return remote_name
    return None


def _has_review_feedback(context: dict) -> bool:
    """Check if the PR context contains any review feedback."""
    return bool(
        context.get("review_comments", "").strip()
        or context.get("reviews", "").strip()
        or context.get("issue_comments", "").strip()
    )


def build_comment_summary(context: dict) -> str:
    """Build a human-readable summary of all PR feedback.

    Useful for understanding what reviewers asked for before rebasing.
    """
    parts = []

    if context.get("reviews"):
        parts.append("### Reviews\n" + context["reviews"])
    if context.get("review_comments"):
        parts.append("### Inline Comments\n" + context["review_comments"])
    if context.get("issue_comments"):
        parts.append("### Discussion\n" + context["issue_comments"])

    if not parts:
        return "No comments or reviews found on this PR."

    return "\n\n".join(parts)


# Whether a plain `/rebase` (no --fix) addresses PR review feedback by default.
# History: `/rebase` used to always rebase AND apply review feedback. The feedback
# leg now lives behind an explicit `--fix` (or any trailing text after the URL,
# resolved by `skill_dispatch._build_rebase_cmd`). A bare `/rebase` is a pure
# rebase. Callers that need feedback pass `fix=True`. During the transition
# window a notice explains the change on the bare-rebase path
# (see `rebase_transition`).
_FEEDBACK_ON_BY_DEFAULT = False


def run_rebase(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    min_severity: Optional[str] = None,
    fix: bool = False,
    feedback_context: str = "",
) -> Tuple[bool, str]:
    """Run the rebase pipeline and emit a single outcome line.

    Intermediate progress is gated behind messaging.level=debug (via the
    progress_notify default); the success/failure outcome always reaches chat.

    ``fix`` opts into the review-feedback leg (step 4). See ``_run_rebase_impl``.
    """
    if notify_fn is None:
        from app.messaging_level import progress_notify
        notify_fn = progress_notify(log_category="rebase")

    outcome_meta: Dict[str, str] = {}
    success, summary = _run_rebase_impl(
        owner, repo, pr_number, project_path,
        notify_fn=notify_fn, skill_dir=skill_dir, min_severity=min_severity,
        fix=fix, feedback_context=feedback_context, outcome_meta=outcome_meta,
    )

    from app.messaging_level import notify_outcome
    pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    if success:
        # A successful rebase that could NOT apply the reviewer's feedback is a
        # partial success — surface it as a warning that always reaches chat
        # (notify_outcome is not messaging.level-gated), so the dropped feedback
        # is not mistaken for "done". The bare ✅ here is exactly why these were
        # easy to miss.
        feedback_unapplied = outcome_meta.get("feedback_unapplied")
        if feedback_unapplied:
            notify_outcome(
                f"⚠️ Rebased {pr_url} — review feedback NOT applied "
                f"({feedback_unapplied}); the reviewer comments still need "
                "attention.",
                notify_fn,
            )
        else:
            notify_outcome(f"✅ Rebased {pr_url}", notify_fn)
    else:
        notify_outcome(f"❌ Rebase failed {pr_url}: {summary}", notify_fn)
    return success, summary


def _run_rebase_impl(
    owner: str,
    repo: str,
    pr_number: str,
    project_path: str,
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    min_severity: Optional[str] = None,
    fix: bool = False,
    feedback_context: str = "",
    outcome_meta: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """Execute the rebase pipeline for a pull request.

    Steps:
        1. Fetch PR context from GitHub (metadata + all comments)
        2. Checkout the PR branch locally
        3. Rebase onto the upstream target branch
        4. Analyze review comments and apply changes (only when the feedback
           leg is enabled — ``fix`` or the default — and feedback exists)
        5. Check existing CI — fix failures before pushing
        6. Force-push to the existing branch (always recycles the PR)
        7. Run the backend-only private review gate and push any fixes
        8. Comment on the PR with a summary

    Args:
        owner: GitHub owner (e.g., "owner")
        repo: GitHub repo name (e.g., "koan")
        pr_number: PR number as string
        project_path: Local path to the project
        notify_fn: Optional callback for progress notifications.
        skill_dir: Path to the rebase skill directory for prompt resolution.
        fix: Address PR review feedback after rebasing. When False, the
            feedback leg runs only if ``_FEEDBACK_ON_BY_DEFAULT`` is set.

    Returns:
        (success, summary) tuple.
    """
    actions_log: List[str] = []

    # The review-feedback leg (step 4) is opt-in: an explicit --fix, or the
    # transitional default. A bare rebase (fix=False, default off) only rebases.
    apply_feedback = fix or _FEEDBACK_ON_BY_DEFAULT
    # Show the transition notice on the bare-rebase path while the window is open,
    # so the changed default (feedback skipped) is not a silent surprise.
    from app import rebase_transition
    show_transition_notice = (not apply_feedback) and rebase_transition.notice_active()

    # ── Step 0: Resolve actual PR location (cross-owner support) ──────
    print(f"[rebase] Resolving PR #{pr_number} location", flush=True)
    try:
        owner, repo = resolve_pr_location(owner, repo, pr_number, project_path)
    except RuntimeError as e:
        return False, str(e)

    full_repo = f"{owner}/{repo}"

    # ── Step 1: Fetch PR context ──────────────────────────────────────
    print(f"[rebase] Fetching PR #{pr_number} context from {owner}/{repo}", flush=True)
    notify_fn(f"Reading PR #{pr_number}...")
    try:
        context = fetch_pr_context(owner, repo, pr_number, project_path)
    except Exception as e:
        return False, f"Failed to fetch PR context: {e}"

    # Skip if the PR is already merged or closed — nothing to rebase
    pr_state = context.get("state", "").upper()
    if pr_state in ("MERGED", "CLOSED"):
        msg = f"PR #{pr_number} is already {pr_state.lower()} — skipping rebase."
        notify_fn(msg)
        return True, msg

    if not context["branch"]:
        return False, "Could not determine PR branch name."

    # ── Already-solved check ──────────────────────────────────────────
    # Ask Claude whether HEAD already addresses the intent of this PR.
    # Must run before checkout to avoid unnecessary git state mutations.
    print(f"[rebase] Running already-solved check via {_cli_label('review')}", flush=True)
    already_solved, resolved_by = _check_if_already_solved(
        actions_log=actions_log,
        pr_context=context,
        skill_dir=skill_dir,
        project_path=project_path,
    )
    if already_solved:
        _close_pr_as_duplicate(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            resolved_by=resolved_by,
            pr_context=context,
            project_path=project_path,
            notify_fn=notify_fn,
        )
        return False, f"PR #{pr_number} closed — already solved by {resolved_by}"

    # Warn about pending (unsubmitted) reviews we cannot read
    if context.get("has_pending_reviews"):
        notify_fn(
            f"⚠️ PR #{pr_number} has pending (unsubmitted) review comments "
            f"that are invisible to the API. The rebase will proceed but may "
            f"miss some feedback. Consider submitting the pending review on "
            f"GitHub."
        )
        actions_log.append("Warning: pending (unsubmitted) review comments detected")

    branch = context["branch"]
    base = context["base"]

    # Determine which local remote corresponds to the PR's target repo
    # so we rebase against the correct upstream, not a stale fork.
    base_remote = _find_remote_for_repo(owner, repo, project_path)

    # Determine which remote hosts the PR's head branch (the fork)
    head_owner = context.get("head_owner", "")
    head_remote = _find_remote_for_repo(head_owner, repo, project_path) if head_owner else None

    # Detect project commit conventions for convention-aware commit messages
    from app.commit_conventions import get_project_commit_guidance
    commit_conventions = get_project_commit_guidance(
        project_path, f"{base_remote}/{base}",
    )

    # Log comment summary for awareness
    comment_summary = build_comment_summary(context)
    if comment_summary and "No comments" not in comment_summary:
        actions_log.append("Read PR comments and review feedback")

    # ── Step 2: Checkout the PR branch ────────────────────────────────
    print(f"[rebase] Checking out branch `{branch}`", flush=True)
    notify_fn(f"Checking out `{branch}`...")

    # Save current branch to restore later
    original_branch = _get_current_branch(project_path)

    try:
        fetch_remote = _checkout_pr_branch(
            branch, project_path,
            head_remote=head_remote,
            head_owner=context.get("head_owner", ""),
            repo=repo,
        )
    except Exception as e:
        return False, f"Failed to checkout branch `{branch}`: {e}"

    # The PR head everything downstream is based on — the reference point for
    # the post-push content-preservation check (force_push_guard).
    pre_rebase_head = ""
    try:
        pre_rebase_head = _run_git(
            ["git", "rev-parse", "HEAD"], cwd=project_path, timeout=30,
        ).strip()
    except Exception as e:
        print(
            f"[rebase_pr] could not capture pre-rebase head: {e}",
            file=sys.stderr,
        )

    # Use API-discovered head_remote, fall back to checkout's fetch_remote
    effective_head_remote = head_remote or fetch_remote

    # ── Step 3: Rebase onto target branch ─────────────────────────────
    print(f"[rebase] Rebasing `{branch}` onto `{base}`", flush=True)
    notify_fn(f"Rebasing `{branch}` onto `{base}`...")
    rebase_meta: Dict[str, str] = {}
    rebase_remote = _rebase_with_conflict_resolution(
        base, project_path, context, actions_log,
        notify_fn=notify_fn, skill_dir=skill_dir,
        max_conflict_rounds=get_rebase_max_conflict_rounds(),
        preferred_remote=base_remote,
        head_remote=effective_head_remote,
        result_meta=rebase_meta,
    )
    if rebase_remote:
        actions_log.append(f"Rebased `{branch}` onto `{rebase_remote}/{base}`")
    else:
        _safe_checkout(original_branch, project_path)
        error_code = rebase_meta.get("error", "rebase_failed")
        detail = rebase_meta.get("detail", "")
        if error_code == "rebase_failed":
            guidance = _build_rebase_recovery_guidance(project_path)
            return False, (
                "[conflict_unresolved] "
                f"Rebase failed on `{base_remote or '?'}/{base}`. "
                f"Could not resolve conflicts."
                f"{' Cause: ' + detail if detail else ''}\n{guidance}"
            )
        return False, (
            f"[{error_code}] Rebase of `{branch}` onto "
            f"`{base_remote or '?'}/{base}` aborted before any push: {detail}"
        )

    # Save the clean rebased state before optional review-feedback edits.
    # If feedback application stalls, we can safely reset to this point
    # and still push a correct rebase.
    rebase_checkpoint = ""
    try:
        rebase_checkpoint = _run_git(
            ["git", "rev-parse", "HEAD"], cwd=project_path, timeout=30,
        ).strip()
    except Exception as e:
        print(
            f"[rebase_pr] could not capture rebase checkpoint: {e}",
            file=sys.stderr,
        )

    # ── Step 4: Analyze review comments and apply changes ──────────────
    change_summary = ""
    feedback_status = ""
    feedback_reason = ""
    # Run the feedback leg when there is reviewer feedback to address OR the
    # human supplied an explicit request. The invariant in specs/skills/rebase.md
    # requires the post-URL context to thread into the feedback prompt, so an
    # explicit request must not be dropped just because the PR has no comments.
    if apply_feedback and (_has_review_feedback(context) or feedback_context.strip()):
        severity_hint = ""
        if min_severity and min_severity != "suggestion":
            included = severity_at_or_above(min_severity)
            severity_hint = f" (severity filter: {', '.join(included)})"
        print(f"[rebase] Applying review feedback via {_cli_label()}{severity_hint}", flush=True)
        notify_fn(f"Analyzing review comments on `{branch}`{severity_hint}...")
        feedback_meta: Dict[str, str] = {"status": "unknown", "error": ""}
        change_summary = _apply_review_feedback(
            context, pr_number, project_path, actions_log,
            skill_dir=skill_dir,
            commit_conventions=commit_conventions,
            min_severity=min_severity,
            feedback_context=feedback_context,
            result_meta=feedback_meta,
        )
        feedback_status = feedback_meta.get("status", "")
        if feedback_status == "feedback_timeout":
            timeout_error = feedback_meta.get("error", "").strip()
            if _get_current_branch(project_path) != branch:
                _safe_checkout(branch, project_path)

            recovered = False
            if rebase_checkpoint:
                try:
                    _run_git(
                        ["git", "reset", "--hard", rebase_checkpoint],
                        cwd=project_path, timeout=30,
                    )
                    recovered = True
                except Exception as e:
                    print(
                        "[rebase_pr] feedback-timeout recovery reset failed: "
                        f"{e}",
                        file=sys.stderr,
                    )

            if not recovered:
                _safe_checkout(original_branch, project_path)
                guidance = _build_rebase_recovery_guidance(project_path)
                return False, (
                    "[feedback_timeout] Rebase feedback timed out and automatic "
                    "recovery to the clean rebased state failed.\n"
                    f"{guidance}"
                )

            suffix = f" ({timeout_error})" if timeout_error else ""
            actions_log.append(
                "Review feedback timed out; restored clean rebased state and "
                "continuing with rebase-only push"
            )
            notify_fn(
                f"Review feedback timed out on `{branch}`{suffix}; "
                "pushing the clean rebase without feedback edits."
            )
            feedback_reason = "the feedback step timed out"
            if outcome_meta is not None:
                outcome_meta["feedback_unapplied"] = "timed out"
        if feedback_status == "feedback_quota":
            # Provider quota is exhausted — no point pushing a half-applied
            # review, and the loop should back off until quota resets.
            _safe_checkout(original_branch, project_path)
            guidance = _build_rebase_recovery_guidance(project_path)
            return False, (
                "[feedback_quota] Rebase paused while applying review feedback: "
                "provider quota exhausted. Retry /rebase --fix after quota reset.\n"
                f"{guidance}"
            )
        if feedback_status == "feedback_failed":
            # The git rebase itself already succeeded; a transient feedback
            # error should not discard it. Push the rebase as-is and flag that
            # review feedback was not applied so the human can re-run
            # /rebase --fix (a bare /rebase would not re-apply feedback).
            error_detail = feedback_meta.get("error", "").strip()
            suffix = f" ({error_detail})" if error_detail else ""
            actions_log.append(
                f"Review feedback step errored{suffix}; "
                "pushing rebase without feedback changes"
            )
            notify_fn(
                f"Could not apply review feedback on `{branch}`{suffix}; "
                "pushing the rebase without feedback changes."
            )
            feedback_reason = "the feedback step errored"
            if outcome_meta is not None:
                outcome_meta["feedback_unapplied"] = "step errored"
        if feedback_status == "feedback_no_disposition":
            _safe_checkout(original_branch, project_path)
            return False, (
                "[feedback_no_disposition] Review feedback made no changes and "
                "did not provide the required explanation. The rebased branch was "
                "not pushed; retry /rebase --fix after reviewing the feedback."
            )

        # Claude may switch branches during feedback — ensure we're still
        # on the expected branch before pushing.
        current = _get_current_branch(project_path)
        if current != branch:
            actions_log.append(
                f"Note: Claude switched to `{current}`, "
                f"restoring `{branch}`"
            )
            _safe_checkout(branch, project_path)

    # ── Step 5: Pre-push CI check — fix existing failures ──────────────
    print("[rebase] Checking pre-push CI status", flush=True)
    _fix_existing_ci_failures(
        branch=branch,
        base=base,
        full_repo=full_repo,
        pr_number=pr_number,
        project_path=project_path,
        context=context,
        actions_log=actions_log,
        notify_fn=notify_fn,
        skill_dir=skill_dir,
        commit_conventions=commit_conventions,
    )

    # ── Step 6: Push the result ───────────────────────────────────────
    print(f"[rebase] Pushing `{branch}`", flush=True)
    notify_fn(f"Pushing `{branch}`...")
    push_result = _push_with_fallback(
        branch, base, full_repo, pr_number, context, project_path,
        head_remote=effective_head_remote,
    )
    actions_log.extend(push_result["actions"])

    if not push_result["success"]:
        _safe_checkout(original_branch, project_path)
        return False, (
            f"[push_failure] Push failed: {push_result.get('error', 'unknown')}\n\n"
            f"Actions completed:\n" +
            "\n".join(f"- {a}" for a in actions_log)
        )

    # Push observations for the content-preservation guard. The private gate
    # may push again; its push_gate_fix updates this state with its own fresh
    # pre-push observation and pushed SHA so no pipeline push is a blind spot.
    push_state = {
        "remote": push_result.get("remote", ""),
        "pushed_head": push_result.get("pushed_head", ""),
        "observations": [h for h in push_result.get("pre_push_remote_heads", []) if h],
        "observation_failed": bool(push_result.get("observation_failed")),
    }

    # ── Step 7: Private review gate ────────────────────────────────────
    gate_result = _run_rebase_private_review_gate(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        base=base,
        full_repo=full_repo,
        context=context,
        project_path=project_path,
        head_remote=effective_head_remote,
        notify_fn=notify_fn,
        skill_dir=skill_dir,
        actions_log=actions_log,
        push_state=push_state,
    )
    gate_action = _format_rebase_private_gate_action(gate_result)
    if gate_action:
        actions_log.append(gate_action)

    # ── Step 7.5: Force-push content-preservation check ────────────────
    # Runs after the pipeline's last push (gate fixes included) while the PR
    # branch is still checked out. Detects dropped/modified original content,
    # clobbered concurrent pushes, and a post-push remote race. Best-effort:
    # never blocks or reverts — it warns loudly on the PR instead.
    push_guard_section = _run_force_push_guard(
        pre_rebase_head=pre_rebase_head,
        target_ref=f"{rebase_remote}/{base}",
        project_path=project_path,
        push_state=push_state,
        branch=branch,
        pr_number=pr_number,
        actions_log=actions_log,
        notify_fn=notify_fn,
    )

    # ── Step 8: Collect final diffstat ─────────────────────────────────
    diffstat = _get_diffstat(f"{rebase_remote}/{base}", project_path)

    # ── Step 9: Enqueue async CI check ─────────────────────────────────
    ci_section = _enqueue_ci_check(
        branch=branch,
        full_repo=full_repo,
        pr_number=pr_number,
        project_path=project_path,
        context=context,
        actions_log=actions_log,
    )

    # ── Step 10: Comment on the PR ────────────────────────────────────
    print(f"[rebase] Commenting on PR #{pr_number}", flush=True)
    comment_body = _build_rebase_comment(
        pr_number, branch, base, actions_log, context,
        diffstat=diffstat,
        ci_section=ci_section,
        change_summary=change_summary,
        feedback_failed=feedback_status in ("feedback_timeout", "feedback_failed"),
        feedback_reason=feedback_reason,
        show_transition_notice=show_transition_notice,
        push_guard_section=push_guard_section,
    )

    try:
        run_gh(
            "pr", "comment", pr_number,
            "--repo", full_repo,
            "--body", sanitize_github_comment(comment_body),
        )
        actions_log.append("Commented on PR")
    except Exception as e:
        # Non-fatal — the rebase itself succeeded
        actions_log.append(f"Comment failed (non-fatal): {str(e)[:100]}")

    # Restore original branch
    _safe_checkout(original_branch, project_path)

    summary = f"PR #{pr_number} rebased.\n" + "\n".join(
        f"- {a}" for a in actions_log
    )
    return True, summary


def _run_rebase_private_review_gate(
    *,
    owner: str,
    repo: str,
    pr_number: str,
    branch: str,
    base: str,
    full_repo: str,
    context: dict,
    project_path: str,
    head_remote: Optional[str],
    notify_fn,
    skill_dir: Optional[Path],
    actions_log: List[str],
    push_state: Optional[dict] = None,
):
    """Run the shared private review gate using /rebase push semantics."""
    if not Path(project_path).is_dir():
        return None

    pr_url = context.get("url") or f"https://github.com/{full_repo}/pull/{pr_number}"
    project_name = ""
    try:
        from app.utils import project_name_for_path
        project_name = project_name_for_path(project_path)
    except Exception as exc:
        print(
            f"[rebase_pr] private gate project lookup failed: {exc}",
            file=sys.stderr,
        )

    review_skill_dir = None
    if skill_dir:
        candidate = Path(skill_dir).parent / "review"
        if candidate.exists():
            review_skill_dir = candidate

    def push_gate_fix() -> None:
        push_result = _push_with_fallback(
            branch, base, full_repo, pr_number, context, project_path,
            head_remote=head_remote,
        )
        actions_log.extend(push_result.get("actions", []))
        if not push_result.get("success"):
            raise RuntimeError(push_result.get("error", "unknown push failure"))
        # Feed the guard: this push's fresh observation and pushed SHA, so a
        # human push landing between the main push and this one is detected.
        if push_state is not None:
            gate_remote = push_result.get("remote", "")
            # Observations only mean something for the remote they were taken
            # on: if the gate landed somewhere else, the earlier ones describe
            # a branch this pipeline never overwrote.
            if gate_remote and gate_remote != push_state.get("remote"):
                push_state["remote"] = gate_remote
                push_state["observations"] = []
                push_state["observation_failed"] = False
            # Always overwrite, including with "": once the gate has pushed,
            # the step-6 SHA is stale and comparing against it would fake a
            # race. No confirmed SHA means the guard skips, which is correct.
            push_state["pushed_head"] = push_result.get("pushed_head", "")
            observations = push_state.setdefault("observations", [])
            for observed in push_result.get("pre_push_remote_heads", []):
                if observed and observed not in observations:
                    observations.append(observed)
            if push_result.get("observation_failed"):
                push_state["observation_failed"] = True

    try:
        from app.private_review_gate import run_private_review_gate

        return run_private_review_gate(
            project_path=project_path,
            project_name=project_name,
            pr_url=pr_url,
            notify_fn=notify_fn,
            plan_url=None,
            skill_origin="rebase",
            review_skill_dir=review_skill_dir,
            push_fn=push_gate_fix,
        )
    except Exception as exc:
        msg = f"Private review gate failed after rebase: {str(exc)[:200]}"
        actions_log.append(f"{msg} (non-fatal)")
        with contextlib.suppress(Exception):
            notify_fn(f"⚠️ {msg}")
        return None


def _format_rebase_private_gate_action(gate_result) -> str:
    """Return the action-log line for a private gate result."""
    if gate_result is None:
        return ""
    summary = str(getattr(gate_result, "summary", "") or "").strip()
    if getattr(gate_result, "ran", False):
        if summary.lower().startswith("private review gate"):
            return summary
        return f"Private review gate: {summary}"
    reason = str(getattr(gate_result, "skipped_reason", "") or "").strip()
    if reason:
        return f"Private review gate skipped: {reason}"
    if summary:
        return f"Private review gate: {summary}"
    return ""


# ---------------------------------------------------------------------------
# Already-solved check
# ---------------------------------------------------------------------------

def _check_if_already_solved(
    actions_log: List[str],
    pr_context: dict,
    skill_dir: Optional[Path],
    project_path: str,
) -> Tuple[bool, Optional[str]]:
    """Ask Claude whether HEAD already addresses the intent of this PR.

    Returns (True, resolved_by) when Claude is highly confident the work is
    already done, (False, None) otherwise.  Falls through on any error so the
    rebase pipeline continues normally.
    """
    from app.cli_provider import build_full_command
    from app.config import get_model_config

    base = pr_context.get("base", "main")

    # Collect recent commits on the base branch for context
    recent_commits = ""
    try:
        recent_commits = _run_git(
            ["git", "log", "--oneline", "-30", base],
            cwd=project_path, timeout=15,
        )
    except Exception as e:
        print(f"[rebase_pr] git log for already-solved check failed: {e}", file=sys.stderr)

    prompt = load_prompt_or_skill(
        skill_dir, "already_solved",
        project_path=project_path or None,
        TITLE=pr_context.get("title", ""),
        BODY=pr_context.get("body", ""),
        BRANCH=pr_context.get("branch", ""),
        BASE=base,
        DIFF=pr_context.get("diff", ""),
        RECENT_COMMITS=recent_commits,
    )

    models = get_model_config()
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=[],
        model=models.get("review", models["mission"]),
        fallback=models["fallback"],
        max_turns=3,
    )

    result = run_claude(cmd, project_path, timeout=120)

    if not result["success"]:
        actions_log.append("Already-solved check: skipped (Claude call failed)")
        return False, None

    # Extract the first JSON object from the output
    raw = result.get("output", "")
    json_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if not json_match:
        actions_log.append("Already-solved check: skipped (no JSON in response)")
        return False, None

    try:
        data = json.loads(json_match.group(0))
    except (json.JSONDecodeError, ValueError):
        actions_log.append("Already-solved check: skipped (JSON parse error)")
        return False, None

    already_solved = data.get("already_solved", False)
    confidence = data.get("confidence", "low")
    resolved_by = data.get("resolved_by") or None
    reasoning = data.get("reasoning", "")

    if already_solved and confidence == "high":
        actions_log.append(
            f"Already-solved check: positive (confidence=high, resolved_by={resolved_by})"
        )
        return True, resolved_by

    # Low/medium confidence or not solved — log and continue
    label = "positive (skipped — confidence not high)" if already_solved else "negative"
    actions_log.append(
        f"Already-solved check: {label} "
        f"(confidence={confidence}, reasoning={reasoning[:100]})"
    )
    return False, None


_CLOSES_RE = re.compile(
    r'(?:closes?|fixes?|resolves?)\s+'
    r'(?:([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)|#(\d+))',
    re.IGNORECASE,
)


def _close_pr_as_duplicate(
    owner: str,
    repo: str,
    pr_number: str,
    resolved_by: Optional[str],
    pr_context: dict,
    project_path: str,
    notify_fn=None,
) -> None:
    """Close a PR that is already solved, with an explanatory comment.

    Also closes the linked issue (Closes #NNN / Fixes #NNN) when found in
    the PR body.
    """
    full_repo = f"{owner}/{repo}"
    resolved_ref = resolved_by or "a recent commit"

    comment_text = (
        f"## PR Closed — Already Solved\n\n"
        f"This PR's intent has already been addressed by {resolved_ref}.\n\n"
        f"Kōan detected (with high confidence) that the work described in this PR "
        f"is no longer needed — the base branch already contains an equivalent fix.\n\n"
        f"If this determination is incorrect, please reopen the PR and add a comment "
        f"explaining what is still needed.\n\n"
        f"---\n_Automated by Kōan_"
    )

    try:
        run_gh("pr", "comment", pr_number, "--repo", full_repo, "--body", sanitize_github_comment(comment_text))
    except Exception as e:
        print(f"[rebase_pr] PR comment failed: {e}", file=sys.stderr)

    try:
        run_gh("pr", "close", pr_number, "--repo", full_repo)
    except Exception as e:
        print(f"[rebase_pr] PR close failed: {e}", file=sys.stderr)

    # Close any linked issue referenced in the PR body
    body = pr_context.get("body", "") or ""
    for match in _CLOSES_RE.finditer(body):
        cross_repo = match.group(1)  # e.g. "org/repo" or None
        issue_num = match.group(2) or match.group(3)
        if not issue_num:
            continue

        if cross_repo:
            issue_repo = cross_repo
        else:
            issue_repo = full_repo

        issue_comment = (
            f"This issue was linked to PR #{pr_number} which has been closed "
            f"because its intent was already addressed by {resolved_ref}.\n\n"
            f"---\n_Automated by Kōan_"
        )
        try:
            run_gh("issue", "comment", issue_num, "--repo", issue_repo, "--body", sanitize_github_comment(issue_comment))
            run_gh("issue", "close", issue_num, "--repo", issue_repo)
        except Exception as e:
            print(f"[rebase_pr] issue close failed ({issue_repo}#{issue_num}): {e}", file=sys.stderr)

    if notify_fn:
        pr_title = pr_context.get("title", f"PR #{pr_number}")
        notify_fn(
            f"PR #{pr_number} ({pr_title}) closed — already solved by {resolved_ref}."
        )


# ---------------------------------------------------------------------------
# Conflict-aware rebase
# ---------------------------------------------------------------------------

def _rebase_with_conflict_resolution(
    base: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    max_conflict_rounds: int = 10,
    preferred_remote: Optional[str] = None,
    head_remote: Optional[str] = None,
    result_meta: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Rebase onto target branch, resolving conflicts via Claude if needed.

    Delegates to :func:`claude_step._rebase_onto_target` for the strict
    fetch-and-rebase flow, injecting a conflict-resolution callback that
    invokes Claude to resolve conflicted files.

    When ``git rebase`` hits conflicts, Claude is invoked to resolve the
    conflicted files, they are staged, and the rebase is continued.  This
    loop repeats for up to *max_conflict_rounds* rounds (one round per
    conflicting commit).

    Returns:
        Remote name used (e.g. "origin") on success, None on total failure
        (*result_meta*, when given, then carries ``error``/``detail``).
    """

    conflict_failure: List[str] = []

    def _on_conflict(proj_path: str) -> bool:
        """Conflict callback: resolve via Claude then continue the rebase."""
        resolved = _resolve_rebase_conflicts(
            base, "",  # remote not needed — conflicts already in progress
            proj_path, context, actions_log,
            notify_fn=notify_fn, skill_dir=skill_dir,
            max_rounds=max_conflict_rounds, failure_detail=conflict_failure,
        )
        if not resolved and result_meta is not None and conflict_failure:
            result_meta["detail"] = conflict_failure[-1]
        return resolved

    return _rebase_onto_target(
        base, project_path,
        preferred_remote=preferred_remote,
        head_remote=head_remote,
        on_conflict=_on_conflict,
        result_meta=result_meta,
    )


# Backward-compatible alias — canonical source is now claude_step.has_rebase_in_progress
_has_rebase_in_progress = has_rebase_in_progress


_UNMERGED_STATUSES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _get_conflicted_files(
    project_path: str, errors: Optional[List[str]] = None,
) -> Optional[List[str]]:
    """Return list of files with unmerged conflicts, or None if git could not tell.

    Uses ``git status --porcelain`` which explicitly reports the merge state
    of each index entry.  Previous implementation used
    ``git diff --name-only --diff-filter=U`` which can silently return
    incomplete results during complex rebase operations (e.g. ``--onto``
    rebases or branches with merge commits being linearised) — so nothing in
    this module may fall back to that command, least of all a safety check.

    A failed status (timeout, held ``index.lock``, non-zero exit) returns
    **None**, never ``[]``: callers act on "nothing is conflicted", and an
    unanswerable question must not masquerade as that answer.  git's own
    error text is appended to *errors* when given, so a caller can report
    the actual cause instead of a bare round count.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, cwd=project_path,
            timeout=30,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:200]
            print(
                f"[rebase_pr] git status failed (rc={result.returncode}): {err}",
                file=sys.stderr,
            )
            if errors is not None:
                errors.append(err or f"git status exited {result.returncode}")
            return None
        files = [
            line[3:].strip()
            for line in result.stdout.splitlines()
            if len(line) >= 4 and line[:2] in _UNMERGED_STATUSES
        ]
        return files
    except Exception as e:
        print(f"[rebase_pr] failed to list conflicted files: {e}", file=sys.stderr)
        if errors is not None:
            errors.append(str(e))
        return None


# git helper failures worth swallowing: a non-zero exit (_run_git raises
# RuntimeError), a hung git, or the binary being unavailable.
_GIT_STEP_EXCEPTIONS = (RuntimeError, subprocess.SubprocessError, OSError)

# Prefix marking "git itself could not be queried" — a transient condition
# (held index.lock, timeout) the conflict loop retries instead of treating as
# a rebase that cannot advance.
_VERIFY_FAILED_PREFIX = "could not verify the tree is unmerged-free"

# A status git never answers is worth re-asking, but only a bounded number of
# times: an index.lock held for the life of the rebase would otherwise eat the
# whole round budget and be reported as "too complex to resolve".
_MAX_UNREADABLE_STATUS_ROUNDS = 3
_UNREADABLE_STATUS_DELAY = 2.0


def _record_conflict_failure(
    failure_detail: Optional[List[str]], detail: str,
) -> None:
    """Record why conflict resolution stopped, for the caller's error message.

    Without this the caller falls back to the *initial* ``git rebase`` error,
    which names the first conflicting commit even when that commit was
    resolved fine and the rebase died later — a misleading report.
    """
    print(f"[rebase_pr] conflict resolution failed: {detail}", file=sys.stderr)
    if failure_detail is not None:
        failure_detail.append(detail)


class _ContinueOutcome(NamedTuple):
    """Result of one ``git rebase --continue`` attempt.

    *completed* is True once no rebase is in progress and git said so with a
    zero exit.  *detail* carries git's own error text for a failed attempt,
    and is empty when the continue merely stopped on the next commit's
    conflicts.  *retryable* marks the case where nothing was attempted
    because git could not be queried — the round is worth spending again
    rather than abandoning resolutions already paid for in LLM quota.
    """

    completed: bool
    detail: str
    retryable: bool = False


def _verify_unmerged_free(project_path: str) -> Tuple[bool, str]:
    """Prove the index holds no unmerged entry, with git's exit status checked.

    ``git add -u`` marks an unmerged path resolved by taking the working-tree
    content verbatim — conflict markers included.  Git refusing ``--continue``
    on unmerged entries is the last backstop against committing them, so it
    may only be bypassed on a *confident* "nothing is unmerged".  This is the
    sole check on the path where the caller's own probe returned None, so it
    asks git through :func:`_get_conflicted_files` — ``git status
    --porcelain``, the listing this module documents as reliable — and treats
    an unanswered status as a refusal to stage.

    Returns ``(verified, why)``; *why* is prefixed with
    ``_VERIFY_FAILED_PREFIX`` when git itself could not be read, which the
    caller turns into a retry rather than a terminal failure.
    """
    errors: List[str] = []
    unmerged = _get_conflicted_files(project_path, errors=errors)
    if unmerged is None:
        err = errors[-1] if errors else "git status could not be read"
        return False, f"{_VERIFY_FAILED_PREFIX}: {err}"
    if unmerged:
        return False, (
            f"{len(unmerged)} file(s) still unmerged: {', '.join(unmerged[:5])}"
        )
    return True, ""


def _list_untracked_files(project_path: str) -> List[str]:
    """Return the files ``git add -u`` leaves out of the replayed commit.

    A resolution that legitimately creates a file (an extracted helper, a
    module the target branch split) leaves it untracked, and nothing else
    would ever notice: untracked files neither block ``--continue`` nor show
    up as unmerged.  The caller records them in the actions log so a dropped
    file reaches the human reading the PR report, not just daemon stderr.
    """
    try:
        out = _run_git(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_path, timeout=30,
        )
    except _GIT_STEP_EXCEPTIONS as e:
        print(f"[rebase_pr] could not list untracked files: {e}", file=sys.stderr)
        return []
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def _continue_rebase(
    project_path: str, actions_log: Optional[List[str]] = None,
) -> _ContinueOutcome:
    """Stage the resolved tree and run ``git rebase --continue``.

    Every *tracked* modification is staged first, not just the conflicted
    paths: ``git rebase --continue`` refuses to run while any tracked file is
    unstaged, and reports it as the misleading "You must edit all merge
    conflicts and then mark them as resolved using git add" even when no
    unmerged path is left.  A coherent conflict resolution routinely edits
    neighbouring files (a helper the replayed commit moved, an import the
    target branch renamed), and the rebase starts from a clean, autostashed
    tree, so those edits are resolution output.  ``git add -u`` keeps
    untracked scratch files out of the replayed commit — they never block
    ``--continue`` anyway; new files the resolution needed are reported in
    *actions_log* instead of committed.

    Staging only ever happens once :func:`_verify_unmerged_free` confirms
    nothing is unmerged, so this cannot turn an unresolved conflict into a
    commit.  A non-zero exit is never folded into success: completion
    requires both a gone rebase directory and git's own rc=0.
    """
    verified, why = _verify_unmerged_free(project_path)
    if not verified:
        print(f"[rebase_pr] refusing to stage: {why}", file=sys.stderr)
        return _ContinueOutcome(
            False, why, retryable=why.startswith(_VERIFY_FAILED_PREFIX),
        )

    try:
        _run_git(["git", "add", "-u"], cwd=project_path, timeout=60)
    except _GIT_STEP_EXCEPTIONS as e:
        # Reporting the `--continue` symptom instead would blame the agent for
        # conflicts it resolved fine.
        return _ContinueOutcome(False, f"staging the resolved tree failed: {e}")

    untracked = _list_untracked_files(project_path)
    if untracked:
        note = (
            f"{len(untracked)} untracked file(s) left out of the replayed "
            f"commit: {', '.join(untracked[:5])}"
        )
        print(f"[rebase_pr] {note}", file=sys.stderr)
        if actions_log is not None:
            actions_log.append(note)

    try:
        # GIT_EDITOR=true prevents an interactive editor for commit messages.
        proc = subprocess.run(
            ["git", "rebase", "--continue"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            cwd=project_path, timeout=60,
            env={**os.environ, "GIT_EDITOR": "true"},
        )
    except _GIT_STEP_EXCEPTIONS as e:
        # A killed or never-launched git is a failure even when
        # `.git/rebase-merge` is gone: git may have unlinked it before
        # replaying the remaining commits, and a missing directory is no
        # evidence the rebase finished.
        return _ContinueOutcome(False, f"git rebase --continue failed: {e}")

    output = (proc.stderr or proc.stdout or "").strip()[:200]
    if _has_rebase_in_progress(project_path):
        if proc.returncode == 0:
            # Stopped on the next commit's conflicts — not an error.
            return _ContinueOutcome(False, "")
        return _ContinueOutcome(False, f"git rebase --continue failed: {output}")
    if proc.returncode != 0:
        # A rejected commit hook or a refused commit tears the rebase
        # directory down too; the replayed commit was never created.
        return _ContinueOutcome(
            False,
            f"git rebase --continue exited {proc.returncode} with no rebase "
            f"in progress: {output}",
        )
    return _ContinueOutcome(True, "")


def _resolve_rebase_conflicts(
    base: str,
    remote: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
    notify_fn=None,
    skill_dir: Optional[Path] = None,
    max_rounds: int = 5,
    failure_detail: Optional[List[str]] = None,
) -> bool:
    """Resolve rebase conflicts via Claude, then continue the rebase.

    Each conflicting commit in the rebase may produce its own set of
    conflicts.  This function loops: resolve → stage → continue → check
    for more conflicts, up to *max_rounds* times.

    Returns True if the rebase completed successfully.
    """
    from app.cli_provider import build_full_command
    from app.config import get_model_config

    status_errors: List[str] = []
    unreadable_rounds = 0

    for round_num in range(1, max_rounds + 1):
        conflicted = _get_conflicted_files(project_path, errors=status_errors)
        if conflicted is None:
            # git could not be asked (held index.lock, timeout). Re-ask after a
            # pause rather than read the non-answer as either state — but a
            # git that never answers is its own failure, not "conflicts too
            # complex to resolve" once the round budget runs out.
            unreadable_rounds += 1
            if unreadable_rounds >= _MAX_UNREADABLE_STATUS_ROUNDS:
                _record_conflict_failure(
                    failure_detail,
                    f"git status could not be read after {unreadable_rounds} "
                    f"attempts: "
                    f"{status_errors[-1] if status_errors else 'unknown error'}",
                )
                return False
            time.sleep(_UNREADABLE_STATUS_DELAY)
            continue
        unreadable_rounds = 0
        if not conflicted:
            # No conflicts — try to continue (may already be done)
            outcome = _continue_rebase(project_path, actions_log=actions_log)
            if outcome.completed:
                return True
            if outcome.retryable:
                # Nothing was staged or continued — git could not be queried.
                # Spend the round again instead of abandoning the rebase.
                time.sleep(_UNREADABLE_STATUS_DELAY)
                continue
            if _get_conflicted_files(project_path) == []:
                # Stuck: nothing to resolve, yet the rebase will not advance.
                # Looping here just burns every remaining round.
                _record_conflict_failure(
                    failure_detail,
                    outcome.detail or "git rebase --continue made no progress",
                )
                return False
            continue

        if notify_fn:
            notify_fn(
                f"Resolving conflicts ({round_num}/{max_rounds}): "
                f"{', '.join(conflicted[:5])}"
                f"{'...' if len(conflicted) > 5 else ''}"
            )

        # Build conflict resolution prompt
        print(f"[rebase] Resolving conflicts via {_cli_label()} (round {round_num})", flush=True)
        prompt = _build_conflict_resolution_prompt(
            context, conflicted, base, skill_dir=skill_dir,
            project_path=project_path,
        )

        # Invoke Claude to resolve conflicts
        models = get_model_config()
        cmd = build_full_command(
            prompt=prompt,
            allowed_tools=["Bash", "Read", "Write", "Glob", "Grep", "Edit"],
            model=models["mission"],
            fallback=models["fallback"],
            max_turns=get_skill_max_turns(),
        )
        timeout = get_rebase_conflict_timeout()
        result = run_claude(cmd, project_path, timeout=timeout)

        if not result["success"]:
            detail = result.get("error") or "agent failed"
            if _is_agent_timeout_error(detail):
                detail = f"conflict-resolution agent timed out after {timeout}s"
            else:
                detail = f"conflict-resolution agent failed: {detail[:200]}"
            if failure_detail is not None:
                failure_detail.append(detail)
            print(
                f"[rebase_pr] Claude conflict resolution failed (round {round_num}): "
                f"{detail}",
                file=sys.stderr,
            )
            return False

        # The agent must leave no unmerged path behind. A None (git could not
        # tell) falls through to _continue_rebase, which re-asks
        # `git status --porcelain` and refuses to stage unless it answers.
        remaining = _get_conflicted_files(project_path)
        if remaining:
            _record_conflict_failure(
                failure_detail,
                f"{len(remaining)} file(s) still conflicted after resolution: "
                f"{', '.join(remaining[:5])}",
            )
            return False

        # Continue the rebase
        outcome = _continue_rebase(project_path, actions_log=actions_log)
        if outcome.completed:
            actions_log.append(
                f"Resolved merge conflicts ({round_num} round(s))"
            )
            return True
        if outcome.retryable:
            # git could not be queried, so nothing was staged or continued —
            # the resolution already paid for stays on disk for next round.
            time.sleep(_UNREADABLE_STATUS_DELAY)
            continue
        if _get_conflicted_files(project_path) == []:
            _record_conflict_failure(
                failure_detail,
                outcome.detail or "git rebase --continue made no progress",
            )
            return False
        # More conflicts from a later commit — resolve them next round.

    _record_conflict_failure(
        failure_detail,
        f"exceeded {max_rounds} conflict-resolution rounds",
    )
    return False


def _build_conflict_resolution_prompt(
    context: dict,
    conflicted_files: List[str],
    base: str,
    skill_dir: Optional[Path] = None,
    project_path: str = "",
) -> str:
    """Build a prompt for Claude to resolve merge conflicts."""
    kwargs = dict(
        TITLE=context.get("title", ""),
        BODY=context.get("body", ""),
        BRANCH=context.get("branch", ""),
        BASE=base,
        CONFLICTED_FILES="\n".join(f"- `{f}`" for f in conflicted_files),
    )
    return load_prompt_or_skill(
        skill_dir, "conflict_resolution",
        project_path=project_path or None,
        apply_caveman=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

MAX_CI_FIX_ATTEMPTS = 2


def check_pr_state(pr_number: str, full_repo: str) -> tuple:
    """Query current PR state and mergeable status.

    Returns:
        (state, mergeable) tuple where state is e.g. "OPEN", "MERGED", "CLOSED"
        and mergeable is e.g. "MERGEABLE", "CONFLICTING", "UNKNOWN".
    """
    try:
        raw = run_gh(
            "pr", "view", pr_number, "--repo", full_repo,
            "--json", "state,mergeable",
        )
        data = json.loads(raw) if raw.strip() else {}
        return (
            data.get("state", "UNKNOWN"),
            data.get("mergeable", "UNKNOWN"),
        )
    except Exception as e:
        print(f"[rebase] PR state check failed: {e}", file=sys.stderr)
        return ("UNKNOWN", "UNKNOWN")


def _fix_existing_ci_failures(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
    notify_fn,
    skill_dir: Optional[Path] = None,
    commit_conventions: str = "",
) -> bool:
    """Check the most recent CI run and fix failures before pushing.

    Inspects the last CI run on the branch (from before the rebase).  If it
    failed, fetches the logs, invokes Claude to apply fixes, and amends the
    commit so the fix is included in the upcoming force-push.

    Returns True if a fix was applied, False otherwise.
    """
    pr_url = context.get("url") or f"https://github.com/{full_repo}/pull/{pr_number}"

    notify_fn(f"Checking existing CI on [{branch}]({pr_url})...")
    ci_status, run_id, ci_logs = check_existing_ci(branch, full_repo)

    if ci_status != "failure":
        if ci_status == "success":
            actions_log.append("Pre-push CI check: previous run passed")
        elif ci_status == "pending":
            actions_log.append("Pre-push CI check: previous run still pending")
        elif ci_status == CI_STATUS_BLOCKED_APPROVAL:
            actions_log.append(
                "Pre-push CI check: previous run waiting for maintainer approval"
            )
        else:
            actions_log.append("Pre-push CI check: no CI runs found")
        return False

    print(f"[rebase] CI failed — invoking {_cli_label()} to fix (run #{run_id})", flush=True)
    notify_fn("Previous CI failed — analyzing logs to fix before push...")
    actions_log.append(f"Pre-push CI check: previous run #{run_id} failed")

    # Build CI fix prompt with current diff
    rebase_remote = "origin"
    diff = ""
    try:
        diff = _run_git(
            ["git", "diff", f"{rebase_remote}/{base}..HEAD"],
            cwd=project_path, timeout=30,
        )
    except Exception as e:
        print(f"[rebase_pr] diff fetch for CI fix failed: {e}", file=sys.stderr)
    diff = truncate_diff(diff, 32000)

    ci_fix_prompt = _build_ci_fix_prompt(
        context, ci_logs, diff, skill_dir=skill_dir,
        commit_conventions=commit_conventions,
        project_path=project_path,
    )

    fixed, timed_out, attempts_used = _run_ci_fix_step_with_timeout_retry(
        prompt=ci_fix_prompt,
        project_path=project_path,
        commit_msg=f"fix: resolve pre-existing CI failures on #{pr_number}",
        success_label="Applied pre-push CI fix",
        failure_label="Pre-push CI fix step produced no changes",
        actions_log=actions_log,
        use_convention_subject=bool(commit_conventions),
    )

    if fixed:
        if attempts_used > 1:
            actions_log.append("Pre-push CI fix applied after timeout retry")
        else:
            actions_log.append("Pre-push CI fix applied")
    else:
        if timed_out:
            actions_log.append("Pre-push CI fix timed out")
        else:
            actions_log.append("Pre-push CI fix: no changes needed or Claude found nothing to fix")

    return fixed


def _enqueue_ci_check(
    branch: str,
    full_repo: str,
    pr_number: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
) -> str:
    """Enqueue an async CI check in the ## CI section of missions.md.

    Returns CI section text for the PR comment.
    """
    import os
    from pathlib import Path

    koan_root = os.environ.get("KOAN_ROOT")
    if not koan_root:
        actions_log.append("CI check skipped (KOAN_ROOT not set)")
        return "CI check skipped (not running under Kōan)."

    from app.config import is_ci_check_enabled
    if not is_ci_check_enabled():
        actions_log.append("CI check skipped (disabled in config)")
        return "CI check skipped (disabled in config)."

    instance_dir = os.path.join(koan_root, "instance")
    pr_url = context.get("url") or f"https://github.com/{full_repo}/pull/{pr_number}"

    try:
        from app.missions import add_ci_item
        from app.utils import load_config, modify_missions_file, project_name_for_path

        config = load_config()
        max_attempts = config.get("ci_fix_max_attempts", 5)
        project_name = project_name_for_path(project_path)
        missions_path = Path(instance_dir) / "missions.md"

        modify_missions_file(
            missions_path,
            lambda c: add_ci_item(c, project_name, pr_url, pr_number, branch, full_repo, max_attempts),
        )
        actions_log.append("CI check enqueued in ## CI (async)")
        return "CI will be checked asynchronously."
    except Exception as e:
        print(f"[rebase] CI enqueue failed: {e}", file=sys.stderr)
        actions_log.append(f"CI enqueue failed: {str(e)[:100]}")
        return "CI check could not be enqueued."


def _run_ci_check_and_fix(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    project_path: str,
    context: dict,
    actions_log: List[str],
    notify_fn,
    skill_dir: Optional[Path] = None,
    commit_conventions: str = "",
) -> str:
    """Poll CI after push, attempt fixes if failing. Returns CI section for PR comment.

    Uses a bounded local fix loop with heartbeat output and timeout-aware
    single retry, then polls CI after each pushed fix attempt.
    """
    pr_url = context.get("url") or f"https://github.com/{full_repo}/pull/{pr_number}"

    notify_fn(f"Checking CI on [{branch}]({pr_url})...")
    ci_status, run_id, ci_logs = wait_for_ci(branch, full_repo)

    if ci_status == "none":
        actions_log.append("No CI runs found")
        return ""

    if ci_status == "success":
        actions_log.append("CI passed")
        return "CI passed."

    if ci_status == "timeout":
        actions_log.append("CI polling timed out")
        return "CI still running (timed out waiting)."

    if ci_status == CI_STATUS_BLOCKED_APPROVAL:
        actions_log.append("CI waiting for maintainer approval — skipping fixes")
        return "CI waiting for maintainer approval — fixes skipped."

    # CI failed — check PR state before attempting fixes
    pr_state, mergeable = check_pr_state(pr_number, full_repo)

    if pr_state == "MERGED":
        actions_log.append("PR already merged — skipping CI fix")
        return "PR already merged — CI fix skipped."

    if mergeable == "CONFLICTING":
        actions_log.append("PR has merge conflicts — skipping CI fix")
        return "PR has merge conflicts — CI fix skipped (rebase needed)."

    notify_fn(f"CI failed on [{pr_url}]({pr_url}). Attempting fixes...")

    def _build_prompt(logs: str, diff: str) -> str:
        return _build_ci_fix_prompt(
            context, logs, diff, skill_dir=skill_dir,
            commit_conventions=commit_conventions,
            project_path=project_path,
        )

    # Delegate the shared diff -> fix -> push -> recheck loop to the canonical
    # implementation, injecting the activity-aware (heartbeat + timeout-retry)
    # step runner so long-but-active CI fixes keep running while stalled ones
    # are killed. The structured ``outcome`` drives the PR-comment summary.
    import os

    koan_root = os.environ.get("KOAN_ROOT", "")
    ci_instance_dir = os.path.join(koan_root, "instance") if koan_root else ""
    from app.utils import project_name_for_path as _project_name_for_path
    ci_project_name = _project_name_for_path(project_path)

    outcome: Dict[str, object] = {}
    _success, last_ci_logs = run_ci_fix_loop(
        branch=branch,
        base=base,
        full_repo=full_repo,
        project_path=project_path,
        ci_logs=ci_logs,
        actions_log=actions_log,
        max_attempts=MAX_CI_FIX_ATTEMPTS,
        commit_conventions=commit_conventions,
        use_polling=True,
        prompt_builder=_build_prompt,
        commit_msg_template=(
            f"fix: resolve CI failures on #{pr_number} (attempt {{attempt}})"
        ),
        step_runner=_run_ci_fix_step_with_timeout_retry,
        push_fn=lambda b, p: _force_push("origin", b, p),
        recheck_fn=lambda b, repo: wait_for_ci(b, repo),
        outcome=outcome,
        instance_dir=ci_instance_dir,
        project_name=ci_project_name,
    )

    result = str(outcome.get("result", "exhausted"))
    attempt = outcome.get("attempt", MAX_CI_FIX_ATTEMPTS)

    if result == "fixed":
        return f"CI failed initially, fixed on attempt {attempt}."
    if result == "quota":
        return "CI fix paused due to provider quota; retry after quota reset."
    if result == "timeout":
        return (
            f"CI fix timed out during `/rebase` "
            f"(attempt {attempt}/{MAX_CI_FIX_ATTEMPTS}). "
            f"Next: run `/rebase {pr_url}` again or inspect locally with "
            "`git status` and `git log -1`."
        )
    if result == "blocked_approval":
        return (
            f"CI fix pushed (attempt {attempt}), but the new run is waiting "
            "for maintainer approval."
        )
    if result == "pending":
        return f"CI fix pushed (attempt {attempt}), CI status: check pending."
    if result == "push_failed":
        push_error = str(outcome.get("push_error", ""))[:120]
        return f"CI fix was applied but push failed: {push_error}"

    # no_changes / exhausted — report failure with log excerpt
    log_excerpt = last_ci_logs[:2000] if last_ci_logs else "(no logs available)"
    return (
        f"CI still failing after {MAX_CI_FIX_ATTEMPTS} fix attempts.\n\n"
        f"<details><summary>Last failure logs</summary>\n\n"
        f"```\n{log_excerpt}\n```\n\n</details>"
    )


def _build_ci_fix_prompt(
    context: dict,
    ci_logs: str,
    diff: str,
    skill_dir: Optional[Path] = None,
    commit_conventions: str = "",
    project_path: str = "",
) -> str:
    """Build a prompt for Claude to fix CI failures."""
    from app.claude_step import _load_commit_subject_instruction

    commit_subject_instruction = ""
    if commit_conventions:
        commit_subject_instruction = _load_commit_subject_instruction(skill_dir)

    kwargs = dict(
        TITLE=context.get("title", ""),
        BRANCH=context.get("branch", ""),
        BASE=context.get("base", ""),
        CI_LOGS=truncate_text(ci_logs, 6000),
        DIFF=truncate_diff(diff, 32000),
        COMMIT_CONVENTIONS=commit_conventions,
        COMMIT_SUBJECT_INSTRUCTION=commit_subject_instruction,
    )
    return load_prompt_or_skill(
        skill_dir, "ci_fix",
        project_path=project_path or None,
        **kwargs,
    )


def _emit_phase_heartbeat(
    stop_event: threading.Event, interval_seconds: int, phase_label: str,
) -> None:
    """Emit periodic progress lines to keep the parent liveness watchdog alive."""
    started = time.monotonic()
    while not stop_event.wait(interval_seconds):
        elapsed = int(time.monotonic() - started)
        print(
            f"[rebase] {phase_label} still running ({elapsed}s elapsed)",
            flush=True,
        )


def _run_claude_step_with_heartbeat(
    *,
    phase_label: str,
    heartbeat_seconds: int,
    prompt: str,
    project_path: str,
    commit_msg: str,
    success_label: str,
    failure_label: str,
    actions_log: List[str],
    max_turns: int,
    timeout: int,
    use_convention_subject: bool,
    idle_timeout: Optional[int] = None,
    max_duration: Optional[int] = None,
    bypass_hook_failures: bool = False,
):
    """Run ``run_claude_step`` while emitting periodic heartbeat lines."""
    stop_heartbeat = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_emit_phase_heartbeat,
        args=(stop_heartbeat, heartbeat_seconds, phase_label),
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        return run_claude_step(
            prompt=prompt,
            project_path=project_path,
            commit_msg=commit_msg,
            success_label=success_label,
            failure_label=failure_label,
            actions_log=actions_log,
            max_turns=max_turns,
            timeout=timeout,
            idle_timeout=idle_timeout,
            max_duration=max_duration,
            use_convention_subject=use_convention_subject,
            bypass_hook_failures=bypass_hook_failures,
        )
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1)


def _run_ci_fix_step_with_timeout_retry(
    *,
    prompt: str,
    project_path: str,
    commit_msg: str,
    success_label: str,
    failure_label: str,
    actions_log: List[str],
    use_convention_subject: bool,
) -> Tuple[object, bool, int]:
    """Run one CI-fix Claude step with one timeout-specific retry.

    Returns ``(step_result, timed_out, attempts_used)``.
    ``timed_out`` is True only when the final result is timeout-shaped.
    """
    timeout = get_skill_timeout()
    max_turns = get_skill_max_turns()
    idle_timeout = get_rebase_ci_idle_timeout()
    max_duration = get_rebase_ci_max_duration()
    step = _run_claude_step_with_heartbeat(
        phase_label="Applying CI fix",
        heartbeat_seconds=_REBASE_CI_FIX_HEARTBEAT_SECONDS,
        prompt=prompt,
        project_path=project_path,
        commit_msg=commit_msg,
        success_label=success_label,
        failure_label=failure_label,
        actions_log=actions_log,
        max_turns=max_turns,
        timeout=timeout,
        idle_timeout=idle_timeout,
        max_duration=max_duration,
        use_convention_subject=use_convention_subject,
    )
    step_error = str(getattr(step, "error", "") or "").strip()
    if step or not _is_agent_timeout_error(step_error):
        return step, False, 1

    actions_log.append("CI fix attempt timed out")
    if _REBASE_CI_FIX_TIMEOUT_RETRIES <= 0:
        return step, True, 1

    actions_log.append("Retrying CI fix once with tighter prompt after timeout")
    retry_prompt = prompt + _REBASE_CI_FIX_TIGHT_RETRY_SUFFIX
    retry_step = _run_claude_step_with_heartbeat(
        phase_label="Retrying CI fix",
        heartbeat_seconds=_REBASE_CI_FIX_HEARTBEAT_SECONDS,
        prompt=retry_prompt,
        project_path=project_path,
        commit_msg=f"{commit_msg} (retry after timeout)",
        success_label=success_label,
        failure_label=failure_label,
        actions_log=actions_log,
        max_turns=max_turns,
        timeout=timeout,
        idle_timeout=idle_timeout,
        max_duration=max_duration,
        use_convention_subject=use_convention_subject,
    )
    retry_error = str(getattr(retry_step, "error", "") or "").strip()
    retry_timed_out = _is_agent_timeout_error(retry_error)
    if retry_timed_out:
        actions_log.append("CI fix retry timed out")
    return retry_step, retry_timed_out, 2


def _build_rebase_prompt(
    context: dict,
    skill_dir: Optional[Path] = None,
    commit_conventions: str = "",
    min_severity: Optional[str] = None,
    project_path: str = "",
    feedback_context: str = "",
) -> str:
    """Build a prompt for Claude to analyze and apply review feedback."""
    prompt = _build_pr_prompt(
        "rebase", context, skill_dir=skill_dir,
        commit_conventions=commit_conventions,
        project_path=project_path,
    )

    if feedback_context.strip():
        from app.prompt_guard import fence_external_data

        prompt += (
            "\n\n## Explicit User Request\n\n"
            "The user explicitly requested this outcome. Implement it when it is "
            "valid, or give a concrete SKIPPED reason tied to the current code.\n"
            + fence_external_data(
                feedback_context.strip()[:1000], "GitHub command context",
            )
        )

    if min_severity and min_severity != "suggestion":
        included = severity_at_or_above(min_severity)
        excluded = [s for s in SEVERITY_LEVELS if s not in included]
        included_labels = ", ".join(
            f"**{s}** (🔴)" if s == "critical"
            else f"**{s}** (🟡)" if s == "warning"
            else f"**{s}** (🟢)"
            for s in included
        )
        excluded_labels = ", ".join(excluded)
        prompt += (
            f"\n\n## Severity Filter\n\n"
            f"Only address review issues at these severity levels: {included_labels}.\n"
            f"**Skip** all issues at: {excluded_labels}.\n"
            f"Look for severity markers in the review comments — sections headed "
            f"with 🔴 Blocking (critical), 🟡 Important (warning), or 🟢 Suggestions.\n"
            f"If a comment has no clear severity marker, treat it as actionable "
            f"only if it reads like a blocking or important concern.\n"
        )

    return prompt


def _apply_review_feedback(
    context: dict,
    pr_number: str,
    project_path: str,
    actions_log: List[str],
    skill_dir: Optional[Path] = None,
    commit_conventions: str = "",
    min_severity: Optional[str] = None,
    feedback_context: str = "",
    result_meta: Optional[dict] = None,
) -> str:
    """Analyze review comments via Claude and apply requested changes.

    Args:
        min_severity: When set, only address review issues at this severity
            level or above.  One of ``"critical"``, ``"warning"``, or
            ``"suggestion"`` (which means "all").

    Returns:
        A change summary string describing what was modified (empty if
        no changes were made).  Used for descriptive commit messages and
        PR comments so that review-driven changes are always explained.
    """
    prompt = _build_rebase_prompt(
        context, skill_dir=skill_dir,
        commit_conventions=commit_conventions,
        min_severity=min_severity,
        project_path=project_path,
        feedback_context=feedback_context,
    )

    try:
        step = _run_claude_step_with_heartbeat(
            phase_label="Applying review feedback",
            heartbeat_seconds=_REBASE_FEEDBACK_HEARTBEAT_SECONDS,
            prompt=prompt,
            project_path=project_path,
            commit_msg=f"rebase: apply review feedback on #{pr_number}",
            success_label="Applied review feedback",
            failure_label="Review feedback step failed",
            actions_log=actions_log,
            max_turns=get_skill_max_turns(),
            timeout=get_skill_timeout(),
            idle_timeout=get_rebase_review_idle_timeout(),
            max_duration=get_rebase_review_max_duration(),
            use_convention_subject=bool(commit_conventions),
            bypass_hook_failures=True,
        )
    except Exception as exc:
        # The git rebase already succeeded by this point. A crash while
        # *applying* review feedback — most commonly a target-repo pre-commit
        # hook rejecting the feedback edits (`GitCommandError`), but any
        # exception qualifies — must not discard that successful rebase. Drop
        # any partially-staged feedback edits so the clean rebase is what gets
        # pushed, then signal ``feedback_failed`` so run_rebase pushes the
        # rebase as-is and flags that feedback was not applied. CI remains the
        # real gate; the human can re-run /rebase --fix to retry the feedback
        # (a bare /rebase only rebases and would not re-apply it).
        try:
            _run_git(
                ["git", "reset", "--hard", "HEAD"],
                cwd=project_path, timeout=30,
            )
        except Exception as reset_exc:
            print(
                "[rebase_pr] feedback-crash reset failed: "
                f"{reset_exc}",
                file=sys.stderr,
            )
        error_text = f"{type(exc).__name__}: {exc}"[:200]
        actions_log.append(
            "Review feedback step crashed; continuing with rebase-only push"
        )
        if result_meta is not None:
            result_meta["status"] = "feedback_failed"
            result_meta["error"] = error_text
        return ""

    if not step.committed:
        status = "no_changes"
        error_text = (step.error or "").strip()
        if getattr(step, "quota_exhausted", False):
            status = "feedback_quota"
            actions_log.append("Review feedback halted due to quota exhaustion")
        elif error_text and _is_agent_timeout_error(error_text):
            status = "feedback_timeout"
            actions_log.append("Review feedback timed out")
        elif error_text:
            status = "feedback_failed"
            actions_log.append("Review feedback failed (continuing with rebase)")
        else:
            no_change_summary = step.output.strip()
            if commit_conventions:
                from app.commit_conventions import strip_commit_subject_line
                no_change_summary = strip_commit_subject_line(no_change_summary)
            if _has_skipped_disposition(no_change_summary):
                status = "evaluated_no_changes"
                actions_log.append("Review feedback evaluated; no changes required")
                if result_meta is not None:
                    result_meta["summary"] = no_change_summary[:1500]
            else:
                status = "feedback_no_disposition"
                actions_log.append("Review feedback returned no changes or explanation")
        if result_meta is not None:
            result_meta["status"] = status
            result_meta["error"] = error_text
        return result_meta.get("summary", "") if result_meta is not None else ""
    if getattr(step, "self_committed", False):
        # The agent ran git commit/--amend itself, so the worktree was already
        # clean when the runner looked. Record it: the landed commit message is
        # the agent's own, not the runner's `commit_msg`.
        actions_log.append("Review feedback committed by the agent itself")
    if result_meta is not None:
        result_meta["status"] = "committed"
        result_meta["error"] = ""

    # Extract change summary from Claude's output for the PR comment
    change_summary = step.output.strip()
    if commit_conventions:
        from app.commit_conventions import strip_commit_subject_line
        change_summary = strip_commit_subject_line(change_summary)

    # Truncate overly long summaries. Keep the HEAD: the structured summary leads
    # with the APPLIED: section, so tail-trimming would drop the changes actually
    # made and leave only skipped items.
    if len(change_summary) > 1500:
        change_summary = change_summary[:1500].rstrip()

    return change_summary


def _has_skipped_disposition(summary: str) -> bool:
    """Return whether a no-change feedback response explains its decision."""
    return bool(re.search(r"(?m)^SKIPPED:\s*\n\s*-\s+\S", summary))


def _is_agent_timeout_error(error_text: str) -> bool:
    """Return True when Claude step error indicates timeout."""
    lowered = error_text.lower()
    return "timeout (" in lowered or "timed out" in lowered


def _build_rebase_recovery_guidance(project_path: str) -> str:
    """Return deterministic cleanup hints after a rebase failure."""
    branch = "unknown"
    try:
        branch = _get_current_branch(project_path)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"[rebase_pr] recovery-guidance branch detection failed: {e}",
              file=sys.stderr)

    rebase_in_progress = _has_rebase_in_progress(project_path)
    dirty = "unknown"
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=project_path,
        )
        dirty = "yes" if status.stdout.strip() else "no"
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[rebase_pr] recovery-guidance git status failed: {e}", file=sys.stderr)

    if rebase_in_progress:
        next_step = "git rebase --continue (or git rebase --abort if the resolution is wrong)"
    else:
        next_step = "git status (then commit or stash local changes before retrying /rebase)"

    return (
        "Recovery hints:\n"
        f"- branch: {branch}\n"
        f"- rebase_in_progress: {'yes' if rebase_in_progress else 'no'}\n"
        f"- working_tree_dirty: {dirty}\n"
        f"- next: {next_step}"
    )



def _run_force_push_guard(
    *,
    pre_rebase_head: str,
    target_ref: str,
    project_path: str,
    push_state: dict,
    branch: str,
    pr_number: str,
    actions_log: List[str],
    notify_fn,
) -> str:
    """Post-push content-preservation check; returns the PR-comment alert.

    *push_state* carries the accumulated push telemetry: the remote actually
    pushed to, the last successfully pushed SHA, and every pre-push remote
    observation of that remote (main push + gate re-pushes). Best-effort by
    contract (specs/components/git-github.md): a guard that cannot run logs
    the fact and returns "" — it never fails the rebase.

    The branch is already force-pushed by the time this runs, so the whole
    body is inside the best-effort boundary: an unexpected failure here must
    not turn a completed rebase into a failed mission, nor skip the PR comment
    and CI enqueue that follow.
    """
    if not pre_rebase_head:
        actions_log.append(
            "Force-push guard skipped: pre-rebase head was not captured"
        )
        return ""
    try:
        section = _build_push_guard_section(
            pre_rebase_head=pre_rebase_head,
            target_ref=target_ref,
            project_path=project_path,
            push_state=push_state,
            branch=branch,
            actions_log=actions_log,
        )
    except Exception as e:
        print(f"[rebase_pr] force-push guard failed: {e}", file=sys.stderr)
        actions_log.append(
            f"Force-push guard skipped: unexpected guard failure ({e}) — "
            f"the rebase itself is unaffected"
        )
        return ""
    if not section:
        return ""
    actions_log.append(
        "Force-push guard: original PR content was dropped, modified, or "
        "raced — see the warning at the top of this comment"
    )
    # A safety finding is an outcome, not progress: notify_outcome() delivers it
    # through the un-gated sink so it reaches the human in normal mode too, and a
    # delivery failure is logged rather than swallowed (the PR comment still
    # carries the full detail either way).
    try:
        from app.messaging_level import notify_outcome

        notify_outcome(
            f"⚠️ Force-push guard: PR #{pr_number} (`{branch}`) — the rewrite "
            f"needs attention; details in the PR comment.",
            notify_fn,
        )
    except Exception as e:
        print(
            f"[rebase_pr] force-push guard alert delivery failed: {e}",
            file=sys.stderr,
        )
    return section


def _build_push_guard_section(
    *,
    pre_rebase_head: str,
    target_ref: str,
    project_path: str,
    push_state: dict,
    branch: str,
    actions_log: List[str],
) -> str:
    """Run the check and render it; "" when there is nothing to report."""
    findings = force_push_guard.verify_content_preserved(
        pre_rebase_head,
        target_ref,
        project_path,
        pre_push_remote_heads=push_state.get("observations", []),
        push_remote=push_state.get("remote", ""),
        branch=branch,
        pushed_head=push_state.get("pushed_head", ""),
        observation_failed=bool(push_state.get("observation_failed")),
    )
    if findings is None:
        actions_log.append(
            "Force-push guard skipped: no confirmed pushed SHA, or the content "
            "check could not run (non-fatal)"
        )
        return ""
    section = force_push_guard.build_push_warning(
        findings, branch, remote=push_state.get("remote") or "origin",
    )
    if not section:
        actions_log.append(
            "Force-push guard: all original PR content verified preserved"
        )
    return section


def _checkout_pr_branch(
    branch: str,
    project_path: str,
    head_remote: Optional[str] = None,
    head_owner: str = "",
    repo: str = "",
) -> str:
    """Checkout the PR branch, fetching from the appropriate remote.

    Uses ``git checkout -B`` to create or reset the local branch,
    ensuring a stale local branch with the same name never blocks
    the checkout.

    When the PR comes from a fork that has no local remote configured,
    the fork is added as a temporary remote named ``fork-<owner>`` and
    fetched from there.

    Args:
        branch: The branch name to checkout.
        project_path: Local path to the git repository.
        head_remote: Pre-resolved remote name for the PR head (from
            ``_find_remote_for_repo``).  Tried first if given.
        head_owner: GitHub owner of the PR's head repository.  Used to
            add a temporary remote when no existing remote matches.
        repo: GitHub repository name.  Used together with *head_owner*.

    Returns:
        The remote name used for the fetch (e.g. ``"origin"`` or ``"upstream"``).
    """
    # Build ordered list of remotes to try: head_remote first, then origin/upstream
    remotes = _ordered_remotes(head_remote, cwd=project_path)

    for remote in remotes:
        try:
            _fetch_branch(remote, branch, cwd=project_path)
            # Success — use this remote
            fetch_remote = remote
            break
        except Exception as e:
            print(f"[rebase_pr] fetch from {remote} failed: {e}", file=sys.stderr)
            continue
    else:
        # None of the known remotes had the branch.
        # If we know the fork owner, add it as a temporary remote and retry.
        if head_owner and repo:
            fork_remote = f"fork-{head_owner}"
            fork_url = f"https://github.com/{head_owner}/{repo}.git"
            try:
                _run_git(
                    ["git", "remote", "add", fork_remote, fork_url],
                    cwd=project_path,
                )
            except Exception as e:
                # Remote may already exist from a previous run
                print(f"[rebase_pr] remote add {fork_remote} failed (may already exist): {e}", file=sys.stderr)
            try:
                _fetch_branch(fork_remote, branch, cwd=project_path)
                fetch_remote = fork_remote
            except Exception as e:
                raise RuntimeError(
                    f"Branch `{branch}` not found on any remote "
                    f"(tried {', '.join(remotes)} and {fork_remote})"
                ) from e
        else:
            raise RuntimeError(
                f"Branch `{branch}` not found on {' or '.join(remotes)}"
            )

    # -B creates the branch if missing, or resets it if it already exists.
    # This avoids the "branch already exists" error when a stale local
    # branch with the same name is present.
    _run_git(
        ["git", "checkout", "-B", branch, f"{fetch_remote}/{branch}"],
        cwd=project_path,
    )
    return fetch_remote


def _push_to_remote(branch: str, project_path: str, remote: str) -> dict:
    """Force-push *branch* to one remote, collecting the guard's telemetry.

    Returns the observations belonging to **this** attempt only — a tip seen
    on a remote whose push then fails was never overwritten, and reporting it
    as clobbered would send the human chasing commits that are still there.
    Raises whatever the push raises.
    """
    observations: List[str] = []
    failures: List[str] = []

    def observe() -> None:
        """Snapshot the remote tip (and fetch its objects) before overwriting it.

        Feeds the content-preservation guard so commits pushed by others
        mid-rebase can be detected and named after this push erases them.

        Never raises: this runs on the live push path (before the push, and
        again from the lease-failure hook), and the guard detects and warns —
        it must not gate the force-push. Anything unexpected is logged and
        recorded as a failed observation, so the guard reports the clobber
        check as unverified instead of the push not happening at all.
        """
        try:
            sha = force_push_guard.observe_remote_head(
                remote, branch, project_path,
            )
        except Exception as e:
            print(
                f"[rebase_pr] pre-push observation of {remote} failed: {e}",
                file=sys.stderr,
            )
            sha = None
        if sha is None:
            failures.append(remote)  # could not look — not "nothing there"
        elif sha and sha not in observations:
            observations.append(sha)

    observe()
    # The SHA we are about to publish. Captured before the push and only
    # reported on success, so a commit that never reached the remote can
    # neither skew the guard's comparison nor fake a post-push race.
    head_to_push = ""
    try:
        head_to_push = _run_git(
            ["git", "rev-parse", "HEAD"], cwd=project_path, timeout=30,
        ).strip()
    except Exception as e:
        print(f"[rebase_pr] pushed-head capture failed: {e}", file=sys.stderr)
    # A rejected --force-with-lease means the remote moved since the
    # observation above; re-observe before the plain --force clobbers it.
    _force_push(remote, branch, project_path, on_lease_failure=observe)
    return {
        "remote": remote,
        "pre_push_remote_heads": observations,
        "observation_failed": bool(failures),
        "pushed_head": head_to_push,
    }


def _push_with_fallback(
    branch: str,
    base: str,
    full_repo: str,
    pr_number: str,
    context: dict,
    project_path: str,
    head_remote: Optional[str] = None,
) -> dict:
    """Push rebased branch, always reusing the existing PR branch.

    Rebase never creates a new branch or PR — it always pushes to the
    same branch to recycle the existing pull request.  Tries *head_remote*
    first (where the PR branch lives), then ``origin`` and ``upstream``.
    Uses ``--force-with-lease`` first, then plain ``--force`` as fallback.
    """
    actions: List[str] = []
    remotes = _ordered_remotes(head_remote, cwd=project_path)
    last_error = ""

    for remote in remotes:
        try:
            pushed = _push_to_remote(branch, project_path, remote)
        except Exception as e:
            print(f"[rebase_pr] push to {remote} failed: {e}", file=sys.stderr)
            last_error = str(e)
            continue
        actions.append(f"Force-pushed `{branch}` to {remote}")
        return {"success": True, "actions": actions, "error": "", **pushed}

    return {
        "success": False,
        "actions": actions,
        "error": (
            f"Cannot push `{branch}`: all remotes rejected the push. "
            f"Check write permissions on the branch."
        ),
    }


# Section headers Claude emits in its rebase summary. Matched only when they are
# the WHOLE line (the `$` anchor), so a content bullet like "Applied the fix to X"
# is never mistaken for a header. Tolerant of surrounding bold/colon and the older
# "**Changes**" / "**Skipped**" phrasings.
_APPLIED_HDR_RE = re.compile(r"^\**\s*(?:applied|changes?)\s*:?\s*\**$", re.I)
_SKIPPED_HDR_RE = re.compile(
    r"^\**\s*(?:skipped|not\s+changed|no\s+change[sd]?)\s*:?\s*\**$", re.I
)


def _split_change_summary(change_summary: str) -> Tuple[List[str], List[str]]:
    """Split Claude's rebase summary into ``(applied, skipped)`` bullet lists.

    Recognizes the ``APPLIED:`` / ``SKIPPED:`` section headers the rebase prompt
    asks for (plus looser ``Changes`` / ``Skipped`` / ``Not changed`` variants).
    Lines before any recognized header default to *applied*, so a summary that
    omits the structure still renders as changes — backward compatible with
    older prompt outputs and non-conforming responses.
    """
    applied: List[str] = []
    skipped: List[str] = []
    bucket = applied
    for raw in change_summary.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _APPLIED_HDR_RE.match(line):
            bucket = applied
            continue
        if _SKIPPED_HDR_RE.match(line):
            bucket = skipped
            continue
        line = re.sub(r"^[-*]\s+", "", line).strip()  # drop a leading bullet marker
        if line:
            bucket.append(line)
    return applied, skipped


def _build_rebase_comment(
    pr_number: str,
    branch: str,
    base: str,
    actions_log: List[str],
    context: dict,
    diffstat: str = "",
    ci_section: str = "",
    change_summary: str = "",
    feedback_failed: bool = False,
    feedback_reason: str = "",
    show_transition_notice: bool = False,
    push_guard_section: str = "",
) -> str:
    """Build a structured markdown comment summarizing the rebase.

    ``feedback_failed`` is supplied by the caller from the structured
    feedback status (``feedback_timeout`` / ``feedback_failed``) rather than
    inferred from log prose, so a reworded log line cannot silently revert
    the summary to "Simple rebase". ``feedback_reason`` carries a short
    human-readable cause (e.g. "the feedback step timed out") that is surfaced
    in a prominent, non-collapsed warning callout so the dropped feedback is
    obvious instead of being buried in the collapsed Actions section.

    Sections:
    1. Summary — rebase type (simple vs. with adjustments) + one-liner
    2. Changes — explicit list of changes beyond the rebase itself
    3. Stats — diff summary (files, insertions, deletions)
    4. Actions — pipeline steps performed
    5. CI — test / CI status
    """
    has_feedback = bool(change_summary.strip()) or any(
        "applied review feedback" in a.lower() for a in actions_log
    )
    has_conflicts = any("conflict" in a.lower() for a in actions_log)

    # Split the reviewer-feedback summary into what was actually changed vs. what
    # was deliberately left alone (already fixed / advisory / disagreed). Rendered
    # as two distinct sections so an unchanged point is never presented as a change.
    applied_summary, skipped_summary = _split_change_summary(change_summary)
    change_items = _extract_change_items(actions_log, "\n".join(applied_summary))

    # ── 1. Summary ──────────────────────────────────────────────────
    if has_feedback:
        rebase_type = "Rebase with requested adjustments"
        if change_items:
            summary_line = (
                f"Branch `{branch}` was rebased onto `{base}` and review "
                f"feedback was applied."
            )
        elif skipped_summary:
            # Feedback was reviewed but nothing needed changing — say so plainly
            # instead of implying a fix was made.
            summary_line = (
                f"Branch `{branch}` was rebased onto `{base}`. No code changes "
                f"were needed — each reviewer point was already addressed or "
                f"advisory (see “Not changed” below)."
            )
        else:
            summary_line = (
                f"Branch `{branch}` was rebased onto `{base}` and review "
                f"feedback was applied."
            )
    elif feedback_failed:
        rebase_type = "Rebase completed; review feedback not applied"
        summary_line = (
            f"Branch `{branch}` was rebased onto `{base}`, but review "
            f"feedback could not be applied automatically."
        )
    elif has_conflicts:
        rebase_type = "Rebase with conflict resolution"
        summary_line = (
            f"Branch `{branch}` was rebased onto `{base}` with "
            f"automatic conflict resolution."
        )
    else:
        rebase_type = "Simple rebase"
        summary_line = (
            f"Branch `{branch}` was rebased onto `{base}` — "
            f"no additional changes were needed."
        )

    parts = [f"## {rebase_type}\n"]
    parts.append(f"{summary_line}\n")

    # Force-push content-preservation warning first — when present it is the
    # single most important thing on the comment (specs/skills/rebase.md).
    if push_guard_section:
        parts.append(push_guard_section + "\n")

    # Transient notice announcing the /rebase default change (bare rebase no
    # longer applies review feedback). Only on the bare-rebase path, and only
    # while the transition window is open.
    if show_transition_notice:
        from app import rebase_transition
        parts.append(rebase_transition.pr_comment_notice() + "\n")

    # Prominent, non-collapsed warning so dropped reviewer feedback is
    # impossible to miss (the cause otherwise hides in the <details> block).
    if feedback_failed:
        reason = feedback_reason.strip() or "the feedback step did not complete"
        parts.append(
            build_alert(
                "WARNING",
                f"**Review feedback was NOT applied** — {reason}. The reviewer "
                "comments above still need to be addressed: re-run `/rebase --fix` "
                "or apply them manually.",
            )
            + "\n"
        )

    # ── 2. Changes / Not changed ────────────────────────────────────
    # Only include when there are meaningful changes beyond rebasing.
    if change_items:
        parts.append("### Changes applied\n")
        parts.extend(f"- {item}" for item in change_items)
        parts.append("")
    # Points the reviewer raised that were intentionally left as-is, in their own
    # clearly-labeled section — so "already fixed" reads as skipped, not applied.
    if skipped_summary:
        parts.append("### Not changed (and why)\n")
        parts.extend(f"- {item}" for item in skipped_summary)
        parts.append("")

    # ── 3. Stats ────────────────────────────────────────────────────
    if diffstat:
        parts.append("### Stats\n")
        parts.append(f"```\n{diffstat}\n```\n")

    # ── 4. Actions ──────────────────────────────────────────────────
    # Filter mechanical pipeline noise
    meaningful_actions = [
        a for a in actions_log
        if not a.startswith("Read PR comments")
        and not a.startswith("Commented on PR")
    ]
    if meaningful_actions:
        parts.append("<details>\n<summary>Actions performed</summary>\n")
        parts.extend(f"- {a}" for a in meaningful_actions)
        parts.append("\n</details>\n")

    # ── 5. CI ───────────────────────────────────────────────────────
    if ci_section:
        parts.append("### CI status\n")
        parts.append(f"{ci_section}\n")

    parts.append("---\n_Automated by Kōan_")

    return "\n".join(parts)


def _extract_change_items(
    actions_log: List[str],
    change_summary: str,
) -> List[str]:
    """Extract meaningful change descriptions for the Changes section.

    Combines review-feedback changes (from Claude's change_summary) with
    notable pipeline actions (conflict resolution, CI fixes, etc.).
    """
    items: List[str] = []

    # Include Claude's change summary — split on newlines for multi-line summaries
    if change_summary:
        for line in change_summary.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip leading "- " if present — we add our own
            if line.startswith("- "):
                line = line[2:]
            if line:
                items.append(line)

    # Add notable pipeline actions (not already covered by change_summary)
    for action in actions_log:
        low = action.lower()
        if "conflict" in low and "resolution" in low:
            items.append(f"**Conflict resolution**: {action}")
        elif "ci fix" in low and "applied" in low:
            items.append(f"**CI fix**: {action}")
        elif "pre-push ci fix applied" in low:
            items.append("**Pre-push CI fix**: resolved failing checks before push")

    return items


def _is_conflict_failure(summary: str) -> bool:
    """Check if a rebase failure summary indicates a git conflict."""
    return (
        "Rebase conflict" in summary
        or "Could not resolve conflicts" in summary
        or "[conflict_unresolved]" in summary
    )


# ---------------------------------------------------------------------------
# CLI entry point — python3 -m app.rebase_pr <url> --project-path <path>
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for rebase_pr.

    On rebase conflict, automatically falls back to recreate_pr which
    creates a fresh branch from upstream and reimplements the feature.

    Returns exit code (0 = success, 1 = failure).
    """
    import argparse
    import sys

    from app.github_url_parser import parse_pr_url as _parse_url

    parser = argparse.ArgumentParser(
        description="Rebase a GitHub PR onto its target branch."
    )
    parser.add_argument("url", help="GitHub PR URL")
    parser.add_argument(
        "--project-path", required=True,
        help="Local path to the project repository",
    )
    parser.add_argument(
        "--min-severity",
        choices=list(SEVERITY_LEVELS),
        default=None,
        help=(
            "Only address review issues at this severity level or above. "
            "E.g. --min-severity warning skips suggestions."
        ),
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        default=False,
        help=(
            "Also address PR review feedback after rebasing. Without --fix, "
            "a bare rebase only rebases onto the target branch."
        ),
    )
    parser.add_argument(
        "--feedback-context",
        default="",
        help="Human request to prioritize while applying review feedback.",
    )
    cli_args = parser.parse_args(argv)

    try:
        owner, repo, pr_number = _parse_url(cli_args.url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    skills_base = Path(__file__).resolve().parent.parent / "skills" / "core"

    success, summary = run_rebase(
        owner, repo, pr_number, cli_args.project_path,
        skill_dir=skills_base / "rebase",
        min_severity=cli_args.min_severity,
        fix=cli_args.fix,
        feedback_context=cli_args.feedback_context,
    )

    if not success and _is_conflict_failure(summary):
        # Check PR state before falling back — recreate only works on open PRs
        try:
            ctx = fetch_pr_context(owner, repo, pr_number, cli_args.project_path)
            pr_state = ctx.get("state", "").upper()
        except Exception as e:
            print(f"[rebase_pr] PR state check failed, proceeding with recreate: {e}", file=sys.stderr)
            pr_state = ""

        if pr_state in ("MERGED", "CLOSED"):
            print(f"{summary}\nCannot fall back to /recreate: PR #{pr_number} is {pr_state.lower()}.")
            return 1

        print(f"{summary}\nFalling back to /recreate...")
        from app.recreate_pr import run_recreate

        recreate_ok, recreate_summary = run_recreate(
            owner, repo, pr_number, cli_args.project_path,
            skill_dir=skills_base / "recreate",
        )
        print(recreate_summary)
        return 0 if recreate_ok else 1

    print(summary)
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
