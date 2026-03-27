"""Tests for git_auto_merge.py — automatic branch merging."""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from app.git_auto_merge import (
    find_matching_rule,
    should_auto_merge,
    run_git,
    get_branch_commit_messages,
    build_merge_commit_message,
    get_author_env,
    is_working_tree_clean,
    is_branch_pushed,
    perform_merge,
    cleanup_branch,
    cleanup_local_branch,
    cleanup_remote_branch,
    auto_merge_branch,
    write_merge_success_to_journal,
    write_merge_failure_to_journal,
    GitAutoMerger,
)
from app.config import get_auto_merge_config


# --- get_auto_merge_config ---

class TestGetAutoMergeConfig:
    def test_global_defaults_only(self):
        """When no project override, return global defaults."""
        config = {
            "git_auto_merge": {
                "enabled": True,
                "base_branch": "main",
                "strategy": "squash",
                "rules": [{"pattern": "koan/*", "auto_merge": True}]
            }
        }
        result = get_auto_merge_config(config, "unknown-project")

        assert result["enabled"] is True
        assert result["base_branch"] == "main"
        assert result["strategy"] == "squash"
        assert len(result["rules"]) == 1

    def test_config_yaml_projects_section_ignored(self):
        """config.yaml projects section is ignored — overrides come from projects.yaml."""
        config = {
            "git_auto_merge": {"enabled": True, "base_branch": "main", "strategy": "squash"},
            "projects": {
                "frontend": {
                    "git_auto_merge": {"enabled": False}
                }
            }
        }
        result = get_auto_merge_config(config, "frontend")

        # projects: section in config.yaml is no longer consulted
        assert result["enabled"] is True  # Global, not overridden
        assert result["base_branch"] == "main"
        assert result["strategy"] == "squash"

    @patch("app.projects_config.load_projects_config")
    def test_projects_yaml_overrides(self, mock_load):
        """Per-project overrides come from projects.yaml."""
        mock_load.return_value = {
            "defaults": {"git_auto_merge": {"enabled": True, "base_branch": "main", "strategy": "squash"}},
            "projects": {
                "backend": {
                    "path": "/p",
                    "git_auto_merge": {"base_branch": "develop", "strategy": "merge"}
                }
            }
        }
        config = {"git_auto_merge": {"enabled": True, "base_branch": "main", "strategy": "squash"}}
        result = get_auto_merge_config(config, "backend")

        assert result["base_branch"] == "develop"  # Overridden via projects.yaml
        assert result["strategy"] == "merge"  # Overridden via projects.yaml
        assert result["enabled"] is True  # Inherited from defaults

    @patch("app.projects_config.load_projects_config", return_value=None)
    def test_missing_config_section(self, mock_projects):
        """When git_auto_merge section missing, return safe defaults."""
        config = {}
        result = get_auto_merge_config(config, "koan")

        assert result["enabled"] is True  # Default
        assert result["base_branch"] == "main"  # Default
        assert result["strategy"] == "squash"  # Default
        assert result["rules"] == []  # Empty


# --- find_matching_rule ---

class TestFindMatchingRule:
    def test_exact_match(self):
        """Exact pattern match."""
        rules = [{"pattern": "koan/fix-*", "auto_merge": True}]
        rule = find_matching_rule("koan/fix-cors", rules)

        assert rule is not None
        assert rule["pattern"] == "koan/fix-*"

    def test_wildcard_match(self):
        """Glob wildcard matching."""
        rules = [{"pattern": "koan/*", "auto_merge": True}]

        assert find_matching_rule("koan/fix-bug", rules) is not None
        assert find_matching_rule("koan/feature-x", rules) is not None
        assert find_matching_rule("main", rules) is None
        assert find_matching_rule("feature/new", rules) is None

    def test_first_match_wins(self):
        """When multiple rules match, first one wins."""
        rules = [
            {"pattern": "koan/hotfix-*", "priority": 1},
            {"pattern": "koan/*", "priority": 2}
        ]
        rule = find_matching_rule("koan/hotfix-cors", rules)

        assert rule["priority"] == 1  # First match

    def test_no_match(self):
        """When no rule matches, return None."""
        rules = [{"pattern": "koan/*", "auto_merge": True}]
        rule = find_matching_rule("main", rules)

        assert rule is None

    def test_empty_rules(self):
        """When rules list is empty, return None."""
        rules = []
        rule = find_matching_rule("koan/fix-bug", rules)

        assert rule is None


# --- should_auto_merge ---

