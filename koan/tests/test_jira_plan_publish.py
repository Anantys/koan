"""Tests for durable, read-back-verified Jira plan publishing."""

import json
from unittest.mock import patch

import pytest

from app.jira_notifications import JiraCommentFetchError
from app.jira_plan_publish import (
    _FOOTER_RE,
    _SUPERSEDED_BODY,
    _MAX_COMMENT_CHARS,
    _MAX_PUBLISH_SESSIONS,
    _PART_BODY_CHARS,
    _STAGE_MAX_AGE_SECONDS,
    _fence_balanced,
    _footer_for,
    _render_comment,
    _revision,
    _plan_parts,
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

    def add(_key, rendered):
        comments.append({"id": str(len(comments) + 1), "body": rendered})
        return True

    def edit(_key, comment_id, rendered):
        next(c for c in comments if c["id"] == comment_id)["body"] = rendered
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

    def add(_key, rendered):
        comments.append({"id": str(len(comments) + 1), "body": rendered})
        return True

    def edit(_key, comment_id, rendered):
        next(c for c in comments if c["id"] == comment_id)["body"] = rendered
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

    def add(_key, rendered):
        comments.append({"id": str(len(comments) + 1), "body": rendered})
        return True

    def edit(_key, comment_id, rendered):
        next(c for c in comments if c["id"] == comment_id)["body"] = rendered
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
    assert reason == "part_1_of_3_verification_failed"


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

    def add(_key, rendered):
        comments.append({"id": str(len(comments) + 1), "body": rendered})
        return True

    def edit(_key, comment_id, rendered):
        next(c for c in comments if c["id"] == comment_id)["body"] = rendered
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

        def edit_but_drop_retirements(_key, comment_id, rendered):
            if rendered == _SUPERSEDED_BODY:
                retire_calls.append(comment_id)
                return True
            return edit(_key, comment_id, rendered)

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
    assert reason == "verification_failed"
    assert load_staged_plan(URL, str(tmp_path)) == "plan"
