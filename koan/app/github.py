"""Shared GitHub CLI (gh) wrapper for all Kōan modules.

Centralizes all `gh` CLI interactions so that consumers don't reinvent
subprocess plumbing.  Auth is handled externally by ``github_auth.py``
which sets ``GH_TOKEN`` — this module has no auth logic.
"""

import json
import logging
import re
import subprocess
import time
from typing import Dict, List, Optional

from app.retry import (
    retry_with_backoff,
    is_gh_transient,
    is_gh_secondary_rate_limit,
    parse_retry_after,
)


# Bot usernames whose @mentions should be escaped in GitHub comments to
# avoid triggering automated bot responses.
_BOT_USERNAMES = ('copilot', 'dependabot', 'github-actions')

# Regex to match bare @bot mentions (case-insensitive), with negative
# lookbehind/lookahead to skip already-backtick-escaped variants.
_BOT_MENTION_RE = re.compile(
    r'(?<!`)@(' + '|'.join(re.escape(u) for u in _BOT_USERNAMES) + r')(?![\w-])(?!`)',
    re.IGNORECASE,
)


def sanitize_github_comment(text: Optional[str]) -> Optional[str]:
    """Escape bare bot @mentions so GitHub doesn't trigger automated bots.

    Replaces ``@copilot``, ``@dependabot``, ``@github-actions`` (any
    capitalisation) with backtick-escaped variants unless already enclosed
    in backticks.  Safe to call on any string including empty strings and
    ``None`` values.
    """
    if not text:
        return text
    return _BOT_MENTION_RE.sub(r'`@\1`', text)


# ---------------------------------------------------------------------------
# Issue/PR cross-reference escaping
# ---------------------------------------------------------------------------
#
# GitHub auto-links any bare ``#123`` (and ``owner/repo#123``) in comment prose
# to an issue or PR in the target repository. Bot comments that number their own
# items — a review's "warning #5", a rebase summary echoing "reviewer #7" — then
# render as links to unrelated issues, which is actively misleading. Escaping the
# hash keeps the reference readable while breaking the auto-link.

# Regions whose contents must never be rewritten: fenced code blocks, inline code
# spans, and URLs (a ``…/pull/12#issuecomment-9`` fragment or a ``#rrggbb``-style
# anchor must survive verbatim).
_PROTECTED_SPAN_RE = re.compile(
    r"```.*?```"            # fenced code block (``` … ```)
    r"|~~~.*?~~~"           # fenced code block (~~~ … ~~~)
    r"|`[^`\n]*`"           # inline code span
    r"|<https?://[^>\s]+>"  # autolink
    r"|https?://\S+",       # bare or markdown-target URL
    re.DOTALL,
)

# A bare cross-reference: ``#`` followed only by digits. ``\b`` keeps hex colors
# (``#1f2937``) and heading markers (``## Stats``) out, and the ``&`` lookbehind
# leaves HTML entities such as ``&#39;`` alone.
_ISSUE_REF_RE = re.compile(r"(?<!&)#(\d+)\b")

# Fullwidth number sign (U+FF03) — visually a hash, but not a GitHub reference.
FULLWIDTH_HASH = "＃"


