"""Guard the script-launch import path used by `pid_manager`.

`_launch_python_process` starts each daemon as a *script*, so CPython puts the
script's own directory first on `sys.path` — ahead of `PYTHONPATH` and the
stdlib. A module named after a stdlib top-level package in one of those
directories therefore shadows it for the whole process, and the daemon dies on
the first third-party import that touches it. That is invisible to the test
suite, which imports the same entrypoints as package modules.
"""

import ast
import sys
from pathlib import Path

KOAN_DIR = Path(__file__).resolve().parents[1]
PID_MANAGER = KOAN_DIR / "app" / "pid_manager.py"


def _launched_scripts() -> set[str]:
    """Every script path `pid_manager` hands to `_launch_python_process`."""
    tree = ast.parse(PID_MANAGER.read_text(encoding="utf-8"))
    scripts = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "_launch_python_process" or len(node.args) < 2:
            continue
        script = node.args[1]
        if isinstance(script, ast.Constant) and isinstance(script.value, str):
            scripts.add(script.value)
    return scripts


def test_the_guard_still_finds_the_launch_sites():
    """A refactor that stops matching must fail here, not pass vacuously."""
    scripts = _launched_scripts()
    assert "app/mcp/__main__.py" in scripts
    assert len(scripts) >= 4


def test_no_daemon_directory_shadows_a_stdlib_module():
    offenders = []
    for script in sorted(_launched_scripts()):
        script_dir = KOAN_DIR / Path(script).parent
        for module in sorted(script_dir.glob("*.py")):
            if module.stem in sys.stdlib_module_names:
                offenders.append(f"{script}: {module.relative_to(KOAN_DIR)}")

    assert not offenders, (
        "These modules sit in a directory that becomes sys.path[0] when the "
        "daemon is launched as a script, so they shadow a stdlib module for "
        "the whole process: " + ", ".join(offenders)
    )
