"""
Kōan diagnostic — Connectivity checks.

Validates Telegram bot token, GitHub CLI auth, and Claude CLI quota.
Only runs when --full flag is passed (adds network latency).
"""

import os
import subprocess
from typing import List

from diagnostics import CheckResult


def run(koan_root: str, instance_dir: str, full: bool = False) -> List[CheckResult]:
    """Run connectivity diagnostic checks (only when full=True)."""
    if not full:
        return []

    results = []

    # --- Telegram API ---
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        results.append(CheckResult(
            name="telegram_api",
            severity="warn",
            message="TELEGRAM_BOT_TOKEN not set",
            hint="Set TELEGRAM_BOT_TOKEN in .env",
        ))
    else:
        try:
            import requests
            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getMe",
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                bot_name = resp.json().get("result", {}).get("username", "unknown")
                results.append(CheckResult(
                    name="telegram_api",
                    severity="ok",
                    message=f"Telegram bot is valid (@{bot_name})",
                ))
            else:
                results.append(CheckResult(
                    name="telegram_api",
                    severity="error",
                    message="Telegram bot token is invalid",
                    hint="Check TELEGRAM_BOT_TOKEN in .env",
                ))
        except ImportError:
            results.append(CheckResult(
                name="telegram_api",
                severity="warn",
                message="Cannot check Telegram (requests not installed)",
            ))
        except Exception as e:
            results.append(CheckResult(
                name="telegram_api",
                severity="warn",
                message=f"Telegram API check failed: {e}",
                hint="Check network connectivity",
            ))

    # --- GitHub CLI ---
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            results.append(CheckResult(
                name="github_cli",
                severity="ok",
                message="GitHub CLI is authenticated",
            ))
        else:
            stderr = result.stderr.strip().splitlines()
            msg = stderr[0] if stderr else "not authenticated"
            results.append(CheckResult(
                name="github_cli",
                severity="warn",
                message=f"GitHub CLI: {msg}",
                hint="Run 'gh auth login' to authenticate",
            ))
    except FileNotFoundError:
        results.append(CheckResult(
            name="github_cli",
            severity="warn",
            message="GitHub CLI (gh) not installed",
            hint="Install gh: https://cli.github.com/",
        ))
    except subprocess.TimeoutExpired:
        results.append(CheckResult(
            name="github_cli",
            severity="warn",
            message="GitHub CLI auth check timed out",
        ))
    except Exception as e:
        results.append(CheckResult(
            name="github_cli",
            severity="warn",
            message=f"GitHub CLI check failed: {e}",
        ))

    # --- Claude CLI ---
    try:
        from app.utils import get_cli_provider_env
        provider = get_cli_provider_env()
        cli_binary = "claude"
        if provider == "copilot":
            cli_binary = "gh"

        import shutil
        if shutil.which(cli_binary):
            results.append(CheckResult(
                name="cli_provider",
                severity="ok",
                message=f"CLI provider '{provider}' binary found",
            ))
        else:
            results.append(CheckResult(
                name="cli_provider",
                severity="error",
                message=f"CLI provider '{provider}' binary not found: {cli_binary}",
                hint=f"Install {cli_binary}",
            ))
    except Exception as e:
        results.append(CheckResult(
            name="cli_provider",
            severity="warn",
            message=f"CLI provider check failed: {e}",
        ))

    return results
