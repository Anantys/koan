"""Tests for app.project_explorer — shared project exploration utilities."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.project_explorer import (
    gather_git_activity,
    gather_project_structure,
    get_missions_context,
    get_projects,
)


# ---------------------------------------------------------------------------
# get_projects
# ---------------------------------------------------------------------------

class TestGetProjects:
    @patch("app.utils.get_known_projects")
    def test_returns_existing_dirs(self, mock_known, tmp_path):
        project_dir = tmp_path / "myapp"
        project_dir.mkdir()
        mock_known.return_value = [("myapp", str(project_dir))]
        result = get_projects()
        assert result == [("myapp", str(project_dir))]

    @patch("app.utils.get_known_projects")
    def test_filters_nonexistent_paths(self, mock_known):
        mock_known.return_value = [("ghost", "/nonexistent/path")]
        result = get_projects()
        assert result == []

    @patch("app.utils.get_known_projects")
    def test_mixed_existing_and_missing(self, mock_known, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        mock_known.return_value = [
            ("real", str(real)),
            ("fake", "/no/such/path"),
        ]
        result = get_projects()
        assert len(result) == 1
        assert result[0][0] == "real"

    @patch("app.utils.get_known_projects", side_effect=Exception("boom"))
    def test_exception_returns_empty(self, mock_known):
        result = get_projects()
        assert result == []

    @patch("app.utils.get_known_projects", return_value=[])
    def test_empty_config_returns_empty(self, mock_known):
        result = get_projects()
        assert result == []


# ---------------------------------------------------------------------------
# gather_git_activity
# ---------------------------------------------------------------------------

class TestGatherGitActivity:
    @patch("app.project_explorer.run_git")
    def test_includes_recent_commits(self, mock_git):
        mock_git.return_value = "abc1234 fix login\ndef5678 add tests"
        result = gather_git_activity("/tmp")
        assert "fix login" in result

    @patch("app.project_explorer.run_git", return_value="")
    def test_handles_empty_output(self, mock_git):
        result = gather_git_activity("/tmp")
        assert "No git activity" in result

    @patch("app.project_explorer.run_git")
    def test_includes_branches(self, mock_git):
        mock_git.return_value = "origin/main\norigin/feature-x"
        result = gather_git_activity("/tmp")
        assert "origin/main" in result

    @patch("app.project_explorer.run_git", return_value="")
    def test_git_failure_returns_no_activity(self, mock_git):
        result = gather_git_activity("/tmp")
        assert "No git activity" in result

    @patch("app.project_explorer.run_git")
    def test_includes_diff_stat(self, mock_git):
        mock_git.return_value = "3 files changed, 10 insertions"
        result = gather_git_activity("/tmp")
        assert "3 files changed" in result

    @patch("app.project_explorer.run_git")
    def test_limits_branches_to_10(self, mock_git):
        branches = "\n".join(f"origin/branch-{i}" for i in range(20))
        # run_git called 3 times: log, branch, diff
        mock_git.side_effect = ["", branches, ""]
        result = gather_git_activity("/tmp")
        assert "branch-9" in result
        assert "branch-10" not in result

    @patch("app.project_explorer.run_git")
    def test_combines_all_sections(self, mock_git):
        """Each git command returns data; all sections present."""
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            return f"data-{call_count[0]}"
        mock_git.side_effect = side_effect
        result = gather_git_activity("/tmp")
        assert "Recent commits:" in result
        assert "Active branches:" in result
        assert "Recent changes:" in result

    @patch("app.project_explorer.run_git")
    def test_diffstat_uses_log_not_head_ref(self, mock_git):
        """Recent changes section uses 'git log --stat' not 'HEAD~10'
        to avoid failures on repos with fewer than 10 commits."""
        mock_git.return_value = "some data"
        gather_git_activity("/tmp")
        # Check the third call (diffstat) uses "log" not "diff"
        calls = mock_git.call_args_list
        diffstat_call = calls[2]  # Third call is the diff/stat call
        args = diffstat_call[0]   # Positional args
        assert "log" in args, f"Expected 'log' in diffstat args, got {args}"
        assert "HEAD~10" not in str(args), "Should not reference HEAD~10"


# ---------------------------------------------------------------------------
# gather_project_structure
# ---------------------------------------------------------------------------

class TestGatherProjectStructure:
    def test_lists_dirs_and_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "README.md").write_text("hello")
        (tmp_path / ".hidden").write_text("skip")

        result = gather_project_structure(str(tmp_path))
        assert "src/" in result
        assert "tests/" in result
        assert "README.md" in result
        assert ".hidden" not in result

    def test_handles_nonexistent_path(self):
        result = gather_project_structure("/nonexistent/path")
        assert "unavailable" in result.lower()

    def test_skips_hidden_dirs(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "src").mkdir()
        result = gather_project_structure(str(tmp_path))
        assert ".git" not in result
        assert "src/" in result

    def test_empty_directory(self, tmp_path):
        result = gather_project_structure(str(tmp_path))
        assert result == ""

    def test_limits_to_20_entries(self, tmp_path):
        for i in range(25):
            (tmp_path / f"dir{i:02d}").mkdir()
        result = gather_project_structure(str(tmp_path))
        assert "dir19" in result
        assert "dir20" not in result


# ---------------------------------------------------------------------------
# get_missions_context
# ---------------------------------------------------------------------------

class TestGetMissionsContext:
    def test_returns_in_progress_and_pending(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- pending task\n\n"
            "## In Progress\n\n- active task\n\n## Done\n"
        )
        result = get_missions_context(tmp_path)
        assert "active task" in result
        assert "pending task" in result

    def test_returns_no_active_when_empty(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        result = get_missions_context(tmp_path)
        assert "No active" in result

    def test_handles_missing_file(self, tmp_path):
        result = get_missions_context(tmp_path)
        assert "No active" in result

    def test_limits_entries(self, tmp_path):
        """Should limit to 5 entries per section."""
        missions_file = tmp_path / "missions.md"
        pending = "\n".join(f"- task {i}" for i in range(10))
        missions_file.write_text(
            f"# Missions\n\n## Pending\n\n{pending}\n\n"
            "## In Progress\n\n## Done\n"
        )
        result = get_missions_context(tmp_path)
        assert "task 4" in result
        assert "task 5" not in result

    def test_only_pending(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n- fix auth\n\n"
            "## In Progress\n\n## Done\n"
        )
        result = get_missions_context(tmp_path)
        assert "fix auth" in result
        assert "In progress" not in result

    def test_only_in_progress(self, tmp_path):
        missions_file = tmp_path / "missions.md"
        missions_file.write_text(
            "# Missions\n\n## Pending\n\n"
            "## In Progress\n\n- working on it\n\n## Done\n"
        )
        result = get_missions_context(tmp_path)
        assert "working on it" in result
        assert "Pending" not in result
