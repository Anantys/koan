"""Tests for git_prep.py — pre-mission git preparation."""

import pytest
from unittest.mock import patch, call

from app.git_prep import (
    get_upstream_remote,
    prepare_project_branch,
    PrepResult,
    detect_remote_default_branch,
)


# --- get_upstream_remote ---


class TestGetUpstreamRemote:
    """Tests for remote resolution logic."""

    def test_explicit_config_wins(self):
        """submit_to_repository.remote from projects.yaml takes priority."""
        config = {"projects": {"myproj": {"submit_to_repository": {"remote": "fork-remote"}}}}
        with patch("app.git_prep.load_projects_config", return_value=config), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={"remote": "fork-remote"}):
            result = get_upstream_remote("/path/to/proj", "myproj", "/koan")
        assert result == "fork-remote"

    def test_upstream_remote_exists(self):
        """When no config, probe for 'upstream' remote."""
        with patch("app.git_prep.load_projects_config", return_value={}), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={}), \
             patch("app.git_prep.run_git", return_value=(0, "git@github.com:foo/bar.git", "")):
            result = get_upstream_remote("/path/to/proj", "myproj", "/koan")
        assert result == "upstream"

    def test_no_upstream_falls_back_to_origin(self):
        """When no config and no 'upstream' remote, fall back to 'origin'."""
        with patch("app.git_prep.load_projects_config", return_value={}), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={}), \
             patch("app.git_prep.run_git", return_value=(1, "", "fatal: No such remote")):
            result = get_upstream_remote("/path/to/proj", "myproj", "/koan")
        assert result == "origin"

    def test_config_loading_failure_falls_back(self):
        """If projects.yaml can't be loaded, probe remotes."""
        with patch("app.git_prep.load_projects_config", side_effect=Exception("broken")), \
             patch("app.git_prep.run_git", return_value=(1, "", "no such remote")):
            result = get_upstream_remote("/path/to/proj", "myproj", "/koan")
        assert result == "origin"

    def test_config_returns_none(self):
        """If load_projects_config returns None, probe remotes."""
        with patch("app.git_prep.load_projects_config", return_value=None), \
             patch("app.git_prep.run_git", return_value=(0, "url", "")):
            result = get_upstream_remote("/path/to/proj", "myproj", "/koan")
        assert result == "upstream"

    def test_submit_config_no_remote_key(self):
        """submit_to_repository exists but has no 'remote' key."""
        config = {"projects": {"myproj": {"submit_to_repository": {"repo": "owner/repo"}}}}
        with patch("app.git_prep.load_projects_config", return_value=config), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={"repo": "owner/repo"}), \
             patch("app.git_prep.run_git", return_value=(0, "url", "")):
            result = get_upstream_remote("/path/to/proj", "myproj", "/koan")
        assert result == "upstream"

    def test_empty_remote_in_config_ignored(self):
        """submit_to_repository.remote is empty string — treated as unset."""
        with patch("app.git_prep.load_projects_config", return_value={"projects": {}}), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={"remote": ""}), \
             patch("app.git_prep.run_git", return_value=(1, "", "no remote")):
            result = get_upstream_remote("/path/to/proj", "myproj", "/koan")
        assert result == "origin"


# --- detect_remote_default_branch ---


