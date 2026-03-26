"""Tests for app.pr_babysit — PR babysitting core logic."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure KOAN_ROOT is set before importing app modules
os.environ.setdefault("KOAN_ROOT", "/tmp/test-koan")


from app.pr_babysit import (
    _extract_ci_status,
    _extract_owner_repo,
    _get_babysit_config,
    _get_tracker_entry,
    _is_in_cooldown,
    _load_tracker,
    _mission_already_queued,
    _now_iso,
    _save_tracker,
    _tracker_path,
    _update_tracker_entry,
    check_pr_health,
    get_babysit_status,
    run_babysit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "instance"
    d.mkdir()
    # Minimal missions.md
    (d / "missions.md").write_text(
        "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n## Failed\n"
    )
    return d


@pytest.fixture
def sample_pr():
    return {
        "url": "https://github.com/owner/repo/pull/42",
        "number": 42,
        "title": "Fix something",
        "headRefName": "koan/fix-something",
        "owner": "owner",
        "repo": "repo",
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "reviewDecision": None,
        "mergeStateStatus": "MERGEABLE",
        "isDraft": False,
        "statusCheckRollup": "SUCCESS",
        "commentCount": 0,
    }


# ---------------------------------------------------------------------------
# _extract_owner_repo
# ---------------------------------------------------------------------------

class TestExtractOwnerRepo:
    def test_standard_url(self):
        result = _extract_owner_repo("https://github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_url_with_git_suffix(self):
        result = _extract_owner_repo("https://github.com/owner/repo.git")
        assert result == ("owner", "repo")

    def test_url_with_trailing_slash(self):
        result = _extract_owner_repo("https://github.com/owner/repo/")
        assert result == ("owner", "repo")

    def test_invalid_url_returns_none(self):
        assert _extract_owner_repo("not-a-url") is None
        assert _extract_owner_repo("https://example.com/owner/repo") is None


# ---------------------------------------------------------------------------
# _extract_ci_status
# ---------------------------------------------------------------------------

class TestExtractCIStatus:
    def test_none_returns_none(self):
        assert _extract_ci_status(None) is None

    def test_empty_string_returns_none(self):
        assert _extract_ci_status("") is None

    def test_string_uppercased(self):
        assert _extract_ci_status("success") == "SUCCESS"
        assert _extract_ci_status("failure") == "FAILURE"

    def test_list_all_success(self):
        rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "NEUTRAL"}]
        assert _extract_ci_status(rollup) == "SUCCESS"

    def test_list_any_failure(self):
        rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]
        assert _extract_ci_status(rollup) == "FAILURE"

    def test_list_pending_when_in_progress(self):
        rollup = [{"conclusion": None, "status": "IN_PROGRESS"}, {"conclusion": "SUCCESS"}]
        assert _extract_ci_status(rollup) == "PENDING"

    def test_empty_list_returns_none(self):
        assert _extract_ci_status([]) is None


# ---------------------------------------------------------------------------
# Tracker helpers
# ---------------------------------------------------------------------------

class TestTrackerHelpers:
    def test_tracker_path(self, instance_dir):
        p = _tracker_path(instance_dir)
        assert p.name == ".babysit-tracker.json"
        assert p.parent == instance_dir

    def test_load_returns_empty_when_no_file(self, instance_dir):
        assert _load_tracker(instance_dir) == {}

    def test_save_and_load_roundtrip(self, instance_dir):
        data = {"url1": {"last_ci_status": "SUCCESS"}}
        _save_tracker(instance_dir, data)
        loaded = _load_tracker(instance_dir)
        assert loaded == data

    def test_load_handles_corrupt_json(self, instance_dir):
        _tracker_path(instance_dir).write_text("not json{{")
        assert _load_tracker(instance_dir) == {}

    def test_update_tracker_entry_creates_new(self, instance_dir):
        _update_tracker_entry(
            instance_dir,
            "https://github.com/o/r/pull/1",
            {"last_ci_status": "FAILURE"},
        )
        data = _load_tracker(instance_dir)
        assert data["https://github.com/o/r/pull/1"]["last_ci_status"] == "FAILURE"

    def test_update_tracker_entry_merges(self, instance_dir):
        _update_tracker_entry(instance_dir, "url1", {"a": 1})
        _update_tracker_entry(instance_dir, "url1", {"b": 2})
        data = _load_tracker(instance_dir)
        assert data["url1"]["a"] == 1
        assert data["url1"]["b"] == 2

    def test_get_tracker_entry_returns_empty_for_unknown(self, instance_dir):
        assert _get_tracker_entry(instance_dir, "no-such-url") == {}

    def test_get_babysit_status_empty(self, instance_dir):
        result = get_babysit_status(str(instance_dir))
        assert result == []

    def test_get_babysit_status_returns_entries(self, instance_dir):
        _save_tracker(instance_dir, {
            "url1": {"last_ci_status": "SUCCESS"},
            "url2": {"last_ci_status": "FAILURE"},
        })
        result = get_babysit_status(str(instance_dir))
        urls = [e["url"] for e in result]
        assert "url1" in urls
        assert "url2" in urls


# ---------------------------------------------------------------------------
# _is_in_cooldown
# ---------------------------------------------------------------------------

class TestIsInCooldown:
    def test_no_last_action_not_in_cooldown(self):
        assert _is_in_cooldown({}, 60) is False

    def test_recent_action_in_cooldown(self):
        recent = datetime.now(timezone.utc).isoformat()
        entry = {"last_action_at": recent}
        assert _is_in_cooldown(entry, 60) is True

    def test_old_action_not_in_cooldown(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        entry = {"last_action_at": old}
        assert _is_in_cooldown(entry, 60) is False

    def test_zero_cooldown_never_blocked(self):
        recent = datetime.now(timezone.utc).isoformat()
        entry = {"last_action_at": recent}
        assert _is_in_cooldown(entry, 0) is False


# ---------------------------------------------------------------------------
# _mission_already_queued
# ---------------------------------------------------------------------------

class TestMissionAlreadyQueued:
    def test_returns_false_when_no_missions_file(self, tmp_path):
        result = _mission_already_queued(
            tmp_path / "nonexistent.md",
            "https://github.com/o/r/pull/1",
            "fix",
        )
        assert result is False

    def test_returns_true_when_pr_url_in_pending(self, instance_dir):
        pr_url = "https://github.com/owner/repo/pull/42"
        missions_md = instance_dir / "missions.md"
        missions_md.write_text(
            "# Missions\n\n## Pending\n\n"
            f"- [project:repo] /fix {pr_url}\n\n"
            "## In Progress\n\n## Done\n\n## Failed\n"
        )
        assert _mission_already_queued(missions_md, pr_url, "fix") is True

    def test_returns_false_when_pr_url_only_in_done(self, instance_dir):
        pr_url = "https://github.com/owner/repo/pull/42"
        missions_md = instance_dir / "missions.md"
        missions_md.write_text(
            "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n\n"
            f"- [project:repo] /fix {pr_url}\n\n## Failed\n"
        )
        assert _mission_already_queued(missions_md, pr_url, "fix") is False

    def test_returns_false_for_different_pr(self, instance_dir):
        missions_md = instance_dir / "missions.md"
        missions_md.write_text(
            "# Missions\n\n## Pending\n\n"
            "- [project:repo] /fix https://github.com/owner/repo/pull/99\n\n"
            "## In Progress\n\n## Done\n\n## Failed\n"
        )
        assert _mission_already_queued(
            missions_md, "https://github.com/owner/repo/pull/42", "fix"
        ) is False


# ---------------------------------------------------------------------------
# check_pr_health
# ---------------------------------------------------------------------------

class TestCheckPRHealth:
    def test_healthy_pr_no_actions(self, sample_pr):
        actions = check_pr_health(sample_pr, {}, {})
        assert actions == []

    def test_ci_failure_triggers_fix(self, sample_pr):
        sample_pr["statusCheckRollup"] = "FAILURE"
        actions = check_pr_health(sample_pr, {}, {})
        types = [a["type"] for a in actions]
        assert "fix" in types

    def test_ci_failure_not_retriggered_when_same_failure_already_addressed(self, sample_pr):
        sample_pr["statusCheckRollup"] = "FAILURE"
        tracker_entry = {"last_ci_status": "FAILURE", "fix_attempts": 1}
        actions = check_pr_health(sample_pr, tracker_entry, {})
        types = [a["type"] for a in actions]
        assert "fix" not in types

    def test_ci_failure_cap_triggers_notify(self, sample_pr):
        sample_pr["statusCheckRollup"] = "FAILURE"
        tracker_entry = {"last_ci_status": "FAILURE", "fix_attempts": 2}
        actions = check_pr_health(sample_pr, tracker_entry, {"max_retries": 2})
        types = [a["type"] for a in actions]
        assert "notify" in types
        assert "fix" not in types

    def test_changes_requested_triggers_review(self, sample_pr):
        sample_pr["reviewDecision"] = "CHANGES_REQUESTED"
        actions = check_pr_health(sample_pr, {}, {})
        types = [a["type"] for a in actions]
        assert "review" in types

    def test_new_comments_triggers_review(self, sample_pr):
        sample_pr["commentCount"] = 3
        tracker_entry = {"last_comment_count": 1}
        actions = check_pr_health(sample_pr, tracker_entry, {})
        types = [a["type"] for a in actions]
        assert "review" in types

    def test_no_new_comments_no_action(self, sample_pr):
        sample_pr["commentCount"] = 2
        tracker_entry = {"last_comment_count": 2}
        actions = check_pr_health(sample_pr, tracker_entry, {})
        assert all(a["type"] != "review" for a in actions)

    def test_merge_conflict_triggers_rebase(self, sample_pr):
        sample_pr["mergeStateStatus"] = "CONFLICTING"
        actions = check_pr_health(sample_pr, {}, {})
        types = [a["type"] for a in actions]
        assert "rebase" in types

    def test_stale_pr_triggers_notify(self, sample_pr):
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        sample_pr["updatedAt"] = old_date
        actions = check_pr_health(sample_pr, {}, {"stale_days": 7})
        types = [a["type"] for a in actions]
        assert "notify" in types

    def test_fresh_pr_no_stale_notify(self, sample_pr):
        # Updated 1 day ago
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        sample_pr["updatedAt"] = recent
        actions = check_pr_health(sample_pr, {}, {"stale_days": 7})
        stale_notifies = [a for a in actions if a["type"] == "notify" and "activity" in a["reason"]]
        assert stale_notifies == []

    def test_draft_pr_with_no_ci_skipped(self, sample_pr):
        sample_pr["isDraft"] = True
        sample_pr["statusCheckRollup"] = None
        actions = check_pr_health(sample_pr, {}, {})
        assert actions == []


# ---------------------------------------------------------------------------
# run_babysit integration (mocked gh calls)
# ---------------------------------------------------------------------------

class TestRunBabysit:
    def test_returns_empty_when_no_prs(self, instance_dir):
        with patch("app.pr_babysit.discover_open_prs", return_value=[]):
            with patch("app.pr_babysit._get_babysit_config", return_value={}):
                result = run_babysit(
                    str(instance_dir), "/tmp", "koan/", None
                )
        assert result == ""

    def test_returns_summary_when_actions_queued(self, instance_dir, sample_pr):
        sample_pr["statusCheckRollup"] = "FAILURE"

        def fake_queue(pr, actions, missions_path, inst, cfg, notify_on_fix=True):
            return ["fix: CI check failed on PR #42"]

        with patch("app.pr_babysit.discover_open_prs", return_value=[sample_pr]):
            with patch("app.pr_babysit._get_babysit_config", return_value={}):
                with patch("app.pr_babysit._get_tracker_entry", return_value={}):
                    with patch("app.pr_babysit.queue_fix_missions", side_effect=fake_queue):
                        result = run_babysit(
                            str(instance_dir), "/tmp", "koan/", None
                        )
        assert "1 action(s)" in result

    def test_no_action_updates_tracker(self, instance_dir, sample_pr):
        # Healthy PR — no actions, just tracker update
        with patch("app.pr_babysit.discover_open_prs", return_value=[sample_pr]):
            with patch("app.pr_babysit._get_babysit_config", return_value={}):
                with patch("app.pr_babysit._get_tracker_entry", return_value={}):
                    with patch("app.pr_babysit.check_pr_health", return_value=[]):
                        with patch("app.pr_babysit._update_tracker_entry") as mock_update:
                            run_babysit(str(instance_dir), "/tmp", "koan/", None)
                            mock_update.assert_called()


# ---------------------------------------------------------------------------
# queue_fix_missions
# ---------------------------------------------------------------------------

class TestQueueFixMissions:
    def test_queues_fix_mission(self, instance_dir, sample_pr):
        sample_pr["statusCheckRollup"] = "FAILURE"
        actions = [{"type": "fix", "reason": "CI failed", "context": "build: error"}]

        with patch("app.pr_babysit._resolve_project_for_pr", return_value="myrepo"):
            with patch("app.utils.append_to_outbox"):
                from app.pr_babysit import queue_fix_missions
                queued = queue_fix_missions(
                    sample_pr,
                    actions,
                    instance_dir / "missions.md",
                    instance_dir,
                    {},
                    notify_on_fix=False,
                )

        assert len(queued) == 1
        assert "fix" in queued[0]

        # Verify mission written to missions.md
        content = (instance_dir / "missions.md").read_text()
        assert "/fix" in content
        assert sample_pr["url"] in content

    def test_skips_when_in_cooldown(self, instance_dir, sample_pr):
        actions = [{"type": "fix", "reason": "CI failed", "context": ""}]
        recent = datetime.now(timezone.utc).isoformat()
        tracker_entry = {"last_action_at": recent, "last_action": "fix"}

        _update_tracker_entry(
            instance_dir, sample_pr["url"], tracker_entry
        )

        with patch("app.pr_babysit._resolve_project_for_pr", return_value="repo"):
            from app.pr_babysit import queue_fix_missions
            queued = queue_fix_missions(
                sample_pr,
                actions,
                instance_dir / "missions.md",
                instance_dir,
                {"cooldown_minutes": 60},
                notify_on_fix=False,
            )

        assert queued == []

    def test_skips_when_mission_already_queued(self, instance_dir, sample_pr):
        pr_url = sample_pr["url"]
        # Pre-populate missions with this PR
        (instance_dir / "missions.md").write_text(
            f"# Missions\n\n## Pending\n\n- [project:repo] /fix {pr_url}\n\n"
            "## In Progress\n\n## Done\n\n## Failed\n"
        )
        actions = [{"type": "fix", "reason": "CI failed", "context": ""}]

        with patch("app.pr_babysit._resolve_project_for_pr", return_value="repo"):
            from app.pr_babysit import queue_fix_missions
            queued = queue_fix_missions(
                sample_pr,
                actions,
                instance_dir / "missions.md",
                instance_dir,
                {},
                notify_on_fix=False,
            )

        assert queued == []

    def test_notify_action_writes_to_outbox(self, instance_dir, sample_pr):
        actions = [{"type": "notify", "reason": "PR stale", "context": ""}]
        outbox_path = instance_dir / "outbox.md"
        outbox_path.write_text("")

        with patch("app.pr_babysit._resolve_project_for_pr", return_value="repo"):
            with patch("app.utils.append_to_outbox") as mock_outbox:
                from app.pr_babysit import queue_fix_missions
                queued = queue_fix_missions(
                    sample_pr,
                    actions,
                    instance_dir / "missions.md",
                    instance_dir,
                    {},
                    notify_on_fix=False,
                )

        mock_outbox.assert_called_once()
        call_args = mock_outbox.call_args[0]
        assert "PR stale" in call_args[1]
        assert "notify: PR stale" in queued
