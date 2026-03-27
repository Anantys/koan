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

Automation rules:
    Declarative rules from instance/automation_rules.yaml are evaluated
    after user hook modules on every fire() call. Each rule maps an event
    to an action (notify, create_mission, pause, resume, auto_merge).
    A per-rule loop guard prevents runaway rule execution.
"""

import importlib.util
import os
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.automation_rules import AutomationRule, load_rules


class HookRegistry:
    """Discovers and manages hook modules from a directory."""

    def __init__(self, hooks_dir: Path, instance_dir: Optional[str] = None):
        self._handlers: Dict[str, List[Callable]] = {}
        self._instance_dir: Optional[str] = instance_dir
        # Per-rule fire timestamps for the loop guard: {rule_id: [timestamp, ...]}
        self._rule_fire_times: Dict[str, List[float]] = defaultdict(list)
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

    def fire(self, event: str, **kwargs) -> Dict[str, str]:
        """Call all handlers for event, catching exceptions per-handler.

        After user hook modules execute, evaluates matching automation rules
        from instance/automation_rules.yaml (if instance_dir was provided).

        Returns a dict mapping failed handler names to error messages.
        Empty dict means all handlers succeeded.
        """
        failures: Dict[str, str] = {}
        handlers = self._handlers.get(event, [])
        for handler in handlers:
            func_name = getattr(handler, "__name__", repr(handler))
            module_name = getattr(handler, "__module__", "")
            handler_name = f"{module_name}.{func_name}" if module_name else func_name
            try:
                handler(kwargs)
            except Exception as exc:
                failures[handler_name] = str(exc)
                print(
                    f"[hooks] Error in {event} handler "
                    f"{handler_name}:\n"
                    f"{traceback.format_exc()}",
                    file=sys.stderr,
                )

        # Execute matching automation rules
        if self._instance_dir is not None:
            self._fire_automation_rules(event, kwargs)

        return failures

    def has_hooks(self, event: str) -> bool:
        """Check if any hooks are registered for event."""
        return bool(self._handlers.get(event))

    # ------------------------------------------------------------------
    # Automation rules
    # ------------------------------------------------------------------

    def _fire_automation_rules(self, event: str, ctx: dict) -> None:
        """Evaluate and execute all enabled rules matching event."""
        try:
            rules = load_rules(self._instance_dir)
        except Exception as exc:
            print(f"[hooks] Failed to load automation rules: {exc}", file=sys.stderr)
            return

        for rule in rules:
            if rule.event != event:
                continue
            if not rule.enabled:
                continue
            if self._loop_guard(rule):
                print(
                    f"[hooks] Loop guard triggered for rule {rule.id} "
                    f"(action={rule.action}) — skipping.",
                    file=sys.stderr,
                )
                continue
            try:
                self._execute_rule(rule, ctx)
                self._write_rule_journal(rule)
            except Exception as exc:
                print(
                    f"[hooks] Error executing automation rule {rule.id} "
                    f"({rule.event} → {rule.action}): {exc}\n"
                    f"{traceback.format_exc()}",
                    file=sys.stderr,
                )

    def _loop_guard(self, rule: AutomationRule) -> bool:
        """Return True (skip) if rule has exceeded max_fires_per_minute.

        The counter is in-memory and resets on process restart.
        Threshold is read from instance/config.yaml under
        automation_rules.max_fires_per_minute (default 5).
        """
        from app.utils import load_config
        config = {}
        try:
            config = load_config() or {}
        except Exception as exc:
            print(f"[hooks] Could not load config for loop guard: {exc}", file=sys.stderr)
        max_fires = (
            config.get("automation_rules", {}).get("max_fires_per_minute", 5)
        )
        window = 60.0  # seconds
        now = time.monotonic()

        # Prune old timestamps outside the window
        self._rule_fire_times[rule.id] = [
            t for t in self._rule_fire_times[rule.id] if now - t < window
        ]

        if len(self._rule_fire_times[rule.id]) >= max_fires:
            return True  # over limit — skip

        self._rule_fire_times[rule.id].append(now)
        return False

    def _execute_rule(self, rule: AutomationRule, ctx: dict) -> None:
        """Execute a single automation rule action. Fire-and-forget."""
        instance_dir = self._instance_dir
        action = rule.action
        params = rule.params or {}

        if action == "notify":
            self._action_notify(instance_dir, params, ctx)
        elif action == "create_mission":
            self._action_create_mission(instance_dir, params, ctx)
        elif action == "pause":
            self._action_pause(instance_dir)
        elif action == "resume":
            self._action_resume(instance_dir)
        elif action == "auto_merge":
            self._action_auto_merge(instance_dir, ctx)
        else:
            print(f"[hooks] Unknown action '{action}' in rule {rule.id}", file=sys.stderr)

    def _action_notify(self, instance_dir: str, params: dict, ctx: dict) -> None:
        """Append a message to instance/outbox.md."""
        message = params.get("message", "Automation rule fired.")
        outbox_path = Path(instance_dir) / "outbox.md"
        from app.utils import atomic_write
        existing = outbox_path.read_text() if outbox_path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        atomic_write(outbox_path, existing + f"- {message}\n")

    def _action_create_mission(self, instance_dir: str, params: dict, ctx: dict) -> None:
        """Append a mission to the Pending section of instance/missions.md."""
        text = params.get("text", "Automation rule: create mission")
        missions_path = Path(instance_dir) / "missions.md"
        from app.utils import insert_pending_mission
        insert_pending_mission(missions_path, text)

    def _action_pause(self, instance_dir: str) -> None:
        """Write .koan-pause to pause the agent."""
        pause_file = Path(instance_dir).parent / ".koan-pause"
        # Idempotent — overwrite is harmless
        pause_file.write_text("automation_rule\n")

    def _action_resume(self, instance_dir: str) -> None:
        """Remove .koan-pause if it exists."""
        pause_file = Path(instance_dir).parent / ".koan-pause"
        try:
            pause_file.unlink()
        except FileNotFoundError:
            pass  # Already absent — idempotent

    def _action_auto_merge(self, instance_dir: str, ctx: dict) -> None:
        """Call git_auto_merge.auto_merge_branch() if project context present."""
        project_path = ctx.get("project_path")
        project_name = ctx.get("project_name")
        branch = ctx.get("branch")
        if not project_path or not project_name:
            print(
                "[hooks] auto_merge action skipped — project_path or project_name absent in ctx.",
                file=sys.stderr,
            )
            return
        if not branch:
            # Try to read current branch from git
            import subprocess
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                branch = result.stdout.strip()
            except Exception as exc:
                print(f"[hooks] auto_merge: failed to get branch: {exc}", file=sys.stderr)
                return
        from app.git_auto_merge import auto_merge_branch
        auto_merge_branch(instance_dir, project_name, project_path, branch)

    def _write_rule_journal(self, rule: AutomationRule) -> None:
        """Write a [automation_rule]-tagged entry to today's journal."""
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            journal_dir = Path(self._instance_dir) / "journal" / today
            journal_dir.mkdir(parents=True, exist_ok=True)
            journal_file = journal_dir / "automation.md"
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            entry = f"[automation_rule] {ts} rule={rule.id} event={rule.event} action={rule.action}\n"
            with open(journal_file, "a") as f:
                f.write(entry)
        except Exception as exc:
            print(f"[hooks] Failed to write rule journal: {exc}", file=sys.stderr)


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
    _registry = HookRegistry(hooks_dir, instance_dir=instance_dir)


def read_automation_rules(instance_dir: str) -> list:
    """Load and return automation rules from instance/automation_rules.yaml."""
    return load_rules(instance_dir)


def fire_hook(event: str, **kwargs) -> Dict[str, str]:
    """Fire a hook event. No-op if registry not initialized.

    Returns a dict mapping failed handler names to error messages.
    Empty dict means all handlers succeeded (or no registry).
    """
    if _registry is not None:
        return _registry.fire(event, **kwargs)
    return {}


def get_registry() -> Optional[HookRegistry]:
    """Return the current registry (for testing)."""
    return _registry


def reset_registry() -> None:
    """Reset the global registry to None (for testing)."""
    global _registry
    _registry = None
