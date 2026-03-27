"""GitHub URL parsing utilities.

Provides centralized parsing for GitHub PR and issue URLs with consistent
error handling and validation.
"""

import re
from typing import Tuple

# GitHub URL patterns
PR_URL_PATTERN = r'https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)'
ISSUE_URL_PATTERN = r'https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)'
PR_OR_ISSUE_PATTERN = r'https?://github\.com/([^/]+)/([^/]+)/(pull|issues)/(\d+)'


def _clean_url(url: str) -> str:
    """Clean a URL by removing fragments and whitespace.
    
    Args:
        url: The URL to clean
        
    Returns:
        Cleaned URL without fragment or surrounding whitespace
    """
    return url.split("#")[0].strip()


def parse_pr_url(url: str) -> Tuple[str, str, str]:
    """Extract owner, repo, and PR number from a GitHub PR URL.

    Args:
        url: GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)

    Returns:
        Tuple of (owner, repo, pr_number) as strings

    Raises:
        ValueError: If the URL doesn't match expected PR format
    """
    clean_url = _clean_url(url)
    match = re.match(PR_URL_PATTERN, clean_url)
    if not match:
        raise ValueError(f"Invalid PR URL: {url}")
    return match.group(1), match.group(2), match.group(3)


def parse_issue_url(url: str) -> Tuple[str, str, str]:
    """Extract owner, repo, and issue number from a GitHub issue URL.

    Args:
        url: GitHub issue URL (e.g., https://github.com/owner/repo/issues/123)

    Returns:
        Tuple of (owner, repo, issue_number) as strings

    Raises:
        ValueError: If the URL doesn't match expected issue format
    """
    clean_url = _clean_url(url)
    match = re.match(ISSUE_URL_PATTERN, clean_url)
    if not match:
        raise ValueError(f"Invalid issue URL: {url}")
    return match.group(1), match.group(2), match.group(3)


def search_pr_url(text: str) -> Tuple[str, str, str]:
    """Search for a GitHub PR URL anywhere in text.

    Unlike parse_pr_url which expects the URL at the start, this searches
    the entire string for an embedded PR URL.

    Args:
        text: Text that may contain a GitHub PR URL

    Returns:
        Tuple of (owner, repo, pr_number) as strings

    Raises:
        ValueError: If no PR URL is found in text
    """
    match = re.search(PR_URL_PATTERN, text)
    if not match:
        raise ValueError(f"No PR URL found in: {text}")
    return match.group(1), match.group(2), match.group(3)


def search_issue_url(text: str) -> Tuple[str, str, str]:
    """Search for a GitHub issue URL anywhere in text.

    Unlike parse_issue_url which expects the URL at the start, this searches
    the entire string for an embedded issue URL.

    Args:
        text: Text that may contain a GitHub issue URL

    Returns:
        Tuple of (owner, repo, issue_number) as strings

    Raises:
        ValueError: If no issue URL is found in text
    """
    match = re.search(ISSUE_URL_PATTERN, text)
    if not match:
        raise ValueError(f"No issue URL found in: {text}")
    return match.group(1), match.group(2), match.group(3)


def parse_github_url(url: str) -> Tuple[str, str, str, str]:
    """Extract owner, repo, type, and number from a GitHub PR or issue URL.

    Args:
        url: GitHub PR or issue URL

    Returns:
        Tuple of (owner, repo, url_type, number) where url_type is 'pull' or 'issues'

    Raises:
        ValueError: If the URL doesn't match expected format
    """
    clean_url = _clean_url(url)
    match = re.match(PR_OR_ISSUE_PATTERN, clean_url)
    if not match:
        raise ValueError(f"Invalid GitHub URL: {url}")
    return match.group(1), match.group(2), match.group(3), match.group(4)
