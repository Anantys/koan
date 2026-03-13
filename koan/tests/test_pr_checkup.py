"""Tests for pr_checkup module."""

import json
import os

import pytest

os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")

from app.pr_checkup import (
    _get_all_github_repos,
    _has_ci_failure,
    _has_conflicts,
    _is_mission_already_queued,
    run_checkup,
)


# ---------------------------------------------------------------------------
# _get_all_github_repos
# ---------------------------------------------------------------------------


class TestGetAllGithubRepos:
    def test_no_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: None,
        )
        assert _get_all_github_repos(str(tmp_path)) == []

    def test_projects_with_github_url(self, monkeypatch):
        config = {
            "projects": {
                "myapp": {
                    "path": "/code/myapp",
                    "github_url": "https://github.com/owner/myapp",
                },
                "lib": {
                    "path": "/code/lib",
                    "github_url": "https://github.com/org/lib.git",
                },
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )
        repos = _get_all_github_repos("/tmp")
        assert len(repos) == 2
        assert {"name": "myapp", "repo": "owner/myapp"} in repos
        assert {"name": "lib", "repo": "org/lib"} in repos

    def test_project_without_github_url_skipped(self, monkeypatch):
        config = {
            "projects": {
                "local": {"path": "/code/local"},
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )
        assert _get_all_github_repos("/tmp") == []

    def test_null_project_skipped(self, monkeypatch):
        config = {"projects": {"empty": None}}
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )
        assert _get_all_github_repos("/tmp") == []


# ---------------------------------------------------------------------------
# _has_ci_failure
# ---------------------------------------------------------------------------


class TestHasCiFailure:
    def test_no_rollup(self):
        assert _has_ci_failure({}) is False
        assert _has_ci_failure({"statusCheckRollup": None}) is False
        assert _has_ci_failure({"statusCheckRollup": []}) is False

    def test_all_success(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
        ]}
        assert _has_ci_failure(pr) is False

    def test_failure_detected(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "FAILURE", "status": "COMPLETED"},
        ]}
        assert _has_ci_failure(pr) is True

    def test_error_detected(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "ERROR", "status": "COMPLETED"},
        ]}
        assert _has_ci_failure(pr) is True

    def test_timed_out_detected(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "TIMED_OUT", "status": "COMPLETED"},
        ]}
        assert _has_ci_failure(pr) is True

    def test_in_progress_not_failure(self):
        pr = {"statusCheckRollup": [
            {"conclusion": None, "status": "IN_PROGRESS"},
        ]}
        assert _has_ci_failure(pr) is False

    def test_action_required_detected(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "ACTION_REQUIRED", "status": "COMPLETED"},
        ]}
        assert _has_ci_failure(pr) is True


# ---------------------------------------------------------------------------
# _has_conflicts
# ---------------------------------------------------------------------------


class TestHasConflicts:
    def test_conflicting(self):
        assert _has_conflicts({"mergeable": "CONFLICTING"}) is True

    def test_mergeable(self):
        assert _has_conflicts({"mergeable": "MERGEABLE"}) is False

    def test_unknown(self):
        assert _has_conflicts({"mergeable": "UNKNOWN"}) is False
        assert _has_conflicts({}) is False


# ---------------------------------------------------------------------------
# _is_mission_already_queued
# ---------------------------------------------------------------------------


class TestIsMissionAlreadyQueued:
    def test_empty_pending(self):
        assert _is_mission_already_queued(
            "", "https://github.com/o/r/pull/1", "rebase",
        ) is False

    def test_matching_rebase(self):
        pending = (
            "- [project:myapp] /rebase "
            "https://github.com/owner/myapp/pull/42"
        )
        url = "https://github.com/owner/myapp/pull/42"
        assert _is_mission_already_queued(pending, url, "rebase") is True

    def test_different_pr_not_matched(self):
        pending = (
            "- [project:myapp] /rebase "
            "https://github.com/owner/myapp/pull/42"
        )
        url = "https://github.com/owner/myapp/pull/99"
        assert _is_mission_already_queued(pending, url, "rebase") is False

    def test_different_action_not_matched(self):
        pending = (
            "- [project:myapp] /check "
            "https://github.com/owner/myapp/pull/42"
        )
        url = "https://github.com/owner/myapp/pull/42"
        assert _is_mission_already_queued(pending, url, "rebase") is False

    def test_case_insensitive(self):
        pending = (
            "- [project:myapp] /REBASE "
            "https://GitHub.com/Owner/MyApp/pull/42"
        )
        url = "https://github.com/owner/myapp/pull/42"
        assert _is_mission_already_queued(pending, url, "rebase") is True


