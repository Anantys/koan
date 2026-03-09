"""
Kōan diagnostic — Environment checks.

Validates Python version, required binaries, and importable packages.
"""

import shutil
import sys
from typing import List

from diagnostics import CheckResult


def run(koan_root: str, instance_dir: str) -> List[CheckResult]:
    """Run environment diagnostic checks."""
    results = []

    # --- Python version ---
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        results.append(CheckResult(
            name="python_version",
            severity="ok",
            message=f"Python {major}.{minor}",
        ))
    else:
        results.append(CheckResult(
            name="python_version",
            severity="error",
            message=f"Python {major}.{minor} (requires >= 3.10)",
            hint="Upgrade Python to 3.10 or later",
        ))

    # --- Required binaries ---
    # Determine which CLI binary to check based on configured provider
    cli_binary = "claude"
    try:
        from app.utils import get_cli_provider_env
        provider = get_cli_provider_env()
        if provider == "copilot":
            cli_binary = "gh"  # Copilot uses gh CLI
        elif provider in ("local", "ollama-launch"):
            cli_binary = "ollama"
    except Exception:
        pass

    required_binaries = ["git", cli_binary]
    optional_binaries = ["gh"]

    for binary in required_binaries:
        path = shutil.which(binary)
        if path:
            results.append(CheckResult(
                name=f"binary_{binary}",
                severity="ok",
                message=f"'{binary}' found: {path}",
            ))
        else:
            results.append(CheckResult(
                name=f"binary_{binary}",
                severity="error",
                message=f"'{binary}' not found in PATH",
                hint=f"Install {binary} and ensure it's in your PATH",
            ))

    for binary in optional_binaries:
        if binary in required_binaries:
            continue  # Already checked
        path = shutil.which(binary)
        if path:
            results.append(CheckResult(
                name=f"binary_{binary}",
                severity="ok",
                message=f"'{binary}' found: {path}",
            ))
        else:
            results.append(CheckResult(
                name=f"binary_{binary}",
                severity="warn",
                message=f"'{binary}' not found in PATH",
                hint=f"Install {binary} for GitHub integration",
            ))

    # --- Required packages ---
    required_packages = [
        ("requests", "HTTP requests"),
        ("flask", "Web dashboard"),
        ("yaml", "YAML config parsing"),
    ]

    for package, purpose in required_packages:
        try:
            __import__(package)
            results.append(CheckResult(
                name=f"package_{package}",
                severity="ok",
                message=f"'{package}' importable",
            ))
        except ImportError:
            results.append(CheckResult(
                name=f"package_{package}",
                severity="warn",
                message=f"'{package}' not importable ({purpose})",
                hint=f"pip install {package}",
            ))

    return results
