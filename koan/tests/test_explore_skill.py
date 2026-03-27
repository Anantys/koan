"""Tests for the /explore and /noexplore core skill — per-project exploration toggle."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Import handler
# ---------------------------------------------------------------------------

HANDLER_PATH = (
    Path(__file__).parent.parent / "skills" / "core" / "explore" / "handler.py"
)


def _load_handler():
    spec = importlib.util.spec_from_file_location("explore_handler", str(HANDLER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def handler():
    return _load_handler()


@pytest.fixture
def ctx(tmp_path):
    """Create a SkillContext for explore tests."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="explore",
        args="",
        send_message=MagicMock(),
    )


def _make_config(*project_names, exploration_overrides=None):
    """Build a minimal projects.yaml dict."""
    projects = {}
    for name in project_names:
        projects[name] = {"path": f"/workspace/{name}"}
    if exploration_overrides:
        for name, val in exploration_overrides.items():
            if name in projects:
                projects[name]["exploration"] = val
    return {"projects": projects}


# ===========================================================================
# _resolve_project_name
# ===========================================================================


class TestResolveProjectName:
    def test_exact_match(self, handler):
        projects = {"koan": {}, "web-app": {}}
        assert handler._resolve_project_name(projects, "koan") == "koan"

    def test_case_insensitive(self, handler):
        projects = {"Koan": {}, "WebApp": {}}
        assert handler._resolve_project_name(projects, "koan") == "Koan"
        assert handler._resolve_project_name(projects, "webapp") == "WebApp"

    def test_unknown_returns_none(self, handler):
        projects = {"koan": {}}
        assert handler._resolve_project_name(projects, "unknown") is None


# ===========================================================================
# _load_config
# ===========================================================================


class TestLoadConfig:
    @patch("app.projects_config.load_projects_config", return_value={"projects": {}})
    def test_success(self, mock_load, handler):
        result = handler._load_config("/some/root")
        assert result == {"projects": {}}

    @patch("app.projects_config.load_projects_config", side_effect=ValueError("bad"))
    def test_value_error_returns_none(self, mock_load, handler):
        assert handler._load_config("/some/root") is None

    @patch("app.projects_config.load_projects_config", side_effect=OSError("missing"))
    def test_os_error_returns_none(self, mock_load, handler):
        assert handler._load_config("/some/root") is None


# ===========================================================================
# _show_status
# ===========================================================================


class TestShowStatus:
    def test_all_enabled(self, handler):
        config = _make_config("alpha", "beta")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=True):
            result = handler._show_status(config, projects)

        assert "alpha: ON" in result
        assert "beta: ON" in result

    def test_mixed_status(self, handler):
        config = _make_config("alpha", "beta")
        projects = config["projects"]

        def mock_status(cfg, name):
            return name == "alpha"

        with patch.object(handler, "_get_exploration_status", side_effect=mock_status):
            result = handler._show_status(config, projects)

        assert "alpha: ON" in result
        assert "beta: OFF" in result

    def test_sorted_alphabetically(self, handler):
        config = _make_config("zeta", "alpha", "middle")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=True):
            result = handler._show_status(config, projects)

        lines = result.split("\n")
        project_lines = [l for l in lines if ":" in l and ("ON" in l or "OFF" in l)]
        names = [l.strip().split()[-2].rstrip(":") for l in project_lines]
        assert names == sorted(names, key=str.lower)

    def test_includes_usage_hints(self, handler):
        config = _make_config("koan")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=True):
            result = handler._show_status(config, projects)

        assert "/explore" in result
        assert "/noexplore" in result


# ===========================================================================
# _set_exploration
# ===========================================================================


class TestSetExploration:
    def test_enable_project(self, handler, tmp_path):
        config = _make_config("koan")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=False), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_exploration(str(tmp_path), config, projects, "koan", True)

        assert "enabled" in result
        assert "koan" in result
        mock_save.assert_called_once()
        assert projects["koan"]["exploration"] is True

    def test_disable_project(self, handler, tmp_path):
        config = _make_config("koan")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_exploration(str(tmp_path), config, projects, "koan", False)

        assert "disabled" in result
        mock_save.assert_called_once()
        assert projects["koan"]["exploration"] is False

    def test_already_enabled(self, handler, tmp_path):
        config = _make_config("koan")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_exploration(str(tmp_path), config, projects, "koan", True)

        assert "already enabled" in result
        mock_save.assert_not_called()

    def test_already_disabled(self, handler, tmp_path):
        config = _make_config("koan")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=False), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_exploration(str(tmp_path), config, projects, "koan", False)

        assert "already disabled" in result
        mock_save.assert_not_called()

    def test_unknown_project(self, handler, tmp_path):
        config = _make_config("koan")
        projects = config["projects"]

        result = handler._set_exploration(str(tmp_path), config, projects, "unknown", True)
        assert "Unknown project" in result
        assert "koan" in result  # known projects listed

    def test_case_insensitive_name(self, handler, tmp_path):
        config = _make_config("Koan")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=False), \
             patch.object(handler, "_save_config"):
            result = handler._set_exploration(str(tmp_path), config, projects, "koan", True)

        assert "enabled" in result
        assert "Koan" in result  # uses canonical name

    def test_workspace_project_auto_creates_yaml_entry(self, handler, tmp_path):
        """Workspace project not in yaml should be auto-added."""
        config = _make_config("koan")
        projects = config["projects"]

        # Create workspace/myapp directory
        ws_dir = tmp_path / "workspace" / "myapp"
        ws_dir.mkdir(parents=True)

        with patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_exploration(str(tmp_path), config, projects, "myapp", False)

        assert "disabled" in result
        assert "myapp" in result
        # Entry was created in config
        assert "myapp" in projects
        assert projects["myapp"]["path"] == str(ws_dir.resolve())
        assert projects["myapp"]["exploration"] is False
        mock_save.assert_called_once()

    def test_workspace_project_case_insensitive(self, handler, tmp_path):
        """Workspace project lookup should be case-insensitive."""
        config = _make_config("koan")
        projects = config["projects"]

        ws_dir = tmp_path / "workspace" / "MyApp"
        ws_dir.mkdir(parents=True)

        with patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config"):
            result = handler._set_exploration(str(tmp_path), config, projects, "myapp", False)

        assert "disabled" in result
        assert "MyApp" in result  # canonical name from workspace

    def test_unknown_project_not_in_workspace_either(self, handler, tmp_path):
        """Project not in yaml AND not in workspace should still fail."""
        config = _make_config("koan")
        projects = config["projects"]

        # Create workspace dir but no matching project
        (tmp_path / "workspace").mkdir()

        result = handler._set_exploration(str(tmp_path), config, projects, "ghost", True)
        assert "Unknown project" in result


