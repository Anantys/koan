"""Tests for mission_runner.py — mission execution pipeline."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


class TestBuildMissionCommand:
    """Test build_mission_command function."""

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_basic_command(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="Do something")
        # Provider-agnostic: check for prompt and output format, not specific binary
        assert "-p" in cmd or any("Do something" in arg for arg in cmd)
        assert "--output-format" in cmd or any("json" in arg for arg in cmd)

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_includes_allowed_tools(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="test")
        # Tools should be present in the command (format depends on provider)
        cmd_str = " ".join(cmd)
        # Either Claude format (--allowedTools Read,Write,...) or converted to provider format
        assert any(tool in cmd_str for tool in ["Bash", "Read", "Write", "Edit"])

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_extra_flags_appended(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="test", extra_flags="--model opus")
        assert "--model" in cmd
        assert "opus" in cmd

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_empty_extra_flags_ignored(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="test", extra_flags="")
        base = build_mission_command(prompt="test")
        assert len(cmd) == len(base)

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_whitespace_extra_flags_ignored(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="test", extra_flags="   ")
        base = build_mission_command(prompt="test")
        assert len(cmd) == len(base)

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    def test_copilot_provider(self):
        # Reset cached provider to pick up env var
        from app.cli_provider import reset_provider
        reset_provider()

        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="test")
        # When copilot is configured, should use gh copilot
        assert "gh" in cmd or "copilot" in cmd[0]

        # Clean up
        reset_provider()

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_plugin_dirs_forwarded(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(
            prompt="test",
            plugin_dirs=["/tmp/koan-plugins"],
        )
        assert "--plugin-dir" in cmd
        idx = cmd.index("--plugin-dir")
        assert cmd[idx + 1] == "/tmp/koan-plugins"

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_plugin_dirs_none_excluded(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="test")
        assert "--plugin-dir" not in cmd


class TestGetMissionFlags:
    """Test get_mission_flags function."""

    @patch("app.config.get_claude_flags_for_role", return_value="--model opus")
    def test_returns_flags_from_config(self, mock_flags):
        from app.mission_runner import get_mission_flags

        result = get_mission_flags("deep")
        assert result == "--model opus"
        mock_flags.assert_called_once_with("mission", "deep", "")

    @patch("app.config.get_claude_flags_for_role", return_value="")
    def test_returns_empty_string_when_no_flags(self, mock_flags):
        from app.mission_runner import get_mission_flags

        result = get_mission_flags()
        assert result == ""


class TestParseClaudeOutput:
    """Test parse_claude_output function."""

    def test_extracts_result_key(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": "Mission completed"})
        assert parse_claude_output(raw) == "Mission completed"

    def test_extracts_content_key(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"content": "Some content"})
        assert parse_claude_output(raw) == "Some content"

    def test_extracts_text_key(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"text": "Some text"})
        assert parse_claude_output(raw) == "Some text"

    def test_prefers_result_over_content(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": "winner", "content": "loser"})
        assert parse_claude_output(raw) == "winner"

    def test_falls_back_to_raw_on_invalid_json(self):
        from app.mission_runner import parse_claude_output

        assert parse_claude_output("not json at all") == "not json at all"

    def test_returns_empty_on_empty_input(self):
        from app.mission_runner import parse_claude_output

        assert parse_claude_output("") == ""
        assert parse_claude_output("   ") == ""

    def test_handles_non_string_values(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": 42, "content": ["a", "b"]})
        # Neither is a string, so fallback to raw
        assert parse_claude_output(raw) == raw.strip()

    def test_handles_json_without_known_keys(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"status": "ok", "data": "test"})
        assert parse_claude_output(raw) == raw.strip()


class TestArchivePending:
    """Test archive_pending function."""

    def test_archives_pending_to_journal(self, tmp_path):
        from app.mission_runner import archive_pending

        # Create pending.md
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("# Mission: test\n09:00 — did something\n")

        result = archive_pending(str(tmp_path), "myproject", 5)

        assert result is True
        assert not pending.exists()
        # Check daily journal was created
        import datetime
        today = datetime.date.today().strftime("%Y-%m-%d")
        journal_file = journal_dir / today / "myproject.md"
        assert journal_file.exists()
        content = journal_file.read_text()
        assert "Run 5" in content
        assert "auto-archived from pending" in content
        assert "did something" in content

    def test_returns_false_when_no_pending(self, tmp_path):
        from app.mission_runner import archive_pending

        result = archive_pending(str(tmp_path), "myproject", 1)
        assert result is False

    def test_appends_to_existing_journal(self, tmp_path):
        from app.mission_runner import archive_pending

        import datetime
        today = datetime.date.today().strftime("%Y-%m-%d")
        journal_dir = tmp_path / "journal" / today
        journal_dir.mkdir(parents=True)
        journal_file = journal_dir / "myproject.md"
        journal_file.write_text("# Previous entry\n")

        pending = tmp_path / "journal" / "pending.md"
        pending.write_text("New content\n")

        archive_pending(str(tmp_path), "myproject", 3)

        content = journal_file.read_text()
        assert "Previous entry" in content
        assert "New content" in content

    @patch("app.journal.append_to_journal")
    def test_uses_append_to_journal_for_locking(self, mock_append, tmp_path):
        """Verify archive_pending uses append_to_journal (which has file locking)."""
        from app.mission_runner import archive_pending

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("test content\n")

        archive_pending(str(tmp_path), "myproject", 1)

        mock_append.assert_called_once()
        args = mock_append.call_args
        assert args[0][1] == "myproject"  # project_name
        assert "test content" in args[0][2]  # content


    def test_handles_file_deleted_between_check_and_read(self, tmp_path):
        """TOCTOU: pending.md disappears between exists() and read_text()."""
        from app.mission_runner import archive_pending

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("content")

        # Patch read_text to simulate file disappearing after exists() returns True
        original_read = Path.read_text

        def disappearing_read(self, *args, **kwargs):
            if self.name == "pending.md":
                raise FileNotFoundError("No such file")
            return original_read(self, *args, **kwargs)

        with patch.object(Path, "read_text", disappearing_read):
            result = archive_pending(str(tmp_path), "myproject", 1)

        assert result is False

    def test_handles_file_deleted_between_read_and_unlink(self, tmp_path):
        """pending.md disappears between read_text() and unlink() — missing_ok."""
        from app.mission_runner import archive_pending

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("content to archive\n")

        # Patch unlink to simulate file already deleted
        original_unlink = Path.unlink

        def disappearing_unlink(self, *args, **kwargs):
            if self.name == "pending.md":
                # Delete first, then call with missing_ok (should not raise)
                if self.exists():
                    original_unlink(self, missing_ok=True)
                # Simulate the file being gone — call again to test missing_ok
                original_unlink(self, missing_ok=True)
                return
            return original_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", disappearing_unlink):
            result = archive_pending(str(tmp_path), "myproject", 2)

        # Should succeed — content was read before the race
        assert result is True


class TestUpdateUsage:
    """Test update_usage function."""

    @patch("app.usage_estimator.cmd_update")
    def test_calls_cmd_update(self, mock_update):
        from app.mission_runner import update_usage

        result = update_usage("/tmp/out.json", "/tmp/state.json", "/tmp/usage.md")
        assert result is True
        mock_update.assert_called_once()

    @patch("app.usage_estimator.cmd_update", side_effect=Exception("fail"))
    def test_returns_false_on_error(self, mock_update):
        from app.mission_runner import update_usage

        result = update_usage("/tmp/out.json", "/tmp/state.json", "/tmp/usage.md")
        assert result is False


class TestTriggerReflection:
    """Test trigger_reflection function."""

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.post_mission_reflection.run_reflection", return_value="Deep insight")
    @patch("app.post_mission_reflection.is_significant_mission", return_value=True)
    @patch("app.post_mission_reflection._read_journal_file", return_value="substantial content")
    def test_generates_reflection_for_significant_mission(
        self, mock_read, mock_sig, mock_run, mock_write, tmp_path
    ):
        from app.mission_runner import trigger_reflection

        result = trigger_reflection(str(tmp_path), "audit security", 60, project_name="koan")
        assert result is True
        mock_write.assert_called_once()
        # Verify journal content is passed to run_reflection
        mock_run.assert_called_once()
        call_kwargs_or_args = mock_run.call_args
        assert "substantial content" in str(call_kwargs_or_args)

    @patch("app.post_mission_reflection.is_significant_mission", return_value=False)
    @patch("app.post_mission_reflection._read_journal_file", return_value="")
    def test_skips_insignificant_missions(self, mock_read, mock_sig, tmp_path):
        from app.mission_runner import trigger_reflection

        result = trigger_reflection(str(tmp_path), "small fix", 5, project_name="koan")
        assert result is False

    @patch("app.post_mission_reflection.run_reflection", return_value="")
    @patch("app.post_mission_reflection.is_significant_mission", return_value=True)
    @patch("app.post_mission_reflection._read_journal_file", return_value="content")
    def test_returns_false_when_no_reflection_generated(self, mock_read, mock_sig, mock_run, tmp_path):
        from app.mission_runner import trigger_reflection

        result = trigger_reflection(str(tmp_path), "deep refactor", 60, project_name="koan")
        assert result is False

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.post_mission_reflection.run_reflection", return_value="Insight")
    @patch("app.post_mission_reflection.is_significant_mission", return_value=True)
    @patch("app.post_mission_reflection._read_journal_file", return_value="journal text")
    def test_passes_project_name_to_read_journal(
        self, mock_read, mock_sig, mock_run, mock_write, tmp_path
    ):
        from app.mission_runner import trigger_reflection

        trigger_reflection(str(tmp_path), "audit", 60, project_name="myproject")
        mock_read.assert_called_once()
        call_args = mock_read.call_args
        assert call_args[0][1] == "myproject"


class TestCheckAutoMerge:
    """Test check_auto_merge function."""

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.git_sync.run_git", return_value="koan/my-feature")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_checks_koan_branch(self, mock_prefix, mock_git, mock_merge, tmp_path):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(str(tmp_path), "project", str(tmp_path))
        assert result == "koan/my-feature"
        mock_merge.assert_called_once()

    @patch("app.git_sync.run_git", return_value="main")
    def test_skips_non_koan_branch(self, mock_git, tmp_path):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(str(tmp_path), "project", str(tmp_path))
        assert result is None

    @patch("app.git_sync.run_git", return_value="")
    def test_returns_none_on_empty_branch(self, mock_git, tmp_path):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(str(tmp_path), "project", str(tmp_path))
        assert result is None


class TestRunPostMission:
    """Test run_post_mission orchestration function."""

    @patch("app.mission_runner.commit_instance")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_successful_run(self, mock_usage, mock_quota, mock_archive,
                            mock_reflect, mock_merge, mock_commit, tmp_path):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        assert result["success"] is True
        assert result["usage_updated"] is True
        assert result["quota_exhausted"] is False

    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=("resets 10am", "Auto-resume in 5h"))
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_quota_exhaustion_early_return(self, mock_usage, mock_quota,
                                           mock_archive, mock_reflect, mock_merge, tmp_path):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=5,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        assert result["quota_exhausted"] is True
        assert result["quota_info"] == ("resets 10am", "Auto-resume in 5h")
        # Should NOT call archive, reflection, or merge
        mock_archive.assert_not_called()
        mock_reflect.assert_not_called()
        mock_merge.assert_not_called()

    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=True)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_pending_archived(self, mock_usage, mock_quota, mock_archive,
                              mock_reflect, mock_merge, tmp_path):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=2,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        assert result["pending_archived"] is True

    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_failed_run_skips_reflection_and_merge(self, mock_usage, mock_quota,
                                                    mock_archive, mock_reflect,
                                                    mock_merge, tmp_path):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=3,
            exit_code=1,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        assert result["success"] is False
        mock_reflect.assert_not_called()
        mock_merge.assert_not_called()

    @patch("app.mission_runner.check_auto_merge", return_value="koan/feature")
    @patch("app.mission_runner.trigger_reflection", return_value=True)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_full_pipeline_success(self, mock_usage, mock_quota, mock_archive,
                                    mock_reflect, mock_merge, tmp_path):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            mission_title="audit security",
            start_time=1000000,
        )

        assert result["success"] is True
        assert result["reflection_written"] is True
        assert result["auto_merge_branch"] == "koan/feature"


class TestPostMissionHookFiring:
    """Test that post_mission hooks fire in all code paths."""

    @patch("app.mission_runner._fire_post_mission_hook")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=("resets 10am", "Auto-resume in 5h"))
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_hook_fires_on_quota_exhaustion(self, mock_usage, mock_quota,
                                            mock_archive, mock_reflect,
                                            mock_merge, mock_hook, tmp_path):
        """post_mission hook must fire even when quota is exhausted."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=5,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            mission_title="test mission",
        )

        assert result["quota_exhausted"] is True
        # Hook must have been called once (in the quota path)
        mock_hook.assert_called_once()
        call_kwargs = mock_hook.call_args
        # Verify quota_exhausted is True in the result dict passed to hook
        assert call_kwargs[0][6]["quota_exhausted"] is True

    @patch("app.mission_runner._fire_post_mission_hook")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_hook_fires_on_normal_completion(self, mock_usage, mock_quota,
                                              mock_archive, mock_reflect,
                                              mock_merge, mock_hook, tmp_path):
        """post_mission hook fires on normal (non-quota) completion."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=1,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        mock_hook.assert_called_once()


class TestRunPostMissionKoanRoot:
    """Test that run_post_mission uses KOAN_ROOT env var for koan_root."""

    @patch("app.mission_runner.commit_instance")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_uses_koan_root_env_var(self, mock_usage, mock_quota, mock_archive,
                                     mock_reflect, mock_merge, mock_commit,
                                     tmp_path, monkeypatch):
        """run_post_mission should prefer KOAN_ROOT env var over Path.parent."""
        from app.mission_runner import run_post_mission

        koan_root = str(tmp_path / "my-koan")
        instance_dir = str(tmp_path / "deep" / "nested" / "instance")
        os.makedirs(instance_dir, exist_ok=True)
        monkeypatch.setenv("KOAN_ROOT", koan_root)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        # quota_handler should receive the KOAN_ROOT, not instance_dir's parent
        call_kwargs = mock_quota.call_args[1]
        assert call_kwargs["koan_root"] == koan_root

    @patch("app.mission_runner.commit_instance")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_falls_back_to_parent_without_env(self, mock_usage, mock_quota,
                                               mock_archive, mock_reflect,
                                               mock_merge, mock_commit,
                                               tmp_path, monkeypatch):
        """Without KOAN_ROOT env, falls back to instance_dir's parent."""
        from app.mission_runner import run_post_mission

        monkeypatch.delenv("KOAN_ROOT", raising=False)
        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        call_kwargs = mock_quota.call_args[1]
        assert call_kwargs["koan_root"] == str(tmp_path)