class TestShouldAutoMerge:
    def test_enabled_with_matching_rule(self):
        """Should merge when enabled and rule matches."""
        config = {
            "enabled": True,
            "base_branch": "main",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        should_merge, rule, base_branch = should_auto_merge(config, "koan/fix-bug")

        assert should_merge is True
        assert rule is not None
        assert base_branch == "main"

    def test_disabled_globally(self):
        """Should not merge when disabled globally."""
        config = {
            "enabled": False,
            "base_branch": "main",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        should_merge, rule, base_branch = should_auto_merge(config, "koan/fix-bug")

        assert should_merge is False
        assert rule is None
        assert base_branch == ""

    def test_no_matching_rule(self):
        """Should not merge when no rule matches."""
        config = {
            "enabled": True,
            "base_branch": "main",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        should_merge, rule, base_branch = should_auto_merge(config, "feature/new-thing")

        assert should_merge is False
        assert rule is None
        assert base_branch == ""

    def test_rule_auto_merge_false(self):
        """Should not merge when rule exists but auto_merge is False."""
        config = {
            "enabled": True,
            "base_branch": "main",
            "rules": [{"pattern": "koan/*", "auto_merge": False}]
        }
        should_merge, rule, base_branch = should_auto_merge(config, "koan/fix-bug")

        assert should_merge is False
        assert rule is None
        assert base_branch == ""

    def test_rule_overrides_base_branch(self):
        """Rule-level base_branch overrides config base_branch."""
        config = {
            "enabled": True,
            "base_branch": "develop",  # Config default
            "rules": [
                {"pattern": "koan/hotfix-*", "auto_merge": True, "base_branch": "main"}  # Rule override
            ]
        }
        should_merge, rule, base_branch = should_auto_merge(config, "koan/hotfix-cors")

        assert should_merge is True
        assert base_branch == "main"  # Rule override wins

    def test_base_branch_precedence(self):
        """Test base_branch resolution precedence: rule > config > default."""
        # Case 1: Rule specifies base_branch
        config1 = {
            "enabled": True,
            "base_branch": "develop",
            "rules": [{"pattern": "koan/*", "auto_merge": True, "base_branch": "staging"}]
        }
        _, _, base1 = should_auto_merge(config1, "koan/test")
        assert base1 == "staging"  # Rule wins

        # Case 2: Config specifies base_branch, rule doesn't
        config2 = {
            "enabled": True,
            "base_branch": "develop",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        _, _, base2 = should_auto_merge(config2, "koan/test")
        assert base2 == "develop"  # Config wins

        # Case 3: Neither specified, use default
        config3 = {
            "enabled": True,
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        _, _, base3 = should_auto_merge(config3, "koan/test")
        assert base3 == "main"  # Default

    def test_multiple_rules_first_match(self):
        """When multiple rules match, first match determines base_branch."""
        config = {
            "enabled": True,
            "base_branch": "develop",
            "rules": [
                {"pattern": "koan/hotfix-*", "auto_merge": True, "base_branch": "main"},
                {"pattern": "koan/*", "auto_merge": True, "base_branch": "staging"}
            ]
        }

        # Hotfix matches first rule
        should_merge1, _, base1 = should_auto_merge(config, "koan/hotfix-auth")
        assert should_merge1 is True
        assert base1 == "main"

        # Regular koan branch matches second rule
        should_merge2, _, base2 = should_auto_merge(config, "koan/feature-x")
        assert should_merge2 is True
        assert base2 == "staging"

    def test_empty_rules_list(self):
        """When rules list is empty, should not merge."""
        config = {
            "enabled": True,
            "base_branch": "main",
            "rules": []
        }
        should_merge, rule, base_branch = should_auto_merge(config, "koan/fix-bug")

        assert should_merge is False
        assert rule is None
        assert base_branch == ""


# --- get_author_env ---

class TestGetAuthorEnv:
    def test_returns_env_vars_when_email_set(self):
        """When KOAN_EMAIL is set, return GIT_AUTHOR/COMMITTER env vars."""
        with patch.dict("os.environ", {"KOAN_EMAIL": "koan@example.com"}):
            env = get_author_env()
            assert env == {
                "GIT_AUTHOR_NAME": "Kōan",
                "GIT_AUTHOR_EMAIL": "koan@example.com",
                "GIT_COMMITTER_NAME": "Kōan",
                "GIT_COMMITTER_EMAIL": "koan@example.com",
            }

    def test_returns_empty_dict_when_no_email(self):
        """When KOAN_EMAIL is not set, return empty dict."""
        with patch.dict("os.environ", {}, clear=False):
            # Ensure KOAN_EMAIL is not present
            import os
            env_backup = os.environ.pop("KOAN_EMAIL", None)
            try:
                env = get_author_env()
                assert env == {}
            finally:
                if env_backup is not None:
                    os.environ["KOAN_EMAIL"] = env_backup

    def test_no_author_flag_in_merge_calls(self):
        """Verify merge strategy passes env kwarg, not --author args."""
        calls = [
            (0, "fix stuff", ""),  # git log
            (0, "", ""),  # checkout
            (0, "", ""),  # pull
            (0, "", ""),  # merge --no-ff
            (0, "", ""),  # push
            (0, "", ""),  # checkout (finally)
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls) as mock, \
             patch.dict("os.environ", {"KOAN_EMAIL": "koan@test.com"}):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "merge")
            assert ok is True
            # The merge call (4th call, index 3) should NOT have --author
            merge_call = mock.call_args_list[3]
            args_passed = list(merge_call[0])  # positional args
            assert "--author" not in args_passed
            # Should have env kwarg
            assert "env" in merge_call[1]
            assert merge_call[1]["env"]["GIT_AUTHOR_EMAIL"] == "koan@test.com"

    def test_squash_commit_uses_env(self):
        """Verify squash strategy passes env to commit, not --author args."""
        calls = [
            (0, "fix stuff", ""),  # git log
            (0, "", ""),  # checkout
            (0, "", ""),  # pull
            (0, "", ""),  # merge --squash
            (0, "", ""),  # commit
            (0, "", ""),  # push
            (0, "", ""),  # checkout (finally)
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls) as mock, \
             patch.dict("os.environ", {"KOAN_EMAIL": "koan@test.com"}):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "squash")
            assert ok is True
            # The commit call (5th call, index 4) should use env, not --author
            commit_call = mock.call_args_list[4]
            args_passed = list(commit_call[0])
            assert "--author" not in args_passed
            assert "env" in commit_call[1]


# --- Integration Tests ---

class TestIntegration:
    @patch("app.projects_config.load_projects_config", return_value=None)
    def test_full_config_resolution_global_only(self, mock_projects):
        """Test full config resolution uses global config.yaml settings."""
        config = {
            "git_auto_merge": {
                "enabled": True,
                "base_branch": "main",
                "strategy": "squash",
                "rules": [{"pattern": "koan/*", "auto_merge": True, "delete_after_merge": True}]
            },
        }

        merged = get_auto_merge_config(config, "koan")
        should_merge, rule, base_branch = should_auto_merge(merged, "koan/fix-cors")

        assert should_merge is True
        assert base_branch == "main"
        assert merged["strategy"] == "squash"

    def test_config_yaml_projects_section_ignored_in_integration(self):
        """config.yaml projects section is ignored; only global settings used."""
        config = {
            "git_auto_merge": {
                "enabled": True,
                "base_branch": "main",
                "strategy": "squash",
                "rules": [{"pattern": "koan/*", "auto_merge": True}]
            },
            "projects": {
                "backend": {
                    "git_auto_merge": {
                        "base_branch": "develop",
                        "strategy": "merge",
                    }
                }
            }
        }

        merged = get_auto_merge_config(config, "backend")

        # config.yaml projects: section is ignored — uses global values
        assert merged["base_branch"] == "main"
        assert merged["strategy"] == "squash"
        assert len(merged["rules"]) == 1


# --- run_git ---

class TestRunGit:
    def test_success(self):
        """run_git returns exit code, stdout, stderr."""
        with patch("app.git_auto_merge._run_git_core") as mock_core:
            mock_core.return_value = (0, "output", "")
            code, out, err = run_git("/tmp", "status")
            assert code == 0
            assert out == "output"
            assert err == ""
            mock_core.assert_called_once_with(
                "status", cwd="/tmp", timeout=30, env=None
            )

    def test_failure(self):
        """run_git returns non-zero on failure."""
        with patch("app.git_auto_merge._run_git_core") as mock_core:
            mock_core.return_value = (128, "", "fatal: error")
            code, out, err = run_git("/tmp", "checkout", "main")
            assert code == 128
            assert err == "fatal: error"

    def test_timeout(self):
        """run_git handles subprocess timeout (via git_utils)."""
        with patch("app.git_auto_merge._run_git_core") as mock_core:
            mock_core.return_value = (1, "", "Git command timed out")
            code, out, err = run_git("/tmp", "fetch")
            assert code == 1
            assert "timed out" in err

    def test_exception(self):
        """run_git handles generic exceptions (via git_utils)."""
        with patch("app.git_auto_merge._run_git_core") as mock_core:
            mock_core.return_value = (1, "", "no git")
            code, out, err = run_git("/tmp", "status")
            assert code == 1
            assert "no git" in err


# --- is_working_tree_clean ---

class TestIsWorkingTreeClean:
    def test_clean(self):
        """Clean working tree returns True."""
        with patch("app.git_auto_merge.run_git", return_value=(0, "", "")):
            assert is_working_tree_clean("/tmp") is True

    def test_dirty(self):
        """Dirty working tree returns False."""
        with patch("app.git_auto_merge.run_git", return_value=(0, "M file.py", "")):
            assert is_working_tree_clean("/tmp") is False

    def test_git_error(self):
        """Git error returns False."""
        with patch("app.git_auto_merge.run_git", return_value=(128, "", "not a repo")):
            assert is_working_tree_clean("/tmp") is False


# --- is_branch_pushed ---

class TestIsBranchPushed:
    def test_pushed(self):
        """Branch exists on remote."""
        with patch("app.git_auto_merge.run_git", return_value=(0, "abc123\trefs/heads/koan/fix", "")):
            assert is_branch_pushed("/tmp", "koan/fix") is True

    def test_not_pushed(self):
        """Branch not on remote."""
        with patch("app.git_auto_merge.run_git", return_value=(0, "", "")):
            assert is_branch_pushed("/tmp", "koan/fix") is False

    def test_git_error(self):
        """Git error returns False."""
        with patch("app.git_auto_merge.run_git", return_value=(128, "", "error")):
            assert is_branch_pushed("/tmp", "koan/fix") is False


# --- perform_merge ---

class TestPerformMerge:
    def _mock_git(self, call_results):
        """Helper: return different results for sequential run_git calls."""
        return patch("app.git_auto_merge.run_git", side_effect=call_results)

    def test_squash_success(self):
        """Squash merge: log, checkout, pull, merge --squash, commit, push, checkout (finally)."""
        calls = [
            (0, "fix bug\nadd test", ""),  # git log (commit messages)
            (0, "", ""),   # checkout main
            (0, "", ""),   # pull
            (0, "", ""),   # merge --squash
            (0, "", ""),   # commit
            (0, "", ""),   # push
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls) as mock:
            ok, err = perform_merge("/tmp", "koan/fix", "main", "squash")
            assert ok is True
            assert err == ""
            # Verify commit message includes branch subjects
            commit_call = mock.call_args_list[4]
            msg = commit_call[0][2]  # 3rd positional arg to run_git is "-m", msg is after
            assert "koan: auto-merge koan/fix (squash)" in str(commit_call)
            assert "fix bug" in str(commit_call)

    def test_squash_conflict(self):
        """Squash merge conflict triggers reset --hard."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (1, "", "CONFLICT"),  # merge --squash fails
            (0, "", ""),   # reset --hard
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "squash")
            assert ok is False
            assert "conflict" in err.lower()

    def test_rebase_success(self):
        """Rebase merge: log, checkout, pull, rebase, checkout, ff-merge, push, checkout (finally)."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout main
            (0, "", ""),   # pull
            (0, "", ""),   # rebase
            (0, "", ""),   # checkout main (after rebase)
            (0, "", ""),   # merge --ff-only
            (0, "", ""),   # push
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "rebase")
            assert ok is True

    def test_rebase_conflict(self):
        """Rebase conflict triggers rebase --abort."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (1, "", "CONFLICT"),  # rebase fails
            (0, "", ""),   # rebase --abort
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "rebase")
            assert ok is False
            assert "conflict" in err.lower()

    def test_merge_noff_success(self):
        """Regular merge with --no-ff."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (0, "", ""),   # merge --no-ff
            (0, "", ""),   # push
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "merge")
            assert ok is True

    def test_merge_noff_conflict(self):
        """Regular merge conflict triggers merge --abort."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (1, "", "CONFLICT"),  # merge fails
            (0, "", ""),   # merge --abort
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "merge")
            assert ok is False
            assert "conflict" in err.lower()

    def test_checkout_failure(self):
        """Checkout failure aborts early."""
        calls = [
            (0, "", ""),   # git log
            (1, "", "error: pathspec 'main' did not match"),  # checkout fails
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "squash")
            assert ok is False
            assert "checkout" in err.lower()

    def test_pull_failure(self):
        """Pull failure aborts."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout ok
            (1, "", "fatal: unable to access"),  # pull fails
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "squash")
            assert ok is False
            assert "pull" in err.lower()

    def test_push_failure_resets_to_origin(self):
        """Push failure after successful merge resets local to origin state."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (0, "", ""),   # merge --squash
            (0, "", ""),   # commit
            (1, "", "rejected"),  # push fails
            (0, "", ""),   # reset --hard origin/main
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls) as mock:
            ok, err = perform_merge("/tmp", "koan/fix", "main", "squash")
            assert ok is False
            assert "push" in err.lower()
            # reset --hard origin/main is call index 6 (after push at index 5)
            reset_call = mock.call_args_list[6]
            assert "reset" in str(reset_call)
            assert "--hard" in str(reset_call)
            assert "origin/main" in str(reset_call)

    def test_push_failure_rebase_resets_to_origin(self):
        """Push failure after successful rebase+ff-merge resets local to origin."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (0, "", ""),   # rebase ok
            (0, "", ""),   # checkout main
            (0, "", ""),   # merge --ff-only
            (1, "", "rejected"),  # push fails
            (0, "", ""),   # reset --hard origin/main
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls) as mock:
            ok, err = perform_merge("/tmp", "koan/fix", "main", "rebase")
            assert ok is False
            assert "push" in err.lower()
            reset_call = mock.call_args_list[7]
            assert "reset" in str(reset_call)
            assert "origin/main" in str(reset_call)

    def test_push_failure_noff_resets_to_origin(self):
        """Push failure after successful --no-ff merge resets local to origin."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (0, "", ""),   # merge --no-ff
            (1, "", "rejected"),  # push fails
            (0, "", ""),   # reset --hard origin/main
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls) as mock:
            ok, err = perform_merge("/tmp", "koan/fix", "main", "merge")
            assert ok is False
            assert "push" in err.lower()
            reset_call = mock.call_args_list[5]
            assert "reset" in str(reset_call)
            assert "origin/main" in str(reset_call)

    def test_rebase_ff_merge_failure(self):
        """After successful rebase, ff-merge fails."""
        calls = [
            (0, "", ""),   # git log
            (0, "", ""),   # checkout
            (0, "", ""),   # pull
            (0, "", ""),   # rebase ok
            (0, "", ""),   # checkout main
            (1, "", "not a fast-forward"),  # ff-merge fails
            (0, "", ""),   # checkout main (finally)
        ]
        with self._mock_git(calls):
            ok, err = perform_merge("/tmp", "koan/fix", "main", "rebase")
            assert ok is False
            assert "fast-forward" in err.lower()


# --- cleanup_local_branch ---

class TestCleanupLocalBranch:
    def test_safe_delete_success(self):
        """Normal delete with -d succeeds."""
        with patch("app.git_auto_merge.run_git", return_value=(0, "", "")):
            assert cleanup_local_branch("/tmp", "koan/fix") is True

    def test_force_delete_fallback(self):
        """When -d fails, falls back to -D."""
        calls = [
            (1, "", "not fully merged"),  # branch -d fails
            (0, "", ""),   # branch -D succeeds
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls):
            assert cleanup_local_branch("/tmp", "koan/fix") is True

    def test_both_deletes_fail(self):
        """When both -d and -D fail, return False."""
        calls = [
            (1, "", "error"),  # branch -d fails
            (1, "", "error"),  # branch -D fails
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls):
            assert cleanup_local_branch("/tmp", "koan/fix") is False


# --- cleanup_remote_branch ---

class TestCleanupRemoteBranch:
    def test_remote_delete_success(self):
        """Remote delete succeeds."""
        with patch("app.git_auto_merge.run_git", return_value=(0, "", "")):
            assert cleanup_remote_branch("/tmp", "koan/fix") is True

    def test_remote_delete_fails(self):
        """Remote delete fails."""
        with patch("app.git_auto_merge.run_git", return_value=(1, "", "error")):
            assert cleanup_remote_branch("/tmp", "koan/fix") is False


# --- cleanup_branch (backward compat wrapper) ---

class TestCleanupBranch:
    def test_cleanup_success(self):
        """Delete local + remote branch."""
        calls = [
            (0, "", ""),   # branch -d
            (0, "", ""),   # push --delete
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls):
            assert cleanup_branch("/tmp", "koan/fix") is True

    def test_force_delete_fallback(self):
        """When -d fails, falls back to -D then remote."""
        calls = [
            (1, "", "not fully merged"),  # branch -d fails
            (0, "", ""),   # branch -D succeeds
            (0, "", ""),   # push --delete
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls):
            assert cleanup_branch("/tmp", "koan/fix") is True

    def test_both_deletes_fail(self):
        """When both -d and -D fail, return False (don't try remote)."""
        calls = [
            (1, "", "error"),  # branch -d fails
            (1, "", "error"),  # branch -D fails
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls):
            assert cleanup_branch("/tmp", "koan/fix") is False

    def test_remote_delete_fails(self):
        """Local delete ok but remote delete fails."""
        calls = [
            (0, "", ""),   # branch -d ok
            (1, "", "remote error"),  # push --delete fails
        ]
        with patch("app.git_auto_merge.run_git", side_effect=calls):
            assert cleanup_branch("/tmp", "koan/fix") is False


# --- Journal writers ---

class TestJournalWriters:
    def test_write_merge_success(self, tmp_path):
        """Write success entry to journal."""
        inst = str(tmp_path)
        write_merge_success_to_journal(inst, "koan", "koan/fix", "main", "squash")

        from datetime import datetime
        journal_dir = tmp_path / "journal" / datetime.now().strftime("%Y-%m-%d")
        journal_file = journal_dir / "koan.md"
        assert journal_file.exists()
        content = journal_file.read_text()
        assert "✓ Merged `koan/fix`" in content
        assert "squash" in content

    def test_write_merge_failure(self, tmp_path):
        """Write failure entry to journal."""
        inst = str(tmp_path)
        write_merge_failure_to_journal(inst, "koan", "koan/fix", "Working tree dirty")

        from datetime import datetime
        journal_dir = tmp_path / "journal" / datetime.now().strftime("%Y-%m-%d")
        journal_file = journal_dir / "koan.md"
        assert journal_file.exists()
        content = journal_file.read_text()
        assert "✗ Failed to merge `koan/fix`" in content
        assert "Working tree dirty" in content
        assert "Manual intervention" in content

    def test_journal_append(self, tmp_path):
        """Multiple writes append, don't overwrite."""
        inst = str(tmp_path)
        write_merge_success_to_journal(inst, "koan", "koan/a", "main", "squash")
        write_merge_success_to_journal(inst, "koan", "koan/b", "main", "merge")

        from datetime import datetime
        journal_file = tmp_path / "journal" / datetime.now().strftime("%Y-%m-%d") / "koan.md"
        content = journal_file.read_text()
        assert "koan/a" in content
        assert "koan/b" in content


# --- auto_merge_branch (orchestrator) ---

class TestAutoMergeBranch:
    """Tests for module-level auto_merge_branch (delegates to GitAutoMerger).

    These tests use patch.object on GitAutoMerger since module-level functions
    now delegate to the class. Tests verify the delegation works correctly.
    """
    def _base_config(self):
        return {
            "git_auto_merge": {
                "enabled": True,
                "base_branch": "main",
                "strategy": "squash",
                "rules": [{"pattern": "koan/*", "auto_merge": True, "delete_after_merge": False}]
            }
        }

    @patch.object(GitAutoMerger, "cleanup_local_branch", return_value=True)
    @patch.object(GitAutoMerger, "write_merge_success_to_journal")
    @patch.object(GitAutoMerger, "perform_merge", return_value=(True, ""))
    @patch.object(GitAutoMerger, "is_branch_pushed", return_value=True)
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=True)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_success_flow(self, mock_load, mock_cfg, mock_clean, mock_pushed, mock_merge, mock_journal, mock_local_cleanup):
        """Happy path: config match, clean, pushed, merge ok. Local branch always deleted."""
        mock_load.return_value = self._base_config()
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        result = auto_merge_branch("/inst", "koan", "/proj", "koan/fix")
        assert result == 0
        mock_merge.assert_called_once_with("koan/fix", "main", "squash")
        mock_local_cleanup.assert_called_once_with("koan/fix")
        mock_journal.assert_called_once()

    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_not_configured(self, mock_load, mock_cfg):
        """Branch not matching any rule returns 0 (skip)."""
        mock_load.return_value = {}
        mock_cfg.return_value = {"enabled": True, "base_branch": "main", "strategy": "squash", "rules": []}
        result = auto_merge_branch("/inst", "koan", "/proj", "koan/fix")
        assert result == 0

    @patch.object(GitAutoMerger, "write_merge_failure_to_journal")
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=False)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_dirty_tree(self, mock_load, mock_cfg, mock_clean, mock_journal):
        """Dirty working tree fails with journal entry."""
        mock_load.return_value = self._base_config()
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        result = auto_merge_branch("/inst", "koan", "/proj", "koan/fix")
        assert result == 1
        mock_journal.assert_called_once()
        assert "uncommitted" in mock_journal.call_args[0][1].lower()

    @patch.object(GitAutoMerger, "write_merge_failure_to_journal")
    @patch.object(GitAutoMerger, "is_branch_pushed", return_value=False)
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=True)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_not_pushed(self, mock_load, mock_cfg, mock_clean, mock_pushed, mock_journal):
        """Branch not pushed fails with journal entry."""
        mock_load.return_value = self._base_config()
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        result = auto_merge_branch("/inst", "koan", "/proj", "koan/fix")
        assert result == 1
        mock_journal.assert_called_once()
        assert "not pushed" in mock_journal.call_args[0][1].lower()

    @patch.object(GitAutoMerger, "cleanup_remote_branch", return_value=True)
    @patch.object(GitAutoMerger, "cleanup_local_branch", return_value=True)
    @patch.object(GitAutoMerger, "write_merge_success_to_journal")
    @patch.object(GitAutoMerger, "perform_merge", return_value=(True, ""))
    @patch.object(GitAutoMerger, "is_branch_pushed", return_value=True)
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=True)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_delete_after_merge(self, mock_load, mock_cfg, mock_clean, mock_pushed, mock_merge, mock_journal, mock_local_cleanup, mock_remote_cleanup):
        """delete_after_merge triggers both local (always) and remote (configured) cleanup."""
        mock_load.return_value = self._base_config()
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True, "delete_after_merge": True}]
        }
        result = auto_merge_branch("/inst", "koan", "/proj", "koan/fix")
        assert result == 0
        mock_local_cleanup.assert_called_once_with("koan/fix")
        mock_remote_cleanup.assert_called_once_with("koan/fix")

    @patch.object(GitAutoMerger, "cleanup_remote_branch")
    @patch.object(GitAutoMerger, "cleanup_local_branch", return_value=True)
    @patch.object(GitAutoMerger, "write_merge_success_to_journal")
    @patch.object(GitAutoMerger, "perform_merge", return_value=(True, ""))
    @patch.object(GitAutoMerger, "is_branch_pushed", return_value=True)
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=True)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_no_remote_delete_without_config(self, mock_load, mock_cfg, mock_clean, mock_pushed, mock_merge, mock_journal, mock_local_cleanup, mock_remote_cleanup):
        """Without delete_after_merge, local branch is deleted but remote is kept."""
        mock_load.return_value = self._base_config()
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True, "delete_after_merge": False}]
        }
        result = auto_merge_branch("/inst", "koan", "/proj", "koan/fix")
        assert result == 0
        mock_local_cleanup.assert_called_once_with("koan/fix")
        mock_remote_cleanup.assert_not_called()

    @patch.object(GitAutoMerger, "write_merge_failure_to_journal")
    @patch.object(GitAutoMerger, "perform_merge", return_value=(False, "Merge conflict"))
    @patch.object(GitAutoMerger, "is_branch_pushed", return_value=True)
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=True)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_merge_failure(self, mock_load, mock_cfg, mock_clean, mock_pushed, mock_merge, mock_journal):
        """Merge failure returns 1 with journal entry."""
        mock_load.return_value = self._base_config()
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True}]
        }
        result = auto_merge_branch("/inst", "koan", "/proj", "koan/fix")
        assert result == 1
        mock_journal.assert_called_once()


