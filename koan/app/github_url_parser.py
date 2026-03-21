"""Git forge URL parsing utilities.

Backwards-compatible parser helpers used across the codebase.
Despite the module name, these functions now support common GitHub,
GitLab, and Codeberg/Gitea PR + issue URL formats.
"""

import re
from typing import List, Optional, Tuple

# Canonical URL type labels returned by parse_github_url():
# - "pull" for PR/MR URLs across forges
# - "issues" for issue URLs across forges
_CANONICAL_PULL = "pull"
_CANONICAL_ISSUE = "issues"

# Ordered from most specific to least specific.
#
# group(1)=owner/namespace, group(2)=repo, group(3)=number
_PR_PATTERNS: List[str] = [
    # GitHub
    r"https?://github\.com/([^/\s]+)/([^/\s#?]+)/pull/(\d+)",
    # GitLab (group/subgroup/repo/-/merge_requests/123)
    r"https?://gitlab\.com/([^/\s]+(?:/[^/\s]+)*)/([^/\s#?]+)/-/merge_requests/(\d+)",
    # Codeberg / Gitea / Forgejo style
    r"https?://codeberg\.org/([^/\s]+)/([^/\s#?]+)/pulls/(\d+)",
]

_ISSUE_PATTERNS: List[str] = [
    # GitHub
    r"https?://github\.com/([^/\s]+)/([^/\s#?]+)/issues/(\d+)",
    # GitLab (group/subgroup/repo/-/issues/123)
    r"https?://gitlab\.com/([^/\s]+(?:/[^/\s]+)*)/([^/\s#?]+)/-/issues/(\d+)",
    # Codeberg / Gitea / Forgejo style
    r"https?://codeberg\.org/([^/\s]+)/([^/\s#?]+)/issues/(\d+)",
]


def _clean_url(url: str) -> str:
    """Clean a URL by removing fragments and whitespace.
    
    Args:
        url: The URL to clean
        
    Returns:
        Cleaned URL without fragment or surrounding whitespace
    """
    return url.split("#")[0].strip()


def _match_any(
    patterns: List[str],
    text: str,
    *,
    search: bool = False,
) -> Optional[Tuple[str, str, str]]:
    """Match text against the first regex pattern that succeeds."""
    for pattern in patterns:
        match = re.search(pattern, text) if search else re.match(pattern, text)
        if match:
            return match.group(1), match.group(2), match.group(3)
    return None


def parse_pr_url(url: str) -> Tuple[str, str, str]:
    """Extract owner, repo, and PR number from a supported PR URL.

    Args:
        url: PR URL (GitHub/GitLab/Codeberg)

    Returns:
        Tuple of (owner, repo, pr_number) as strings

    Raises:
        ValueError: If the URL doesn't match expected PR format
    """
    clean_url = _clean_url(url)
    parsed = _match_any(_PR_PATTERNS, clean_url)
    if not parsed:
        raise ValueError(f"Invalid PR URL: {url}")
    return parsed


def parse_issue_url(url: str) -> Tuple[str, str, str]:
    """Extract owner, repo, and issue number from a supported issue URL.

    Args:
        url: Issue URL (GitHub/GitLab/Codeberg)

    Returns:
        Tuple of (owner, repo, issue_number) as strings

    Raises:
        ValueError: If the URL doesn't match expected issue format
    """
    clean_url = _clean_url(url)
    parsed = _match_any(_ISSUE_PATTERNS, clean_url)
    if not parsed:
        raise ValueError(f"Invalid issue URL: {url}")
    return parsed


def search_pr_url(text: str) -> Tuple[str, str, str]:
    """Search for a supported PR URL anywhere in text.

    Unlike parse_pr_url which expects the URL at the start, this searches
    the entire string for an embedded PR URL.

    Args:
        text: Text that may contain a GitHub PR URL

    Returns:
        Tuple of (owner, repo, pr_number) as strings

    Raises:
        ValueError: If no PR URL is found in text
    """
    parsed = _match_any(_PR_PATTERNS, text, search=True)
    if not parsed:
        raise ValueError(f"No PR URL found in: {text}")
    return parsed


def search_issue_url(text: str) -> Tuple[str, str, str]:
    """Search for a supported issue URL anywhere in text.

    Unlike parse_issue_url which expects the URL at the start, this searches
    the entire string for an embedded issue URL.

    Args:
        text: Text that may contain a GitHub issue URL

    Returns:
        Tuple of (owner, repo, issue_number) as strings

    Raises:
        ValueError: If no issue URL is found in text
    """
    parsed = _match_any(_ISSUE_PATTERNS, text, search=True)
    if not parsed:
        raise ValueError(f"No issue URL found in: {text}")
    return parsed


def parse_github_url(url: str) -> Tuple[str, str, str, str]:
    """Extract owner, repo, type, and number from a supported PR/issue URL.

    Args:
        url: PR or issue URL (GitHub/GitLab/Codeberg)

    Returns:
        Tuple of (owner, repo, url_type, number) where url_type is the
        canonical 'pull' or 'issues' label.

    Raises:
        ValueError: If the URL doesn't match expected format
    """
    clean_url = _clean_url(url)
    pr = _match_any(_PR_PATTERNS, clean_url)
    if pr:
        owner, repo, number = pr
        return owner, repo, _CANONICAL_PULL, number

    issue = _match_any(_ISSUE_PATTERNS, clean_url)
    if issue:
        owner, repo, number = issue
        return owner, repo, _CANONICAL_ISSUE, number

    raise ValueError(f"Invalid GitHub URL: {url}")