class TestCommitInstance:
    """Test commit_instance function."""

    @patch("app.git_sync.run_git")
    def test_commits_and_pushes_when_changes(self, mock_git, tmp_path):
        from app.mission_runner import commit_instance

        # run_git calls: add -A, diff --cached --name-only, commit, rev-parse, push
        mock_git.side_effect = [
            "",                    # git add -A (no output)
            "file1.md\nfile2.md",  # git diff --cached --name-only (has changes)
            "",                    # git commit
            "main",                # git rev-parse --abbrev-ref HEAD
            "",                    # git push origin main
        ]

        result = commit_instance(str(tmp_path))
        assert result is True
        assert mock_git.call_count == 5

    @patch("app.git_sync.run_git")
    def test_returns_false_when_no_changes(self, mock_git, tmp_path):
        from app.mission_runner import commit_instance

        mock_git.side_effect = [
            "",   # git add -A
            "",   # git diff --cached --name-only (no changes → empty string)
        ]

        result = commit_instance(str(tmp_path))
        assert result is False
        assert mock_git.call_count == 2


    @patch("app.git_sync.run_git")
    def test_pushes_to_current_branch_not_hardcoded_main(self, mock_git, tmp_path):
        """commit_instance should detect and push to the current branch."""
        from app.mission_runner import commit_instance

        # run_git calls: add -A, diff, commit, rev-parse, push
        mock_git.side_effect = [
            "",                    # git add -A
            "file1.md",            # git diff --cached --name-only
            "",                    # git commit
            "develop",             # git rev-parse --abbrev-ref HEAD
            "",                    # git push origin develop
        ]

        result = commit_instance(str(tmp_path))
        assert result is True
        assert mock_git.call_count == 5
        # Verify push uses detected branch, not "main"
        push_call = mock_git.call_args_list[4]
        assert push_call[0] == (str(tmp_path), "push", "origin", "develop")

    @patch("app.git_sync.run_git")
    def test_skips_push_on_empty_branch(self, mock_git, tmp_path):
        """If rev-parse returns empty, skip push instead of pushing to main."""
        from app.mission_runner import commit_instance

        mock_git.side_effect = [
            "",          # git add -A
            "changes",   # git diff --cached --name-only
            "",          # git commit
            "",          # git rev-parse --abbrev-ref HEAD (empty)
        ]

        result = commit_instance(str(tmp_path))
        assert result is True
        # Should NOT have called push (only 4 git calls, not 5)
        assert mock_git.call_count == 4

    @patch("app.git_sync.run_git")
    def test_skips_push_on_detached_head(self, mock_git, tmp_path):
        """If rev-parse returns 'HEAD' (detached), skip push."""
        from app.mission_runner import commit_instance

        mock_git.side_effect = [
            "",          # git add -A
            "changes",   # git diff --cached --name-only
            "",          # git commit
            "HEAD",      # git rev-parse --abbrev-ref HEAD (detached)
        ]

        result = commit_instance(str(tmp_path))
        assert result is True
        assert mock_git.call_count == 4

    @patch("app.git_sync.run_git")
    def test_returns_false_on_push_failure(self, mock_git, tmp_path):
        """If git push raises, commit_instance returns False gracefully."""
        from app.mission_runner import commit_instance

        mock_git.side_effect = [
            "",          # git add -A
            "changes",   # git diff --cached --name-only
            "",          # git commit
            "main",      # git rev-parse --abbrev-ref HEAD
            Exception("network error"),  # git push fails
        ]

        result = commit_instance(str(tmp_path))
        assert result is False

    @patch("app.git_sync.run_git")
    def test_returns_false_on_commit_failure(self, mock_git, tmp_path):
        """If git commit raises, commit_instance returns False gracefully."""
        from app.mission_runner import commit_instance

        mock_git.side_effect = [
            "",          # git add -A
            "changes",   # git diff --cached --name-only
            Exception("commit failed"),  # git commit raises
        ]

        result = commit_instance(str(tmp_path))
        assert result is False