def escape_issue_refs(text: Optional[str]) -> Optional[str]:
    """Neutralize bare ``#123`` refs so GitHub does not auto-link them.

    Replaces the ASCII ``#`` of every bare numeric cross-reference with the
    fullwidth number sign ``＃`` (U+FF03), outside of code spans, fenced code
    blocks and URLs. Use on generated bot prose that numbers its own items;
    do NOT use on text where ``#123`` is a deliberate link to a real issue.
    """
    if not text:
        return text

    def _escape(chunk: str) -> str:
        return _ISSUE_REF_RE.sub(FULLWIDTH_HASH + r"\1", chunk)

    out: List[str] = []
    pos = 0
    for match in _PROTECTED_SPAN_RE.finditer(text):
        out.append(_escape(text[pos:match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_escape(text[pos:]))
    return "".join(out)


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


def run_gh(
    *args, cwd=None, timeout=30, stdin_data=None, idempotent=True, max_attempts=None,
):
    """Run a ``gh`` CLI command and return stripped stdout.

    Args:
        *args: Arguments passed after ``gh`` (e.g. ``"pr", "view", "1"``).
        cwd: Working directory for the subprocess.
        timeout: Seconds before the command is killed.
        stdin_data: Optional string passed to the process via stdin.
        idempotent: Deprecated — secondary rate limits are now never
            retried (they indicate abuse and retrying escalates GitHub's
            response).  Kept for backward compatibility.
        max_attempts: Override retry budget (default 3).  Use 1 for
            best-effort polling paths where timeout retries would stall
            a hot loop (e.g. review-comment dispatch).

    Returns:
        Stripped stdout string.

    Raises:
        RuntimeError: If the ``gh`` command exits with a non-zero code.
        subprocess.TimeoutExpired: If the subprocess exceeds ``timeout``
            after exhausting retries.
    """
    cmd = ["gh", *args]
    stdin_kwarg = {"input": stdin_data} if stdin_data is not None else {"stdin": subprocess.DEVNULL}

    def _invoke():
        result = subprocess.run(
            cmd, **stdin_kwarg,
            capture_output=True, timeout=timeout, cwd=cwd,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            if _is_sso_error(result.stderr):
                raise SSOAuthRequired(result.stderr)
            raise RuntimeError(
                f"gh failed: {' '.join(cmd[:4])}... — {result.stderr[:300]}"
            )
        return result.stdout.strip()

    from app.security_audit import GIT_OPERATION, _redact_list, log_event

    retry_kwargs = {
        "retryable": (RuntimeError, OSError, subprocess.TimeoutExpired),
        "is_transient": is_gh_transient,
        "non_retryable": is_gh_secondary_rate_limit,
        "get_retry_delay": parse_retry_after,
        "label": f"gh {' '.join(args[:2])}",
    }
    if max_attempts is not None:
        retry_kwargs["max_attempts"] = max_attempts

    try:
        result = retry_with_backoff(
            _invoke,
            **retry_kwargs,
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
    return run_gh(*args, cwd=cwd, idempotent=False)


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
    return run_gh(*args, cwd=cwd, idempotent=False)


AUDIT_ISSUE_MARKER = "Created by Kōan from audit session"


def list_open_issues(
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    body_contains: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """List currently-open issues on a repository.

    Wraps ``gh issue list --state open --json number,title,url,body``.
    When ``body_contains`` is provided, only issues whose body contains
    that substring are returned — useful for filtering down to issues
    created by a specific tool/marker.

    Returns ``[]`` on any error (safe default for callers performing
    dedup checks — failing to fetch should not block issue creation).

    Args:
        repo: Repository in ``owner/repo`` format (omit for local repo).
        cwd: Working directory (must be inside a git repo when ``repo``
            is None).
        body_contains: Optional substring to filter issue bodies.
        limit: Maximum number of issues to fetch (default 200).

    Returns:
        List of ``{"number", "title", "url", "body"}`` dicts.
    """
    args = [
        "issue", "list",
        "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,url,body",
    ]
    if repo:
        args.extend(["--repo", repo])
    try:
        output = run_gh(*args, cwd=cwd)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        logging.getLogger(__name__).warning(
            "list_open_issues failed: %s", exc, exc_info=True,
        )
        return []
    if not output:
        return []
    try:
        issues = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(issues, list):
        return []

    result = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        body = item.get("body") or ""
        if body_contains and body_contains not in body:
            continue
        result.append({
            "number": item.get("number"),
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "body": body,
        })
    return result


def list_open_audit_issues(
    repo: Optional[str] = None,
    cwd: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """List open issues that were created by a previous Kōan audit run.

    Filters ``list_open_issues()`` by the audit marker embedded in
    every audit-created issue body. Used by the audit pipeline to
    avoid duplicating findings already tracked on the repo.

    Returns ``[]`` on any failure (callers fall back to creating new
    issues — duplicates are recoverable, missed audits are not).
    """
    return list_open_issues(
        repo=repo, cwd=cwd,
        body_contains=AUDIT_ISSUE_MARKER,
        limit=limit,
    )


def issue_edit(number, body, cwd=None, repo=None):
    """Update a GitHub issue body via ``gh issue edit``.

    Args:
        number: Issue number (string or int).
        body: New body text (markdown).
        cwd: Working directory (must be inside a git repo).
        repo: Optional ``owner/repo`` to target (otherwise inferred from cwd).
    """
    from app.leak_detector import scan_and_redact

    body = scan_and_redact(body, context="Issue body")
    args = ["issue", "edit", str(number), "--body", body]
    if repo:
        args.extend(["--repo", repo])
    return run_gh(*args, cwd=cwd, idempotent=False)


def set_commit_status(repo, sha, state, *, context="koan/mission",
                      description="", target_url=None, cwd=None):
    """POST a commit status on a branch head via ``gh api`` (raw JSON body).

    Mirrors ``security_advisory_report``'s raw-body transport. ``description``
    is capped at GitHub's 140-char limit. Lets ``RuntimeError`` propagate so
    the best-effort caller can degrade.

    Args:
        repo: Repository in ``owner/repo`` format.
        sha: Commit SHA the status is attached to.
        state: One of ``pending``, ``success``, ``failure``, ``error``.
        context: Status context label (default ``koan/mission``).
        description: Short human-readable description (truncated to 140 chars).
        target_url: Optional URL the status links to.
        cwd: Optional working directory.
    """
    payload = {"state": state, "context": context,
               "description": (description or "")[:140]}
    if target_url:
        payload["target_url"] = target_url
    return api(f"repos/{repo}/statuses/{sha}", method="POST",
               input_data=json.dumps(payload), raw_body=True, cwd=cwd)


def add_issue_label(repo, number, label, cwd=None):
    """Add a label to an issue via ``gh issue edit --add-label`` (idempotent)."""
    return run_gh("issue", "edit", str(number), "--repo", repo,
                  "--add-label", label, cwd=cwd)


def remove_issue_label(repo, number, label, cwd=None):
    """Remove a label from an issue via ``gh issue edit --remove-label``."""
    return run_gh("issue", "edit", str(number), "--repo", repo,
                  "--remove-label", label, cwd=cwd)


def ensure_label(repo, label, *, color="fbca04",
                 description="Kōan is working on this issue", cwd=None):
    """Create-or-update a repo label idempotently via ``gh label create --force``."""
    return run_gh("label", "create", label, "--repo", repo, "--color", color,
                  "--description", description, "--force", cwd=cwd)


def api(endpoint, method="GET", jq=None, input_data=None, cwd=None,
        extra_args=None, timeout=30, raw_body=False):
    """Call ``gh api`` for lower-level GitHub API access.

    Args:
        endpoint: API path (e.g. ``repos/owner/repo/pulls/1/comments``).
        method: HTTP method (default GET).
        jq: Optional jq filter applied server-side.
        input_data: If provided, passed via stdin. Uses ``--input -``
            when ``raw_body=True`` (sends stdin as the raw HTTP body),
            otherwise uses ``-F body=@-`` (wraps stdin in a ``body`` field).
        cwd: Working directory.
        extra_args: Additional arguments for ``gh api``.
        timeout: Seconds before the subprocess is killed (default 30).
        raw_body: When True, send input_data as the raw HTTP request body
            via ``--input -`` instead of wrapping in ``-F body=@-``.

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
        if raw_body:
            args.extend(["--input", "-"])
        else:
            args.extend(["-F", "body=@-"])

    return run_gh(*args, cwd=cwd, stdin_data=input_data, timeout=timeout)


def fetch_issue_state(owner, repo, issue_number):
    """Fetch the state of a GitHub issue (open/closed).

    Returns:
        The issue state string (e.g. "open", "closed"), or "open" on error.
    """
    try:
        result = api(
            f"repos/{owner}/{repo}/issues/{issue_number}",
            jq=".state",
        )
        state = result.strip().strip('"')
        return state if state in ("open", "closed") else "open"
    except Exception as e:
        logging.getLogger(__name__).warning(
            "fetch_issue_state failed: %s", e, exc_info=True,
        )
        return "open"


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
        title = data.get("title") or ""
        body = data.get("body") or ""
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
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        logging.getLogger(__name__).warning(
            "get_gh_username failed: %s", exc, exc_info=True,
        )
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


def origin_repo(project_path: str) -> Optional[str]:
    """Return ``owner/repo`` parsed from the ``origin`` git remote URL.

    Reflects the actual push target (the fork, in a fork workflow), unlike
    ``gh repo view`` which resolves to the upstream/base repo when an
    ``upstream`` remote is present. Returns ``None`` when there's no origin
    remote or its URL can't be parsed as a GitHub slug.
    """
    url = _get_remote_url(project_path, "origin")
    if url:
        return _parse_remote_url(url)
    return None


_UNSET = object()


def _config_target_repo(
    project_path: str, project_name: str,
) -> object:
    """Check ``submit_to_repository.repo`` in projects.yaml.

    Returns the configured target repo, ``None`` when origin already IS
    the canonical repo, or the ``_UNSET`` sentinel when no config exists.
    """
    import os
    koan_root = os.environ.get("KOAN_ROOT", "")
    if not koan_root:
        return _UNSET
    try:
        from app.projects_config import (
            load_projects_config, get_project_submit_to_repository,
        )
        config = load_projects_config(koan_root)
        if not config:
            return _UNSET
        submit_cfg = get_project_submit_to_repository(config, project_name)
        configured_repo = submit_cfg.get("repo")
        if not configured_repo:
            return _UNSET
        origin_url = _get_remote_url(project_path, "origin")
        if origin_url:
            origin_slug = _parse_remote_url(origin_url)
            if origin_slug and origin_slug.lower() == configured_repo.lower():
                return None
        return configured_repo
    except Exception as exc:
        import logging
        logging.getLogger(__name__).debug("_config_target_repo failed: %s", exc)
        return _UNSET


def resolve_target_repo(
    project_path: str, *, project_name: str = "",
) -> Optional[str]:
    """Return the upstream ``owner/repo`` if working in a fork, else ``None``.

    Resolution order:
    0. ``submit_to_repository.repo`` from projects.yaml (when *project_name*
       is provided).  If the configured repo matches ``origin``, returns
       ``None`` — origin IS the canonical repo, not a fork.
    1. GitHub fork parent (via ``gh repo view --json parent``)
    2. Git ``upstream`` remote (if it differs from ``origin``)

    When the local repo is a fork the returned value should be used as
    the ``--repo`` argument for ``gh pr create`` / ``gh issue create``
    so that operations target the upstream repository instead of the fork.
    """
    if project_name:
        configured = _config_target_repo(project_path, project_name)
        if configured is not _UNSET:
            return configured

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


def list_open_pr_branches(repo: str, author: str, cwd: str = None) -> List[str]:
    """List branch names of open PRs by a specific author in a repository.

    Args:
        repo: Repository in ``owner/repo`` format.
        author: GitHub username to filter by. If empty, returns ``[]``.
        cwd: Optional working directory.

    Returns:
        Sorted list of branch names (headRefName) for open PRs.
        Returns empty list on error.
    """
    if not author:
        return []

    try:
        output = run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--author", author,
            "--json", "headRefName",
            cwd=cwd, timeout=15,
        )
        prs = json.loads(output) if output else []
        if not isinstance(prs, list):
            return []
        return sorted({
            pr["headRefName"]
            for pr in prs
            if isinstance(pr, dict) and pr.get("headRefName")
        })
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError,
            TypeError, KeyError):
        return []


def find_bot_comment(
    owner: str, repo: str, pr_number: int, marker: str,
    bot_username: str = "",
    prefer_newest: bool = False,
) -> Optional[dict]:
    """Search issue comments on a PR for a comment containing ``marker``.

    Only searches conversation (issue-level) comments, not inline review
    comments.  Returns the first matching comment, or ``None`` if absent.

    When ``bot_username`` is provided, only a comment authored by that account
    is returned.  This matters when the review bot account changes between runs
    (e.g. switching from one bot to another): GitHub only lets an account edit
    its OWN comments, so PATCHing a marked comment left by a different bot
    fails with a 403.  Filtering by author makes the current bot ignore the
    other bot's comment and post a fresh one instead.  When ``bot_username`` is
    empty (unconfigured), the first marker match wins regardless of author —
    preserving backward-compatible behaviour.

    When ``prefer_newest`` is True, the highest-id (most recently created)
    match is returned instead of the first (oldest). This matters when more
    than one comment can legitimately carry the marker at once — e.g. with
    ``review_history.preserve_previous`` the prior review is left intact
    alongside the freshly-posted one, both authored by the same bot, so the
    author filter can't disambiguate and the newest match is the current one.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number (int or str).
        marker: Marker string to search for (e.g. ``SUMMARY_TAG``).
        bot_username: If provided, only return a comment authored by this
            account (case-insensitive).
        prefer_newest: If True, return the highest-id match rather than the
            first one encountered.

    Returns:
        Dict with keys ``id``, ``body``, ``user`` from the GitHub API, or
        ``None`` if no matching comment is found or on any error.
    """
    try:
        raw = run_gh(
            "api",
            f"repos/{owner}/{repo}/issues/{pr_number}/comments",
            "--paginate",
            "--jq", r'.[] | {id: .id, body: .body, user: .user.login}',
            timeout=30,
        )
    except RuntimeError:
        return None

    if not raw.strip():
        return None

    wanted_user = bot_username.strip().lower()
    newest = None
    for line in raw.strip().split("\n"):
        try:
            comment = json.loads(line)
        except json.JSONDecodeError:
            continue
        if marker not in comment.get("body", ""):
            continue
        if wanted_user and str(comment.get("user", "")).lower() != wanted_user:
            continue
        if not prefer_newest:
            return comment
        if newest is None or comment.get("id", 0) > newest.get("id", 0):
            newest = comment

    return newest


def check_pvrs_enabled(repo: str, cwd: str = None) -> bool:
    """Check if Private Vulnerability Reporting is enabled on a repository.

    Calls ``GET /repos/{owner}/{repo}/private-vulnerability-reporting``.
    Returns ``False`` on any error (safe default — falls back to public issues).

    Args:
        repo: Repository in ``owner/repo`` format.
        cwd: Optional working directory.

    Returns:
        True if PVRS is enabled, False otherwise.
    """
    try:
        output = api(
            f"repos/{repo}/private-vulnerability-reporting",
            cwd=cwd, timeout=15,
        )
        data = json.loads(output)
        return data.get("enabled", False) is True
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError,
            OSError, TypeError, KeyError):
        return False


def security_advisory_report(
    summary: str,
    description: str,
    severity: str,
    ecosystem: str = "other",
    package_name: str = "",
    repo: str = None,
    cwd: str = None,
) -> str:
    """Submit a private vulnerability report via GitHub PVRS.

    Calls ``POST /repos/{owner}/{repo}/security-advisories/reports``.

    Args:
        summary: Advisory title.
        description: Markdown body with vulnerability details.
        severity: One of ``critical``, ``high``, ``medium``, ``low``.
        ecosystem: Package ecosystem (``pip``, ``npm``, ``go``, etc.).
        package_name: Package or project name.
        repo: Repository in ``owner/repo`` format.
        cwd: Optional working directory.

    Returns:
        The advisory URL (``html_url``) on success.

    Raises:
        RuntimeError: If the API call fails.
    """
    from app.leak_detector import scan_and_redact

    summary = scan_and_redact(summary, context="PVRS summary")
    description = scan_and_redact(description, context="PVRS description")

    payload = json.dumps({
        "summary": summary,
        "description": description,
        "severity": severity,
        "vulnerabilities": [{
            "package": {
                "ecosystem": ecosystem,
                "name": package_name or "unknown",
            },
            "vulnerable_version_range": "*",
            "patched_versions": "*",
        }],
    })

    output = api(
        f"repos/{repo}/security-advisories/reports",
        method="POST",
        input_data=payload,
        raw_body=True,
        cwd=cwd,
        timeout=30,
    )

    try:
        data = json.loads(output)
        url = data.get("html_url", "")
        if url:
            return url
        ghsa = data.get("ghsa_id", "")
        if ghsa:
            return f"GHSA: {ghsa}"
    except (json.JSONDecodeError, TypeError):
        pass

    return output.strip() if output else ""


def detect_ecosystem(project_path: str) -> str:
    """Infer the package ecosystem from project files.

    Checks for common package manager files and returns the corresponding
    ecosystem identifier used by GitHub's advisory API.

    Args:
        project_path: Path to the project root.

    Returns:
        Ecosystem string: ``pip``, ``npm``, ``go``, ``cargo``, ``maven``,
        ``nuget``, ``rubygems``, ``composer``, or ``other``.
    """
    from pathlib import Path

    root = Path(project_path)

    # Order matters: more specific files first
    indicators = [
        (("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"), "pip"),
        (("package.json",), "npm"),
        (("go.mod",), "go"),
        (("Cargo.toml",), "cargo"),
        (("pom.xml", "build.gradle", "build.gradle.kts"), "maven"),
        (("*.csproj", "*.sln"), "nuget"),
        (("Gemfile",), "rubygems"),
        (("composer.json",), "composer"),
    ]

    for filenames, ecosystem in indicators:
        for filename in filenames:
            if "*" in filename:
                if list(root.glob(filename)):
                    return ecosystem
            elif (root / filename).exists():
                return ecosystem

    return "other"


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
