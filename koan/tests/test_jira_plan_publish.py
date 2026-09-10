"""Tests for durable, read-back-verified Jira plan publishing."""

import json
from unittest.mock import patch

import pytest
from app.jira_notifications import JiraCommentFetchError, _adf_to_text, markdown_to_adf
from app.jira_plan_publish import (
    _FOOTER_RE,
    _MAX_COMMENT_CHARS,
    _MAX_PUBLISH_SESSIONS,
    _PART_BODY_CHARS,
    _STAGE_MAX_AGE_SECONDS,
    _SUPERSEDED_BODY,
    _fence_balanced,
    _footer_for,
    _plan_parts,
    _render_comment,
    _revision,
    _split_comment_body,
    load_staged_plan,
    publish_staged_plan,
    stage_path_for,
    stage_plan,
)

URL = "https://org.atlassian.net/browse/PROJ-9"


def _footer(body):
    """The single-part footer the publisher signs ``body`` with."""
    return _footer_for(_revision(body))


def _rendered(body):
    """The comment text the publisher is expected to send for ``body``."""
    return f"{body}\n\n{_footer(body)}"


def _properties_map(properties):
    """Mirror how ``jira_list_comments_checked`` returns entity properties."""
    return {item["key"]: item["value"] for item in properties or []}


def _as_jira_returns(rendered):
    """What a read-back of ``rendered`` actually yields.

    Jira stores ADF, and ``jira_list_comments_checked`` renders it back through
    ``_adf_to_text`` — which joins sibling inline nodes with a space, so a
    linked URL comes back spaced differently from what was sent. A fake listing
    that echoes the raw markdown hides idempotency checks that never match.
    """
    return _adf_to_text(markdown_to_adf(rendered))


def test_publish_creates_then_verifies_and_clears_stage(tmp_path):
    body = "Koan plan update\n\nPhase 1: do the work"
    stage_plan(URL, body, str(tmp_path))
    comments = []

    def add(_key, rendered, properties=None):
        comments.append({
            "id": "42", "body": rendered, "properties": _properties_map(properties),
        })
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


def test_update_false_without_readback_retries_and_keeps_stage(tmp_path):
    """Re-editing the plan comment we already own is idempotent, so it retries."""
    stage_plan(URL, "revised plan", str(tmp_path))
    koan_part = {
        "id": "10",
        "body": _rendered("original plan"),
        "properties": {"koan.jira.plan": {"revision": _revision("original plan"),
                                          "part": 1, "parts": 1}},
    }

    with (
        patch(
            "app.jira_plan_publish.jira_list_comments_checked",
            side_effect=lambda _k: [koan_part],
        ),
        patch(
            "app.jira_plan_publish.jira_edit_comment", return_value=False
        ) as edit_comment,
        patch("app.jira_plan_publish.jira_add_comment") as add_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "verification_failed"
    assert edit_comment.call_count == 3
    add_comment.assert_not_called()
    assert load_staged_plan(URL, str(tmp_path)) == "revised plan"


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


def test_failures_reach_the_run_log_not_only_the_audit_sink(tmp_path):
    """`log_event` is config-gated and never raises, so it cannot be the only sink.

    With `audit.enabled` false, a Jira error whose sole trace is
    `security.jsonl` is invisible to whoever runs `make logs`.
    """
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch(
            "app.jira_plan_publish.jira_list_comments_checked",
            side_effect=JiraCommentFetchError("boom"),
        ),
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
        patch("app.jira_plan_publish._log_runner") as run_log,
    ):
        publish_staged_plan(URL, str(tmp_path))

    messages = [call.args[1] for call in run_log.call_args_list]
    assert any("lookup failed" in m and "PROJ-9" in m for m in messages)
    assert any("boom" in m for m in messages)