# --- GitAutoMerger class tests ---

class TestGitAutoMergerClass:
    """Tests for the OO GitAutoMerger class."""

    def _make_merger(self, instance_dir="/inst", project_name="koan", project_path="/proj"):
        return GitAutoMerger(instance_dir, project_name, project_path)

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_is_working_tree_clean_true(self, mock_git):
        merger = self._make_merger()
        assert merger.is_working_tree_clean() is True
        mock_git.assert_called_once_with("/proj", "status", "--porcelain")

    @patch("app.git_auto_merge.run_git", return_value=(0, "M file.py", ""))
    def test_is_working_tree_clean_false(self, mock_git):
        merger = self._make_merger()
        assert merger.is_working_tree_clean() is False

    @patch("app.git_auto_merge.run_git", return_value=(0, "abc123\trefs/heads/koan/fix", ""))
    def test_is_branch_pushed_true(self, mock_git):
        merger = self._make_merger()
        assert merger.is_branch_pushed("koan/fix") is True

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_is_branch_pushed_false(self, mock_git):
        merger = self._make_merger()
        assert merger.is_branch_pushed("koan/fix") is False

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_cleanup_local_branch_success(self, mock_git):
        merger = self._make_merger()
        assert merger.cleanup_local_branch("koan/fix") is True

    @patch("app.git_auto_merge.run_git")
    def test_cleanup_local_branch_fallback_to_force(self, mock_git):
        mock_git.side_effect = [(1, "", "not merged"), (0, "", "")]
        merger = self._make_merger()
        assert merger.cleanup_local_branch("koan/fix") is True
        assert mock_git.call_count == 2

    @patch("app.git_auto_merge.run_git")
    def test_cleanup_local_branch_both_fail(self, mock_git):
        mock_git.side_effect = [(1, "", "error"), (1, "", "error")]
        merger = self._make_merger()
        assert merger.cleanup_local_branch("koan/fix") is False

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_cleanup_remote_branch(self, mock_git):
        merger = self._make_merger()
        assert merger.cleanup_remote_branch("koan/fix") is True

    @patch("app.git_auto_merge.run_git", return_value=(1, "", "error"))
    def test_cleanup_remote_branch_failure(self, mock_git):
        merger = self._make_merger()
        assert merger.cleanup_remote_branch("koan/fix") is False

    @patch("app.git_auto_merge.run_git")
    def test_cleanup_branch_wrapper(self, mock_git):
        mock_git.return_value = (0, "", "")
        merger = self._make_merger()
        assert merger.cleanup_branch("koan/fix") is True

    @patch("app.git_auto_merge.run_git")
    def test_cleanup_branch_wrapper_local_fail(self, mock_git):
        mock_git.side_effect = [(1, "", "err"), (1, "", "err")]
        merger = self._make_merger()
        assert merger.cleanup_branch("koan/fix") is False

    @patch("app.git_auto_merge.run_git")
    def test_perform_merge_squash(self, mock_git):
        merger = self._make_merger()
        mock_git.side_effect = [
            (0, "fix stuff", ""),       # log (commit messages)
            (0, "", ""),                 # checkout base
            (0, "", ""),                 # pull
            (0, "", ""),                 # merge --squash
            (0, "", ""),                 # commit
            (0, "", ""),                 # push
            (0, "", ""),                 # checkout base (finally)
        ]
        success, error = merger.perform_merge("koan/fix", "main", "squash")
        assert success is True
        assert error == ""

    @patch("app.git_auto_merge.run_git")
    def test_perform_merge_squash_conflict(self, mock_git):
        merger = self._make_merger()
        mock_git.side_effect = [
            (0, "", ""),                 # log
            (0, "", ""),                 # checkout
            (0, "", ""),                 # pull
            (1, "", "CONFLICT"),         # merge --squash fails
            (0, "", ""),                 # reset --hard
            (0, "", ""),                 # checkout base (finally)
        ]
        success, error = merger.perform_merge("koan/fix", "main", "squash")
        assert success is False
        assert "conflict" in error.lower()

    @patch("app.git_auto_merge.run_git")
    def test_perform_merge_noff(self, mock_git):
        merger = self._make_merger()
        mock_git.side_effect = [
            (0, "add feature", ""),      # log
            (0, "", ""),                 # checkout
            (0, "", ""),                 # pull
            (0, "", ""),                 # merge --no-ff
            (0, "", ""),                 # push
            (0, "", ""),                 # checkout base (finally)
        ]
        success, error = merger.perform_merge("koan/fix", "main", "merge")
        assert success is True

    @patch("app.git_auto_merge.run_git")
    def test_perform_merge_rebase(self, mock_git):
        merger = self._make_merger()
        mock_git.side_effect = [
            (0, "rebase stuff", ""),     # log
            (0, "", ""),                 # checkout base
            (0, "", ""),                 # pull
            (0, "", ""),                 # rebase
            (0, "", ""),                 # checkout base
            (0, "", ""),                 # merge --ff-only
            (0, "", ""),                 # push
            (0, "", ""),                 # checkout base (finally)
        ]
        success, error = merger.perform_merge("koan/fix", "main", "rebase")
        assert success is True

    @patch("app.git_auto_merge.run_git")
    def test_perform_merge_checkout_failure(self, mock_git):
        merger = self._make_merger()
        mock_git.side_effect = [
            (0, "", ""),                 # log
            (1, "", "checkout failed"),  # checkout fails
            (0, "", ""),                 # checkout base (finally)
        ]
        success, error = merger.perform_merge("koan/fix", "main", "squash")
        assert success is False
        assert "checkout" in error.lower()

    @patch("app.journal.append_to_journal")
    def test_write_merge_success_to_journal(self, mock_journal):
        merger = self._make_merger(instance_dir="/inst")
        merger.write_merge_success_to_journal("koan/fix", "main", "squash")
        mock_journal.assert_called_once()
        call_content = mock_journal.call_args[0][2]
        assert "koan/fix" in call_content
        assert "main" in call_content

    @patch("app.journal.append_to_journal")
    def test_write_merge_failure_to_journal(self, mock_journal):
        merger = self._make_merger(instance_dir="/inst")
        merger.write_merge_failure_to_journal("koan/fix", "Conflict")
        mock_journal.assert_called_once()
        call_content = mock_journal.call_args[0][2]
        assert "koan/fix" in call_content
        assert "Conflict" in call_content


