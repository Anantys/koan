"""Tests for Jira end-of-mission outcome publishing."""

from unittest.mock import patch


def _tagged(issue_key: str, command: str, comment_id: str = "1", body: str = "posted"):
    """A read-back listing carrying the dedup property the publisher writes.

    Every write is confirmed by re-listing the comments, so a test that stubs
    the listing has to answer that second call too.
    """
    from app.jira_outcome_publish import _OUTCOME_PROPERTY_KEY, _outcome_digest

    return [{
        "id": comment_id,
        "body": body,
        "properties": {
            _OUTCOME_PROPERTY_KEY: {
                "digest": _outcome_digest(issue_key, command),
                "command": command,
            },
        },
    }]


def _as_koan():
    """Make Jira attribute Koan's own account to comments marked with it.

    Without the property, the visible footer only identifies a status comment
    on a comment Jira positively attributes to Koan — the publisher is about to
    replace that body, so "cannot tell" is not enough.
    """
    return patch(
        "app.jira_notifications.jira_self_identity",
        return_value=("koan-account", ""),
    )


class TestPublishJiraMissionOutcome:
    def test_skips_when_no_jira_url(self):
        from app.jira_outcome_publish import publish_jira_mission_outcome

        with (
            patch("app.jira_outcome_publish.jira_list_comments_checked") as mock_list,
            patch("app.jira_outcome_publish.jira_add_comment") as mock_add,
        ):
            result = publish_jira_mission_outcome(
                mission_title="/fix https://github.com/o/r/issues/1",
                pending_content="Draft PR: https://github.com/o/r/pull/1",
                exit_code=0,
            )

        assert result["published"] == "false"
        assert result["reason"] == "no_jira_url"
        mock_list.assert_not_called()
        mock_add.assert_not_called()

    def test_success_with_pr_posts_structured_comment(self):
        from app.jira_outcome_publish import publish_jira_mission_outcome

        with (
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[[], _tagged("PROJ-42", "fix")],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as mock_add,
            patch(
                "app.jira_outcome_publish._fetch_pr_details",
                return_value=("Repair widget validators", ""),
            ),
        ):
            result = publish_jira_mission_outcome(
                mission_title="/fix https://org.atlassian.net/browse/PROJ-42 branch:main",
                pending_content="Fix complete.\nDraft PR: https://github.com/o/r/pull/123",
                exit_code=0,
                base_branch="main",
            )

        assert result["published"] == "true"
        assert result["outcome"] == "pr_success"
        assert result["pr_url"] == "https://github.com/o/r/pull/123"
        mock_add.assert_called_once()
        body = mock_add.call_args.args[1]
        assert body.startswith("### Kōan · draft pull request created")
        assert "- **Mission**: `/fix`" in body
        assert (
            "- **Pull request**: [PR #123 — Repair widget validators]"
            "(https://github.com/o/r/pull/123)"
        ) in body
        assert "<!-- koan-jira-outcome:" not in body

        from app.jira_outcome_publish import _footer_for, _outcome_digest

        assert body.endswith(_footer_for(_outcome_digest("PROJ-42", "fix")))

    def test_success_comment_enriched_from_pr_body(self):
        # The publisher fetches the PR body from GitHub so the agent-path
        # comment includes the What/Why summary, matching the skill path.
        from app.jira_outcome_publish import publish_jira_mission_outcome

        pr_body = "## Summary\n\n- Reworked parser\n\n## Why\n\nFixes the crash"
        with (
            patch("app.jira_outcome_publish.jira_list_comments_checked", return_value=[]),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as mock_add,
            patch("app.jira_outcome_publish._fetch_pr_details",
                  return_value=("fix: crash", pr_body)) as mock_fetch,
        ):
            publish_jira_mission_outcome(
                mission_title="/fix https://org.atlassian.net/browse/PROJ-42",
                pending_content="Draft PR: https://github.com/o/r/pull/123",
                exit_code=0,
            )

        mock_fetch.assert_called_once_with("https://github.com/o/r/pull/123")
        body = mock_add.call_args.args[1]
        assert "**What changed**" in body
        assert "Reworked parser" in body
        assert "**Why**\nFixes the crash" in body

    def test_success_with_pr_updates_existing_comment(self):
        from app.jira_outcome_publish import _marker_for, publish_jira_mission_outcome

        marker = _marker_for("PROJ-42", "fix")
        existing = [
            {
                "id": "99",
                "body": f"old\n\n{marker}",
                "author_account_id": "koan-account",
            },
        ]
        with (
            _as_koan(),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[existing, _tagged("PROJ-42", "fix", comment_id="99")],
            ),
            patch("app.jira_outcome_publish.jira_edit_comment", return_value=True) as mock_edit,
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as mock_add,
            patch("app.jira_outcome_publish._fetch_pr_details", return_value=("", "")),
        ):
            result = publish_jira_mission_outcome(
                mission_title="/fix https://org.atlassian.net/browse/PROJ-42",
                pending_content="Draft PR: https://github.com/o/r/pull/123",
                exit_code=0,
            )

        assert result["published"] == "true"
        assert result["reason"] == "updated"
        mock_edit.assert_called_once()
        mock_add.assert_not_called()

    def test_failure_posts_comment_without_pr(self):
        from app.jira_outcome_publish import publish_jira_mission_outcome

        with (
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[[], _tagged("PROJ-99", "implement")],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as mock_add,
        ):
            result = publish_jira_mission_outcome(
                mission_title="/implement https://org.atlassian.net/browse/PROJ-99",
                pending_content="Implementation failed: test suite crashed",
                exit_code=1,
            )

        assert result["published"] == "true"
        assert result["outcome"] == "failure"
        body = mock_add.call_args.args[1]
        assert body.startswith("### Kōan · pull request creation failed")
        assert "- **Mission**: `/implement`" in body

    def test_success_without_pr_is_skipped(self):
        # exit 0 but no PR URL in the output → nothing to report.
        from app.jira_outcome_publish import publish_jira_mission_outcome

        with (
            patch("app.jira_outcome_publish.jira_list_comments_checked") as mock_list,
            patch("app.jira_outcome_publish.jira_add_comment") as mock_add,
        ):
            result = publish_jira_mission_outcome(
                mission_title="/fix https://org.atlassian.net/browse/PROJ-42",
                pending_content="All done, no PR opened.",
                exit_code=0,
            )

        assert result["published"] == "false"
        assert result["reason"] == "success_without_pr"
        mock_list.assert_not_called()
        mock_add.assert_not_called()

    def test_update_failure_reports_not_published(self):
        # jira_edit_comment returns False → published=false, reason=update_failed.
        from app.jira_outcome_publish import _marker_for, publish_jira_mission_outcome

        marker = _marker_for("PROJ-42", "fix")
        existing = [
            {
                "id": "7",
                "body": f"old\n\n{marker}",
                "author_account_id": "koan-account",
            },
        ]
        with (
            _as_koan(),
            patch("app.jira_outcome_publish.jira_list_comments_checked", return_value=existing),
            patch("app.jira_outcome_publish.jira_edit_comment", return_value=False),
            patch("app.jira_outcome_publish._fetch_pr_details", return_value=("", "")),
        ):
            result = publish_jira_mission_outcome(
                mission_title="/fix https://org.atlassian.net/browse/PROJ-42",
                pending_content="Draft PR: https://github.com/o/r/pull/123",
                exit_code=0,
            )

        assert result["published"] == "false"
        assert result["reason"] == "update_failed"


