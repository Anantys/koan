"""
Kōan — Diagnostic check runner.

Discovers and runs all diagnostic check modules in this directory,
in alphabetical order. Each module must expose:

    def run(koan_root: str, instance_dir: str) -> List[CheckResult]
        Returns a list of CheckResult named tuples.

Modules are discovered by filename: any .py file that isn't __init__.py
is treated as a diagnostic check module.

Unlike sanity checks (which auto-fix at startup), diagnostics are
read-only and user-triggered via /doctor.
"""

import importlib
import pkgutil
from pathlib import Path
from typing import List, NamedTuple, Tuple


class CheckResult(NamedTuple):
    """Result of a single diagnostic check."""
    name: str        # Short check name (e.g. "python_version")
    severity: str    # "ok", "warn", or "error"
    message: str     # Human-readable description
    hint: str = ""   # Optional remediation hint


def discover_checks() -> List[str]:
    """Return sorted list of diagnostic check module names in this package."""
    package_dir = Path(__file__).parent
    modules = []
    for info in pkgutil.iter_modules([str(package_dir)]):
        if not info.ispkg:
            modules.append(info.name)
    return sorted(modules)


def run_all(koan_root: str, instance_dir: str, full: bool = False) -> List[Tuple[str, List[CheckResult]]]:
    """Run all diagnostic checks in alphabetical order.

    Args:
        koan_root: Path to KOAN_ROOT directory.
        instance_dir: Path to instance/ directory.
        full: If True, run slow connectivity checks too.

    Returns:
        List of (module_name, results) tuples.
    """
    results = []
    for name in discover_checks():
        module = importlib.import_module(f"diagnostics.{name}")
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            continue
        try:
            # Pass full flag to checks that accept it
            import inspect
            sig = inspect.signature(run_fn)
            if "full" in sig.parameters:
                check_results = run_fn(koan_root, instance_dir, full=full)
            else:
                check_results = run_fn(koan_root, instance_dir)
        except Exception as e:
            check_results = [CheckResult(
                name=f"{name}_error",
                severity="error",
                message=f"Check module '{name}' crashed: {e}",
                hint="This is a bug in the diagnostic check itself.",
            )]
        results.append((name, check_results))
    return results