class TestGitAutoMergerOrchestrator:
    """Tests for GitAutoMerger.auto_merge_branch (the class-level orchestrator)."""

    def _make_merger(self):
        return GitAutoMerger("/inst", "koan", "/proj")

    @patch.object(GitAutoMerger, "write_merge_success_to_journal")
    @patch.object(GitAutoMerger, "cleanup_remote_branch", return_value=True)
    @patch.object(GitAutoMerger, "cleanup_local_branch", return_value=True)
    @patch.object(GitAutoMerger, "perform_merge", return_value=(True, ""))
    @patch.object(GitAutoMerger, "is_branch_pushed", return_value=True)
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=True)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_full_success_with_remote_delete(self, mock_load, mock_cfg, mock_clean, mock_pushed,
                                              mock_merge, mock_local, mock_remote, mock_journal):
        mock_load.return_value = {}
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True, "delete_after_merge": True}],
        }
        merger = self._make_merger()
        result = merger.auto_merge_branch("koan/fix")
        assert result == 0
        mock_local.assert_called_once()
        mock_remote.assert_called_once()
        mock_journal.assert_called_once()

    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_not_configured_returns_0(self, mock_load, mock_cfg):
        mock_load.return_value = {}
        mock_cfg.return_value = {"enabled": False}
        merger = self._make_merger()
        result = merger.auto_merge_branch("koan/fix")
        assert result == 0

    @patch.object(GitAutoMerger, "write_merge_failure_to_journal")
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=False)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_dirty_tree_returns_1(self, mock_load, mock_cfg, mock_clean, mock_journal):
        mock_load.return_value = {}
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True}],
        }
        merger = self._make_merger()
        result = merger.auto_merge_branch("koan/fix")
        assert result == 1
        mock_journal.assert_called_once()

    @patch.object(GitAutoMerger, "write_merge_failure_to_journal")
    @patch.object(GitAutoMerger, "is_branch_pushed", return_value=False)
    @patch.object(GitAutoMerger, "is_working_tree_clean", return_value=True)
    @patch("app.git_auto_merge.get_auto_merge_config")
    @patch("app.git_auto_merge.load_config")
    def test_not_pushed_returns_1(self, mock_load, mock_cfg, mock_clean, mock_pushed, mock_journal):
        mock_load.return_value = {}
        mock_cfg.return_value = {
            "enabled": True, "base_branch": "main", "strategy": "squash",
            "rules": [{"pattern": "koan/*", "auto_merge": True}],
        }
        merger = self._make_merger()
        result = merger.auto_merge_branch("koan/fix")
        assert result == 1