# ---------------------------------------------------------------------------
# run_checkup — integration tests
# ---------------------------------------------------------------------------


class TestRunCheckup:
    @pytest.fixture
    def instance_dir(self, tmp_path):
        d = tmp_path / "instance"
        d.mkdir()
        missions = d / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
        )
        return d

    def test_no_github_username(self, instance_dir, tmp_path, monkeypatch):
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "")
        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert not ok
        assert "username" in msg.lower()

    def test_no_repos_configured(self, instance_dir, tmp_path, monkeypatch):
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: None,
        )
        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert "nothing to check" in msg.lower()

    def test_all_healthy(self, instance_dir, tmp_path, monkeypatch):
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        config = {
            "projects": {
                "myapp": {
                    "path": "/code/myapp",
                    "github_url": "https://github.com/owner/myapp",
                }
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )

        healthy_pr = {
            "number": 1,
            "title": "feat: good stuff",
            "url": "https://github.com/owner/myapp/pull/1",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [
                {"conclusion": "SUCCESS", "status": "COMPLETED"},
            ],
            "updatedAt": "2026-03-13T10:00:00Z",
            "isDraft": False,
        }
        monkeypatch.setattr(
            "app.pr_checkup.run_gh",
            lambda *a, **kw: json.dumps([healthy_pr]),
        )

        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert "all healthy" in msg.lower()

    def test_conflict_queues_rebase(self, instance_dir, tmp_path, monkeypatch):
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        config = {
            "projects": {
                "myapp": {
                    "path": "/code/myapp",
                    "github_url": "https://github.com/owner/myapp",
                }
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )

        conflicting_pr = {
            "number": 5,
            "title": "fix: broken thing",
            "url": "https://github.com/owner/myapp/pull/5",
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [],
            "updatedAt": "2026-03-13T11:00:00Z",
            "isDraft": False,
        }
        monkeypatch.setattr(
            "app.pr_checkup.run_gh",
            lambda *a, **kw: json.dumps([conflicting_pr]),
        )

        queued = []
        monkeypatch.setattr(
            "app.pr_checkup._queue_mission",
            lambda path, proj, text: queued.append((proj, text)),
        )

        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert len(queued) == 1
        assert queued[0][0] == "myapp"
        assert "/rebase" in queued[0][1]

    def test_ci_failure_queues_check(self, instance_dir, tmp_path, monkeypatch):
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        config = {
            "projects": {
                "lib": {
                    "path": "/code/lib",
                    "github_url": "https://github.com/org/lib",
                }
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )

        failing_pr = {
            "number": 10,
            "title": "feat: new feature",
            "url": "https://github.com/org/lib/pull/10",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [
                {"conclusion": "FAILURE", "status": "COMPLETED"},
            ],
            "updatedAt": "2026-03-13T12:00:00Z",
            "isDraft": False,
        }
        monkeypatch.setattr(
            "app.pr_checkup.run_gh",
            lambda *a, **kw: json.dumps([failing_pr]),
        )

        queued = []
        monkeypatch.setattr(
            "app.pr_checkup._queue_mission",
            lambda path, proj, text: queued.append((proj, text)),
        )

        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert len(queued) == 1
        assert queued[0][0] == "lib"
        assert "/check" in queued[0][1]

    def test_dedup_skips_existing_rebase(
        self, instance_dir, tmp_path, monkeypatch,
    ):
        """If a /rebase mission is already pending, don't queue another."""
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        config = {
            "projects": {
                "myapp": {
                    "path": "/code/myapp",
                    "github_url": "https://github.com/owner/myapp",
                }
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )

        # Write an existing rebase mission in pending
        missions = instance_dir / "missions.md"
        missions.write_text(
            "# Missions\n\n## Pending\n"
            "- [project:myapp] /rebase "
            "https://github.com/owner/myapp/pull/5\n"
            "\n## In Progress\n\n## Done\n"
        )

        conflicting_pr = {
            "number": 5,
            "title": "fix: broken thing",
            "url": "https://github.com/owner/myapp/pull/5",
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [],
            "updatedAt": "2026-03-13T11:00:00Z",
            "isDraft": False,
        }
        monkeypatch.setattr(
            "app.pr_checkup.run_gh",
            lambda *a, **kw: json.dumps([conflicting_pr]),
        )

        queued = []
        monkeypatch.setattr(
            "app.pr_checkup._queue_mission",
            lambda path, proj, text: queued.append((proj, text)),
        )

        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert len(queued) == 0  # dedup should prevent queue
        assert "already queued" in msg.lower()

    def test_unchanged_pr_skipped(
        self, instance_dir, tmp_path, monkeypatch,
    ):
        """PRs that haven't changed since last check should be skipped."""
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        config = {
            "projects": {
                "myapp": {
                    "path": "/code/myapp",
                    "github_url": "https://github.com/owner/myapp",
                }
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )

        pr_data = {
            "number": 1,
            "title": "feat: stuff",
            "url": "https://github.com/owner/myapp/pull/1",
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [],
            "updatedAt": "2026-03-13T10:00:00Z",
            "isDraft": False,
        }
        monkeypatch.setattr(
            "app.pr_checkup.run_gh",
            lambda *a, **kw: json.dumps([pr_data]),
        )

        # Pre-mark as checked with the same timestamp
        from app.check_tracker import mark_checked
        mark_checked(
            instance_dir,
            "https://github.com/owner/myapp/pull/1",
            "2026-03-13T10:00:00Z",
        )

        queued = []
        monkeypatch.setattr(
            "app.pr_checkup._queue_mission",
            lambda path, proj, text: queued.append((proj, text)),
        )

        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert len(queued) == 0  # should skip unchanged PR

    def test_both_conflicts_and_ci_failure(
        self, instance_dir, tmp_path, monkeypatch,
    ):
        """PR with both conflicts and CI failure queues both missions."""
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        config = {
            "projects": {
                "myapp": {
                    "path": "/code/myapp",
                    "github_url": "https://github.com/owner/myapp",
                }
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )

        bad_pr = {
            "number": 7,
            "title": "feat: disaster",
            "url": "https://github.com/owner/myapp/pull/7",
            "mergeable": "CONFLICTING",
            "statusCheckRollup": [
                {"conclusion": "FAILURE", "status": "COMPLETED"},
            ],
            "updatedAt": "2026-03-13T13:00:00Z",
            "isDraft": False,
        }
        monkeypatch.setattr(
            "app.pr_checkup.run_gh",
            lambda *a, **kw: json.dumps([bad_pr]),
        )

        queued = []
        monkeypatch.setattr(
            "app.pr_checkup._queue_mission",
            lambda path, proj, text: queued.append((proj, text)),
        )

        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert len(queued) == 2
        actions = [q[1] for q in queued]
        assert any("/rebase" in a for a in actions)
        assert any("/check" in a for a in actions)

    def test_multiple_repos(self, instance_dir, tmp_path, monkeypatch):
        """Checkup scans across multiple repos."""
        monkeypatch.setattr("app.pr_checkup.get_gh_username", lambda: "bot")
        config = {
            "projects": {
                "app1": {
                    "path": "/code/app1",
                    "github_url": "https://github.com/o/app1",
                },
                "app2": {
                    "path": "/code/app2",
                    "github_url": "https://github.com/o/app2",
                },
            }
        }
        monkeypatch.setattr(
            "app.pr_checkup.load_projects_config", lambda _: config,
        )

        def mock_gh(*args, **kwargs):
            # Return different PRs based on which repo is queried
            for i, arg in enumerate(args):
                if arg == "--repo":
                    repo = args[i + 1]
                    if "app1" in repo:
                        return json.dumps([{
                            "number": 1, "title": "pr1",
                            "url": f"https://github.com/{repo}/pull/1",
                            "mergeable": "MERGEABLE",
                            "statusCheckRollup": [],
                            "updatedAt": "2026-03-13T10:00:00Z",
                        }])
                    elif "app2" in repo:
                        return json.dumps([{
                            "number": 2, "title": "pr2",
                            "url": f"https://github.com/{repo}/pull/2",
                            "mergeable": "CONFLICTING",
                            "statusCheckRollup": [],
                            "updatedAt": "2026-03-13T10:00:00Z",
                        }])
            return "[]"

        monkeypatch.setattr("app.pr_checkup.run_gh", mock_gh)

        queued = []
        monkeypatch.setattr(
            "app.pr_checkup._queue_mission",
            lambda path, proj, text: queued.append((proj, text)),
        )

        ok, msg = run_checkup(
            str(tmp_path), str(instance_dir), notify_fn=lambda _: None,
        )
        assert ok
        assert "2 open PR(s)" in msg
        assert len(queued) == 1  # only app2 has conflicts
        assert queued[0][0] == "app2"
