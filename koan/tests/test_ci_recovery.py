"""Tests for app.ci_recovery — CI failure recovery state machine."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "instance"
    d.mkdir()
    (d / "missions.md").write_text("## Pending\n\n## In Progress\n\n## Done\n")
    return d


def _make_config(auto=True, retries=2, cooldown_minutes=30):
    return {
        "projects": {
            "myapp": {
                "path": "/tmp/myapp",
                "ci_recovery": {
                    "auto": auto,
                    "retries": retries,
                    "cooldown_minutes": cooldown_minutes,
                },
            }
        }
    }


PR_URL = "https://github.com/owner/repo/pull/42"


class TestHandleCIFailure:
    def test_skipped_disabled_when_auto_false(self, instance_dir):
        from app.ci_recovery import handle_ci_failure

        config = _make_config(auto=False)
        result = handle_ci_failure(
            instance_dir=instance_dir,
            pr_url=PR_URL,
            pr_number="42",
            project_name="myapp",
            repo="owner/repo",
            config=config,
        )
        assert result == "skipped_disabled"

    def test_dispatched_on_first_attempt(self, instance_dir):
        from app.ci_recovery import handle_ci_failure

        config = _make_config()
        with patch("app.ci_recovery._fetch_logs", return_value="error: test failed"), \
             patch("app.ci_recovery._dispatch_mission") as mock_dispatch:
            result = handle_ci_failure(
                instance_dir=instance_dir,
                pr_url=PR_URL,
                pr_number="42",
                project_name="myapp",
                repo="owner/repo",
                config=config,
            )
        assert result == "dispatched"
        mock_dispatch.assert_called_once()

    def test_escalated_when_max_retries_reached(self, instance_dir):
        from app.ci_recovery import handle_ci_failure
        from app.check_tracker import set_ci_status

        # Set attempt_count to max (2)
        set_ci_status(instance_dir, PR_URL, "fix_dispatched", 2)

        config = _make_config(retries=2)
        with patch("app.ci_recovery._write_escalation") as mock_esc:
            result = handle_ci_failure(
                instance_dir=instance_dir,
                pr_url=PR_URL,
                pr_number="42",
                project_name="myapp",
                repo="owner/repo",
                config=config,
            )
        assert result == "escalated"
        mock_esc.assert_called_once()

    def test_skipped_cooldown_when_recent_attempt(self, instance_dir):
        from app.ci_recovery import handle_ci_failure
        from app.check_tracker import set_ci_status

        # Set a recent attempt (1 minute ago — within 30 min cooldown)
        set_ci_status(instance_dir, PR_URL, "fix_dispatched", 1)
        # The last_attempt_at is set to now by set_ci_status, so cooldown is fresh

        config = _make_config(retries=2, cooldown_minutes=30)
        result = handle_ci_failure(
            instance_dir=instance_dir,
            pr_url=PR_URL,
            pr_number="42",
            project_name="myapp",
            repo="owner/repo",
            config=config,
        )
        assert result == "skipped_cooldown"

    def test_skipped_when_mission_already_queued(self, instance_dir):
        from app.ci_recovery import handle_ci_failure

        # Pre-populate missions with an existing fix mission
        missions_path = instance_dir / "missions.md"
        missions_path.write_text(
            "## Pending\n\n"
            "- [project:myapp] Fix CI failure on PR #42 — ...\n\n"
            "## In Progress\n\n## Done\n"
        )

        config = _make_config()
        result = handle_ci_failure(
            instance_dir=instance_dir,
            pr_url=PR_URL,
            pr_number="42",
            project_name="myapp",
            repo="owner/repo",
            config=config,
        )
        assert result == "skipped_cooldown"

    def test_dispatched_increments_attempt_count(self, instance_dir):
        from app.ci_recovery import handle_ci_failure
        from app.check_tracker import get_ci_attempt_count

        config = _make_config()
        with patch("app.ci_recovery._fetch_logs", return_value="error"), \
             patch("app.ci_recovery._dispatch_mission"):
            handle_ci_failure(
                instance_dir=instance_dir,
                pr_url=PR_URL,
                pr_number="42",
                project_name="myapp",
                repo="owner/repo",
                config=config,
            )
        assert get_ci_attempt_count(instance_dir, PR_URL) == 1