def test_transient_lookup_failure_then_success_posts_once(tmp_path):
    stage_plan(URL, "plan", str(tmp_path))
    comments = []
    calls = {"n": 0}

    def listing(_key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JiraCommentFetchError("transient")
        return comments

    def add(_key, rendered, properties=None):
        comments.append({
            "id": "7", "body": rendered, "properties": _properties_map(properties),
        })
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

    def edit(_key, _comment_id, rendered, properties=None):
        existing["body"] = rendered
        if properties is not None:
            existing["properties"] = _properties_map(properties)
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
    assert existing["body"].endswith(_footer(body))
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
    unrelated = {"id": "3", "body": f"I think {_footer(body)} is the marker, right?"}
    posted = []

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: [unrelated] + posted),
        patch(
            "app.jira_plan_publish.jira_add_comment",
            side_effect=lambda _k, r, properties=None: posted.append(
                {"id": "9", "body": r, "properties": _properties_map(properties)}
            ) or True,
        ),
        patch("app.jira_plan_publish.jira_edit_comment") as edit_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "9"
    edit_comment.assert_not_called()


def test_a_quoted_footer_at_the_end_of_a_human_comment_is_never_edited(tmp_path):
    """A reviewer quoting the tail of a plan must not become the plan comment.

    End-anchoring alone does not help here: the quote *is* the last thing in
    the reviewer's comment. Only Koan's own comments carry the entity property.
    """
    stage_plan(URL, "revised plan", str(tmp_path))
    koan_part = {
        "id": "10",
        "body": _rendered("original plan"),
        "properties": {"koan.jira.plan": {"revision": _revision("original plan"),
                                          "part": 1, "parts": 1}},
    }
    human = {"id": "11", "body": f"Why this step?\n\n{_footer('original plan')}"}
    comments = [human, koan_part]

    def edit(_key, comment_id, rendered, properties=None):
        target = next(c for c in comments if c["id"] == comment_id)
        target["body"] = rendered
        if properties is not None:
            target["properties"] = _properties_map(properties)
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit),
        patch("app.jira_plan_publish.jira_add_comment") as add_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "10"
    add_comment.assert_not_called()
    assert human["body"] == f"Why this step?\n\n{_footer('original plan')}"
    assert koan_part["body"].endswith(_footer("revised plan"))


def test_a_quoted_stale_footer_in_a_human_comment_is_never_retired(tmp_path):
    """Retirement blanks a comment's whole body — never a human's."""
    stage_plan(URL, "revised plan", str(tmp_path))
    koan_part = {
        "id": "10",
        "body": _rendered("original plan"),
        "properties": {"koan.jira.plan": {"revision": _revision("original plan"),
                                          "part": 1, "parts": 1}},
    }
    # A stale revision *and* a part number beyond the new plan: both retirement
    # triggers at once.
    human = {"id": "11", "body": f"Quoting part 2:\n\n{_footer_for(_revision('older'), 2, 2)}"}
    comments = [koan_part, human]

    def edit(_key, comment_id, rendered, properties=None):
        target = next(c for c in comments if c["id"] == comment_id)
        target["body"] = rendered
        if properties is not None:
            target["properties"] = _properties_map(properties)
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit),
        patch("app.jira_plan_publish.jira_add_comment") as add_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "10"
    add_comment.assert_not_called()
    assert _SUPERSEDED_BODY not in human["body"]
    assert load_staged_plan(URL, str(tmp_path)) is None


def test_property_less_fallback_still_spares_a_human_comment(tmp_path):
    """Authorship, not the footer, decides when properties are unavailable.

    On a deployment that drops comment properties every plan comment falls into
    the footer-only path — including a human's quoted plan tail, which the next
    revision would otherwise overwrite and the retirement pass blank.
    """
    stage_plan(URL, "revised plan", str(tmp_path))
    koan_part = {
        "id": "10",
        "body": _rendered("original plan"),
        "properties": {},
        "author_account_id": "koan-account",
    }
    human_body = f"Quoting part 2:\n\n{_footer_for(_revision('older'), 2, 2)}"
    human = {
        "id": "11",
        "body": human_body,
        "properties": {},
        "author_account_id": "human-account",
    }
    comments = [koan_part, human]

    def edit(_key, comment_id, rendered, properties=None):
        target = next(c for c in comments if c["id"] == comment_id)
        target["body"] = rendered
        return True

    with (
        patch("app.jira_notifications.jira_self_identity", return_value=("koan-account", "")),
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit),
        patch("app.jira_plan_publish.jira_add_comment") as add_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "10"
    add_comment.assert_not_called()
    assert human["body"] == human_body
    assert koan_part["body"].endswith(_footer("revised plan"))