class TestExtractPrUrl:
    def test_returns_empty_for_empty_text(self):
        from app.jira_outcome_publish import extract_pr_url

        assert extract_pr_url("") == ""

    def test_returns_empty_when_no_pr_url(self):
        from app.jira_outcome_publish import extract_pr_url

        assert extract_pr_url("just some text, no links here") == ""

    def test_extracts_first_pr_url(self):
        from app.jira_outcome_publish import extract_pr_url

        text = "see https://github.com/o/r/pull/9 and https://github.com/o/r/pull/10"
        assert extract_pr_url(text) == "https://github.com/o/r/pull/9"


class TestFetchPrDetails:
    def test_empty_url_returns_empty_pair(self):
        from app.jira_outcome_publish import _fetch_pr_details

        assert _fetch_pr_details("") == ("", "")

    def test_parses_title_and_body_from_gh(self):
        from app.jira_outcome_publish import _fetch_pr_details

        payload = '{"title": "fix: thing", "body": "the body"}'
        with patch("app.github.run_gh", return_value=payload):
            assert _fetch_pr_details("https://github.com/o/r/pull/1") == (
                "fix: thing",
                "the body",
            )

    def test_gh_error_degrades_to_empty_pair(self):
        from app.jira_outcome_publish import _fetch_pr_details

        with patch("app.github.run_gh", side_effect=RuntimeError("boom")):
            assert _fetch_pr_details("https://github.com/o/r/pull/1") == ("", "")

    def test_empty_gh_output_returns_empty_pair(self):
        from app.jira_outcome_publish import _fetch_pr_details

        with patch("app.github.run_gh", return_value=""):
            assert _fetch_pr_details("https://github.com/o/r/pull/1") == ("", "")

    def test_non_dict_json_returns_empty_pair(self):
        from app.jira_outcome_publish import _fetch_pr_details

        with patch("app.github.run_gh", return_value="[1, 2, 3]"):
            assert _fetch_pr_details("https://github.com/o/r/pull/1") == ("", "")