class TestHelperFunctions:
    """Test standalone helper functions."""

    @patch("app.git_auto_merge.run_git", return_value=(0, "fix bug\nadd test", ""))
    def test_get_branch_commit_messages(self, mock_git):
        msgs = get_branch_commit_messages("/proj", "koan/fix", "main")
        assert msgs == ["fix bug", "add test"]

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_get_branch_commit_messages_empty(self, mock_git):
        msgs = get_branch_commit_messages("/proj", "koan/fix", "main")
        assert msgs == []

    def test_build_merge_commit_message_with_subjects(self):
        msg = build_merge_commit_message("koan/fix", "squash", ["fix bug", "add test"])
        assert "koan/fix" in msg
        assert "squash" in msg
        assert "- fix bug" in msg
        assert "- add test" in msg

    def test_build_merge_commit_message_no_subjects(self):
        msg = build_merge_commit_message("koan/fix", "merge", [])
        assert "koan/fix" in msg
        assert "\n" not in msg

    def test_get_author_env_with_env(self, monkeypatch):
        monkeypatch.setenv("KOAN_EMAIL", "koan@example.com")
        env = get_author_env()
        assert env["GIT_AUTHOR_NAME"] == "Kōan"
        assert env["GIT_AUTHOR_EMAIL"] == "koan@example.com"

    def test_get_author_env_without_env(self, monkeypatch):
        monkeypatch.delenv("KOAN_EMAIL", raising=False)
        env = get_author_env()
        assert env == {}