class TestCLIParseOutput:
    """Test parse-output CLI subcommand."""

    def test_cli_parse_output(self, tmp_path):
        from app.mission_runner import _cli_parse_output

        json_file = tmp_path / "output.json"
        json_file.write_text(json.dumps({"result": "Hello world"}))

        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            _cli_parse_output([str(json_file)])
        assert f.getvalue().strip() == "Hello world"

    def test_cli_parse_output_missing_file(self):
        from app.mission_runner import _cli_parse_output

        with pytest.raises(SystemExit) as exc_info:
            _cli_parse_output(["/nonexistent/file.json"])
        assert exc_info.value.code == 1

    def test_cli_parse_output_no_args(self):
        from app.mission_runner import _cli_parse_output

        with pytest.raises(SystemExit) as exc_info:
            _cli_parse_output([])
        assert exc_info.value.code == 1


class TestCLIPostMission:
    """Test post-mission CLI subcommand."""

    @patch("app.mission_runner.run_post_mission")
    def test_cli_post_mission_success(self, mock_run, tmp_path):
        from app.mission_runner import _cli_post_mission

        mock_run.return_value = {
            "success": True,
            "usage_updated": True,
            "pending_archived": False,
            "reflection_written": False,
            "auto_merge_branch": None,
            "quota_exhausted": False,
            "quota_info": None,
        }

        with pytest.raises(SystemExit) as exc_info:
            _cli_post_mission([
                "--instance", str(tmp_path),
                "--project-name", "koan",
                "--project-path", str(tmp_path),
                "--run-num", "1",
                "--exit-code", "0",
                "--stdout-file", "/tmp/out",
                "--stderr-file", "/tmp/err",
            ])
        assert exc_info.value.code == 0

    @patch("app.mission_runner.run_post_mission")
    def test_cli_post_mission_quota_exhausted(self, mock_run, tmp_path):
        from app.mission_runner import _cli_post_mission

        mock_run.return_value = {
            "success": True,
            "usage_updated": True,
            "pending_archived": False,
            "reflection_written": False,
            "auto_merge_branch": None,
            "quota_exhausted": True,
            "quota_info": ("resets 10am", "Auto-resume at 10am"),
        }

        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with pytest.raises(SystemExit) as exc_info:
            with redirect_stdout(f):
                _cli_post_mission([
                    "--instance", str(tmp_path),
                    "--project-name", "koan",
                    "--project-path", str(tmp_path),
                    "--run-num", "5",
                    "--exit-code", "0",
                    "--stdout-file", "/tmp/out",
                    "--stderr-file", "/tmp/err",
                ])
        assert exc_info.value.code == 2
        assert "QUOTA_EXHAUSTED" in f.getvalue()

    @patch("app.mission_runner.run_post_mission")
    def test_cli_post_mission_failure(self, mock_run, tmp_path):
        from app.mission_runner import _cli_post_mission

        mock_run.return_value = {
            "success": False,
            "usage_updated": True,
            "pending_archived": False,
            "reflection_written": False,
            "auto_merge_branch": None,
            "quota_exhausted": False,
            "quota_info": None,
        }

        with pytest.raises(SystemExit) as exc_info:
            _cli_post_mission([
                "--instance", str(tmp_path),
                "--project-name", "koan",
                "--project-path", str(tmp_path),
                "--run-num", "3",
                "--exit-code", "1",
                "--stdout-file", "/tmp/out",
                "--stderr-file", "/tmp/err",
            ])
        assert exc_info.value.code == 1


class TestReadPendingContent:
    """Test _read_pending_content private helper."""

    def test_reads_existing_pending_file(self, tmp_path):
        from app.mission_runner import _read_pending_content

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("# Mission: test\n09:00 — started\n")

        content = _read_pending_content(str(tmp_path))
        assert "Mission: test" in content
        assert "09:00" in content

    def test_returns_empty_when_no_pending(self, tmp_path):
        from app.mission_runner import _read_pending_content

        # No journal dir at all
        content = _read_pending_content(str(tmp_path))
        assert content == ""

    def test_returns_empty_when_journal_exists_but_no_pending(self, tmp_path):
        from app.mission_runner import _read_pending_content

        (tmp_path / "journal").mkdir()
        content = _read_pending_content(str(tmp_path))
        assert content == ""

    def test_returns_empty_on_os_error(self, tmp_path):
        from app.mission_runner import _read_pending_content

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        pending = journal_dir / "pending.md"
        pending.write_text("content")

        # Simulate OSError during read
        with patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
            content = _read_pending_content(str(tmp_path))
        assert content == ""


class TestReadStdoutSummary:
    """Test _read_stdout_summary — fallback content for session classification."""

    def test_reads_json_output(self, tmp_path):
        from app.mission_runner import _read_stdout_summary

        stdout = tmp_path / "out.json"
        stdout.write_text('{"result": "Branch pushed. PR #42 created."}')
        assert "Branch pushed" in _read_stdout_summary(str(stdout))

    def test_reads_plain_text(self, tmp_path):
        from app.mission_runner import _read_stdout_summary

        stdout = tmp_path / "out.txt"
        stdout.write_text("Implemented feature. Tests pass.")
        assert "Tests pass" in _read_stdout_summary(str(stdout))

    def test_returns_empty_for_missing_file(self):
        from app.mission_runner import _read_stdout_summary

        assert _read_stdout_summary("/nonexistent/path") == ""

    def test_returns_empty_for_empty_file(self, tmp_path):
        from app.mission_runner import _read_stdout_summary

        stdout = tmp_path / "out.json"
        stdout.write_text("")
        assert _read_stdout_summary(str(stdout)) == ""

    def test_truncates_long_output(self, tmp_path):
        from app.mission_runner import _read_stdout_summary

        stdout = tmp_path / "out.txt"
        stdout.write_text("x" * 5000)
        result = _read_stdout_summary(str(stdout), max_chars=100)
        assert len(result) == 100

    def test_handles_json_with_content_key(self, tmp_path):
        from app.mission_runner import _read_stdout_summary

        stdout = tmp_path / "out.json"
        stdout.write_text('{"content": "Draft PR submitted."}')
        assert "Draft PR" in _read_stdout_summary(str(stdout))