class TestExtractFailureReason:
    def test_skips_metadata_and_cli_lines_returns_first_real_line(self):
        from app.jira_outcome_publish import _extract_failure_reason

        content = (
            "# Mission: /fix something\n"
            "Project: koan\n"
            "Started: now\n"
            "Run: 1/60\n"
            "Mode: deep\n"
            "---\n"
            "\n"
            "[cli] starting session\n"
            "Real error: the thing broke\n"
        )
        assert _extract_failure_reason(content, 1) == "Real error: the thing broke"

    def test_truncates_long_line_to_220_chars(self):
        from app.jira_outcome_publish import _extract_failure_reason

        long_line = "x" * 500
        assert _extract_failure_reason(long_line, 1) == "x" * 220

    def test_falls_back_to_exit_code_when_no_usable_line(self):
        from app.jira_outcome_publish import _extract_failure_reason

        content = "# Mission: /fix x\nProject: koan\n---\n[cli] noise\n"
        assert _extract_failure_reason(content, 3) == "Mission failed (exit code 3)."

    def test_empty_content_falls_back_to_exit_code(self):
        from app.jira_outcome_publish import _extract_failure_reason

        assert _extract_failure_reason("", 5) == "Mission failed (exit code 5)."


class TestUpsertJiraComment:
    def test_new_outcome_carries_property_and_visible_footer(self):
        from app.jira_outcome_publish import (
            _OUTCOME_PROPERTY_KEY,
            _footer_for,
            _outcome_digest,
            upsert_jira_comment,
        )

        digest = _outcome_digest("PROJ-1", "fix")
        posted = [{
            "id": "1",
            "body": f"hello world\n\n{_footer_for(digest)}",
            "properties": {
                _OUTCOME_PROPERTY_KEY: {"digest": digest, "command": "fix"},
            },
        }]
        with (
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[[], posted],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as mock_add,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "hello world")

        assert (ok, mode) == (True, "created")
        body = mock_add.call_args.args[1]
        assert body == f"hello world\n\n{_footer_for(digest)}"
        assert "<!--" not in body
        assert mock_add.call_args.kwargs["properties"] == [{
            "key": _OUTCOME_PROPERTY_KEY,
            "value": {"digest": digest, "command": "fix"},
        }]

    def test_existing_property_identifies_comment_for_update(self):
        from app.jira_outcome_publish import (
            _OUTCOME_PROPERTY_KEY,
            _outcome_digest,
            upsert_jira_comment,
        )

        existing = [{
            "id": "99",
            "body": "old body",
            "properties": {
                _OUTCOME_PROPERTY_KEY: {
                    "digest": _outcome_digest("PROJ-1", "fix"),
                    "command": "fix",
                },
            },
        }]
        with (
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                return_value=existing,
            ),
            patch(
                "app.jira_outcome_publish.jira_edit_comment",
                return_value=True,
            ) as edit_comment,
            patch("app.jira_outcome_publish.jira_add_comment") as add_comment,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "new body")

        assert (ok, mode) == (True, "updated")
        assert edit_comment.call_args.args[:2] == ("PROJ-1", "99")
        assert edit_comment.call_args.args[2].startswith("new body")
        assert edit_comment.call_args.kwargs["properties"]
        add_comment.assert_not_called()

    def test_legacy_marker_is_migrated_to_property_and_removed_from_body(self):
        from app.jira_outcome_publish import (
            _OUTCOME_PROPERTY_KEY,
            _footer_for,
            _marker_for,
            _outcome_digest,
            upsert_jira_comment,
        )

        digest = _outcome_digest("PROJ-1", "fix")
        existing = [{
            "id": "99",
            "body": f"old body\n\n{_marker_for('PROJ-1', 'fix')}",
            "properties": {},
            "author_account_id": "koan-account",
        }]
        migrated = [{
            "id": "99",
            "body": f"new body\n\n{_footer_for(digest)}",
            "properties": {
                _OUTCOME_PROPERTY_KEY: {"digest": digest, "command": "fix"},
            },
        }]
        with (
            _as_koan(),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[existing, migrated],
            ),
            patch(
                "app.jira_outcome_publish.jira_edit_comment",
                return_value=True,
            ) as edit_comment,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "new body")

        assert (ok, mode) == (True, "updated")
        assert edit_comment.call_args.args[2] == f"new body\n\n{_footer_for(digest)}"
        assert "koan-jira-outcome" not in edit_comment.call_args.args[2]
        assert edit_comment.call_args.kwargs["properties"]

    def test_legacy_marker_migrates_even_when_another_comment_has_a_property(self):
        """One property-carrying comment must not disqualify the rest.

        The issue already has a post-upgrade `/plan` status comment, so the
        property is demonstrably supported here — but the `/fix` status still
        predates it and carries only the legacy marker. Gating the whole
        listing on "some comment has the property" would skip it, post a
        second `/fix` status, and leave two contradictory outcomes on the
        issue.
        """
        from app.jira_outcome_publish import (
            _OUTCOME_PROPERTY_KEY,
            _footer_for,
            _marker_for,
            _outcome_digest,
            upsert_jira_comment,
        )

        digest = _outcome_digest("PROJ-1", "fix")
        plan_digest = _outcome_digest("PROJ-1", "plan")
        existing = [
            {
                "id": "1",
                "body": f"plan status\n\n{_footer_for(plan_digest)}",
                "properties": {
                    _OUTCOME_PROPERTY_KEY: {"digest": plan_digest, "command": "plan"},
                },
            },
            {
                "id": "99",
                "body": f"old body\n\n{_marker_for('PROJ-1', 'fix')}",
                "properties": {},
                "author_account_id": "koan-account",
            },
        ]
        migrated = [{
            "id": "99",
            "body": f"new body\n\n{_footer_for(digest)}",
            "properties": {
                _OUTCOME_PROPERTY_KEY: {"digest": digest, "command": "fix"},
            },
        }]
        with (
            _as_koan(),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[existing, migrated],
            ),
            patch(
                "app.jira_outcome_publish.jira_edit_comment",
                return_value=True,
            ) as edit_comment,
            patch("app.jira_outcome_publish.jira_add_comment") as add_comment,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "new body")

        assert (ok, mode) == (True, "updated")
        assert edit_comment.call_args.args[:2] == ("PROJ-1", "99")
        add_comment.assert_not_called()

    def test_dropped_property_still_verifies_via_visible_footer(self):
        """A Jira that does not persist the property must not fail the publish.

        The visible footer is the durable identity, so a read-back that shows
        the footer proves the comment is findable again.
        """
        from app.jira_outcome_publish import _footer_for, _outcome_digest, upsert_jira_comment

        digest = _outcome_digest("PROJ-1", "fix")
        posted = [{
            "id": "1",
            "body": f"hello world\n\n{_footer_for(digest)}",
            "properties": {},
            "author_account_id": "koan-account",
        }]
        with (
            _as_koan(),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[[], posted],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True),
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "hello world")

        assert (ok, mode) == (True, "created")

    def test_second_run_updates_footer_only_comment_instead_of_duplicating(self):
        """The regression the property-only identity introduced.

        A deployment that drops comment properties leaves nothing but the
        visible footer behind. The next mission must recognize that comment and
        edit it, not stack a second status comment on the issue.
        """
        from app.jira_outcome_publish import _footer_for, _outcome_digest, upsert_jira_comment

        digest = _outcome_digest("PROJ-1", "fix")
        # What the first run left on the issue: footer kept, property dropped.
        on_issue = [{
            "id": "1",
            "body": f"first status\n\n{_footer_for(digest)}",
            "properties": {},
            "author_account_id": "koan-account",
        }]
        updated = [{
            "id": "1",
            "body": f"second status\n\n{_footer_for(digest)}",
            "properties": {},
            "author_account_id": "koan-account",
        }]
        with (
            _as_koan(),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[on_issue, updated],
            ),
            patch(
                "app.jira_outcome_publish.jira_edit_comment", return_value=True,
            ) as edit_comment,
            patch("app.jira_outcome_publish.jira_add_comment") as add_comment,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "second status")

        assert (ok, mode) == (True, "updated")
        assert edit_comment.call_args.args[:2] == ("PROJ-1", "1")
        add_comment.assert_not_called()

    def test_footer_quoted_mid_body_is_not_mistaken_for_the_status_comment(self):
        """Only a trailing footer identifies a status comment.

        A human quoting a previous status inside their own comment must not
        make Koan overwrite that comment.
        """
        from app.jira_outcome_publish import _footer_for, _outcome_digest, upsert_jira_comment

        digest = _outcome_digest("PROJ-1", "fix")
        quoted = [{
            "id": "5",
            "body": f"I saw this:\n\n{_footer_for(digest)}\n\nany idea why?",
            "properties": {},
            "author_account_id": "human-account",
        }]
        posted = quoted + [{
            "id": "6",
            "body": f"status\n\n{_footer_for(digest)}",
            "properties": {},
            "author_account_id": "koan-account",
        }]
        with (
            _as_koan(),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[quoted, posted],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as add_comment,
            patch("app.jira_outcome_publish.jira_edit_comment") as edit_comment,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "status")

        assert (ok, mode) == (True, "created")
        add_comment.assert_called_once()
        edit_comment.assert_not_called()

    def test_footer_quoted_at_the_end_by_a_human_is_not_overwritten(self):
        """End-anchoring alone is not identity — authorship decides.

        A reviewer who quotes the tail of a status Koan posted ends their own
        comment with the footer. Editing it would destroy their text.
        """
        from app.jira_outcome_publish import _footer_for, _outcome_digest, upsert_jira_comment

        digest = _outcome_digest("PROJ-1", "fix")
        human_body = f"Is this still true?\n\n{_footer_for(digest)}"
        on_issue = [{
            "id": "5",
            "body": human_body,
            "properties": {},
            "author_account_id": "human-account",
        }]
        posted = on_issue + [{
            "id": "6",
            "body": f"status\n\n{_footer_for(digest)}",
            "properties": {},
            "author_account_id": "koan-account",
        }]
        with (
            patch(
                "app.jira_notifications.jira_self_identity",
                return_value=("koan-account", ""),
            ),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[on_issue, posted],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as add_comment,
            patch("app.jira_outcome_publish.jira_edit_comment") as edit_comment,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "status")

        assert (ok, mode) == (True, "created")
        add_comment.assert_called_once()
        edit_comment.assert_not_called()
        assert on_issue[0]["body"] == human_body

    def test_quoted_footer_survives_when_jira_cannot_resolve_identity(self):
        """The overwrite guard must not depend on ``/myself`` answering.

        On a tenant where the self-identity lookup is forbidden, Jira attributes
        the reviewer's comment to an account Koan cannot compare itself to. That
        is "cannot tell" — and a body replacement must read it as "not mine",
        so the reviewer's question stays and a fresh status is posted instead.
        """
        from app.jira_outcome_publish import (
            _footer_for,
            _outcome_digest,
            upsert_jira_comment,
        )

        digest = _outcome_digest("PROJ-1", "fix")
        human_body = f"Is this still true?\n\n{_footer_for(digest)}"
        on_issue = [{
            "id": "5",
            "body": human_body,
            "properties": {},
            "author_account_id": "human-account",
        }]
        posted = on_issue + [{
            "id": "6",
            "body": f"status\n\n{_footer_for(digest)}",
            "properties": {
                "koan.jira.outcome": {"digest": digest, "command": "fix"},
            },
            "author_account_id": "koan-account",
        }]
        with (
            # `/myself` is 403 → identity unknown, so no comment on the issue
            # can be positively attributed to Koan.
            patch("app.jira_notifications.jira_self_identity", return_value=("", "")),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[on_issue, posted],
            ),
            patch(
                "app.jira_outcome_publish.jira_add_comment", return_value=True,
            ) as add_comment,
            patch("app.jira_outcome_publish.jira_edit_comment") as edit_comment,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "status")

        assert (ok, mode) == (True, "created")
        add_comment.assert_called_once()
        edit_comment.assert_not_called()
        assert on_issue[0]["body"] == human_body

    def test_a_humans_quoted_footer_does_not_verify_koans_own_write(self):
        """Read-back must find *Koan's* comment, not a quote of it.

        A write that landed with neither identity is unfindable next run; a
        reviewer's quoted footer must not paper over that.
        """
        from app.jira_outcome_publish import _footer_for, _outcome_digest, upsert_jira_comment

        digest = _outcome_digest("PROJ-1", "fix")
        posted = [
            {
                "id": "5",
                "body": f"Is this still true?\n\n{_footer_for(digest)}",
                "properties": {},
                "author_account_id": "human-account",
            },
            {
                "id": "6",
                "body": "status",  # property dropped, footer stripped
                "properties": {},
                "author_account_id": "koan-account",
            },
        ]
        with (
            patch(
                "app.jira_notifications.jira_self_identity",
                return_value=("koan-account", ""),
            ),
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[[], posted],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True),
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "status")

        assert (ok, mode) == (False, "created_unverified")

    def test_write_that_kept_neither_identity_is_reported_unverified(self):
        """Jira can accept the write and keep neither property nor footer.

        Nothing then re-identifies the comment, so the next run would post a
        duplicate. Report it instead of claiming success.
        """
        from app.jira_outcome_publish import upsert_jira_comment

        posted_bare = [{"id": "1", "body": "hello world", "properties": {}}]
        with (
            patch(
                "app.jira_outcome_publish.jira_list_comments_checked",
                side_effect=[[], posted_bare],
            ),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True),
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "hello world")

        assert (ok, mode) == (False, "created_unverified")


