"""Shared GitHub CLI (gh) wrapper for all Kōan modules.

Centralizes all `gh` CLI interactions so that consumers don't reinvent
subprocess plumbing.  Auth is handled externally by ``github_auth.py``
which sets ``GH_TOKEN`` — this module has no auth logic.
"""

import json
import re
import subprocess
import time
from typing import Dict, Optional

from app.retry import retry_with_backoff, is_gh_transient


class SSOAuthRequired(RuntimeError):
    """Raised when a GitHub API call fails due to missing SSO authorization.

    The token is valid but not authorized for the target organization's
    SAML SSO policy.  The user must re-authorize with:
        gh auth refresh -h github.com -s read:org
    """

    def __init__(self, stderr_text: str):
        remediation = "gh auth refresh -h github.com -s read:org"
        super().__init__(
            f"GitHub API 403: SSO/SAML authorization required. "
            f"Run: {remediation}\n"
            f"Details: {stderr_text[:300]}"
        )
        self.stderr_text = stderr_text


def _is_sso_error(stderr: str) -> bool:
    """Check if a gh CLI stderr message indicates an SSO/SAML auth failure."""
    upper = stderr.upper()
    return "SSO" in upper or "SAML" in upper

# Cached GitHub username (from gh api user fallback).
# None = not yet queried, "" = query failed.
_cached_gh_username = None


