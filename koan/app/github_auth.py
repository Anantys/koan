#!/usr/bin/env python3
"""
Kōan -- GitHub CLI authentication helper.

Manages GitHub CLI identity switching for gh commands. When GITHUB_USER
is configured, retrieves a session token via `gh auth token --user <user>`
and exports it as GH_TOKEN for all gh CLI calls in the session.

Usage from Python:
    from app.github_auth import get_gh_env, setup_github_auth

    # Get env dict to pass to subprocess.run()
    env = get_gh_env()

    # Or setup once at session start (validates + alerts on failure)
    setup_github_auth()

Usage from shell:
    python3 -m app.github_auth
    # Prints GH_TOKEN=<token> on success, exits 1 on failure.
"""

import os
import subprocess
import sys
from typing import Optional, Dict


def get_github_user() -> str:
    """Return the configured GITHUB_USER, or empty string if not set."""
    return os.environ.get("GITHUB_USER", "").strip()


def get_gh_token(username: str) -> Optional[str]:
    """Retrieve a GitHub token for the given user via gh auth token.

    Args:
        username: The GitHub username to get a token for.

    Returns:
        The token string, or None if retrieval failed.
    """
    if not username:
        return None
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--user", username],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def get_gh_env() -> Dict[str, str]:
    """Return environment variables for gh CLI commands.

    If GITHUB_USER is configured, retrieves the token and returns
    a dict with GH_TOKEN set. Otherwise returns an empty dict.

    This is designed to be merged into subprocess.run() env:
        env = {**os.environ, **get_gh_env()}
    """
    username = get_github_user()
    if not username:
        return {}

    # Check if GH_TOKEN is already set in environment
    existing_token = os.environ.get("GH_TOKEN", "")
    if existing_token:
        return {"GH_TOKEN": existing_token}

    token = get_gh_token(username)
    if token:
        return {"GH_TOKEN": token}
    return {}


def setup_github_auth() -> bool:
    """Setup GitHub authentication for the session.

    Reads GITHUB_USER, retrieves token, sets GH_TOKEN in os.environ.
    Sends a Telegram alert if the token retrieval fails.

    Returns:
        True if auth was set up successfully (or GITHUB_USER not configured).
        False if GITHUB_USER is set but token retrieval failed.
    """
    username = get_github_user()
    if not username:
        return True  # No user configured, nothing to do

    token = get_gh_token(username)
    if token:
        os.environ["GH_TOKEN"] = token
        print(f"[github_auth] Authenticated as {username}")
        return True

    # Token retrieval failed — alert the user
    error_msg = (
        f"⚠️ GitHub authentication failed for user `{username}`.\n"
        f"Cannot retrieve token via `gh auth token --user {username}`.\n"
        f"Ensure the user is authenticated with: `gh auth login --user {username}`\n"
        f"gh CLI commands may fail during this session."
    )
    print(f"[github_auth] {error_msg}", file=sys.stderr)

    try:
        from app.notify import send_telegram
        send_telegram(error_msg)
    except Exception as e:
        print(f"[github_auth] Failed to send alert: {e}", file=sys.stderr)

    return False


if __name__ == "__main__":
    # CLI entry point.
    # Prints GH_TOKEN=<token> on success (for eval in bash).
    # Exits 0 if no GITHUB_USER configured or token retrieved.
    # Exits 1 if GITHUB_USER set but token retrieval failed.
    username = get_github_user()
    if not username:
        sys.exit(0)

    if setup_github_auth():
        # setup_github_auth sets os.environ["GH_TOKEN"] on success
        print(f"GH_TOKEN={os.environ['GH_TOKEN']}")
        sys.exit(0)

    sys.exit(1)
