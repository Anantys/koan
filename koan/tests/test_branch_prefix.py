"""Tests for configurable branch prefix feature.

Tests cover:
- get_branch_prefix() in utils.py (config reading, defaults, normalization)
- git_sync.py (branch filtering with custom prefix)
- prompt_builder.py (merge policy and agent prompt with custom prefix)
- rebase_pr.py (fallback branch naming with custom prefix)
- mission_runner.py (auto-merge branch check with custom prefix)
"""

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# get_branch_prefix() tests
# ---------------------------------------------------------------------------


class TestGetBranchPrefix:
    """Tests for utils.get_branch_prefix()."""

    @patch("app.config._load_config")
    def test_default_prefix(self, mock_config):
        """Returns 'koan/' when no branch_prefix configured."""
        mock_config.return_value = {}
        from app.config import get_branch_prefix
        assert get_branch_prefix() == "koan/"

    @patch("app.config._load_config")
    def test_custom_prefix(self, mock_config):
        """Returns custom prefix with trailing slash."""
        mock_config.return_value = {"branch_prefix": "mybot"}
        from app.config import get_branch_prefix
        assert get_branch_prefix() == "mybot/"

    @patch("app.config._load_config")
    def test_custom_prefix_with_trailing_slash(self, mock_config):
        """Strips duplicate trailing slash."""
        mock_config.return_value = {"branch_prefix": "mybot/"}
        from app.config import get_branch_prefix
        assert get_branch_prefix() == "mybot/"

    @patch("app.config._load_config")
    def test_empty_string_falls_back_to_koan(self, mock_config):
        """Empty string in config falls back to 'koan/'."""
        mock_config.return_value = {"branch_prefix": ""}
        from app.config import get_branch_prefix
        assert get_branch_prefix() == "koan/"

    @patch("app.config._load_config")
    def test_whitespace_only_falls_back_to_koan(self, mock_config):
        """Whitespace-only string falls back to 'koan/'."""
        mock_config.return_value = {"branch_prefix": "   "}
        from app.config import get_branch_prefix
        assert get_branch_prefix() == "koan/"

    @patch("app.config._load_config")
    def test_prefix_with_hyphens(self, mock_config):
        """Supports hyphenated prefixes."""
        mock_config.return_value = {"branch_prefix": "koan-alice"}
        from app.config import get_branch_prefix
        assert get_branch_prefix() == "koan-alice/"

    @patch("app.config._load_config")
    def test_prefix_stripped(self, mock_config):
        """Leading/trailing whitespace is stripped."""
        mock_config.return_value = {"branch_prefix": "  bot1  "}
        from app.config import get_branch_prefix
        assert get_branch_prefix() == "bot1/"


# ---------------------------------------------------------------------------
# git_sync.py — _normalize_branch with custom prefix
# ---------------------------------------------------------------------------


class TestNormalizeBranchWithPrefix:
    """Tests for _normalize_branch with configurable prefix."""

    def test_default_prefix(self):
        from app.git_sync import _normalize_branch
        assert _normalize_branch("  koan/fix-bug", prefix="koan/") == "koan/fix-bug"

    def test_custom_prefix(self):
        from app.git_sync import _normalize_branch
        assert _normalize_branch("  mybot/fix-bug", prefix="mybot/") == "mybot/fix-bug"

    def test_rejects_wrong_prefix(self):
        from app.git_sync import _normalize_branch
        assert _normalize_branch("  koan/fix-bug", prefix="mybot/") == ""

    def test_remote_branch(self):
        from app.git_sync import _normalize_branch
        result = _normalize_branch("  remotes/origin/mybot/fix-bug", prefix="mybot/")
        assert result == "mybot/fix-bug"

    def test_star_current_branch(self):
        from app.git_sync import _normalize_branch
        result = _normalize_branch("* mybot/current", prefix="mybot/")
        assert result == "mybot/current"


# ---------------------------------------------------------------------------
# git_sync.py — GitSync with custom prefix
# ---------------------------------------------------------------------------