class TestDetectRemoteDefaultBranch:
    """Tests for remote default branch detection."""

    def test_local_symbolic_ref_master(self):
        """Detects 'master' from local symbolic ref."""
        with patch("app.git_prep.run_git") as mock_git:
            mock_git.return_value = (0, "refs/remotes/origin/master", "")
            result = detect_remote_default_branch("origin", "/proj")
        assert result == "master"

    def test_local_symbolic_ref_main(self):
        """Detects 'main' from local symbolic ref."""
        with patch("app.git_prep.run_git") as mock_git:
            mock_git.return_value = (0, "refs/remotes/upstream/main", "")
            result = detect_remote_default_branch("upstream", "/proj")
        assert result == "main"

    def test_local_ref_fails_falls_to_ls_remote(self):
        """When symbolic-ref fails, falls back to ls-remote."""
        with patch("app.git_prep.run_git") as mock_git:
            mock_git.side_effect = [
                (1, "", "not a symbolic ref"),  # symbolic-ref fails
                (0, "ref: refs/heads/master\tHEAD\nabc123\tHEAD", ""),  # ls-remote
            ]
            result = detect_remote_default_branch("origin", "/proj")
        assert result == "master"

    def test_both_methods_fail_returns_main(self):
        """When both methods fail, returns 'main' as fallback."""
        with patch("app.git_prep.run_git") as mock_git:
            mock_git.side_effect = [
                (1, "", "error"),  # symbolic-ref fails
                (1, "", "error"),  # ls-remote fails
            ]
            result = detect_remote_default_branch("origin", "/proj")
        assert result == "main"

    def test_empty_symbolic_ref_falls_to_ls_remote(self):
        """Empty symbolic-ref output falls back to ls-remote."""
        with patch("app.git_prep.run_git") as mock_git:
            mock_git.side_effect = [
                (0, "", ""),  # symbolic-ref returns empty
                (0, "ref: refs/heads/develop\tHEAD\nabc\tHEAD", ""),
            ]
            result = detect_remote_default_branch("origin", "/proj")
        assert result == "develop"

    def test_ls_remote_no_ref_line(self):
        """ls-remote output with no ref: line falls back to 'main'."""
        with patch("app.git_prep.run_git") as mock_git:
            mock_git.side_effect = [
                (1, "", "error"),
                (0, "abc123\tHEAD", ""),  # no ref: line
            ]
            result = detect_remote_default_branch("origin", "/proj")
        assert result == "main"


# --- PrepResult ---


class TestPrepResult:
    """Tests for the PrepResult dataclass."""

    def test_defaults(self):
        r = PrepResult()
        assert r.remote_used == "origin"
        assert r.base_branch == "main"
        assert r.stashed is False
        assert r.previous_branch == ""
        assert r.success is True
        assert r.error is None


# --- prepare_project_branch ---


def _make_run_git_side_effect(overrides=None):
    """Build a run_git mock that handles standard git commands.

    Returns (returncode, stdout, stderr) based on the first git argument.
    Overrides is a dict mapping command keys to (rc, stdout, stderr) tuples.
    """
    defaults = {
        "rev-parse": (0, "feature-branch", ""),
        "remote": (1, "", "no such remote"),  # no 'upstream' remote
        "fetch": (0, "", ""),
        "status": (0, "", ""),  # clean working tree
        "checkout": (0, "", ""),
        "merge": (0, "", ""),
        "stash": (0, "", ""),
        "reset": (0, "", ""),
    }
    if overrides:
        defaults.update(overrides)

    def side_effect(*args, **kwargs):
        cmd = args[0] if args else ""
        return defaults.get(cmd, (0, "", ""))

    return side_effect