class TestModuleLevelDelegation:
    """Verify module-level functions correctly delegate to GitAutoMerger."""

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_is_working_tree_clean_delegates(self, mock_git):
        """Module-level is_working_tree_clean delegates to class."""
        result = is_working_tree_clean("/proj")
        assert result is True
        mock_git.assert_called_once_with("/proj", "status", "--porcelain")

    @patch("app.git_auto_merge.run_git", return_value=(0, "abc\trefs/heads/koan/fix", ""))
    def test_is_branch_pushed_delegates(self, mock_git):
        """Module-level is_branch_pushed delegates to class."""
        result = is_branch_pushed("/proj", "koan/fix")
        assert result is True

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_cleanup_local_branch_delegates(self, mock_git):
        """Module-level cleanup_local_branch delegates to class."""
        result = cleanup_local_branch("/proj", "koan/fix")
        assert result is True

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_cleanup_remote_branch_delegates(self, mock_git):
        """Module-level cleanup_remote_branch delegates to class."""
        result = cleanup_remote_branch("/proj", "koan/fix")
        assert result is True

    @patch("app.git_auto_merge.run_git", return_value=(0, "", ""))
    def test_cleanup_branch_delegates(self, mock_git):
        """Module-level cleanup_branch delegates to class."""
        result = cleanup_branch("/proj", "koan/fix")
        assert result is True

    def test_write_merge_success_delegates(self, tmp_path):
        """Module-level write_merge_success_to_journal delegates to class."""
        inst = str(tmp_path)
        write_merge_success_to_journal(inst, "koan", "koan/fix", "main", "squash")
        from datetime import datetime
        journal_file = tmp_path / "journal" / datetime.now().strftime("%Y-%m-%d") / "koan.md"
        assert journal_file.exists()
        assert "koan/fix" in journal_file.read_text()

    def test_write_merge_failure_delegates(self, tmp_path):
        """Module-level write_merge_failure_to_journal delegates to class."""
        inst = str(tmp_path)
        write_merge_failure_to_journal(inst, "koan", "koan/fix", "conflict")
        from datetime import datetime
        journal_file = tmp_path / "journal" / datetime.now().strftime("%Y-%m-%d") / "koan.md"
        assert journal_file.exists()
        assert "conflict" in journal_file.read_text()