def test_status_footer_survives_the_jira_renderer_round_trip():
    """The footer is only a durable identity if the transport keeps it.

    The body is rendered to ADF on the way out and extracted back to text on
    the way in; a renderer that swallowed the footer (as it does HTML comments)
    would put us back to a property-only identity without anyone noticing.
    """
    from app.jira_notifications import _adf_to_text, markdown_to_adf
    from app.jira_outcome_publish import _has_outcome_footer, _outcome_digest, _with_footer
    from app.tracker_comment_format import build_pr_comment_success

    digest = _outcome_digest("PROJ-42", "fix")
    body = build_pr_comment_success(
        "jira",
        pr_url="https://github.com/o/r/pull/123",
        pr_title="Repair widget validators",
        pr_body="## Summary\n\n- Reworked parser",
        skill_name="fix",
        base_branch="main",
    )
    round_tripped = _adf_to_text(markdown_to_adf(_with_footer(body, digest)))

    assert _has_outcome_footer({"body": round_tripped}, digest)


def test_lookup_failure_never_creates_a_duplicate_status_comment():
    """A broken read path must not look like "no status comment yet".

    jira_list_comments degrades to [] on API error, which is indistinguishable
    from an issue with no comments; creating on that signal stacks duplicates.
    """
    from app.jira_outcome_publish import upsert_jira_comment

    # Patch the transport, not the module-local import name: patching the
    # latter would only exist post-fix and so could never fail pre-fix.
    with (
        patch(
            "app.jira_notifications._list_comments_result",
            return_value=(False, []),
        ),
        patch("app.jira_outcome_publish.jira_add_comment") as add_comment,
        patch("app.jira_outcome_publish.jira_edit_comment") as edit_comment,
    ):
        ok, reason = upsert_jira_comment("FOO-1", "implement", "body")

    assert ok is False
    assert reason == "lookup_failed"
    add_comment.assert_not_called()
    edit_comment.assert_not_called()
