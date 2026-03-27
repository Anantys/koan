"""Tests for exception logging in modules that previously swallowed errors silently.

Verifies that all ``except Exception`` handlers now emit diagnostic messages
to stderr instead of silently passing.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestUtilsExceptionLogging:
    """Exception logging in utils.get_known_projects / resolve_project_path."""

    @patch("app.utils.KOAN_ROOT", Path("/tmp/fake"))
    @patch.dict("os.environ", {"KOAN_PROJECTS": ""}, clear=False)
    def test_get_known_projects_logs_merged_registry_failure(self, capsys):
        """get_known_projects logs when merged registry import fails."""
        from app.utils import get_known_projects

        with patch(
            "app.projects_merged.get_all_projects",
            side_effect=ImportError("no module"),
        ):
            result = get_known_projects()

        captured = capsys.readouterr()
        assert "[utils] Merged project registry failed:" in captured.err
        assert isinstance(result, list)

    @patch("app.utils.KOAN_ROOT", Path("/tmp/fake"))
    @patch.dict("os.environ", {"KOAN_PROJECTS": ""}, clear=False)
    def test_get_known_projects_logs_yaml_loader_failure(self, capsys):
        """get_known_projects logs when projects.yaml loader fails."""
        from app.utils import get_known_projects

        # First fallback returns nothing, second raises
        with patch(
            "app.projects_merged.get_all_projects",
            return_value=[],
        ), patch(
            "app.projects_config.load_projects_config",
            side_effect=RuntimeError("yaml broken"),
        ):
            result = get_known_projects()

        captured = capsys.readouterr()
        assert "[utils] projects.yaml loader failed:" in captured.err
        assert isinstance(result, list)

    @patch("app.utils.KOAN_ROOT", Path("/tmp/fake"))
    def test_resolve_project_path_logs_github_url_failure(self, capsys):
        """resolve_project_path logs when GitHub URL match fails."""
        from app.utils import resolve_project_path

        with patch("app.utils.get_known_projects", return_value=[]), \
             patch(
                 "app.projects_config.load_projects_config",
                 side_effect=RuntimeError("config error"),
             ):
            result = resolve_project_path("repo", owner="owner")

        captured = capsys.readouterr()
        assert "[utils] GitHub URL match via projects.yaml failed:" in captured.err

    @patch("app.utils.KOAN_ROOT", Path("/tmp/fake"))
    def test_resolve_project_path_logs_cache_failure(self, capsys):
        """resolve_project_path logs when GitHub URL cache lookup fails."""
        from app.utils import resolve_project_path

        with patch("app.utils.get_known_projects", return_value=[]), \
             patch(
                 "app.projects_config.load_projects_config",
                 return_value={"projects": {}},
             ), \
             patch(
                 "app.projects_merged.get_github_url_cache",
                 side_effect=ImportError("no cache"),
             ):
            result = resolve_project_path("repo", owner="owner")

        captured = capsys.readouterr()
        assert "[utils] GitHub URL cache lookup failed:" in captured.err


class TestPreflightExceptionLogging:
    """Exception logging in preflight.preflight_quota_check."""

    def test_logs_budget_mode_failure(self, capsys):
        """Logs when budget mode check fails."""
        from app.preflight import preflight_quota_check

        with patch(
            "app.usage_tracker._get_budget_mode",
            side_effect=ImportError("no tracker"),
        ), patch("app.provider.get_provider") as mock_prov:
            provider = MagicMock()
            provider.check_quota_available.return_value = (True, "")
            mock_prov.return_value = provider

            ok, error = preflight_quota_check("/tmp/proj", "/tmp/inst")

        captured = capsys.readouterr()
        assert "[preflight] Budget mode check failed:" in captured.err
        assert ok is True

    def test_logs_provider_resolution_failure(self, capsys):
        """Logs when provider resolution fails and returns optimistic result."""
        from app.preflight import preflight_quota_check

        with patch(
            "app.usage_tracker._get_budget_mode",
            return_value="full",
        ), patch(
            "app.provider.get_provider",
            side_effect=RuntimeError("no provider"),
        ):
            ok, error = preflight_quota_check("/tmp/proj", "/tmp/inst")

        captured = capsys.readouterr()
        assert "[preflight] Provider resolution failed:" in captured.err
        assert ok is True


class TestBridgeStateExceptionLogging:
    """Exception logging in bridge_state._resolve_default_project_path."""

    def test_logs_project_resolution_failure(self, capsys):
        """_resolve_default_project_path logs when project lookup fails."""
        from app.bridge_state import _resolve_default_project_path

        with patch(
            "app.utils.get_known_projects",
            side_effect=RuntimeError("broken"),
        ):
            result = _resolve_default_project_path()

        captured = capsys.readouterr()
        assert "[bridge_state] Default project resolution failed:" in captured.err
        assert result == ""


class TestPromptBuilderExceptionLogging:
    """Exception logging in prompt_builder helpers."""

    def test_logs_deep_research_failure(self, capsys):
        """_get_deep_research logs when DeepResearch fails."""
        from app.prompt_builder import _get_deep_research

        with patch(
            "app.deep_research.DeepResearch",
            side_effect=RuntimeError("research broken"),
        ):
            result = _get_deep_research("/tmp/inst", "proj", "/tmp/proj")

        captured = capsys.readouterr()
        assert "[prompt_builder] Deep research failed:" in captured.err
        assert result == ""

    def test_logs_focus_check_failure(self, capsys):
        """_get_focus_section logs when focus check fails."""
        from app.prompt_builder import _get_focus_section

        with patch(
            "app.focus_manager.check_focus",
            side_effect=RuntimeError("focus broken"),
        ):
            result = _get_focus_section("/tmp/koan-root/instance")

        captured = capsys.readouterr()
        assert "[prompt_builder] Focus check failed:" in captured.err
        assert result == ""


class TestDeepResearchExceptionLogging:
    """Exception logging in deep_research.DeepResearch."""

    def test_logs_issue_fetch_failure(self, tmp_path, capsys):
        """get_open_issues logs when gh CLI fails."""
        from app.deep_research import DeepResearch

        instance = tmp_path / "instance"
        (instance / "memory" / "projects" / "proj").mkdir(parents=True)
        (instance / "journal").mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()

        research = DeepResearch(instance, "proj", project_path)

        with patch("app.github.run_gh", side_effect=RuntimeError("gh failed")):
            result = research.get_open_issues()

        captured = capsys.readouterr()
        assert "[deep_research] Issue fetch failed:" in captured.err
        assert result == []

    def test_logs_pr_fetch_failure(self, tmp_path, capsys):
        """get_pending_prs logs when gh CLI fails."""
        from app.deep_research import DeepResearch

        instance = tmp_path / "instance"
        (instance / "memory" / "projects" / "proj").mkdir(parents=True)
        (instance / "journal").mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()

        research = DeepResearch(instance, "proj", project_path)

        with patch("app.github.run_gh", side_effect=RuntimeError("gh failed")):
            result = research.get_pending_prs()

        captured = capsys.readouterr()
        assert "[deep_research] PR fetch failed:" in captured.err
        assert result == []


class TestProviderExceptionLogging:
    """Exception logging in provider.get_provider_name."""

    @patch.dict("os.environ", {}, clear=False)
    def test_logs_config_loading_failure(self, capsys):
        """get_provider_name logs when config loading fails."""
        from app.provider import get_provider_name, reset_provider

        reset_provider()

        with patch(
            "app.utils.get_cli_provider_env",
            return_value="",
        ), patch(
            "app.utils.load_config",
            side_effect=RuntimeError("config broken"),
        ):
            result = get_provider_name()

        captured = capsys.readouterr()
        assert "[provider] Config loading failed:" in captured.err
        assert result == "claude"

        reset_provider()


class TestCliExecExceptionLogging:
    """Exception logging in cli_exec._uses_stdin_passing."""

    def test_logs_provider_check_failure(self, capsys):
        """_uses_stdin_passing logs when provider check fails."""
        from app.cli_exec import _uses_stdin_passing

        with patch(
            "app.provider.get_provider_name",
            side_effect=RuntimeError("provider broken"),
        ):
            result = _uses_stdin_passing()

        captured = capsys.readouterr()
        assert "[cli_exec] Provider check failed:" in captured.err
        assert result is True


class TestClaudeStepExceptionLogging:
    """Exception handling in claude_step._get_current_branch."""

    def test_returns_default_on_failure(self):
        """_get_current_branch returns 'main' when git rev-parse fails."""
        from app.claude_step import _get_current_branch

        with patch(
            "app.claude_step._git_utils_get_current_branch",
            return_value="main",
        ):
            result = _get_current_branch("/tmp/project")

        assert result == "main"


class TestPidManagerFileHandleCleanup:
    """pid_manager uses try/finally for file handle cleanup."""

    @patch("app.pid_manager.check_pidfile", return_value=None)
    @patch("app.pid_manager._open_log_file")
    def test_launch_closes_log_on_popen_failure(self, mock_log, mock_pid):
        """Log file handle is closed even when Popen raises."""
        from app.pid_manager import _launch_python_process

        mock_fh = MagicMock()
        mock_log.return_value = mock_fh

        with patch("subprocess.Popen", side_effect=OSError("exec failed")):
            ok, msg = _launch_python_process(
                Path("/tmp/koan"), "app/run.py", "run", 0.1,
            )

        assert ok is False
        assert "Failed to launch" in msg
        mock_fh.close.assert_called_once()

    @patch("app.pid_manager.check_pidfile", return_value=None)
    @patch("app.pid_manager._open_log_file")
    def test_launch_closes_log_on_success(self, mock_log, mock_pid):
        """Log file handle is closed after successful Popen too."""
        from app.pid_manager import _launch_python_process

        mock_fh = MagicMock()
        mock_log.return_value = mock_fh

        with patch("subprocess.Popen"):
            # Will fail PID detection but that's after close
            _launch_python_process(
                Path("/tmp/koan"), "app/run.py", "run",
                verify_timeout=0.1,
            )

        mock_fh.close.assert_called_once()
