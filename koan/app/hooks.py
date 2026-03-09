"""Hook system for extensible pre/post-action events.

Discovers hook modules from instance/hooks/ at startup and provides
fire-and-forget event dispatching. Hook modules are .py files with a
HOOKS dict mapping event names to callables.

Example hook module (instance/hooks/my_hook.py):

    def on_post_mission(ctx):
        print(f"Mission completed: {ctx['mission_title']}")

    HOOKS = {
        "post_mission": on_post_mission,
    }

Supported events:
    - session_start: Fired after startup completes
    - session_end: Fired on shutdown (in finally block)
    - pre_mission: Fired before Claude execution
    - post_mission: Fired after post-mission pipeline completes
"""

import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional


class HookRegistry:
    """Discovers and manages hook modules from a directory."""

    def __init__(self, hooks_dir: Path):
        self._handlers: Dict[str, List[Callable]] = {}
        self._discover(hooks_dir)

    def _discover(self, hooks_dir: Path) -> None:
        """Scan hooks_dir for .py files and register their HOOKS dicts."""
        if not hooks_dir.is_dir():
            return

        for hook_file in sorted(hooks_dir.glob("*.py")):
            if hook_file.name.startswith("_"):
                continue
            try:
                self._load_module(hook_file)
            except Exception as e:
                print(
                    f"[hooks] Failed to load {hook_file.name}: {e}",
                    file=sys.stderr,
                )

    def _load_module(self, path: Path) -> None:
        """Load a single hook module and register its HOOKS dict."""
        module_name = f"koan_hook_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        hooks_dict = getattr(module, "HOOKS", None)
        if not isinstance(hooks_dict, dict):
            return

        for event_name, handler in hooks_dict.items():
            if callable(handler):
                self._handlers.setdefault(event_name, []).append(handler)

    def fire(self, event: str, **kwargs) -> None:
        """Call all handlers for event, catching exceptions per-handler."""
        handlers = self._handlers.get(event, [])
        for handler in handlers:
            try:
                handler(kwargs)
            except Exception:
                print(
                    f"[hooks] Error in {event} handler "
                    f"{getattr(handler, '__name__', repr(handler))}:\n"
                    f"{traceback.format_exc()}",
                    file=sys.stderr,
                )

    def has_hooks(self, event: str) -> bool:
        """Check if any hooks are registered for event."""
        return bool(self._handlers.get(event))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: Optional[HookRegistry] = None


def init_hooks(instance_dir: str) -> None:
    """Initialize the global hook registry from instance/hooks/.

    Creates the hooks directory if it doesn't exist.
    Safe to call multiple times — reinitializes the registry.
    """
    global _registry
    hooks_dir = Path(instance_dir) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    _registry = HookRegistry(hooks_dir)


def fire_hook(event: str, **kwargs) -> None:
    """Fire a hook event. No-op if registry not initialized."""
    if _registry is not None:
        _registry.fire(event, **kwargs)


def get_registry() -> Optional[HookRegistry]:
    """Return the current registry (for testing)."""
    return _registry


def reset_registry() -> None:
    """Reset the global registry to None (for testing)."""
    global _registry
    _registry = None