def test_a_created_part_is_not_created_again_when_the_listing_lags(tmp_path):
    """Jira's comment listing is not read-your-writes.

    A create that reported success but has not replicated yet must not be
    posted a second time — that is the duplicate this module exists to prevent.
    """
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", return_value=[]),
        patch("app.jira_plan_publish.jira_add_comment", return_value=True) as add_comment,
        patch("app.jira_plan_publish.jira_edit_comment") as edit_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "created_unverified"
    assert add_comment.call_count == 1
    edit_comment.assert_not_called()
    # The stage survives, so the next mission run re-verifies without paying
    # for another model run.
    assert load_staged_plan(URL, str(tmp_path)) == "plan"


def test_a_create_whose_response_was_lost_is_not_posted_again(tmp_path):
    """`jira_add_comment` returning False does not mean nothing was written.

    A POST whose response is lost (socket timeout) is reported as a failure for
    a comment Jira did create, so the attempt — not its reported result — has to
    disqualify a second create.
    """
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", return_value=[]),
        patch(
            "app.jira_plan_publish.jira_add_comment", return_value=False
        ) as add_comment,
        patch("app.jira_plan_publish.jira_edit_comment") as edit_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "created_unverified"
    assert add_comment.call_count == 1
    edit_comment.assert_not_called()
    assert load_staged_plan(URL, str(tmp_path)) == "plan"


def test_a_create_that_raised_is_not_posted_again(tmp_path):
    """A write that raised mid-flight is just as ambiguous as one that timed out."""
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", return_value=[]),
        patch(
            "app.jira_plan_publish.jira_add_comment",
            side_effect=RuntimeError("connection reset"),
        ) as add_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "created_unverified"
    assert add_comment.call_count == 1
    assert load_staged_plan(URL, str(tmp_path)) == "plan"


def test_a_lagging_create_verifies_on_a_later_attempt_without_a_second_post(tmp_path):
    """Once the replica catches up, the same attempt loop verifies the comment."""
    stage_plan(URL, "plan", str(tmp_path))
    comments = []
    reads = {"n": 0}

    def listing(_key):
        reads["n"] += 1
        # The create lands in the listing only from the third read on, so
        # attempt 1's own post-write read-back still comes back empty.
        return comments if reads["n"] > 2 else []

    def add(_key, rendered, properties=None):
        comments.append({
            "id": "42", "body": rendered, "properties": _properties_map(properties),
        })
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=listing),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add) as add_comment,
        patch("app.jira_plan_publish.jira_edit_comment") as edit_comment,
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, comment_id = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert comment_id == "42"
    assert add_comment.call_count == 1
    edit_comment.assert_not_called()
    assert len(comments) == 1


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
            assert reason == "created_unverified"
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


def test_corrupt_stage_is_reported_not_silently_treated_as_absent(tmp_path):
    """A truncated stage file must not read as "nothing was staged".

    What it holds is the model run this module exists to protect; discarding
    it without a trace makes an expensive loss look like a fresh start.
    """
    stage_plan(URL, "plan", str(tmp_path))
    stage_path_for(URL, str(tmp_path)).write_text('{"issue_url": "https://org')

    with patch("app.jira_plan_publish.log_event") as log_event:
        assert load_staged_plan(URL, str(tmp_path)) is None

    actions = [call.kwargs["details"]["action"] for call in log_event.call_args_list]
    assert "stage_read" in actions