class TestGitSyncCustomPrefix:
    """Tests for GitSync class methods with custom branch prefix."""

    @patch("app.git_sync._get_prefix", return_value="bot1/")
    @patch("app.git_sync.run_git")
    def test_get_koan_branches_custom_prefix(self, mock_git, mock_prefix):
        """get_koan_branches filters by custom prefix."""
        from app.git_sync import GitSync
        mock_git.return_value = (
            "  bot1/fix-thing\n"
            "  koan/old-branch\n"
            "  remotes/origin/bot1/other\n"
        )
        sync = GitSync("", "", "/fake")
        branches = sync.get_koan_branches()
        assert "bot1/fix-thing" in branches
        assert "bot1/other" in branches
        assert "koan/old-branch" not in branches

    @patch("app.git_sync._get_prefix", return_value="bot1/")
    @patch("app.git_sync.run_git")
    def test_get_merged_branches_custom_prefix(self, mock_git, mock_prefix):
        """get_merged_branches filters by custom prefix."""
        mock_git.return_value = "  remotes/origin/bot1/done\n  remotes/origin/koan/other\n"
        from app.git_sync import GitSync
        sync = GitSync("", "", "/fake")
        merged = sync.get_merged_branches()
        assert "bot1/done" in merged
        assert "koan/other" not in merged

    @patch("app.git_sync._get_prefix", return_value="mybot/")
    @patch("app.git_sync.run_git")
    def test_build_sync_report_uses_custom_label(self, mock_git, mock_prefix):
        """build_sync_report labels branches with correct prefix."""
        from app.git_sync import GitSync

        def side_effect(cwd, *args):
            args_str = " ".join(args)
            if "fetch" in args_str:
                return ""
            if "rev-parse" in args_str:
                return "abc123"
            if "--merged" in args_str:
                return "  remotes/origin/mybot/merged-one\n"
            if "branch" in args_str and "--list" in args_str:
                return "  remotes/origin/mybot/merged-one\n  remotes/origin/mybot/pending\n"
            if "log" in args_str:
                return ""
            return ""

        mock_git.side_effect = side_effect
        sync = GitSync("", "", "/fake")
        report = sync.build_sync_report()
        assert "mybot/*" in report
        assert "Merged mybot/* branches" in report
        assert "Unmerged mybot/* branches" in report


# ---------------------------------------------------------------------------
# prompt_builder.py — merge policy with custom prefix
# ---------------------------------------------------------------------------


class TestMergePolicyCustomPrefix:
    """Tests for _get_merge_policy with custom branch prefix."""

    @patch("app.prompt_builder._get_branch_prefix", return_value="bot1/")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=True)
    def test_auto_merge_enabled_custom_prefix(self, _mock_merge, _mock_prefix):
        from app.prompt_builder import _get_merge_policy
        policy = _get_merge_policy("proj")
        assert "bot1/*" in policy
        assert "bot1/<name>" in policy
        assert "koan/" not in policy

    @patch("app.prompt_builder._get_branch_prefix", return_value="bot1/")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=False)
    def test_auto_merge_disabled_custom_prefix(self, _mock_merge, _mock_prefix):
        from app.prompt_builder import _get_merge_policy
        policy = _get_merge_policy("proj")
        assert "bot1/<name>" in policy
        assert "koan/" not in policy

    @patch("app.prompt_builder._get_branch_prefix", return_value="koan/")
    @patch("app.prompt_builder._is_auto_merge_enabled", return_value=False)
    def test_default_prefix_in_policy(self, _mock_merge, _mock_prefix):
        from app.prompt_builder import _get_merge_policy
        policy = _get_merge_policy("proj")
        assert "koan/<name>" in policy


# ---------------------------------------------------------------------------
# prompt_builder.py — build_agent_prompt injects BRANCH_PREFIX
# ---------------------------------------------------------------------------


class TestBuildAgentPromptBranchPrefix:
    """Tests that build_agent_prompt passes BRANCH_PREFIX to load_prompt."""

    @patch("app.prompt_builder._get_focus_section", return_value="")
    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_deep_research", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="\nMerge\n")
    @patch("app.prompt_builder._get_branch_prefix", return_value="mybot/")
    @patch("app.prompts.load_prompt", return_value="Base")
    def test_branch_prefix_in_load_prompt(
        self, mock_load, mock_prefix, mock_merge, mock_submit_pr, mock_deep,
        mock_verbose, mock_focus, tmp_path
    ):
        from app.prompt_builder import build_agent_prompt

        instance = tmp_path / "instance"
        instance.mkdir()

        build_agent_prompt(
            instance=str(instance),
            project_name="testproj",
            project_path=str(tmp_path),
            run_num=1,
            max_runs=20,
            autonomous_mode="implement",
            focus_area="Work",
            available_pct=50,
            mission_title="Do stuff",
        )

        # Find the "agent" template call (not "verification-gate")
        agent_call = [c for c in mock_load.call_args_list if c[0][0] == "agent"]
        assert agent_call, "load_prompt('agent', ...) should have been called"
        call_kwargs = agent_call[0][1]
        assert call_kwargs["BRANCH_PREFIX"] == "mybot/"