class TestRecordSessionOutcome:
    """Test _record_session_outcome fire-and-forget helper."""

    @patch("app.session_tracker.record_outcome")
    def test_calls_record_outcome(self, mock_record, tmp_path):
        from app.mission_runner import _record_session_outcome

        _record_session_outcome(
            str(tmp_path), "koan", "implement", 15, "journal content"
        )
        mock_record.assert_called_once_with(
            instance_dir=str(tmp_path),
            project="koan",
            mode="implement",
            duration_minutes=15,
            journal_content="journal content",
            mission_title="",
        )

    @patch("app.session_tracker.record_outcome")
    def test_passes_mission_title(self, mock_record, tmp_path):
        from app.mission_runner import _record_session_outcome

        _record_session_outcome(
            str(tmp_path), "koan", "implement", 15, "",
            mission_title="/rebase https://github.com/o/r/pull/1",
        )
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["mission_title"] == "/rebase https://github.com/o/r/pull/1"

    @patch("app.session_tracker.record_outcome")
    def test_uses_unknown_mode_when_empty(self, mock_record, tmp_path):
        from app.mission_runner import _record_session_outcome

        _record_session_outcome(str(tmp_path), "koan", "", 5, "")
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["mode"] == "unknown"

    @patch("app.session_tracker.record_outcome", side_effect=Exception("db error"))
    def test_silently_catches_exceptions(self, mock_record, tmp_path, capsys):
        from app.mission_runner import _record_session_outcome

        # Should not raise
        _record_session_outcome(str(tmp_path), "koan", "deep", 30, "text")
        captured = capsys.readouterr()
        assert "Session outcome recording failed" in captured.err


class TestRunPostMissionDuration:
    """Test duration computation in run_post_mission."""

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_duration_computed_from_start_time(
        self, mock_usage, mock_quota, mock_read_pending, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        from app.mission_runner import run_post_mission
        import time

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)
        start = int(time.time()) - 600  # 10 minutes ago

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            start_time=start,
        )

        # trigger_reflection should receive duration ~10 minutes
        mock_reflect.assert_called_once()
        call_args = mock_reflect.call_args
        duration = call_args[1].get("duration_minutes") if "duration_minutes" in (call_args[1] or {}) else call_args[0][2]
        assert 9 <= duration <= 11

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_zero_start_time_gives_zero_duration(
        self, mock_usage, mock_quota, mock_read_pending, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            start_time=0,
        )

        mock_reflect.assert_called_once()
        call_args = mock_reflect.call_args
        duration = call_args[0][2]  # positional arg
        assert duration == 0

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_autonomous_mode_fallback_mission_text(
        self, mock_usage, mock_quota, mock_read_pending, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """When no mission_title, reflection uses 'Autonomous X on Y' text."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            autonomous_mode="deep",
        )

        mock_reflect.assert_called_once()
        mission_text = mock_reflect.call_args[0][1]
        assert "Autonomous deep on koan" in mission_text

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="pending content here")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_pending_content_passed_to_record_outcome(
        self, mock_usage, mock_quota, mock_read_pending, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """Pending content is read before archival and passed to _record_session_outcome."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        mock_record.assert_called_once()
        # _record_session_outcome is called with positional args
        call_args = mock_record.call_args[0]
        assert call_args[4] == "pending content here"  # journal_content is 5th positional arg


class TestCheckAutoMergeVerifyBlocked:
    """Test check_auto_merge with verify_blocked parameter."""

    @patch("app.git_sync.run_git", return_value="koan/feature")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_verify_blocked_prevents_merge(self, mock_prefix, mock_git, tmp_path, capsys):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(
            str(tmp_path), "project", str(tmp_path), verify_blocked=True
        )
        assert result is None
        captured = capsys.readouterr()
        assert "blocked by verification failure" in captured.out

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.git_sync.run_git", return_value="koan/feature")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_verify_not_blocked_allows_merge(self, mock_prefix, mock_git, mock_merge, tmp_path):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(
            str(tmp_path), "project", str(tmp_path), verify_blocked=False
        )
        assert result == "koan/feature"
        mock_merge.assert_called_once()

    @patch("app.git_sync.run_git", return_value="koan/feature")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_verify_blocked_independent_of_lint(self, mock_prefix, mock_git, tmp_path, capsys):
        """Verify failure blocks auto-merge even when lint is not blocking."""
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(
            str(tmp_path), "project", str(tmp_path),
            lint_blocked=False, verify_blocked=True,
        )
        assert result is None
        captured = capsys.readouterr()
        assert "blocked by verification failure" in captured.out
        assert "lint gate" not in captured.out

    @patch("app.git_sync.run_git", return_value="koan/feature")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_lint_blocked_checked_before_verify(self, mock_prefix, mock_git, tmp_path, capsys):
        """When both lint and verify block, lint message appears (checked first)."""
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(
            str(tmp_path), "project", str(tmp_path),
            lint_blocked=True, verify_blocked=True,
        )
        assert result is None
        captured = capsys.readouterr()
        assert "blocked by lint gate" in captured.out


class TestCheckAutoMergeErrors:
    """Test check_auto_merge error handling."""

    @patch("app.git_auto_merge.auto_merge_branch", side_effect=Exception("merge conflict"))
    @patch("app.git_sync.run_git", return_value="koan/feature")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_returns_none_on_merge_error(self, mock_prefix, mock_git, mock_merge, tmp_path, capsys):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(str(tmp_path), "koan", str(tmp_path))
        assert result is None
        captured = capsys.readouterr()
        assert "Auto-merge check failed" in captured.err

    @patch("app.git_sync.run_git", side_effect=Exception("git error"))
    def test_returns_none_on_git_error(self, mock_git, tmp_path, capsys):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(str(tmp_path), "koan", str(tmp_path))
        assert result is None
        captured = capsys.readouterr()
        assert "Auto-merge check failed" in captured.err


class TestTriggerReflectionErrors:
    """Test trigger_reflection error handling."""

    @patch("app.post_mission_reflection._read_journal_file", side_effect=Exception("IO error"))
    def test_returns_false_on_exception(self, mock_read, tmp_path, capsys):
        from app.mission_runner import trigger_reflection

        result = trigger_reflection(str(tmp_path), "audit", 60, project_name="koan")
        assert result is False
        captured = capsys.readouterr()
        assert "Reflection failed" in captured.err