def test_missing_stage_is_not_reported_as_a_failure(tmp_path):
    with patch("app.jira_plan_publish.log_event") as log_event:
        assert load_staged_plan(URL, str(tmp_path)) is None

    log_event.assert_not_called()


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
    from app.jira_notifications import _adf_to_text, markdown_to_adf

    round_tripped = _adf_to_text(markdown_to_adf(_rendered(body)))

    assert round_tripped.rstrip().endswith(_footer(body))


# ---------------------------------------------------------------------------
# Oversized plans split across linked parts
# ---------------------------------------------------------------------------


def _split_fixture(part_count=3):
    """A plan body long enough to need ``part_count`` Jira comments."""
    unit = "section content\n\n"
    return unit * ((_PART_BODY_CHARS * (part_count - 1)) // len(unit) + 200) + "final section"


def test_long_plan_splits_at_safe_boundaries_and_preserves_content():
    body = ("paragraph one\n\n" * 2_500) + "final paragraph"

    parts = _split_comment_body(body)
    revision = _revision(body)

    assert len(parts) > 1
    assert "".join(parts) == body
    assert all(
        len(_render_comment(part, revision, index + 1, len(parts))) <= _MAX_COMMENT_CHARS
        for index, part in enumerate(parts)
    )


def test_short_plan_is_not_split():
    assert _split_comment_body("a short plan") == ["a short plan"]


def test_long_plan_creates_linked_verified_parts(tmp_path):
    body = _split_fixture(3)
    stage_plan(URL, body, str(tmp_path))
    comments = []

    def add(_key, rendered, properties=None):
        comments.append({
            "id": str(len(comments) + 1),
            "body": rendered,
            "properties": _properties_map(properties),
        })
        return True

    def edit(_key, comment_id, rendered, properties=None):
        target = next(c for c in comments if c["id"] == comment_id)
        target["body"] = rendered
        if properties is not None:
            target["properties"] = _properties_map(properties)
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add) as add_comment,
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit) as edit_comment,
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, ids = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert ids == "1, 2, 3"
    assert add_comment.call_count == 3
    assert edit_comment.call_count == 3  # one navigation pass, no retirements
    assert all(len(c["body"]) <= _MAX_COMMENT_CHARS for c in comments)
    assert "Part 1 of 3" in comments[0]["body"]
    assert f"Next part: {URL}?focusedCommentId=2" in comments[0]["body"]
    assert f"Previous part: {URL}?focusedCommentId=2" in comments[2]["body"]
    assert "Next part" not in comments[2]["body"]
    assert "".join(_split_comment_body(body)) == body
    assert load_staged_plan(URL, str(tmp_path)) is None


def test_resuming_a_fully_published_split_plan_rewrites_nothing(tmp_path):
    body = _split_fixture(3)
    stage_plan(URL, body, str(tmp_path))
    comments = []

    def add(_key, rendered, properties=None):
        comments.append({
            "id": str(len(comments) + 1),
            "body": _as_jira_returns(rendered),
            "properties": _properties_map(properties),
        })
        return True

    def edit(_key, comment_id, rendered, properties=None):
        target = next(c for c in comments if c["id"] == comment_id)
        target["body"] = _as_jira_returns(rendered)
        if properties is not None:
            target["properties"] = _properties_map(properties)
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add),
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit),
        patch("app.jira_plan_publish.log_event"),
    ):
        publish_staged_plan(URL, str(tmp_path))
        stage_plan(URL, body, str(tmp_path))

        with (
            patch("app.jira_plan_publish.jira_add_comment") as add2,
            patch("app.jira_plan_publish.jira_edit_comment") as edit2,
        ):
            ok, ids = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    assert ids == "1, 2, 3"
    add2.assert_not_called()
    edit2.assert_not_called()