class TestPrepareProjectBranch:
    """Tests for prepare_project_branch()."""

    def _patch_all(self, run_git_side_effect=None, config=None, auto_merge=None):
        """Return a context manager that patches all dependencies."""
        from contextlib import ExitStack

        stack = ExitStack()

        if run_git_side_effect is None:
            run_git_side_effect = _make_run_git_side_effect()

        patches = {
            "run_git": stack.enter_context(
                patch("app.git_prep.run_git", side_effect=run_git_side_effect)
            ),
            "load_config": stack.enter_context(
                patch("app.git_prep.load_projects_config", return_value=config)
            ),
            "submit": stack.enter_context(
                patch("app.git_prep.get_project_submit_to_repository", return_value={})
            ),
            "auto_merge": stack.enter_context(
                patch("app.git_prep.get_project_auto_merge", return_value=auto_merge or {"base_branch": "main"})
            ),
        }
        return stack, patches

    def test_happy_path(self):
        """Fetch + checkout + merge all succeed."""
        stack, mocks = self._patch_all()
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.base_branch == "main"
        assert result.remote_used == "origin"
        assert result.stashed is False
        assert result.previous_branch == "feature-branch"
        assert result.error is None

    def test_dirty_working_tree_stashed(self):
        """Dirty working tree is stashed."""
        side_effect = _make_run_git_side_effect({
            "status": (0, "M  file.py\n?? new.txt", ""),
        })
        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.stashed is True

    def test_fetch_failure_with_explicit_project_config(self):
        """Fetch failure with project-level base_branch config returns success=False."""
        side_effect = _make_run_git_side_effect({
            "fetch": (1, "", "Could not resolve host"),
        })
        stack, _ = self._patch_all(
            run_git_side_effect=side_effect,
            config={"projects": {"myproj": {"git_auto_merge": {"base_branch": "main"}}}},
            auto_merge={"base_branch": "main"},
        )
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is False
        assert "fetch failed" in result.error

    def test_defaults_base_branch_does_not_prevent_autodetect(self):
        """defaults.git_auto_merge.base_branch should NOT prevent auto-detection.

        Regression: when projects.yaml has defaults.git_auto_merge.base_branch=main
        but no project-level override, repos with 'master' as default branch failed
        because auto-detection was skipped (config_explicit=True from defaults).
        """
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "fetch":
                fetch_calls = [c for c in calls if c[0] == "fetch"]
                if len(fetch_calls) == 1:
                    return (1, "", "fatal: couldn't find remote ref main")
                return (0, "", "")
            if cmd == "symbolic-ref":
                return (0, "refs/remotes/upstream/master", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "no remote")

        # Simulate: defaults have base_branch=main, but project has NO override
        stack, _ = self._patch_all(
            run_git_side_effect=side_effect,
            config={
                "defaults": {"git_auto_merge": {"base_branch": "main"}},
                "projects": {},  # No project-level config for myproj
            },
            auto_merge={"base_branch": "main"},
        )
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.base_branch == "master"

    def test_fetch_failure_detects_master_branch(self):
        """Fetch 'main' fails, detects 'master' as remote default, retries successfully."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "fetch":
                # First fetch (main) fails, second (master) succeeds
                fetch_calls = [c for c in calls if c[0] == "fetch"]
                if len(fetch_calls) == 1:
                    return (1, "", "fatal: couldn't find remote ref main")
                return (0, "", "")
            if cmd == "symbolic-ref":
                return (0, "refs/remotes/origin/master", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "no remote")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.base_branch == "master"

    def test_defaults_base_branch_does_not_prevent_detection(self):
        """Regression: defaults.git_auto_merge.base_branch should NOT prevent auto-detection.

        This was the root cause of the p5-File-Copy-Recursive failure:
        projects.yaml had defaults.git_auto_merge.base_branch='main', which
        set config_explicit=True, preventing detection of 'master' as the
        actual remote default branch.
        """
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "fetch":
                # First fetch (main) fails, second (master) succeeds
                fetch_calls = [c for c in calls if c[0] == "fetch"]
                if len(fetch_calls) == 1:
                    return (1, "", "fatal: couldn't find remote ref main")
                return (0, "", "")
            if cmd == "symbolic-ref":
                return (0, "refs/remotes/origin/master", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "no remote")

        # Config has defaults.base_branch="main" but NO per-project override
        config = {
            "defaults": {"git_auto_merge": {"base_branch": "main"}},
            "projects": {"myproj": {}},
        }
        stack, _ = self._patch_all(
            run_git_side_effect=side_effect,
            config=config,
            auto_merge={"base_branch": "main"},
        )
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True, f"Expected success but got error: {result.error}"
        assert result.base_branch == "master"

    def test_fetch_failure_detection_same_branch_no_retry(self):
        """When detection returns same branch ('main'), no retry — fails immediately."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "fetch":
                return (1, "", "Could not resolve host")
            if cmd == "symbolic-ref":
                return (0, "refs/remotes/origin/main", "")
            return (1, "", "")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is False
        assert "fetch failed" in result.error
        # Only one fetch call — no retry since detected == configured
        fetch_calls = [c for c in calls if c[0] == "fetch"]
        assert len(fetch_calls) == 1

    def test_branch_doesnt_exist_locally(self):
        """Base branch doesn't exist locally — creates from remote tracking."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "HEAD", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                # First checkout (no -b) fails, second (with -b) succeeds
                if "-b" not in args:
                    return (1, "", "error: pathspec 'main' did not match")
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "no such remote")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        # Verify checkout -b was called
        checkout_b_calls = [c for c in calls if len(c) >= 2 and c[0] == "checkout" and "-b" in c]
        assert len(checkout_b_calls) == 1

    def test_detached_head(self):
        """Detached HEAD state — checkout still works."""
        side_effect = _make_run_git_side_effect({
            "rev-parse": (0, "HEAD", ""),
        })
        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.previous_branch == "HEAD"

    def test_already_on_correct_branch(self):
        """Already on base branch and up to date — merge is no-op."""
        side_effect = _make_run_git_side_effect({
            "rev-parse": (0, "main", ""),
            "merge": (0, "Already up to date.", ""),
        })
        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.previous_branch == "main"

    def test_ff_merge_fails_resets_to_remote(self):
        """ff-merge fails (local diverged) — resets to remote ref."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "main", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (1, "", "fatal: Not possible to fast-forward")
            if cmd == "reset":
                return (0, "", "")
            return (1, "", "no remote")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        # Verify reset --hard was called
        reset_calls = [c for c in calls if c[0] == "reset"]
        assert len(reset_calls) == 1
        assert "--hard" in reset_calls[0]

    def test_ff_merge_and_reset_both_fail(self):
        """Both ff-merge and reset fail — returns error."""
        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "rev-parse":
                return (0, "main", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (1, "", "cannot fast-forward")
            if cmd == "reset":
                return (1, "", "reset failed badly")
            return (1, "", "no remote")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is False
        assert "reset failed" in result.error

    def test_stash_failure_on_dirty_tree_aborts(self):
        """Stash failure on dirty tree aborts to prevent data loss."""
        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "M dirty.py", "")
            if cmd == "stash":
                return (1, "", "stash failed")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "no remote")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is False
        assert result.stashed is False
        assert "stash failed" in result.error

    def test_stash_failure_on_dirty_tree_skips_checkout(self):
        """When stash fails on dirty tree, checkout and merge are never called."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(cmd)
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "M dirty.py", "")
            if cmd == "stash":
                return (1, "", "cannot stash")
            return (0, "", "")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is False
        assert "checkout" not in calls
        assert "merge" not in calls
        assert "reset" not in calls

    def test_checkout_failure_after_stash(self):
        """Checkout fails after successful stash — reports error."""
        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "M dirty.py", "")
            if cmd == "stash":
                return (0, "", "")
            if cmd == "checkout":
                return (1, "", "checkout error")
            return (1, "", "no remote")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is False
        assert result.stashed is True
        assert "checkout failed" in result.error

    def test_custom_base_branch_from_config(self):
        """Respects base_branch from project auto-merge config."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "old-branch", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "no remote")

        stack, _ = self._patch_all(
            run_git_side_effect=side_effect,
            config={"projects": {}},
            auto_merge={"base_branch": "develop"},
        )
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.base_branch == "develop"
        # Verify fetch used 'develop'
        fetch_calls = [c for c in calls if c[0] == "fetch"]
        assert any("develop" in c for c in fetch_calls)

    def test_upstream_remote_used(self):
        """When 'upstream' remote exists, it's used for fetch."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "feature", "")
            if cmd == "remote":
                return (0, "git@github.com:upstream/repo.git", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (0, "", "")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.remote_used == "upstream"

    def test_rev_parse_failure_continues(self):
        """rev-parse failure sets empty previous_branch but prep continues."""
        side_effect = _make_run_git_side_effect({
            "rev-parse": (1, "", "fatal: not a git repo"),
        })
        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.previous_branch == ""

    def test_config_load_failure_uses_defaults(self):
        """Config loading failure uses default base_branch='main'."""
        side_effect = _make_run_git_side_effect()
        with patch("app.git_prep.run_git", side_effect=side_effect), \
             patch("app.git_prep.load_projects_config", side_effect=Exception("boom")), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={}), \
             patch("app.git_prep.get_project_auto_merge", return_value={"base_branch": "main"}):
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.base_branch == "main"
        assert result.remote_used == "origin"

    def test_explicit_remote_from_config(self):
        """submit_to_repository.remote overrides auto-detection."""
        side_effect = _make_run_git_side_effect()
        with patch("app.git_prep.run_git", side_effect=side_effect), \
             patch("app.git_prep.load_projects_config", return_value={"projects": {}}), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={"remote": "myfork"}), \
             patch("app.git_prep.get_project_auto_merge", return_value={"base_branch": "main"}):
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.remote_used == "myfork"

    def test_clean_tree_no_stash(self):
        """Clean working tree — stash is not called."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(cmd)
            if cmd == "rev-parse":
                return (0, "main", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")  # clean
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "no remote")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.stashed is False
        assert "stash" not in calls

    def test_fetch_with_correct_timeout(self):
        """Fetch uses timeout=30."""
        with patch("app.git_prep.run_git", side_effect=_make_run_git_side_effect()) as mock_git, \
             patch("app.git_prep.load_projects_config", return_value=None), \
             patch("app.git_prep.get_project_submit_to_repository", return_value={}), \
             patch("app.git_prep.get_project_auto_merge", return_value={"base_branch": "main"}):
            prepare_project_branch("/proj", "myproj", "/koan")

        # Find the fetch call
        fetch_calls = [c for c in mock_git.call_args_list if c[0][0] == "fetch"]
        assert len(fetch_calls) == 1
        assert fetch_calls[0][1].get("timeout") == 30

    def test_checkout_creates_branch_from_remote(self):
        """When checkout fails, creates branch tracking remote."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(args)
            if cmd == "rev-parse":
                return (0, "old", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                if "-b" in args:
                    return (0, "", "")
                return (1, "", "did not match")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        # Verify checkout -b main origin/main was called
        checkout_b = [c for c in calls if c[0] == "checkout" and "-b" in c]
        assert len(checkout_b) == 1
        assert "main" in checkout_b[0]
        assert "origin/main" in checkout_b[0]

    def test_both_checkouts_fail(self):
        """Both checkout attempts fail — returns error."""
        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "rev-parse":
                return (0, "old", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (0, "", "")
            if cmd == "checkout":
                return (1, "", "checkout error")
            return (1, "", "")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is False
        assert "checkout failed" in result.error

    def test_status_porcelain_failure_skips_stash(self):
        """If git status --porcelain fails, skip stash."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            calls.append(cmd)
            if cmd == "rev-parse":
                return (0, "main", "")
            if cmd == "fetch":
                return (0, "", "")
            if cmd == "status":
                return (1, "", "status error")
            if cmd == "checkout":
                return (0, "", "")
            if cmd == "merge":
                return (0, "", "")
            return (1, "", "")

        stack, _ = self._patch_all(run_git_side_effect=side_effect)
        with stack:
            result = prepare_project_branch("/proj", "myproj", "/koan")

        assert result.success is True
        assert result.stashed is False
        assert "stash" not in calls


# --- Integration: _run_iteration calls git prep ---


class TestRunIterationIntegration:
    """Verify git prep is called from run.py's _run_iteration."""

    def test_git_prep_called_in_run_iteration(self):
        """prepare_project_branch is imported and called in _run_iteration."""
        # Verify the import exists in run.py by checking the source
        import inspect
        from app import run

        source = inspect.getsource(run)
        assert "from app.git_prep import prepare_project_branch" in source
        assert "prepare_project_branch(project_path, project_name, koan_root)" in source

    def test_git_prep_failure_aborts_iteration(self):
        """Git prep failure aborts the iteration — returns False."""
        import inspect
        from app import run

        source = inspect.getsource(run)
        # Find the git prep block — it should abort on failure
        idx = source.find("prepare_project_branch(project_path")
        assert idx > 0
        # After the call, there should be a return False on failure
        following = source[idx:idx + 600]
        assert "return False" in following
        assert "abort" in following.lower()