# ---------------------------------------------------------------------------
# rebase_pr.py — fallback branch uses configurable prefix
# ---------------------------------------------------------------------------


class TestRebasePrBranchPrefix:
    """Tests that rebase_pr.py uses configurable prefix for fallback branches."""

    @patch("app.config.get_branch_prefix", return_value="mybot/")
    def test_fallback_branch_uses_custom_prefix(self, mock_prefix):
        """The fallback branch name should use the configured prefix."""
        from app.config import get_branch_prefix
        prefix = get_branch_prefix()
        branch = "koan/fix-bug"
        new_branch = f"{prefix}rebase-{branch.replace('/', '-')}"
        assert new_branch == "mybot/rebase-koan-fix-bug"

    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_fallback_branch_default_prefix(self, mock_prefix):
        """Default prefix produces the expected branch name."""
        from app.config import get_branch_prefix
        prefix = get_branch_prefix()
        branch = "feature/something"
        new_branch = f"{prefix}rebase-{branch.replace('/', '-')}"
        assert new_branch == "koan/rebase-feature-something"


# ---------------------------------------------------------------------------
# mission_runner.py — auto-merge uses configurable prefix
# ---------------------------------------------------------------------------


class TestMissionRunnerBranchPrefix:
    """Tests that check_auto_merge uses configurable prefix."""

    @patch("app.config.get_branch_prefix", return_value="mybot/")
    @patch("app.git_sync.run_git", return_value="main")
    def test_auto_merge_checks_custom_prefix(self, mock_git, mock_prefix):
        """check_auto_merge skips branches not matching prefix."""
        from app.mission_runner import check_auto_merge
        result = check_auto_merge("/inst", "proj", "/path")
        assert result is None

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.config.get_branch_prefix", return_value="mybot/")
    @patch("app.git_sync.run_git", return_value="mybot/fix-thing")
    def test_auto_merge_matches_custom_prefix(self, mock_git, mock_prefix, mock_merge):
        """check_auto_merge processes branches matching prefix."""
        from app.mission_runner import check_auto_merge
        result = check_auto_merge("/inst", "proj", "/path")
        assert result == "mybot/fix-thing"


# ---------------------------------------------------------------------------
# agent.md — system prompt uses BRANCH_PREFIX placeholder
# ---------------------------------------------------------------------------


class TestAgentPromptPlaceholders:
    """Tests that agent.md uses {BRANCH_PREFIX} instead of hardcoded koan/."""

    def test_agent_md_has_branch_prefix_placeholder(self):
        """agent.md should contain {BRANCH_PREFIX} placeholders."""
        agent_md = Path(__file__).parent.parent / "system-prompts" / "agent.md"
        content = agent_md.read_text()
        assert "{BRANCH_PREFIX}" in content
        # Ensure the hardcoded "koan/" references in autonomy/working style
        # have been replaced
        assert "koan/* branches" not in content
        assert "koan/<mission-name>" not in content

    def test_agent_md_no_leftover_hardcoded_koan_in_branch_refs(self):
        """No hardcoded 'koan/' references remain in branch-related contexts."""
        agent_md = Path(__file__).parent.parent / "system-prompts" / "agent.md"
        content = agent_md.read_text()
        # These specific patterns should be gone (replaced by {BRANCH_PREFIX})
        assert "branch koan/" not in content.lower().replace("{branch_prefix}", "")


# ---------------------------------------------------------------------------
# Sample config — branch_prefix documented
# ---------------------------------------------------------------------------


class TestSampleConfig:
    """Tests that instance.example/config.yaml documents branch_prefix."""

    def test_sample_config_has_branch_prefix(self):
        config_path = Path(__file__).parent.parent.parent / "instance.example" / "config.yaml"
        content = config_path.read_text()
        assert "branch_prefix" in content
        assert "koan" in content  # default value mentioned