def test_shrinking_plan_retires_orphaned_parts(tmp_path):
    """A 3-part plan replaced by a 1-part plan must not strand parts 2 and 3."""
    stage_plan(URL, _split_fixture(3), str(tmp_path))
    comments = []

    def add(_key, rendered, properties=None):
        comments.append({
            "id": str(len(comments) + 1),
            "body": rendered,
            "properties": _properties_map(properties),
        })
        return True

    def edit(_key, comment_id, rendered, properties=None):
        target = next(c for c in comments if c["id"] == comment_id)
        target["body"] = rendered
        if properties is not None:
            target["properties"] = _properties_map(properties)
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add),
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit),
        patch("app.jira_plan_publish.log_event"),
    ):
        publish_staged_plan(URL, str(tmp_path))
        assert len(comments) == 3

        stage_plan(URL, "a much shorter plan", str(tmp_path))
        ok, ids = publish_staged_plan(URL, str(tmp_path))

    assert ok is True
    # The short plan reuses part 1's comment rather than posting a fourth.
    assert ids == "1"
    assert len(comments) == 3
    assert comments[0]["body"].endswith(_footer("a much shorter plan"))
    assert "focusedCommentId" not in comments[0]["body"]
    for orphan in comments[1:]:
        assert "Superseded" in orphan["body"]
        assert "Koan current plan (rev" not in orphan["body"]


def test_split_part_failure_reports_which_part(tmp_path):
    stage_plan(URL, _split_fixture(3), str(tmp_path))

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", return_value=[]),
        patch("app.jira_plan_publish.jira_add_comment", return_value=False),
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "part_1_of_3_created_unverified"


def test_split_inside_a_code_fence_keeps_the_footer_verifiable():
    """A part cut mid-fence must not swallow its own footer.

    Comments render via ``markdown_to_adf`` and read back via ``_adf_to_text``,
    which drops ``codeBlock`` content — an unclosed fence would hide the footer
    and the plan could never verify.
    """
    from app.jira_notifications import _adf_to_text, markdown_to_adf

    raw = ["Step 1\n\n```python\nx = 1\ny = 2", "z = 3\n```\n\nStep 2"]
    parts = _fence_balanced(raw)
    revision = _revision("".join(raw))

    for index, part in enumerate(parts):
        rendered = _render_comment(part, revision, index + 1, len(parts))
        read_back = _adf_to_text(markdown_to_adf(rendered)).rstrip()
        assert _FOOTER_RE.search(read_back), f"part {index + 1} lost its footer"


def test_fence_balancing_reopens_the_block_in_the_next_part():
    parts = _fence_balanced(["a\n\n```py\nx = 1", "y = 2\n```\n\nb"])

    assert parts[0].endswith("```")
    assert parts[1].startswith("```py\n")
    assert parts[0].count("```") == 2
    assert parts[1].count("```") == 2


def test_fence_balancing_leaves_balanced_parts_untouched():
    parts = ["a\n\n```py\nx = 1\n```\n", "plain text"]

    assert _fence_balanced(parts) == parts


def test_plan_parts_splits_and_balances_together():
    body = ("filler paragraph\n\n" * 2_000) + "```py\n" + ("code line\n" * 2_000) + "```\n"
    parts = _plan_parts(body)

    assert len(parts) > 1
    for part in parts:
        assert part.count("```") % 2 == 0, "every published part must be fence-balanced"


def test_split_does_not_emit_a_runt_first_part():
    """A File Map is a long blank-line-free run.

    Preferring the coarsest separator outright would cut at the last "\n\n"
    before it — near the very start — emitting an absurd Part 1 and an extra
    publish round trip.
    """
    body = "## Plan\n\nIntro.\n\n### Phase 1\n\nprose.\n\n" + "\n".join(
        f"| file{i}.py | modify | yes |" for i in range(4000)
    )
    parts = _plan_parts(body)

    assert "".join(_split_comment_body(body)) == body
    assert all(len(part) > _PART_BODY_CHARS // 2 for part in parts[:-1]), (
        f"runt part in {[len(p) for p in parts]}"
    )