def run_gh(*args, cwd=None, timeout=30, stdin_data=None):
    """Run a ``gh`` CLI command and return stripped stdout.

    Args:
        *args: Arguments passed after ``gh`` (e.g. ``"pr", "view", "1"``).
        cwd: Working directory for the subprocess.
        timeout: Seconds before the command is killed.
        stdin_data: Optional string passed to the process via stdin.

    Returns:
        Stripped stdout string.

    Raises:
        RuntimeError: If the ``gh`` command exits with a non-zero code.
    """
    cmd = ["gh", *args]
    stdin_kwarg = {"input": stdin_data} if stdin_data is not None else {"stdin": subprocess.DEVNULL}

    def _invoke():
        result = subprocess.run(
            cmd, **stdin_kwarg,
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        if result.returncode != 0:
            if _is_sso_error(result.stderr):
                raise SSOAuthRequired(result.stderr)
            raise RuntimeError(
                f"gh failed: {' '.join(cmd[:4])}... — {result.stderr[:300]}"
            )
        return result.stdout.strip()

    from app.security_audit import GIT_OPERATION, _redact_list, log_event

    try:
        result = retry_with_backoff(
            _invoke,
            retryable=(RuntimeError, OSError, subprocess.TimeoutExpired),
            is_transient=is_gh_transient,
            label=f"gh {' '.join(args[:2])}",
        )
        log_event(GIT_OPERATION, details={
            "cmd": _redact_list(cmd),
            "result": result[:500] if result else "",
        })
        return result
    except Exception:
        log_event(GIT_OPERATION, details={
            "cmd": _redact_list(cmd),
        }, result="failure")
        raise


def pr_create(title, body, draft=True, base=None, repo=None, head=None, cwd=None):
    """Create a pull request via ``gh pr create``.

    Args:
        title: PR title.
        body: PR body (markdown).
        draft: If True (default), create a draft PR.
        base: Target branch (omit to let ``gh`` pick the default).
        repo: Repository in ``owner/repo`` format (omit to use local repo).
        head: Branch containing the changes (omit to use current branch).
        cwd: Working directory (must be inside a git repo).

    Returns:
        The URL of the newly created PR.
    """
    from app.leak_detector import scan_and_redact

    title = scan_and_redact(title, context="PR title")
    body = scan_and_redact(body, context="PR body")
    args = ["pr", "create", "--title", title, "--body", body]
    if draft:
        args.append("--draft")
    if base:
        args.extend(["--base", base])
    if repo:
        args.extend(["--repo", repo])
    if head:
        args.extend(["--head", head])
    return run_gh(*args, cwd=cwd)


def issue_create(title, body, labels=None, repo=None, cwd=None):
    """Create a GitHub issue via ``gh issue create``.

    Args:
        title: Issue title.
        body: Issue body (markdown).
        labels: Optional list of label names.
        repo: Repository in ``owner/repo`` format (omit to use local repo).
        cwd: Working directory (must be inside a git repo).

    Returns:
        The URL of the newly created issue.
    """
    from app.leak_detector import scan_and_redact

    title = scan_and_redact(title, context="Issue title")
    body = scan_and_redact(body, context="Issue body")
    args = ["issue", "create", "--title", title, "--body", body]
    if labels:
        args.extend(["--label", ",".join(labels)])
    if repo:
        args.extend(["--repo", repo])
    return run_gh(*args, cwd=cwd)


def api(endpoint, method="GET", jq=None, input_data=None, cwd=None,
        extra_args=None):
    """Call ``gh api`` for lower-level GitHub API access.

    Args:
        endpoint: API path (e.g. ``repos/owner/repo/pulls/1/comments``).
        method: HTTP method (default GET).
        jq: Optional jq filter applied server-side.
        input_data: If provided, passed via stdin (``-F body=@-``).
        cwd: Working directory.
        extra_args: Additional arguments for ``gh api``.

    Returns:
        Stripped stdout string.
    """
    args = ["api", endpoint]
    if method and method.upper() != "GET":
        args.extend(["-X", method.upper()])
    if jq:
        args.extend(["--jq", jq])
    if extra_args:
        args.extend(extra_args)
    if input_data is not None:
        args.extend(["-F", "body=@-"])

    return run_gh(*args, cwd=cwd, stdin_data=input_data)


def fetch_issue_with_comments(owner, repo, issue_number):
    """Fetch issue title, body and comments via gh API.

    Args:
        owner: Repository owner.
        repo: Repository name.
        issue_number: Issue number (as string or int).

    Returns:
        Tuple of (title, body, comments) where comments is a list of dicts
        with keys: author, date, body.

    Raises:
        RuntimeError: If the gh API call fails.
    """
    issue_json = api(
        f"repos/{owner}/{repo}/issues/{issue_number}",
        jq='{"title": .title, "body": .body}',
    )
    try:
        data = json.loads(issue_json)
        title = data.get("title", "")
        body = data.get("body", "")
    except (json.JSONDecodeError, TypeError):
        title = ""
        body = issue_json

    comments_json = api(
        f"repos/{owner}/{repo}/issues/{issue_number}/comments",
        jq='[.[] | {author: .user.login, date: .created_at, body: .body}]',
    )

    try:
        comments = json.loads(comments_json)
        if not isinstance(comments, list):
            comments = []
    except (json.JSONDecodeError, TypeError):
        comments = []

    return title, body, comments


def get_gh_username() -> str:
    """Return the GitHub username to use for PR author filtering.

    Resolution order:
    1. ``GITHUB_USER`` env var (via ``github_auth.get_github_user()``)
    2. ``gh api user --jq .login`` (cached after first call)

    Returns empty string if neither source yields a username.
    """
    global _cached_gh_username

    from app.github_auth import get_github_user
    env_user = get_github_user()
    if env_user:
        return env_user

    # Fallback: ask gh who is authenticated
    if _cached_gh_username is not None:
        return _cached_gh_username

    try:
        _cached_gh_username = run_gh("api", "user", "--jq", ".login", timeout=15)
    except (RuntimeError, subprocess.SubprocessError, OSError):
        _cached_gh_username = ""

    return _cached_gh_username


def detect_parent_repo(project_path: str) -> Optional[str]:
    """Detect if the local repo is a fork and return the parent owner/repo.

    Calls ``gh repo view --json parent`` to check if the current repository
    is a fork.  Returns the parent in ``owner/repo`` format, or ``None``
    if the repo is not a fork, has no parent, or on any error.

    Args:
        project_path: Path to the local git repository.

    Returns:
        Parent repository slug (``owner/repo``) or ``None``.
    """
    try:
        output = run_gh(
            "repo", "view", "--json", "parent",
            "--jq", '.parent.owner.login + "/" + .parent.name',
            cwd=project_path, timeout=15,
        )
        # gh returns empty or "null/null" when parent is null
        if not output or output == "/" or "null" in output:
            return None
        # Validate owner/repo format
        parts = output.strip().split("/")
        if len(parts) == 2 and all(parts):
            return output.strip()
        return None
    except (RuntimeError, subprocess.SubprocessError, OSError):
        return None


_GITHUB_URL_RE = re.compile(
    r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$"
)


def _parse_remote_url(url: str) -> Optional[str]:
    """Extract ``owner/repo`` from a GitHub remote URL."""
    m = _GITHUB_URL_RE.search(url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


def _get_remote_url(project_path: str, remote: str) -> Optional[str]:
    """Return the URL of a git remote, or None."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            capture_output=True, text=True, timeout=5,
            cwd=project_path, stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _upstream_remote_repo(project_path: str) -> Optional[str]:
    """Return ``owner/repo`` from the ``upstream`` git remote if it
    differs from ``origin``.  Returns ``None`` when there's no
    ``upstream`` remote or it points to the same repo as ``origin``.
    """
    upstream_url = _get_remote_url(project_path, "upstream")
    if not upstream_url:
        return None
    upstream_repo = _parse_remote_url(upstream_url)
    if not upstream_repo:
        return None

    # Only return upstream if it's different from origin
    origin_url = _get_remote_url(project_path, "origin")
    if origin_url:
        origin_repo = _parse_remote_url(origin_url)
        if origin_repo and origin_repo.lower() == upstream_repo.lower():
            return None

    return upstream_repo


def resolve_target_repo(project_path: str) -> Optional[str]:
    """Return the upstream ``owner/repo`` if working in a fork, else ``None``.

    Resolution order:
    1. GitHub fork parent (via ``gh repo view --json parent``)
    2. Git ``upstream`` remote (if it differs from ``origin``)

    When the local repo is a fork the returned value should be used as
    the ``--repo`` argument for ``gh pr create`` / ``gh issue create``
    so that operations target the upstream repository instead of the fork.
    """
    parent = detect_parent_repo(project_path)
    if parent:
        return parent

    # Fallback: check if there's a distinct 'upstream' git remote
    return _upstream_remote_repo(project_path)


# TTL cache for count_open_prs results (avoids repeated gh CLI calls)
_pr_count_cache: Dict[str, tuple] = {}  # key -> (count, timestamp)
_PR_COUNT_TTL = 300  # 5 minutes


def cached_count_open_prs(github_url: str, author: str) -> int:
    """count_open_prs with a 5-minute TTL cache.

    Args:
        github_url: Repository in ``owner/repo`` format.
        author: GitHub username to filter by.

    Returns:
        Number of open PRs, or ``-1`` on error.
        Errors are cached too to avoid hammering gh on repeated failures.
    """
    key = f"{github_url}:{author}"
    now = time.monotonic()
    cached = _pr_count_cache.get(key)
    if cached and (now - cached[1]) < _PR_COUNT_TTL:
        return cached[0]

    result = count_open_prs(github_url, author)
    _pr_count_cache[key] = (result, now)
    return result


def batch_count_open_prs(repos: list, author: str) -> Dict[str, int]:
    """Count open PRs across multiple repos in a single GraphQL call.

    Uses GitHub's ``search`` API with aliased queries to fetch PR counts
    for all repos at once, instead of one ``gh pr list`` per repo.

    Args:
        repos: List of repository identifiers in ``owner/repo`` format.
        author: GitHub username to filter by.

    Returns:
        Dict mapping ``owner/repo`` → open PR count.
        Repos that errored individually are omitted from the result.
        On total failure, returns an empty dict (caller should fall back).
    """
    if not repos or not author:
        return {}

    # Deduplicate while preserving association
    unique_repos = list(dict.fromkeys(repos))

    # Build aliased GraphQL query
    fragments = []
    alias_map = {}  # alias -> repo
    for i, repo in enumerate(unique_repos):
        alias = f"r{i}"
        alias_map[alias] = repo
        # Escape quotes in repo name (defensive)
        safe_repo = repo.replace('"', '\\"')
        safe_author = author.replace('"', '\\"')
        fragments.append(
            f'{alias}: search(query: "repo:{safe_repo} is:pr is:open '
            f'author:{safe_author}", type: ISSUE, first: 1) {{ issueCount }}'
        )

    query = "query { " + " ".join(fragments) + " }"

    try:
        output = run_gh(
            "api", "graphql",
            "-f", f"query={query}",
            timeout=20,
        )
        data = json.loads(output)
        results = {}
        now = time.monotonic()
        for alias, repo in alias_map.items():
            node = data.get("data", {}).get(alias)
            if node is not None:
                count = node.get("issueCount", -1)
                results[repo] = count
                # Populate the TTL cache so cached_count_open_prs benefits
                cache_key = f"{repo}:{author}"
                _pr_count_cache[cache_key] = (count, now)
        return results
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError,
            OSError, TypeError, KeyError):
        return {}


def count_open_prs(repo: str, author: str, cwd: str = None) -> int:
    """Count open pull requests by a specific author in a repository.

    Args:
        repo: Repository in ``owner/repo`` format.
        author: GitHub username to filter by. If empty, returns ``-1``.
        cwd: Optional working directory.

    Returns:
        Number of open PRs, or ``-1`` on error (gh unavailable, auth
        failure, network error).
    """
    if not author:
        return -1

    try:
        output = run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--author", author,
            "--json", "number",
            "--jq", "length",
            cwd=cwd, timeout=15,
        )
        return int(output)
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, TypeError):
        return -1