# ===========================================================================
# _set_all
# ===========================================================================


class TestSetAll:
    def test_enable_all(self, handler, tmp_path):
        config = _make_config("alpha", "beta")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=False), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_all(str(tmp_path), config, projects, True)

        assert "enabled" in result
        assert "2 project(s)" in result
        mock_save.assert_called_once()

    def test_disable_all(self, handler, tmp_path):
        config = _make_config("alpha", "beta")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_all(str(tmp_path), config, projects, False)

        assert "disabled" in result
        assert "2 project(s)" in result

    def test_all_already_enabled(self, handler, tmp_path):
        config = _make_config("alpha", "beta")
        projects = config["projects"]

        with patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config") as mock_save:
            result = handler._set_all(str(tmp_path), config, projects, True)

        assert "already enabled" in result
        mock_save.assert_not_called()

    def test_partial_change(self, handler, tmp_path):
        config = _make_config("alpha", "beta")
        projects = config["projects"]

        def mock_status(cfg, name):
            return name == "alpha"  # alpha already enabled, beta not

        with patch.object(handler, "_get_exploration_status", side_effect=mock_status), \
             patch.object(handler, "_save_config"):
            result = handler._set_all(str(tmp_path), config, projects, True)

        assert "1 project(s)" in result


# ===========================================================================
# handle() — main entry point
# ===========================================================================


class TestHandle:
    def test_no_config_returns_error(self, handler, ctx):
        with patch.object(handler, "_load_config", return_value=None):
            result = handler.handle(ctx)
        assert "No projects.yaml" in result

    def test_empty_projects_returns_error(self, handler, ctx):
        with patch.object(handler, "_load_config", return_value={"projects": {}}):
            result = handler.handle(ctx)
        assert "No projects configured" in result

    def test_no_args_shows_status(self, handler, ctx):
        config = _make_config("koan")
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=True):
            result = handler.handle(ctx)
        assert "Exploration status" in result

    def test_explore_specific_project(self, handler, ctx):
        config = _make_config("koan")
        ctx.args = "koan"
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=False), \
             patch.object(handler, "_save_config"):
            result = handler.handle(ctx)
        assert "enabled" in result

    def test_noexplore_command(self, handler, ctx):
        config = _make_config("koan")
        ctx = SkillContext(
            koan_root=ctx.koan_root,
            instance_dir=ctx.instance_dir,
            command_name="noexplore",
            args="koan",
            send_message=MagicMock(),
        )
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config"):
            result = handler.handle(ctx)
        assert "disabled" in result

    def test_explore_all(self, handler, ctx):
        config = _make_config("alpha", "beta")
        ctx.args = "all"
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=False), \
             patch.object(handler, "_save_config"):
            result = handler.handle(ctx)
        assert "enabled" in result
        assert "2 project(s)" in result

    def test_explore_none(self, handler, ctx):
        config = _make_config("alpha", "beta")
        ctx.args = "none"
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config"):
            result = handler.handle(ctx)
        assert "disabled" in result

    def test_explore_all_case_insensitive(self, handler, ctx):
        config = _make_config("koan")
        ctx.args = "ALL"
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=False), \
             patch.object(handler, "_save_config"):
            result = handler.handle(ctx)
        assert "enabled" in result

    def test_empty_args_string(self, handler, ctx):
        config = _make_config("koan")
        ctx.args = "   "
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=True):
            result = handler.handle(ctx)
        assert "Exploration status" in result

    def test_none_args(self, handler, ctx):
        config = _make_config("koan")
        ctx.args = None
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=True):
            result = handler.handle(ctx)
        assert "Exploration status" in result

    def test_workspace_project_with_empty_yaml(self, handler, ctx):
        """Workspace project should be auto-added even when projects.yaml has no projects."""
        ws_dir = ctx.koan_root / "workspace" / "myapp"
        ws_dir.mkdir(parents=True)

        config = {"projects": {}}
        ctx.args = "myapp"
        with patch.object(handler, "_load_config", return_value=config), \
             patch.object(handler, "_get_exploration_status", return_value=True), \
             patch.object(handler, "_save_config"):
            result = handler.handle(ctx)
        assert "enabled" in result or "disabled" in result
        assert "myapp" in result