def test_failed_orphan_retirement_keeps_the_stage_and_fails(tmp_path):
    """`/implement` prefers a multipart group, so a stranded one wins.

    Reporting success while an older group survives means the next
    `/implement` builds the obsolete plan instead of the one just published.
    """
    stage_plan(URL, _split_fixture(3), str(tmp_path))
    comments = []

    def add(_key, rendered, properties=None):
        comments.append({
            "id": str(len(comments) + 1),
            "body": rendered,
            "properties": _properties_map(properties),
        })
        return True

    def edit(_key, comment_id, rendered, properties=None):
        target = next(c for c in comments if c["id"] == comment_id)
        target["body"] = rendered
        if properties is not None:
            target["properties"] = _properties_map(properties)
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add),
        patch("app.jira_plan_publish.jira_edit_comment", side_effect=edit),
        patch("app.jira_plan_publish.log_event"),
    ):
        publish_staged_plan(URL, str(tmp_path))
        assert len(comments) == 3

        # Jira accepts the retirement edits but never applies them; ordinary
        # publish edits still work, so this isolates the cleanup step.
        retire_calls = []

        def edit_but_drop_retirements(_key, comment_id, rendered, properties=None):
            if rendered == _SUPERSEDED_BODY:
                retire_calls.append(comment_id)
                return True
            return edit(_key, comment_id, rendered, properties=properties)

        stage_plan(URL, "a much shorter plan", str(tmp_path))
        with patch(
            "app.jira_plan_publish.jira_edit_comment",
            side_effect=edit_but_drop_retirements,
        ):
            ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "superseded_parts_not_retired"
    assert retire_calls, "retirement was attempted"
    assert load_staged_plan(URL, str(tmp_path)) == "a much shorter plan"


def test_clear_failure_does_not_claim_the_stage_was_abandoned(tmp_path):
    stage_plan(URL, "plan", str(tmp_path))

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", return_value=[]),
        patch("app.jira_plan_publish.jira_add_comment", return_value=False),
        patch("app.jira_plan_publish.time.sleep"),
        patch("app.jira_plan_publish.log_event"),
    ):
        for _ in range(_MAX_PUBLISH_SESSIONS - 1):
            publish_staged_plan(URL, str(tmp_path))

        with patch("pathlib.Path.unlink", side_effect=OSError("read-only fs")):
            ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    # The stage survived, so "abandoned" would be a lie.
    assert reason == "created_unverified"
    assert load_staged_plan(URL, str(tmp_path)) == "plan"


def test_clear_failure_after_a_verified_publish_is_not_reported_as_success(tmp_path):
    """The comments landed, but a surviving stage still misleads the next run.

    `/plan` on this issue would resume and republish the same plan instead of
    generating the one the user asked for.
    """
    body = "plan"
    stage_plan(URL, body, str(tmp_path))
    comments = []

    def add(_key, rendered, properties=None):
        comments.append({
            "id": "42", "body": rendered, "properties": _properties_map(properties),
        })
        return True

    with (
        patch("app.jira_plan_publish.jira_list_comments_checked", side_effect=lambda _k: comments),
        patch("app.jira_plan_publish.jira_add_comment", side_effect=add),
        patch("app.jira_plan_publish.log_event"),
        patch("pathlib.Path.unlink", side_effect=OSError("read-only fs")),
    ):
        ok, reason = publish_staged_plan(URL, str(tmp_path))

    assert ok is False
    assert reason == "stage_clear_failed"
    # The comment really was posted — the failure is local, not on Jira.
    assert len(comments) == 1
    assert load_staged_plan(URL, str(tmp_path)) == body


def test_expired_stage_is_never_returned_even_if_deletion_fails(tmp_path):
    stage_plan(URL, "old plan", str(tmp_path))
    path = stage_path_for(URL, str(tmp_path))
    payload = json.loads(path.read_text())
    payload["staged_at"] -= _STAGE_MAX_AGE_SECONDS + 1
    path.write_text(json.dumps(payload))

    with (
        patch("pathlib.Path.unlink", side_effect=OSError("read-only fs")),
        patch("app.jira_plan_publish.log_event") as log_event,
    ):
        assert load_staged_plan(URL, str(tmp_path)) is None

    assert path.exists()  # deletion failed …
    assert log_event.call_args.kwargs["details"]["action"] == "stage_clear"  # … and was audited
