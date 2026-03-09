"""Tests for hooks.py — hook registry and discovery."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.hooks import HookRegistry, fire_hook, init_hooks, reset_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the global registry before and after each test."""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def hooks_dir(tmp_path):
    """Create an empty hooks directory."""
    d = tmp_path / "hooks"
    d.mkdir()
    return d


def _write_hook(hooks_dir: Path, name: str, code: str) -> Path:
    """Write a hook module to the hooks directory."""
    path = hooks_dir / f"{name}.py"
    path.write_text(code)
    return path


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestHookDiscovery:
    def test_empty_dir(self, hooks_dir):
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")

    def test_nonexistent_dir(self, tmp_path):
        registry = HookRegistry(tmp_path / "nonexistent")
        assert not registry.has_hooks("post_mission")

    def test_discovers_valid_hook(self, hooks_dir):
        _write_hook(hooks_dir, "my_hook", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        assert registry.has_hooks("post_mission")

    def test_discovers_multiple_events(self, hooks_dir):
        _write_hook(hooks_dir, "multi", (
            "def on_pre(ctx): pass\n"
            "def on_post(ctx): pass\n"
            "HOOKS = {'pre_mission': on_pre, 'post_mission': on_post}\n"
        ))
        registry = HookRegistry(hooks_dir)
        assert registry.has_hooks("pre_mission")
        assert registry.has_hooks("post_mission")

    def test_discovers_multiple_modules(self, hooks_dir):
        _write_hook(hooks_dir, "hook_a", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        _write_hook(hooks_dir, "hook_b", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        assert registry.has_hooks("post_mission")
        # Both handlers should be registered
        assert len(registry._handlers["post_mission"]) == 2

    def test_skips_underscore_files(self, hooks_dir):
        _write_hook(hooks_dir, "__init__", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        _write_hook(hooks_dir, "_private", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")

    def test_skips_non_py_files(self, hooks_dir):
        (hooks_dir / "readme.md").write_text("# hooks")
        (hooks_dir / "data.json").write_text("{}")
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")

    def test_skips_module_without_hooks_dict(self, hooks_dir):
        _write_hook(hooks_dir, "no_hooks", "x = 42\n")
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")

    def test_skips_module_with_non_dict_hooks(self, hooks_dir):
        _write_hook(hooks_dir, "bad_hooks", "HOOKS = 'not a dict'\n")
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")

    def test_skips_non_callable_values(self, hooks_dir):
        _write_hook(hooks_dir, "bad_vals", (
            "HOOKS = {'post_mission': 'not callable'}\n"
        ))
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")

    def test_syntax_error_skipped(self, hooks_dir, capsys):
        _write_hook(hooks_dir, "broken", "def f(\n")
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")
        captured = capsys.readouterr()
        assert "[hooks] Failed to load broken.py" in captured.err

    def test_import_error_skipped(self, hooks_dir, capsys):
        _write_hook(hooks_dir, "bad_import", "import nonexistent_module_xyz\n")
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("post_mission")
        captured = capsys.readouterr()
        assert "[hooks] Failed to load bad_import.py" in captured.err

    def test_valid_hooks_loaded_despite_broken_module(self, hooks_dir, capsys):
        _write_hook(hooks_dir, "a_broken", "def f(\n")
        _write_hook(hooks_dir, "b_valid", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        assert registry.has_hooks("post_mission")
        captured = capsys.readouterr()
        assert "[hooks] Failed to load a_broken.py" in captured.err


# ---------------------------------------------------------------------------
# Fire tests
# ---------------------------------------------------------------------------


class TestHookFire:
    def test_fire_no_hooks(self, hooks_dir):
        registry = HookRegistry(hooks_dir)
        # Should not raise
        registry.fire("post_mission", project_name="test")

    def test_fire_calls_handler(self, hooks_dir):
        _write_hook(hooks_dir, "tracker", (
            "calls = []\n"
            "def handler(ctx): calls.append(ctx)\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        registry.fire("post_mission", project_name="myproj", exit_code=0)
        # Verify the handler was called by importing the module's state
        mod_name = "koan_hook_tracker"
        assert mod_name in sys.modules
        assert len(sys.modules[mod_name].calls) == 1
        assert sys.modules[mod_name].calls[0]["project_name"] == "myproj"
        assert sys.modules[mod_name].calls[0]["exit_code"] == 0

    def test_fire_multiple_handlers(self, hooks_dir):
        _write_hook(hooks_dir, "hook_a", (
            "count = 0\n"
            "def handler(ctx):\n"
            "    global count\n"
            "    count += 1\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        _write_hook(hooks_dir, "hook_b", (
            "count = 0\n"
            "def handler(ctx):\n"
            "    global count\n"
            "    count += 1\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        registry.fire("post_mission")
        assert sys.modules["koan_hook_hook_a"].count == 1
        assert sys.modules["koan_hook_hook_b"].count == 1

    def test_fire_handler_error_logged(self, hooks_dir, capsys):
        _write_hook(hooks_dir, "crasher", (
            "def handler(ctx): raise ValueError('boom')\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        # Should not raise
        registry.fire("post_mission")
        captured = capsys.readouterr()
        assert "[hooks] Error in post_mission handler handler" in captured.err
        assert "boom" in captured.err

    def test_fire_error_doesnt_block_other_hooks(self, hooks_dir, capsys):
        _write_hook(hooks_dir, "hook_a_crash", (
            "def handler(ctx): raise RuntimeError('fail')\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        _write_hook(hooks_dir, "hook_b_ok", (
            "called = False\n"
            "def handler(ctx):\n"
            "    global called\n"
            "    called = True\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        registry.fire("post_mission")
        assert sys.modules["koan_hook_hook_b_ok"].called is True
        captured = capsys.readouterr()
        assert "fail" in captured.err

    def test_fire_unknown_event(self, hooks_dir):
        _write_hook(hooks_dir, "hook", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        # Should not raise
        registry.fire("unknown_event")

    def test_has_hooks_false_for_unregistered(self, hooks_dir):
        _write_hook(hooks_dir, "hook", (
            "def handler(ctx): pass\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        assert not registry.has_hooks("pre_mission")

    def test_context_passed_as_dict(self, hooks_dir):
        _write_hook(hooks_dir, "ctx_check", (
            "received = None\n"
            "def handler(ctx):\n"
            "    global received\n"
            "    received = ctx\n"
            "HOOKS = {'test_event': handler}\n"
        ))
        registry = HookRegistry(hooks_dir)
        registry.fire("test_event", a=1, b="two")
        mod = sys.modules["koan_hook_ctx_check"]
        assert mod.received == {"a": 1, "b": "two"}


# ---------------------------------------------------------------------------
# Module-level convenience function tests
# ---------------------------------------------------------------------------


class TestFireHookConvenience:
    def test_fire_hook_noop_without_init(self):
        # Should not raise when registry is None
        fire_hook("post_mission", project_name="test")

    def test_fire_hook_after_init(self, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        _write_hook(hooks_dir, "tracker", (
            "calls = []\n"
            "def handler(ctx): calls.append(ctx)\n"
            "HOOKS = {'post_mission': handler}\n"
        ))
        # init_hooks expects instance_dir, hooks_dir = instance_dir/hooks
        init_hooks(str(tmp_path))
        fire_hook("post_mission", project_name="proj")
        mod = sys.modules["koan_hook_tracker"]
        assert len(mod.calls) == 1


class TestInitHooks:
    def test_creates_hooks_dir_if_missing(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        hooks_dir = instance / "hooks"
        assert not hooks_dir.exists()
        init_hooks(str(instance))
        assert hooks_dir.is_dir()

    def test_reinitializes_on_second_call(self, tmp_path):
        instance = tmp_path / "instance"
        instance.mkdir()
        init_hooks(str(instance))
        from app.hooks import get_registry
        r1 = get_registry()
        init_hooks(str(instance))
        r2 = get_registry()
        assert r1 is not r2