class TestParseClaudeOutputEdgeCases:
    """Additional edge cases for parse_claude_output."""

    def test_json_null_value(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": None, "content": None})
        # None is not str, so falls back to raw
        assert parse_claude_output(raw) == raw.strip()

    def test_json_with_nested_content(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": {"inner": "data"}, "text": "plain"})
        # result is dict (not str), text is str → should return "plain"
        assert parse_claude_output(raw) == "plain"

    def test_json_with_empty_string_result(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": "", "content": "fallback"})
        # Empty string IS a string, so it's returned (truthy check not done on value)
        assert parse_claude_output(raw) == ""

    def test_whitespace_only_json(self):
        from app.mission_runner import parse_claude_output

        assert parse_claude_output("  \n\t  ") == ""


class TestCLIMain:
    """Test main CLI entry point."""

    def test_unknown_subcommand(self):
        from app.mission_runner import main

        with patch.object(sys, "argv", ["mission_runner.py", "unknown"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_args(self):
        from app.mission_runner import main

        with patch.object(sys, "argv", ["mission_runner.py"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_build_command_subcommand(self):
        """main() dispatches build-command to _cli_build_command."""
        from app.mission_runner import main

        with patch("app.mission_runner._cli_build_command") as mock_cli:
            with patch.object(sys, "argv", ["mr", "build-command", "--prompt", "x"]):
                main()
            mock_cli.assert_called_once_with(["--prompt", "x"])

    def test_parse_output_subcommand(self):
        """main() dispatches parse-output to _cli_parse_output."""
        from app.mission_runner import main

        with patch("app.mission_runner._cli_parse_output") as mock_cli:
            with patch.object(sys, "argv", ["mr", "parse-output", "/tmp/f"]):
                main()
            mock_cli.assert_called_once_with(["/tmp/f"])

    def test_post_mission_subcommand(self):
        """main() dispatches post-mission to _cli_post_mission."""
        from app.mission_runner import main

        with patch("app.mission_runner._cli_post_mission") as mock_cli:
            with patch.object(sys, "argv", ["mr", "post-mission", "--instance", "x"]):
                main()
            mock_cli.assert_called_once_with(["--instance", "x"])


class TestBuildMissionCommandReviewMode:
    """Test build_mission_command in review mode — enforces read-only tools."""

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_review_mode_uses_read_only_tools(self, mock_provider):
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(prompt="review code", autonomous_mode="review")
        cmd_str = " ".join(cmd)
        # Review mode must include Read, Glob, Grep
        assert "Read" in cmd_str
        assert "Glob" in cmd_str
        assert "Grep" in cmd_str
        # Review mode must NOT include write tools
        assert "Bash" not in cmd_str
        assert "Write" not in cmd_str
        assert "Edit" not in cmd_str

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_review_mode_uses_review_model(self, mock_provider):
        """Review mode should use the review_mode model when configured."""
        from app.mission_runner import build_mission_command

        with patch("app.config.get_model_config", return_value={
            "mission": "sonnet",
            "review_mode": "haiku",
            "fallback": "sonnet",
        }):
            cmd = build_mission_command(
                prompt="review code", autonomous_mode="review"
            )
            cmd_str = " ".join(cmd)
            assert "haiku" in cmd_str

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_review_mode_falls_back_to_mission_model(self, mock_provider):
        """When review_mode model is empty, falls back to mission model."""
        from app.mission_runner import build_mission_command

        with patch("app.config.get_model_config", return_value={
            "mission": "opus",
            "review_mode": "",
            "fallback": "sonnet",
        }):
            cmd = build_mission_command(
                prompt="review code", autonomous_mode="review"
            )
            cmd_str = " ".join(cmd)
            # When review_mode is empty/falsy, mission model is used
            assert "opus" in cmd_str

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_non_review_mode_uses_full_tools(self, mock_provider):
        """Implement/deep modes should include write tools."""
        from app.mission_runner import build_mission_command

        for mode in ("implement", "deep"):
            cmd = build_mission_command(prompt="code", autonomous_mode=mode)
            cmd_str = " ".join(cmd)
            # Non-review modes get the full toolset from config
            assert "Bash" in cmd_str or "Read" in cmd_str


class TestBuildMissionCommandProjectOverrides:
    """Test build_mission_command with per-project tool/model overrides."""

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_project_name_forwarded_to_get_mission_tools(self, mock_provider):
        from app.mission_runner import build_mission_command

        with patch("app.config.get_mission_tools", return_value="Read,Grep") as mock_tools:
            build_mission_command(
                prompt="test", project_name="backend"
            )
            mock_tools.assert_called_once_with("backend")

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_project_name_forwarded_to_model_config(self, mock_provider):
        from app.mission_runner import build_mission_command

        with patch("app.config.get_model_config") as mock_models:
            mock_models.return_value = {
                "mission": "sonnet", "review_mode": "", "fallback": "haiku",
            }
            build_mission_command(prompt="test", project_name="backend")
            mock_models.assert_called_once_with("backend")

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_multiple_plugin_dirs(self, mock_provider):
        """Multiple plugin dirs should all appear as --plugin-dir flags."""
        from app.mission_runner import build_mission_command

        cmd = build_mission_command(
            prompt="test",
            plugin_dirs=["/tmp/plugin-a", "/tmp/plugin-b"],
        )
        indices = [i for i, arg in enumerate(cmd) if arg == "--plugin-dir"]
        assert len(indices) == 2
        assert cmd[indices[0] + 1] == "/tmp/plugin-a"
        assert cmd[indices[1] + 1] == "/tmp/plugin-b"


class TestParseClaudeOutputAdditional:
    """Additional edge cases for parse_claude_output."""

    def test_json_array_returns_raw(self):
        """A JSON array (not object) should fall back to raw text."""
        from app.mission_runner import parse_claude_output

        raw = json.dumps([1, 2, 3])
        result = parse_claude_output(raw)
        # json.loads succeeds but result is a list, not dict — no .get() possible
        # Actually, the code does `for key in ("result", ...): if key in data`
        # which works on lists (checks membership), not key lookup
        # But lists don't have .get(), and `key in [1,2,3]` checks values
        assert result == raw.strip()

    def test_json_boolean_values(self):
        """Boolean values for result/content keys are not strings."""
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": True, "content": False, "text": "actual"})
        assert parse_claude_output(raw) == "actual"

    def test_unicode_content(self):
        from app.mission_runner import parse_claude_output

        raw = json.dumps({"result": "Kōan — réflexion 🧘"})
        assert parse_claude_output(raw) == "Kōan — réflexion 🧘"

    def test_very_large_json(self):
        """Large JSON payloads should parse correctly."""
        from app.mission_runner import parse_claude_output

        big_text = "x" * 100_000
        raw = json.dumps({"result": big_text})
        assert parse_claude_output(raw) == big_text

    def test_multiline_raw_text(self):
        from app.mission_runner import parse_claude_output

        raw = "line1\nline2\nline3"
        assert parse_claude_output(raw) == "line1\nline2\nline3"

    def test_json_with_extra_whitespace(self):
        from app.mission_runner import parse_claude_output

        raw = '  \n  {"result": "clean"}  \n  '
        assert parse_claude_output(raw) == "clean"


class TestCLIBuildCommand:
    """Test _cli_build_command — previously untested."""

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_basic_build_command_output(self, mock_provider, capsys):
        from app.mission_runner import _cli_build_command

        _cli_build_command(["--prompt", "Hello"])
        output = capsys.readouterr().out
        # Output is newline-separated command parts
        parts = output.strip().splitlines()
        assert len(parts) > 0
        # Should contain the prompt somewhere
        assert any("Hello" in p for p in parts)

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_build_command_with_extra_flags(self, mock_provider, capsys):
        from app.mission_runner import _cli_build_command

        _cli_build_command([
            "--prompt", "test",
            "--extra-flags", "--verbose --debug",
        ])
        output = capsys.readouterr().out
        parts = output.strip().splitlines()
        assert "--verbose" in parts
        assert "--debug" in parts

    @patch("app.cli_provider.get_provider_name", return_value="claude")
    def test_build_command_review_mode(self, mock_provider, capsys):
        from app.mission_runner import _cli_build_command

        _cli_build_command([
            "--prompt", "review",
            "--autonomous-mode", "review",
        ])
        output = capsys.readouterr().out
        # Review mode should restrict tools to read-only
        assert "Read" in output
        # Bash should NOT appear in review mode
        assert "Bash" not in output


class TestCLIPostMissionOutputDetails:
    """Test post-mission CLI output formatting."""

    @patch("app.mission_runner.run_post_mission")
    def test_outputs_pending_archived_to_stderr(self, mock_run, tmp_path, capsys):
        from app.mission_runner import _cli_post_mission

        mock_run.return_value = {
            "success": True,
            "usage_updated": True,
            "pending_archived": True,
            "reflection_written": False,
            "auto_merge_branch": None,
            "quota_exhausted": False,
            "quota_info": None,
        }

        with pytest.raises(SystemExit) as exc_info:
            _cli_post_mission([
                "--instance", str(tmp_path),
                "--project-name", "koan",
                "--project-path", str(tmp_path),
                "--run-num", "1",
                "--exit-code", "0",
                "--stdout-file", "/tmp/out",
                "--stderr-file", "/tmp/err",
            ])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "PENDING_ARCHIVED" in captured.err

    @patch("app.mission_runner.run_post_mission")
    def test_outputs_auto_merge_to_stderr(self, mock_run, tmp_path, capsys):
        from app.mission_runner import _cli_post_mission

        mock_run.return_value = {
            "success": True,
            "usage_updated": True,
            "pending_archived": False,
            "reflection_written": False,
            "auto_merge_branch": "koan/my-feature",
            "quota_exhausted": False,
            "quota_info": None,
        }

        with pytest.raises(SystemExit) as exc_info:
            _cli_post_mission([
                "--instance", str(tmp_path),
                "--project-name", "koan",
                "--project-path", str(tmp_path),
                "--run-num", "1",
                "--exit-code", "0",
                "--stdout-file", "/tmp/out",
                "--stderr-file", "/tmp/err",
            ])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "AUTO_MERGE|koan/my-feature" in captured.err

    @patch("app.mission_runner.run_post_mission")
    def test_quota_exhausted_output_format(self, mock_run, tmp_path, capsys):
        """Quota exhaustion output has specific pipe-delimited format."""
        from app.mission_runner import _cli_post_mission

        mock_run.return_value = {
            "success": True,
            "usage_updated": True,
            "pending_archived": False,
            "reflection_written": False,
            "auto_merge_branch": None,
            "quota_exhausted": True,
            "quota_info": ("14:30 UTC", "Pausing until 14:30"),
        }

        with pytest.raises(SystemExit) as exc_info:
            _cli_post_mission([
                "--instance", str(tmp_path),
                "--project-name", "koan",
                "--project-path", str(tmp_path),
                "--run-num", "5",
                "--exit-code", "0",
                "--stdout-file", "/tmp/out",
                "--stderr-file", "/tmp/err",
            ])
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "QUOTA_EXHAUSTED|14:30 UTC|Pausing until 14:30" in captured.out

    @patch("app.mission_runner.run_post_mission")
    def test_passes_all_cli_args_to_run_post_mission(self, mock_run, tmp_path):
        """Verify all CLI args are correctly forwarded."""
        from app.mission_runner import _cli_post_mission

        mock_run.return_value = {
            "success": True, "usage_updated": True, "pending_archived": False,
            "reflection_written": False, "auto_merge_branch": None,
            "quota_exhausted": False, "quota_info": None,
        }

        with pytest.raises(SystemExit):
            _cli_post_mission([
                "--instance", "/tmp/inst",
                "--project-name", "myproj",
                "--project-path", "/tmp/proj",
                "--run-num", "7",
                "--exit-code", "0",
                "--stdout-file", "/tmp/stdout",
                "--stderr-file", "/tmp/stderr",
                "--mission-title", "audit security",
                "--autonomous-mode", "deep",
                "--start-time", "1700000000",
            ])

        mock_run.assert_called_once_with(
            instance_dir="/tmp/inst",
            project_name="myproj",
            project_path="/tmp/proj",
            run_num=7,
            exit_code=0,
            stdout_file="/tmp/stdout",
            stderr_file="/tmp/stderr",
            mission_title="audit security",
            autonomous_mode="deep",
            start_time=1700000000,
        )


class TestRunPostMissionOrdering:
    """Test run_post_mission execution ordering and invariants."""

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_pending_content_read_before_archive(
        self, mock_usage, mock_quota, mock_reflect, mock_merge,
        mock_record, tmp_path
    ):
        """_read_pending_content must be called before archive_pending
        so the content is available for session tracking even after archival."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        journal_dir = Path(instance_dir) / "journal"
        journal_dir.mkdir(parents=True)
        pending = journal_dir / "pending.md"
        pending.write_text("# Mission: ordering test\n10:00 — working\n")

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        # pending should be archived
        assert result["pending_archived"] is True
        # _record_session_outcome should have received the pending content
        mock_record.assert_called_once()
        journal_content = mock_record.call_args[0][4]
        assert "ordering test" in journal_content

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_session_outcome_recorded_on_failure(
        self, mock_usage, mock_quota, mock_archive, mock_reflect,
        mock_merge, mock_record, tmp_path
    ):
        """Session outcome must be recorded even when exit_code != 0."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=3,
            exit_code=1,  # failure
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            autonomous_mode="implement",
        )

        # Even on failure, session outcome is recorded
        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        assert args[1] == "koan"
        assert args[2] == "implement"

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner._read_pending_content", return_value="did stuff")
    @patch("app.quota_handler.handle_quota_exhaustion",
           return_value=("resets 10am", "Auto-resume"))
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_session_outcome_recorded_on_quota_exhaustion(
        self, mock_usage, mock_quota, mock_read_pending, mock_record, tmp_path
    ):
        """Quota exhaustion still records session outcome before early return."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=5,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            autonomous_mode="implement",
        )

        assert result["quota_exhausted"] is True
        # Quota exhaustion must still record outcome (prevents staleness bias)
        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        assert args[0] == instance_dir
        assert args[1] == "koan"
        assert args[2] == "implement"

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_mission_title_used_over_autonomous_fallback(
        self, mock_usage, mock_quota, mock_read, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """When mission_title is provided, it's used instead of autonomous fallback."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            mission_title="Fix the login bug",
            autonomous_mode="implement",
        )

        mock_reflect.assert_called_once()
        mission_text = mock_reflect.call_args[0][1]
        assert mission_text == "Fix the login bug"
        assert "Autonomous" not in mission_text

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_stdout_fallback_when_pending_empty(
        self, mock_usage, mock_quota, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """When pending.md is empty (agent deleted it), stdout content is used."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)
        # No pending.md — agent cleaned up

        # Create stdout file with productive content
        stdout_file = str(tmp_path / "stdout.json")
        Path(stdout_file).write_text('{"result": "Branch pushed. PR #42 created."}')

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file=stdout_file,
            stderr_file="/tmp/err.txt",
        )

        mock_record.assert_called_once()
        journal_content = mock_record.call_args[0][4]
        assert "Branch pushed" in journal_content

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_mission_title_passed_to_record_outcome(
        self, mock_usage, mock_quota, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """mission_title is forwarded to _record_session_outcome."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            mission_title="/rebase https://github.com/o/r/pull/42",
        )

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["mission_title"] == "/rebase https://github.com/o/r/pull/42"


class TestCheckAutoMergeBranchPrefix:
    """Test check_auto_merge with various branch prefixes."""

    @patch("app.git_auto_merge.auto_merge_branch")
    @patch("app.git_sync.run_git", return_value="koan.atoomic/my-feature")
    @patch("app.config.get_branch_prefix", return_value="koan.atoomic/")
    def test_matches_dotted_prefix(self, mock_prefix, mock_git, mock_merge, tmp_path):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(str(tmp_path), "koan", str(tmp_path))
        assert result == "koan.atoomic/my-feature"
        mock_merge.assert_called_once()

    @patch("app.git_sync.run_git", return_value="feature/new-login")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_rejects_non_matching_prefix(self, mock_prefix, mock_git, tmp_path):
        from app.mission_runner import check_auto_merge

        result = check_auto_merge(str(tmp_path), "koan", str(tmp_path))
        assert result is None

    @patch("app.git_sync.run_git", return_value="koan/")
    @patch("app.config.get_branch_prefix", return_value="koan/")
    def test_prefix_only_branch_name(self, mock_prefix, mock_git, tmp_path):
        """A branch named exactly like the prefix (no suffix) should still match."""
        from app.mission_runner import check_auto_merge

        with patch("app.git_auto_merge.auto_merge_branch"):
            result = check_auto_merge(str(tmp_path), "koan", str(tmp_path))
        assert result == "koan/"


class TestCommitInstanceEdgeCases:
    """Additional edge cases for commit_instance."""

    @patch("app.git_sync.run_git")
    def test_commit_message_includes_timestamp(self, mock_git, tmp_path):
        """Commit message should contain 'koan:' prefix and date-time."""
        from app.mission_runner import commit_instance

        mock_git.side_effect = [
            "",          # git add -A
            "changes",   # git diff --cached --name-only
            "",          # git commit
            "main",      # git rev-parse
            "",          # git push
        ]

        commit_instance(str(tmp_path))
        commit_call = mock_git.call_args_list[2]
        commit_msg = commit_call[0][3]  # 4th positional arg to run_git
        assert commit_msg.startswith("koan: ")
        # Should contain date format YYYY-MM-DD
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}", commit_msg)

    @patch("app.git_sync.run_git")
    def test_custom_message_used_when_provided(self, mock_git, tmp_path):
        """Custom message overrides the default timestamp message."""
        from app.mission_runner import commit_instance

        mock_git.side_effect = [
            "",          # git add -A
            "changes",   # git diff --cached --name-only
            "",          # git commit
            "main",      # git rev-parse
            "",          # git push
        ]

        commit_instance(str(tmp_path), "koan: quota exhausted 2026-03-07-17:00")
        commit_call = mock_git.call_args_list[2]
        commit_msg = commit_call[0][3]
        assert commit_msg == "koan: quota exhausted 2026-03-07-17:00"

    @patch("app.git_sync.run_git", side_effect=Exception("not a git repo"))
    def test_returns_false_on_add_failure(self, mock_git, tmp_path):
        """If even git add fails, returns False."""
        from app.mission_runner import commit_instance

        result = commit_instance(str(tmp_path))
        assert result is False


class TestTriggerReflectionEdgeCases:
    """Additional edge cases for trigger_reflection."""

    @patch("app.post_mission_reflection.write_to_journal", side_effect=Exception("IO"))
    @patch("app.post_mission_reflection.run_reflection", return_value="insight")
    @patch("app.post_mission_reflection.is_significant_mission", return_value=True)
    @patch("app.post_mission_reflection._read_journal_file", return_value="content")
    def test_returns_false_when_write_fails(
        self, mock_read, mock_sig, mock_run, mock_write, tmp_path, capsys
    ):
        """If run_reflection succeeds but write_to_journal fails, returns False."""
        from app.mission_runner import trigger_reflection

        result = trigger_reflection(str(tmp_path), "audit", 60, project_name="koan")
        assert result is False
        captured = capsys.readouterr()
        assert "Reflection failed" in captured.err

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.post_mission_reflection.run_reflection", return_value="insight")
    @patch("app.post_mission_reflection.is_significant_mission", return_value=True)
    @patch("app.post_mission_reflection._read_journal_file", return_value="content")
    def test_empty_project_name(
        self, mock_read, mock_sig, mock_run, mock_write, tmp_path
    ):
        """Empty project_name should still be forwarded to _read_journal_file."""
        from app.mission_runner import trigger_reflection

        trigger_reflection(str(tmp_path), "audit", 60, project_name="")
        mock_read.assert_called_once()
        assert mock_read.call_args[0][1] == ""

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.post_mission_reflection.run_reflection", return_value=None)
    @patch("app.post_mission_reflection.is_significant_mission", return_value=True)
    @patch("app.post_mission_reflection._read_journal_file", return_value="content")
    def test_none_reflection_returns_false(
        self, mock_read, mock_sig, mock_run, mock_write, tmp_path
    ):
        """When run_reflection returns None (not empty string), returns False."""
        from app.mission_runner import trigger_reflection

        result = trigger_reflection(str(tmp_path), "audit", 60, project_name="koan")
        assert result is False
        mock_write.assert_not_called()


class TestStatusCallback:
    """Test status_callback reporting during post-mission pipeline."""

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_callback_called_for_each_step(
        self, mock_usage, mock_quota, mock_read, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """status_callback is called with a description for each finalization step."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)
        steps = []

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            status_callback=steps.append,
        )

        assert len(steps) >= 4
        assert "updating usage stats" in steps
        assert "checking quota" in steps
        assert "archiving journal" in steps
        assert "recording session outcome" in steps

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_callback_includes_reflection_and_merge_on_success(
        self, mock_usage, mock_quota, mock_read, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """On exit_code=0, reflection and auto-merge steps are reported."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)
        steps = []

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            status_callback=steps.append,
        )

        assert "running reflection" in steps
        assert "checking auto-merge" in steps

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_callback_skips_reflection_on_failure(
        self, mock_usage, mock_quota, mock_read, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """On exit_code != 0, reflection and auto-merge steps are skipped."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)
        steps = []

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=1,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            status_callback=steps.append,
        )

        assert "running reflection" not in steps
        assert "checking auto-merge" not in steps

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_no_callback_does_not_crash(
        self, mock_usage, mock_quota, mock_read, mock_archive,
        mock_reflect, mock_merge, mock_record, tmp_path
    ):
        """Omitting status_callback should work without errors."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        assert result["success"] is True

    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner._read_pending_content", return_value="")
    @patch("app.quota_handler.handle_quota_exhaustion",
           return_value=("resets 10am", "Auto-resume"))
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_callback_called_on_quota_early_return(
        self, mock_usage, mock_quota, mock_read, mock_record, tmp_path
    ):
        """Even on quota exhaustion early return, steps are reported."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)
        steps = []

        run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            status_callback=steps.append,
        )

        assert "updating usage stats" in steps
        assert "checking quota" in steps
        # Should NOT have later steps due to early return
        assert "running reflection" not in steps


class TestUpdateUsageArgs:
    """Test update_usage argument forwarding."""

    @patch("app.usage_estimator.cmd_update")
    def test_passes_path_objects_to_cmd_update(self, mock_update):
        from app.mission_runner import update_usage

        update_usage("/tmp/stdout.json", "/tmp/state.json", "/tmp/usage.md")
        mock_update.assert_called_once()
        args = mock_update.call_args[0]
        assert isinstance(args[0], Path)
        assert isinstance(args[1], Path)
        assert isinstance(args[2], Path)
        assert str(args[0]) == "/tmp/stdout.json"
        assert str(args[1]) == "/tmp/state.json"
        assert str(args[2]) == "/tmp/usage.md"


class TestPipelineTracker:
    """Test _PipelineTracker step outcome tracking."""

    def test_record_and_to_dict(self):
        from app.mission_runner import _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("step_a", "success", "0.5s")
        tracker.record("step_b", "fail", "import error")

        d = tracker.to_dict()
        assert d["step_a"]["status"] == "success"
        assert d["step_b"]["status"] == "fail"
        assert d["step_b"]["detail"] == "import error"

    def test_invalid_status_raises(self):
        from app.mission_runner import _PipelineTracker

        tracker = _PipelineTracker()
        with pytest.raises(ValueError, match="Invalid status"):
            tracker.record("x", "unknown")

    def test_has_failures(self):
        from app.mission_runner import _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("a", "success")
        assert not tracker.has_failures()
        tracker.record("b", "fail", "boom")
        assert tracker.has_failures()

    def test_has_issues(self):
        from app.mission_runner import _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("a", "success")
        assert not tracker.has_issues()

        tracker2 = _PipelineTracker()
        tracker2.record("a", "timeout", "deadline")
        assert tracker2.has_issues()

        tracker3 = _PipelineTracker()
        tracker3.record("a", "skipped", "non-zero exit code")
        assert tracker3.has_issues()

        tracker4 = _PipelineTracker()
        tracker4.record("a", "fail", "boom")
        assert tracker4.has_issues()

    def test_summary_lines_format(self):
        from app.mission_runner import _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("usage", "success", "0.1s")
        tracker.record("lint", "fail", "flake8 error")
        tracker.record("merge", "skipped", "non-zero exit")
        tracker.record("quality", "timeout", "pipeline deadline exceeded")

        lines = tracker.summary_lines()
        assert len(lines) == 4
        assert "✓ usage: success (0.1s)" in lines[0]
        assert "✗ lint: fail (flake8 error)" in lines[1]
        assert "– merge: skipped" in lines[2]
        assert "⏱ quality: timeout" in lines[3]

    def test_run_step_success(self):
        from app.mission_runner import _PipelineTracker
        import threading

        tracker = _PipelineTracker()
        result = tracker.run_step("test_step", lambda: 42)
        assert result == 42
        assert tracker.steps["test_step"]["status"] == "success"

    def test_run_step_failure(self):
        from app.mission_runner import _PipelineTracker

        tracker = _PipelineTracker()

        def failing():
            raise RuntimeError("boom")

        result = tracker.run_step("bad_step", failing)
        assert result is None
        assert tracker.steps["bad_step"]["status"] == "fail"
        assert "boom" in tracker.steps["bad_step"]["detail"]

    def test_run_step_timeout(self):
        from app.mission_runner import _PipelineTracker
        import threading

        tracker = _PipelineTracker()
        expired = threading.Event()
        expired.set()

        result = tracker.run_step("timed_out", lambda: 1, pipeline_expired=expired)
        assert result is None
        assert tracker.steps["timed_out"]["status"] == "timeout"


class TestPipelineStepsInResult:
    """Test that run_post_mission includes pipeline_steps in result."""

    @patch("app.mission_runner._write_pipeline_summary")
    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_pipeline_steps_present_on_success(
        self, mock_usage, mock_quota, mock_archive,
        mock_reflect, mock_merge, mock_record, mock_summary, tmp_path
    ):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        assert "pipeline_steps" in result
        steps = result["pipeline_steps"]
        assert steps["usage_update"]["status"] == "success"
        assert steps["quota_check"]["status"] == "success"
        # Summary was written
        mock_summary.assert_called_once()

    @patch("app.mission_runner._write_pipeline_summary")
    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_failed_exit_skips_success_steps(
        self, mock_usage, mock_quota, mock_archive,
        mock_reflect, mock_merge, mock_record, mock_summary, tmp_path
    ):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=1,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        steps = result["pipeline_steps"]
        for step in ("verification", "quality_pipeline", "lint_gate", "reflection", "auto_merge"):
            assert steps[step]["status"] == "skipped"

    @patch("app.mission_runner._write_pipeline_summary")
    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner._fire_post_mission_hook")
    @patch("app.quota_handler.handle_quota_exhaustion",
           return_value=("resets 10am", "Auto-resume in 5h"))
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_quota_early_return_has_pipeline_steps(
        self, mock_usage, mock_quota, mock_hook, mock_record, mock_summary,
        tmp_path
    ):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        assert result["quota_exhausted"] is True
        assert "pipeline_steps" in result
        assert result["pipeline_steps"]["quota_check"]["status"] == "success"
        mock_summary.assert_called_once()

    @patch("app.mission_runner._write_pipeline_summary")
    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    @patch("app.mission_runner._run_mission_verification", side_effect=RuntimeError("verify crash"))
    def test_step_failure_recorded_not_swallowed(
        self, mock_verify, mock_usage, mock_quota, mock_archive,
        mock_reflect, mock_merge, mock_record, mock_summary, tmp_path
    ):
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
        )

        # Verification failed but pipeline continued
        assert result["pipeline_steps"]["verification"]["status"] == "fail"
        assert "verify crash" in result["pipeline_steps"]["verification"]["detail"]
        # Other steps still ran
        assert result["pipeline_steps"]["session_outcome"]["status"] == "success"

    @patch("app.mission_runner._write_pipeline_summary")
    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", return_value=False)
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_verification_failed_recorded_as_fail(
        self, mock_usage, mock_quota, mock_archive,
        mock_reflect, mock_merge, mock_record, mock_summary, tmp_path
    ):
        """Verification that returns passed=False must be recorded as 'fail', not 'success'."""
        from app.mission_runner import run_post_mission
        from app.mission_verifier import VerifyResult

        failed_verify = VerifyResult(passed=False, summary="no commits on branch")

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        with patch("app.mission_runner._run_mission_verification", return_value=failed_verify):
            result = run_post_mission(
                instance_dir=instance_dir,
                project_name="koan",
                project_path=str(tmp_path),
                run_num=1,
                exit_code=0,
                stdout_file="/tmp/out.json",
                stderr_file="/tmp/err.txt",
            )

        # The tracker must record verification as "fail", not "success"
        assert result["pipeline_steps"]["verification"]["status"] == "fail"
        assert "no commits on branch" in result["pipeline_steps"]["verification"]["detail"]
        # The result dict should also reflect the failure
        assert result["verification"]["passed"] is False


class TestNotifyPipelineFailures:
    """Test _notify_pipeline_failures writes warnings to outbox.md."""

    def test_no_notification_when_all_success(self, tmp_path):
        from app.mission_runner import _notify_pipeline_failures, _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("usage_update", "success")
        tracker.record("reflection", "success", "2.1s")

        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        _notify_pipeline_failures(tracker, "test mission", str(tmp_path))
        assert outbox.read_text() == ""

    def test_writes_notification_on_failure(self, tmp_path):
        from app.mission_runner import _notify_pipeline_failures, _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("usage_update", "success")
        tracker.record("reflection", "fail", "timeout after 60s")
        tracker.record("hooks", "fail", "failed: my_hook")

        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        _notify_pipeline_failures(tracker, "audit security", str(tmp_path))
        msg = outbox.read_text()
        assert "⚠️" in msg
        assert "audit security" in msg
        assert "✗ reflection (timeout after 60s)" in msg
        assert "✗ hooks (failed: my_hook)" in msg

    def test_no_mission_title_omits_prefix(self, tmp_path):
        from app.mission_runner import _notify_pipeline_failures, _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("verification", "fail", "verify crash")

        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        _notify_pipeline_failures(tracker, "", str(tmp_path))
        msg = outbox.read_text()
        assert msg.startswith("⚠️ Pipeline issues:")
        assert "✗ verification (verify crash)" in msg

    def test_notification_failure_does_not_raise(self, tmp_path):
        from app.mission_runner import _notify_pipeline_failures, _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("reflection", "fail", "boom")

        with patch("app.utils.append_to_outbox", side_effect=RuntimeError("disk full")):
            # Should not raise — fire-and-forget
            _notify_pipeline_failures(tracker, "test", str(tmp_path))

    def test_reports_timeout_and_skipped_statuses(self, tmp_path):
        from app.mission_runner import _notify_pipeline_failures, _PipelineTracker

        tracker = _PipelineTracker()
        tracker.record("reflection", "timeout", "pipeline deadline exceeded")
        tracker.record("hooks", "skipped", "non-zero exit code")

        outbox = tmp_path / "outbox.md"
        outbox.write_text("")
        _notify_pipeline_failures(tracker, "test", str(tmp_path))
        msg = outbox.read_text()
        assert "⏱ reflection (pipeline deadline exceeded)" in msg
        assert "– hooks (non-zero exit code)" in msg

    @patch("app.mission_runner._write_pipeline_summary")
    @patch("app.mission_runner._record_session_outcome")
    @patch("app.mission_runner.check_auto_merge", return_value=None)
    @patch("app.mission_runner.trigger_reflection", side_effect=RuntimeError("reflection boom"))
    @patch("app.mission_runner.archive_pending", return_value=False)
    @patch("app.quota_handler.handle_quota_exhaustion", return_value=None)
    @patch("app.mission_runner.update_usage", return_value=True)
    def test_integration_notification_on_step_failure(
        self, mock_usage, mock_quota, mock_archive,
        mock_reflect, mock_merge, mock_record, mock_summary, tmp_path
    ):
        """End-to-end: a step failure in run_post_mission triggers notification."""
        from app.mission_runner import run_post_mission

        instance_dir = str(tmp_path / "instance")
        os.makedirs(instance_dir, exist_ok=True)

        outbox = Path(instance_dir) / "outbox.md"
        outbox.write_text("")
        result = run_post_mission(
            instance_dir=instance_dir,
            project_name="koan",
            project_path=str(tmp_path),
            run_num=1,
            exit_code=0,
            stdout_file="/tmp/out.json",
            stderr_file="/tmp/err.txt",
            mission_title="test pipeline notify",
        )

        assert result["pipeline_steps"]["reflection"]["status"] == "fail"
        msg = outbox.read_text()
        assert "reflection" in msg
        assert "test pipeline notify" in msg
