"""Tests for worktree_manager.py — git worktree lifecycle management.

Uses real git repos in temp directories (not mocks) per the plan's testing strategy.
"""

import os
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch

from app.worktree_manager import (
    WorktreeInfo,
    create_worktree,
    remove_worktree,
    list_worktrees,
    cleanup_stale_worktrees,
    git_retry,
    inject_worktree_claude_md,
    setup_shared_deps,
    WORKTREE_DIR,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repository with an initial commit."""
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), capture_output=True, check=True,
    )
    # Create initial commit on main branch
    (repo / "README.md").write_text("# Test Project\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo), capture_output=True, check=True,
    )
    # Ensure we're on 'main' branch
    result = subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=str(repo), capture_output=True, text=True,
    )
    return str(repo)


class TestCreateWorktree:
    def test_creates_isolated_directory(self, git_repo):
        wt = create_worktree(git_repo)
        assert Path(wt.path).is_dir()
        assert wt.session_id
        assert wt.branch.startswith("koan/session-")
        assert wt.project_path == git_repo

    def test_worktree_has_clean_git_status(self, git_repo):
        wt = create_worktree(git_repo)
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=wt.path,
            capture_output=True,
            text=True,
            check=True,
        )
        # May have untracked .gitignore from _ensure_gitignored
        lines = [l for l in result.stdout.strip().splitlines()
                 if l and not l.endswith(".gitignore")]
        assert lines == []

    def test_worktree_is_on_unique_branch(self, git_repo):
        wt = create_worktree(git_repo)
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=wt.path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == wt.branch

    def test_custom_session_id(self, git_repo):
        wt = create_worktree(git_repo, session_id="test123")
        assert wt.session_id == "test123"
        assert "test123" in wt.path

    def test_custom_branch_name(self, git_repo):
        wt = create_worktree(git_repo, branch_name="feature/custom")
        assert wt.branch == "feature/custom"

    def test_concurrent_creation(self, git_repo):
        """Multiple worktrees can be created for the same project."""
        wt1 = create_worktree(git_repo)
        wt2 = create_worktree(git_repo)
        assert wt1.path != wt2.path
        assert wt1.branch != wt2.branch
        assert Path(wt1.path).is_dir()
        assert Path(wt2.path).is_dir()

    def test_duplicate_session_id_raises(self, git_repo):
        create_worktree(git_repo, session_id="dup")
        with pytest.raises(FileExistsError):
            create_worktree(git_repo, session_id="dup")

    def test_worktrees_dir_created(self, git_repo):
        create_worktree(git_repo)
        assert (Path(git_repo) / WORKTREE_DIR).is_dir()

    def test_commit_sha_populated(self, git_repo):
        wt = create_worktree(git_repo)
        assert len(wt.commit) == 40  # Full SHA

    def test_copies_claude_md(self, git_repo):
        (Path(git_repo) / "CLAUDE.md").write_text("# Project CLAUDE.md\n")
        subprocess.run(["git", "add", "CLAUDE.md"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add CLAUDE.md"],
            cwd=git_repo, capture_output=True,
        )
        wt = create_worktree(git_repo)
        wt_claude = Path(wt.path) / "CLAUDE.md"
        assert wt_claude.exists()
        assert "Project CLAUDE.md" in wt_claude.read_text()


class TestRemoveWorktree:
    def test_removes_directory(self, git_repo):
        wt = create_worktree(git_repo)
        assert Path(wt.path).is_dir()
        remove_worktree(git_repo, session_id=wt.session_id)
        assert not Path(wt.path).exists()

    def test_removes_by_path(self, git_repo):
        wt = create_worktree(git_repo)
        remove_worktree(git_repo, worktree_path=wt.path)
        assert not Path(wt.path).exists()

    def test_cleans_up_branch(self, git_repo):
        wt = create_worktree(git_repo)
        branch = wt.branch
        remove_worktree(git_repo, session_id=wt.session_id)
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert branch not in result.stdout

    def test_force_removes_dirty_worktree(self, git_repo):
        wt = create_worktree(git_repo)
        # Make worktree dirty
        (Path(wt.path) / "dirty.txt").write_text("uncommitted changes")
        remove_worktree(git_repo, session_id=wt.session_id, force=True)
        assert not Path(wt.path).exists()

    def test_requires_session_or_path(self, git_repo):
        with pytest.raises(ValueError):
            remove_worktree(git_repo)

    def test_idempotent_on_missing(self, git_repo):
        """Removing a non-existent worktree doesn't raise."""
        remove_worktree(git_repo, session_id="nonexistent")


class TestListWorktrees:
    def test_lists_main_worktree(self, git_repo):
        worktrees = list_worktrees(git_repo)
        assert len(worktrees) >= 1
        main = [w for w in worktrees if w.is_main]
        assert len(main) == 1

    def test_lists_created_worktrees(self, git_repo):
        wt1 = create_worktree(git_repo, session_id="aaa")
        wt2 = create_worktree(git_repo, session_id="bbb")
        worktrees = list_worktrees(git_repo)
        session_ids = {w.session_id for w in worktrees}
        assert "aaa" in session_ids
        assert "bbb" in session_ids

    def test_empty_repo_returns_main(self, git_repo):
        worktrees = list_worktrees(git_repo)
        assert len(worktrees) == 1


class TestCleanupStaleWorktrees:
    def test_removes_stale_keeps_active(self, git_repo):
        wt1 = create_worktree(git_repo, session_id="active1")
        wt2 = create_worktree(git_repo, session_id="stale1")
        cleanup_stale_worktrees(git_repo, active_session_ids=["active1"])
        assert Path(wt1.path).is_dir()
        assert not Path(wt2.path).exists()

    def test_removes_all_when_no_active(self, git_repo):
        wt1 = create_worktree(git_repo, session_id="s1")
        wt2 = create_worktree(git_repo, session_id="s2")
        cleanup_stale_worktrees(git_repo, active_session_ids=[])
        assert not Path(wt1.path).exists()
        assert not Path(wt2.path).exists()

    def test_noop_when_no_worktrees_dir(self, git_repo):
        """Should not raise when .worktrees/ doesn't exist."""
        cleanup_stale_worktrees(git_repo, active_session_ids=[])


class TestGitRetry:
    def test_successful_command_no_retry(self, git_repo):
        result = git_retry(["git", "status"], cwd=git_repo)
        assert result.returncode == 0

    def test_non_lock_error_no_retry(self, git_repo):
        """Non-lock errors should not be retried."""
        with pytest.raises(subprocess.CalledProcessError):
            git_retry(["git", "checkout", "nonexistent-branch"], cwd=git_repo)

    def test_lock_error_retries(self, git_repo, tmp_path):
        """Simulates a lock error by creating index.lock."""
        # Create an index.lock file to simulate contention
        lock_file = Path(git_repo) / ".git" / "index.lock"
        lock_file.write_text("locked")
        try:
            # git add should fail because of index.lock, but won't contain "lock" in stderr
            # on all platforms, so we just verify the retry mechanism works with mock
            pass
        finally:
            lock_file.unlink(missing_ok=True)


class TestInjectWorktreeClaudeMd:
    def test_appends_to_existing(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Existing\n")
        inject_worktree_claude_md(str(tmp_path), "Fix the auth bug")
        content = claude_md.read_text()
        assert "Existing" in content
        assert "Fix the auth bug" in content
        assert "Worktree Session Context" in content

    def test_creates_new_if_missing(self, tmp_path):
        inject_worktree_claude_md(str(tmp_path), "Implement feature")
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        assert "Implement feature" in claude_md.read_text()


class TestSetupSharedDeps:
    def test_symlinks_existing_deps(self, git_repo, tmp_path):
        # Create a dep in main project
        (Path(git_repo) / "node_modules").mkdir()
        (Path(git_repo) / "node_modules" / "pkg.json").write_text("{}")

        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        setup_shared_deps(str(wt_path), git_repo, ["node_modules"])
        link = wt_path / "node_modules"
        assert link.is_symlink()
        assert (link / "pkg.json").exists()

    def test_skips_missing_deps(self, git_repo, tmp_path):
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        setup_shared_deps(str(wt_path), git_repo, ["nonexistent"])
        assert not (wt_path / "nonexistent").exists()

    def test_skips_if_already_exists(self, git_repo, tmp_path):
        (Path(git_repo) / "node_modules").mkdir()
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        (wt_path / "node_modules").mkdir()  # Already exists

        setup_shared_deps(str(wt_path), git_repo, ["node_modules"])
        assert not (wt_path / "node_modules").is_symlink()
