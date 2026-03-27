"""Tests for the /magic skill handler — creative project exploration."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.skills import SkillContext


def _make_ctx(koan_root, instance_dir, send_message=None):
    """Create a minimal SkillContext for testing."""
    ctx = MagicMock(spec=SkillContext)
    ctx.command_name = "magic"
    ctx.koan_root = koan_root
    ctx.instance_dir = instance_dir
    ctx.args = ""
    ctx.send_message = send_message
    return ctx


class TestResolveProject:
    """Tests for _resolve_project helper."""

    def test_resolves_by_name(self):
        from skills.core.magic.handler import _resolve_project

        projects = [("koan", "/path/to/koan"), ("backend", "/path/to/backend")]
        name, path = _resolve_project(projects, "koan")
        assert name == "koan"
        assert path == "/path/to/koan"

    def test_resolves_case_insensitive(self):
        from skills.core.magic.handler import _resolve_project

        projects = [("Koan", "/path/to/koan")]
        name, path = _resolve_project(projects, "koan")
        assert name == "Koan"

    def test_returns_none_for_unknown(self):
        from skills.core.magic.handler import _resolve_project

        projects = [("koan", "/path/to/koan")]
        name, path = _resolve_project(projects, "unknown")
        assert name is None
        assert path is None


class TestHandle:
    """Tests for the main handle function."""

    def test_returns_message_when_no_projects(self, tmp_path, monkeypatch):
        from skills.core.magic.handler import handle

        monkeypatch.delenv("KOAN_PROJECTS", raising=False)

        ctx = _make_ctx(tmp_path, tmp_path)
        with patch("app.utils.get_known_projects", return_value=[]):
            result = handle(ctx)

        assert "No projects" in result

    @patch("skills.core.magic.handler.gather_git_activity", return_value="commits")
    @patch("skills.core.magic.handler.gather_project_structure", return_value="src/")
    @patch("skills.core.magic.handler.get_missions_context", return_value="No active missions.")
    def test_sends_exploring_message(
        self, mock_missions, mock_struct, mock_git, tmp_path, monkeypatch
    ):
        from skills.core.magic.handler import handle

        project_path = tmp_path / "testproj"
        project_path.mkdir()
        (tmp_path / "soul.md").write_text("Test soul")

        send_fn = MagicMock()
        ctx = _make_ctx(tmp_path, tmp_path, send_message=send_fn)

        with patch(
            "app.utils.get_known_projects",
            return_value=[("testproj", str(project_path))],
        ):
            with patch("app.cli_exec.run_cli") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="Great idea!", stderr=""
                )
                handle(ctx)

        send_fn.assert_called_once()
        assert "testproj" in send_fn.call_args[0][0]

    @patch("skills.core.magic.handler.gather_git_activity", return_value="commits")
    @patch("skills.core.magic.handler.gather_project_structure", return_value="src/")
    @patch("skills.core.magic.handler.get_missions_context", return_value="No active missions.")
    def test_handles_claude_timeout(
        self, mock_missions, mock_struct, mock_git, tmp_path, monkeypatch
    ):
        from skills.core.magic.handler import handle
        import subprocess as sp

        project_path = tmp_path / "testproj"
        project_path.mkdir()
        (tmp_path / "soul.md").write_text("Test soul")

        ctx = _make_ctx(tmp_path, tmp_path)

        with patch(
            "app.utils.get_known_projects",
            return_value=[("testproj", str(project_path))],
        ):
            with patch(
                "app.cli_exec.run_cli",
                side_effect=sp.TimeoutExpired(cmd="claude", timeout=90),
            ):
                result = handle(ctx)

        assert "Timeout" in result or "Try again" in result

    @patch("skills.core.magic.handler.gather_git_activity", return_value="commits")
    @patch("skills.core.magic.handler.gather_project_structure", return_value="src/")
    @patch("skills.core.magic.handler.get_missions_context", return_value="No active missions.")
    def test_handles_claude_error(
        self, mock_missions, mock_struct, mock_git, tmp_path, monkeypatch
    ):
        from skills.core.magic.handler import handle

        project_path = tmp_path / "testproj"
        project_path.mkdir()
        (tmp_path / "soul.md").write_text("Test soul")

        ctx = _make_ctx(tmp_path, tmp_path)

        with patch(
            "app.utils.get_known_projects",
            return_value=[("testproj", str(project_path))],
        ):
            with patch(
                "app.cli_exec.run_cli",
                side_effect=Exception("Something went wrong"),
            ):
                result = handle(ctx)

        assert "Error" in result or "Try again" in result

    @patch("skills.core.magic.handler.gather_git_activity", return_value="commits")
    @patch("skills.core.magic.handler.gather_project_structure", return_value="src/")
    @patch("skills.core.magic.handler.get_missions_context", return_value="No active missions.")
    def test_targets_specific_project(
        self, mock_missions, mock_struct, mock_git, tmp_path, monkeypatch
    ):
        """When /magic <project> is called, explore that project, not random."""
        from skills.core.magic.handler import handle

        koan_dir = tmp_path / "koan"
        koan_dir.mkdir()
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        (tmp_path / "soul.md").write_text("Test soul")

        send_fn = MagicMock()
        ctx = _make_ctx(tmp_path, tmp_path, send_message=send_fn)
        ctx.args = "backend"

        with patch(
            "app.utils.get_known_projects",
            return_value=[
                ("koan", str(koan_dir)),
                ("backend", str(backend_dir)),
            ],
        ):
            with patch("app.cli_exec.run_cli") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="Backend ideas!", stderr=""
                )
                handle(ctx)

        # Should have sent "Exploring backend..."
        send_fn.assert_called_once()
        assert "backend" in send_fn.call_args[0][0]

    def test_unknown_project_returns_error(self, tmp_path, monkeypatch):
        """When /magic <unknown> is called, return error with known list."""
        from skills.core.magic.handler import handle

        project_path = tmp_path / "koan"
        project_path.mkdir()

        ctx = _make_ctx(tmp_path, tmp_path)
        ctx.args = "nonexistent"

        with patch(
            "app.utils.get_known_projects",
            return_value=[("koan", str(project_path))],
        ):
            result = handle(ctx)

        assert "Unknown project" in result
        assert "nonexistent" in result
        assert "koan" in result

    @patch("skills.core.magic.handler.gather_git_activity", return_value="commits")
    @patch("skills.core.magic.handler.gather_project_structure", return_value="src/")
    @patch("skills.core.magic.handler.get_missions_context", return_value="No active missions.")
    def test_no_args_picks_random(
        self, mock_missions, mock_struct, mock_git, tmp_path, monkeypatch
    ):
        """When /magic is called without args, random project is picked."""
        from skills.core.magic.handler import handle

        project_path = tmp_path / "testproj"
        project_path.mkdir()
        (tmp_path / "soul.md").write_text("Test soul")

        ctx = _make_ctx(tmp_path, tmp_path)
        ctx.args = ""  # no args

        with patch(
            "app.utils.get_known_projects",
            return_value=[("testproj", str(project_path))],
        ):
            with patch("app.cli_exec.run_cli") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="Ideas!", stderr=""
                )
                result = handle(ctx)

        assert isinstance(result, str)