class TestSquashCommitFailureCleanup:
    """Verify squash commit failure resets working tree."""

    @patch("app.git_auto_merge.run_git")
    def test_squash_commit_failure_resets_hard(self, mock_git):
        """When squash commit fails, git reset --hard is called to clean up."""
        mock_git.side_effect = [
            (0, "fix stuff", ""),  # git log
            (0, "", ""),           # checkout base
            (0, "", ""),           # pull
            (0, "", ""),           # merge --squash (succeeds)
            (1, "", "commit failed"),  # commit fails
            (0, "", ""),           # reset --hard (cleanup)
            (0, "", ""),           # checkout base (finally)
        ]
        merger = GitAutoMerger("/inst", "koan", "/proj")
        success, error = merger.perform_merge("koan/fix", "main", "squash")
        assert success is False
        assert "commit" in error.lower()
        # Verify reset --hard was called (6th call, index 5)
        reset_call = mock_git.call_args_list[5]
        assert "reset" in reset_call[0]
        assert "--hard" in reset_call[0]


# ---------------------------------------------------------------------------
# CLI __main__ interface
# ---------------------------------------------------------------------------

class TestCLIMainBlock:
    """Test the CLI interface via runpy."""

    def test_no_args_exits_1(self):
        """CLI with insufficient arguments exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["git_auto_merge"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.git_auto_merge", run_name="__main__")
            assert exc_info.value.code == 1

    def test_too_few_args_exits_1(self):
        """CLI with 3 args (needs 4) exits 1."""
        import sys
        from unittest.mock import patch
        from tests._helpers import run_module

        with patch.object(sys, "argv", ["git_auto_merge", "/inst", "proj", "/path"]):
            with pytest.raises(SystemExit) as exc_info:
                run_module("app.git_auto_merge", run_name="__main__")
            assert exc_info.value.code == 1

    def test_calls_auto_merge_branch(self, tmp_path):
        """CLI with 4 args creates merger and calls auto_merge_branch."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "app.git_auto_merge",
             str(tmp_path), "testproj", str(tmp_path), "koan/fake-branch"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent),
            env={**os.environ, "KOAN_ROOT": str(tmp_path)},
        )
        # Should run without crashing (may fail gracefully since no git config)
        # The key test: arg parsing works and it reaches the merger logic
        assert "Usage:" not in result.stdout
        assert result.returncode in (0, 1)  # 0=success or 1=merge not configured
