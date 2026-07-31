"""Tests for durable, read-back-verified Jira plan publishing."""

import json
from unittest.mock import patch

import pytest

from app.jira_notifications import JiraCommentFetchError
from app.jira_plan_publish import (
    _MAX_PUBLISH_SESSIONS,
    _STAGE_MAX_AGE_SECONDS,
    _footer_for,
    load_staged_plan,
    publish_staged_plan,
    stage_path_for,
    stage_plan,
)

URL = "https://org.atlassian.net/browse/PROJ-9"


def _rendered(body):
    """The comment text the publisher is expected to send for ``body``."""
    return f"{body}\n\n{_footer_for(body)}"


def test_publish_creates_then_verifies_and_clears_stage(tmp_path):
    body = "Koan plan update\n\nPhase 1: do the work"
    stage_plan(URL, body, str(tmp_path))
    comments = []

    def add(_key, rendered):
        comments.append({"id": "42", "body": rendered})
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add) as add_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "42"
    assert add_comment.call_count == 1
    assert load_staged_plan(URL, str(tmp_path)) is None


def test_publish_false_without_readback_retries_and_keeps_stage(tmp_path):
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", return_value=[]),
        patch("app.jira_plan_publish.jira_add_comment", return_value=False) as add_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "verification_failed"
    assert add_comment.call_count == 3
    assert load_staged_plan(URL, str(tmp_path)) == "plan"


def test_lookup_failure_never_blind_posts_a_duplicate(tmp_path):
    """A broken read path must not look like "no plan comment yet".

    Jira's comment listing can fail while posting still works; creating a
    comment on that signal is how one plan becomes three.
    """
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch(
            "app.jira_plan_publish.jira_list_comments_checked",
            side_effect=JiraCommentFetchError("boom"),
        ),
        patch("app.jira_plan_publish.jira_add_comment", return_value=True) as add_comment,
        patch("app.jira_plan_publish.jira_edit_comment", return_value=True) as edit_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "verification_failed"
    add_comment.assert_not_called()
    edit_comment.assert_not_called()
    assert load_staged_plan(URL, str(tmp_path)) == "plan"


def test_transient_lookup_failure_then_success_posts_once(tmp_path):
    stage_plan(URL, "plan", str(tmp_path))
    comments = []
    calls = {"n": 0}

    def listing(_key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JiraCommentFetchError("transient")
        return comments

    def add(_key, rendered):
        comments.append({"id": "7", "body": rendered})
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=listing),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add) as add_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "7"
    assert add_comment.call_count == 1


def test_existing_current_plan_is_updated_not_appended(tmp_path):
    body = "new plan"
    stage_plan(URL, body, str(tmp_path))
    existing = {"id": "11", "body": _rendered("stale plan")}

    def edit(_key, _comment_id, rendered):
        existing["body"] = rendered
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: [existing]),
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit) as edit_comment,
        patch("app.jira_plan_publish.jira_add_comment") as add_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "11"
    assert existing["body"].endswith(_footer_for(body))
    edit_comment.assert_called_once()
    add_comment.assert_not_called()


def test_already_published_plan_is_not_rewritten(tmp_path):
    """A resume after a lost success verifies instead of posting again."""
    body = "plan"
    stage_plan(URL, body, str(tmp_path))
    existing = {"id": "5", "body": _rendered(body)}

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: [existing]),
        patch("app.jira_plan_publish.jira_add_comment") as add_comment,
        patch("app.jira_plan_publish.jira_edit_comment") as edit_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "5"
    add_comment.assert_not_called()
    edit_comment.assert_not_called()
    assert load_staged_plan(URL, str(tmp_path)) is None


def test_footer_quoted_mid_body_is_not_mistaken_for_the_plan_comment(tmp_path):
    """Only a trailing footer identifies the plan comment."""
    body = "plan"
    stage_plan(URL, body, str(tmp_path))
    unrelated = {"id": "3", "body": f"I think {_footer_for(body)} is the marker, right?"}
    posted = []

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: [unrelated] + posted),
        patch(
            "app.jira_plan_publish.jira_add_comment",
            side_effect=lambda _k, r: posted.append({"id": "9", "body": r}) or True,
        ),
        patch("app.jira_plan_publish.jira_edit_comment") as edit_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "9"
    edit_comment.assert_not_called()


def test_repeated_failed_runs_eventually_abandon_the_stage(tmp_path):
    """A permanently undeliverable plan must not wedge the issue forever."""
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", return_value=[]),
        patch("app.jira_plan_publish.jira_add_comment", return_value=False),
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        for _ in range(_MAX_PUBLISH_SESSIONS - 1):
            ok, reason = publish_staged_plan(URL, str(tmp_path))
            assert ok is False
            assert reason == "verification_failed"
            assert load_staged_plan(URL, str(tmp_path)) == "plan"

        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == f"abandoned_after_{_MAX_PUBLISH_SESSIONS}_failed_runs"
    assert load_staged_plan(URL, str(tmp_path)) is None


def test_expired_stage_is_discarded(tmp_path):
    stage_plan(URL, "old plan", str(tmp_path))
    path = stage_path_for(URL, str(tmp_path))
    payload = json.loads(path.read_text())
    payload["staged_at"] -= _STAGE_MAX_AGE_SECONDS + 1
    path.write_text(json.dumps(payload))

    assert load_staged_plan(URL, str(tmp_path)) is None
    assert not path.exists()


def test_publish_without_a_stage_is_a_no_op(tmp_path):
    with patch("app.jira_plan_publish.jira_add_comment") as add_comment:
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "no_staged_plan"
    add_comment.assert_not_called()


def test_staged_plan_round_trips_atomically(tmp_path):
    stage_plan(URL, "plan body", str(tmp_path))
    stored = list((tmp_path / "pending-jira-plan-publishes").glob("*.json"))

    assert len(stored) == 1
    assert json.loads(stored[0].read_text())["issue_url"] == URL
    assert load_staged_plan(URL, str(tmp_path)) == "plan body"


@pytest.mark.parametrize("body", ["plan", "plan\n\nwith paragraphs", "x" * 5000])
def test_footer_survives_the_adf_round_trip(tmp_path, body):
    """The footer must still be matchable after Jira's ADF conversion."""
    from app.jira_notifications import _adf_to_text, _text_to_adf

    round_tripped = _adf_to_text(_text_to_adf(_rendered(body)))

    assert round_tripped.rstrip().endswith(_footer_for(body))
