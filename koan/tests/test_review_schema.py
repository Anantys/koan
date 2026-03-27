"""Tests for review_schema.py — JSON schema validation for structured reviews."""

import pytest

from app.review_schema import validate_review


# ---------------------------------------------------------------------------
# Valid inputs
# ---------------------------------------------------------------------------

class TestValidateReviewValid:
    def test_minimal_lgtm(self):
        """LGTM review with no findings is valid."""
        data = {
            "file_comments": [],
            "review_summary": {
                "lgtm": True,
                "summary": "Clean code.",
                "checklist": [],
            },
        }
        valid, errors = validate_review(data)
        assert valid is True
        assert errors == []

    def test_full_review(self):
        """Review with findings, checklist, and code snippets is valid."""
        data = {
            "file_comments": [
                {
                    "file": "auth.py",
                    "line_start": 42,
                    "line_end": 45,
                    "severity": "critical",
                    "title": "SQL injection",
                    "comment": "User input not sanitized.",
                    "code_snippet": "query = f\"SELECT * FROM {user_input}\"",
                },
                {
                    "file": "utils.py",
                    "line_start": 10,
                    "line_end": 10,
                    "severity": "suggestion",
                    "title": "Naming",
                    "comment": "Consider a more descriptive name.",
                    "code_snippet": "",
                },
            ],
            "review_summary": {
                "lgtm": False,
                "summary": "Blocking SQL injection issue.",
                "checklist": [
                    {"item": "No hardcoded secrets", "passed": True, "finding_ref": ""},
                    {"item": "Input validation", "passed": False, "finding_ref": "critical #1"},
                ],
            },
        }
        valid, errors = validate_review(data)
        assert valid is True
        assert errors == []

    def test_all_severity_levels(self):
        """All three severity values are accepted."""
        for sev in ("critical", "warning", "suggestion"):
            data = {
                "file_comments": [{
                    "file": "a.py", "line_start": 1, "line_end": 1,
                    "severity": sev, "title": "t", "comment": "c",
                    "code_snippet": "",
                }],
                "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
            }
            valid, errors = validate_review(data)
            assert valid is True, f"severity '{sev}' should be valid"

    def test_zero_line_numbers(self):
        """Line numbers of 0 (whole-file comments) are valid."""
        data = {
            "file_comments": [{
                "file": "a.py", "line_start": 0, "line_end": 0,
                "severity": "warning", "title": "t", "comment": "c",
                "code_snippet": "",
            }],
            "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is True


# ---------------------------------------------------------------------------
# Invalid inputs
# ---------------------------------------------------------------------------

class TestValidateReviewInvalid:
    def test_not_a_dict(self):
        valid, errors = validate_review("not a dict")
        assert valid is False
        assert "Root must be a JSON object" in errors[0]

    def test_not_a_dict_list(self):
        valid, errors = validate_review([1, 2, 3])
        assert valid is False

    def test_missing_file_comments(self):
        data = {
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("file_comments" in e for e in errors)

    def test_missing_review_summary(self):
        data = {"file_comments": []}
        valid, errors = validate_review(data)
        assert valid is False
        assert any("review_summary" in e for e in errors)

    def test_file_comments_not_array(self):
        data = {
            "file_comments": "not an array",
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("array" in e for e in errors)

    def test_review_summary_not_object(self):
        data = {"file_comments": [], "review_summary": "not an object"}
        valid, errors = validate_review(data)
        assert valid is False
        assert any("object" in e for e in errors)

    def test_invalid_severity(self):
        data = {
            "file_comments": [{
                "file": "a.py", "line_start": 1, "line_end": 1,
                "severity": "blocker", "title": "t", "comment": "c",
                "code_snippet": "",
            }],
            "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("blocker" in e for e in errors)

    def test_missing_comment_field(self):
        data = {
            "file_comments": [{
                "file": "a.py", "line_start": 1, "line_end": 1,
                "severity": "warning", "title": "t",
                # missing comment and code_snippet
            }],
            "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("comment" in e for e in errors)

    def test_wrong_type_line_start(self):
        data = {
            "file_comments": [{
                "file": "a.py", "line_start": "not_int", "line_end": 1,
                "severity": "warning", "title": "t", "comment": "c",
                "code_snippet": "",
            }],
            "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("int" in e for e in errors)

    def test_missing_lgtm(self):
        data = {
            "file_comments": [],
            "review_summary": {"summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("lgtm" in e for e in errors)

    def test_checklist_item_wrong_type(self):
        data = {
            "file_comments": [],
            "review_summary": {
                "lgtm": True, "summary": "s",
                "checklist": [{"item": "x", "passed": "yes", "finding_ref": ""}],
            },
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("bool" in e for e in errors)

    def test_checklist_item_missing_field(self):
        data = {
            "file_comments": [],
            "review_summary": {
                "lgtm": True, "summary": "s",
                "checklist": [{"item": "x", "passed": True}],  # missing finding_ref
            },
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("finding_ref" in e for e in errors)

    def test_float_line_numbers_accepted(self):
        """JSON has no int type — float values like 42.0 should be accepted."""
        data = {
            "file_comments": [{
                "file": "a.py", "line_start": 42.0, "line_end": 42.0,
                "severity": "warning", "title": "t", "comment": "c",
                "code_snippet": "",
            }],
            "review_summary": {"lgtm": False, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is True


# ---------------------------------------------------------------------------
# comment_replies validation
# ---------------------------------------------------------------------------

class TestValidateCommentReplies:
    def test_valid_replies(self):
        """Review with comment_replies is valid."""
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
            "comment_replies": [
                {"comment_id": 123, "reply": "Good question — here's why."},
            ],
        }
        valid, errors = validate_review(data)
        assert valid is True
        assert errors == []

    def test_empty_replies(self):
        """Empty comment_replies array is valid."""
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
            "comment_replies": [],
        }
        valid, errors = validate_review(data)
        assert valid is True

    def test_no_replies_field(self):
        """Omitting comment_replies entirely is valid (field is optional)."""
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
        }
        valid, errors = validate_review(data)
        assert valid is True

    def test_replies_not_array(self):
        """comment_replies must be an array."""
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
            "comment_replies": "not an array",
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("array" in e for e in errors)

    def test_reply_missing_comment_id(self):
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
            "comment_replies": [{"reply": "text"}],
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("comment_id" in e for e in errors)

    def test_reply_missing_reply_text(self):
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
            "comment_replies": [{"comment_id": 100}],
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("reply" in e for e in errors)

    def test_reply_wrong_type_comment_id(self):
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
            "comment_replies": [{"comment_id": "not_int", "reply": "text"}],
        }
        valid, errors = validate_review(data)
        assert valid is False
        assert any("int" in e for e in errors)

    def test_reply_float_comment_id_accepted(self):
        """Float comment IDs like 123.0 are accepted (JSON has no int type)."""
        data = {
            "file_comments": [],
            "review_summary": {"lgtm": True, "summary": "s", "checklist": []},
            "comment_replies": [{"comment_id": 123.0, "reply": "text"}],
        }
        valid, errors = validate_review(data)
        assert valid is True
