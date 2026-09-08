"""Tests for Jira end-of-mission outcome publishing."""

from unittest.mock import patch


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
            patch("app.jira_outcome_publish.jira_list_comments_checked", return_value=[]),
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
        existing = [{"id": "99", "body": f"old\n\n{marker}"}]
        with (
            patch("app.jira_outcome_publish.jira_list_comments_checked", return_value=existing),
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
            patch("app.jira_outcome_publish.jira_list_comments_checked", return_value=[]),
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
        existing = [{"id": "7", "body": f"old\n\n{marker}"}]
        with (
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
    def test_new_outcome_uses_hidden_property_without_body_marker(self):
        from app.jira_outcome_publish import (
            _OUTCOME_PROPERTY_KEY,
            _outcome_digest,
            upsert_jira_comment,
        )

        with (
            patch("app.jira_outcome_publish.jira_list_comments_checked", return_value=[]),
            patch("app.jira_outcome_publish.jira_add_comment", return_value=True) as mock_add,
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "hello world")

        assert (ok, mode) == (True, "created")
        assert mock_add.call_args.args[1] == "hello world"
        assert "<!--" not in mock_add.call_args.args[1]
        assert mock_add.call_args.kwargs["properties"] == [{
            "key": _OUTCOME_PROPERTY_KEY,
            "value": {
                "digest": _outcome_digest("PROJ-1", "fix"),
                "command": "fix",
            },
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
        assert edit_comment.call_args.args[:3] == ("PROJ-1", "99", "new body")
        assert edit_comment.call_args.kwargs["properties"]
        add_comment.assert_not_called()

    def test_legacy_marker_is_migrated_to_property_and_removed_from_body(self):
        from app.jira_outcome_publish import _marker_for, upsert_jira_comment

        existing = [{
            "id": "99",
            "body": f"old body\n\n{_marker_for('PROJ-1', 'fix')}",
            "properties": {},
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
        ):
            ok, mode = upsert_jira_comment("PROJ-1", "fix", "new body")

        assert (ok, mode) == (True, "updated")
        assert edit_comment.call_args.args[2] == "new body"
        assert "koan-jira-outcome" not in edit_comment.call_args.args[2]
        assert edit_comment.call_args.kwargs["properties"]


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
