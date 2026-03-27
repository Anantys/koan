"""
Kōan -- JSON schema definitions for structured review output.

Defines two focused schemas for code reviews:
1. FILE_COMMENTS_SCHEMA — per-file inline comments with severity
2. REVIEW_SUMMARY_SCHEMA — overall review summary with checklist

All fields are required with explicit sentinel values (empty arrays,
empty strings, False) instead of optional/nullable fields.
"""

# ---------------------------------------------------------------------------
# Schema: file_comments
# ---------------------------------------------------------------------------

FILE_COMMENTS_SCHEMA = {
    "type": "array",
    "description": "Array of per-file inline review comments.",
    "items": {
        "type": "object",
        "required": [
            "file", "line_start", "line_end", "severity",
            "title", "comment", "code_snippet",
        ],
        "properties": {
            "file": {
                "type": "string",
                "description": "File path as shown in the diff (e.g. 'src/auth.py').",
            },
            "line_start": {
                "type": "integer",
                "description": (
                    "First line number in the diff where the issue starts. "
                    "Use 0 if the comment applies to the whole file."
                ),
            },
            "line_end": {
                "type": "integer",
                "description": (
                    "Last line number in the diff where the issue ends. "
                    "Same as line_start for single-line issues. Use 0 if whole-file."
                ),
            },
            "severity": {
                "type": "string",
                "description": (
                    "Severity level. Must be one of: "
                    "'critical' (blocking, must fix before merge), "
                    "'warning' (important, should fix), "
                    "'suggestion' (nice to have, non-blocking)."
                ),
            },
            "title": {
                "type": "string",
                "description": "Short title summarizing the issue (e.g. 'Missing input validation').",
            },
            "comment": {
                "type": "string",
                "description": "Detailed explanation of the issue and suggested fix.",
            },
            "code_snippet": {
                "type": "string",
                "description": (
                    "Relevant code snippet illustrating the issue. "
                    "Use empty string if no snippet is needed."
                ),
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Schema: review_summary
# ---------------------------------------------------------------------------

REVIEW_SUMMARY_SCHEMA = {
    "type": "object",
    "description": "Overall review summary with checklist results.",
    "required": ["lgtm", "summary", "checklist"],
    "properties": {
        "lgtm": {
            "type": "boolean",
            "description": (
                "True if the PR is merge-ready with no blocking issues. "
                "False if there are critical or warning-level findings."
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "Final assessment paragraph — what's good, what needs fixing, "
                "and whether it's merge-ready after addressing blocking items."
            ),
        },
        "checklist": {
            "type": "array",
            "description": (
                "Review checklist results. Empty array if the PR is too trivial "
                "for a checklist (1-3 lines, typos, config changes)."
            ),
            "items": {
                "type": "object",
                "required": ["item", "passed", "finding_ref"],
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Checklist item description (e.g. 'No hardcoded secrets').",
                    },
                    "passed": {
                        "type": "boolean",
                        "description": "True if the check passed, False if it failed.",
                    },
                    "finding_ref": {
                        "type": "string",
                        "description": (
                            "Cross-reference to the related finding "
                            "(e.g. 'critical #1'). Empty string if passed."
                        ),
                    },
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Schema: comment_replies (optional)
# ---------------------------------------------------------------------------

COMMENT_REPLY_SCHEMA = {
    "type": "object",
    "required": ["comment_id", "reply"],
    "properties": {
        "comment_id": {
            "type": "integer",
            "description": (
                "The GitHub comment ID to reply to, as provided in the "
                "repliable comments list."
            ),
        },
        "reply": {
            "type": "string",
            "description": (
                "The reply text. Be complete and detailed — explain the why "
                "and how, not just the what. Keep the tone constructive."
            ),
        },
    },
}

COMMENT_REPLIES_SCHEMA = {
    "type": "array",
    "description": (
        "Replies to user comments and questions on the PR. "
        "Only include replies that add value — skip acknowledgements "
        "and trivial responses."
    ),
    "items": COMMENT_REPLY_SCHEMA,
}

# ---------------------------------------------------------------------------
# Combined review schema (top-level object)
# ---------------------------------------------------------------------------

PLAN_ALIGNMENT_SCHEMA = {
    "type": "object",
    "description": (
        "Optional plan alignment findings. Present only when the review was "
        "performed against a plan (via --plan-url or auto-detection)."
    ),
    "properties": {
        "requirements_met": {
            "type": "array",
            "description": "Plan requirements that are implemented in the diff.",
            "items": {"type": "string"},
        },
        "requirements_missing": {
            "type": "array",
            "description": "Plan requirements not found or incomplete in the diff.",
            "items": {"type": "string"},
        },
        "out_of_scope": {
            "type": "array",
            "description": (
                "Changes in the diff not mentioned in the plan "
                "(neutral observation — not necessarily bad)."
            ),
            "items": {"type": "string"},
        },
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "description": "Complete structured review output.",
    "required": ["file_comments", "review_summary"],
    "properties": {
        "file_comments": FILE_COMMENTS_SCHEMA,
        "review_summary": REVIEW_SUMMARY_SCHEMA,
        "comment_replies": COMMENT_REPLIES_SCHEMA,
        "plan_alignment": PLAN_ALIGNMENT_SCHEMA,
    },
}

# Valid severity values
_VALID_SEVERITIES = {"critical", "warning", "suggestion"}


def validate_review(data: object) -> tuple:
    """Validate review data against the expected schema.

    Returns:
        (is_valid, errors) where errors is a list of human-readable strings.
        Empty errors list when valid.
    """
    errors: list = []

    if not isinstance(data, dict):
        return False, ["Root must be a JSON object"]

    # -- file_comments --
    if "file_comments" not in data:
        errors.append("Missing required field: 'file_comments'")
    else:
        fc = data["file_comments"]
        if not isinstance(fc, list):
            errors.append("'file_comments' must be an array")
        else:
            for i, item in enumerate(fc):
                errors.extend(_validate_file_comment(item, i))

    # -- review_summary --
    if "review_summary" not in data:
        errors.append("Missing required field: 'review_summary'")
    else:
        rs = data["review_summary"]
        if not isinstance(rs, dict):
            errors.append("'review_summary' must be an object")
        else:
            errors.extend(_validate_review_summary(rs))

    # -- comment_replies (optional) --
    if "comment_replies" in data:
        cr = data["comment_replies"]
        if not isinstance(cr, list):
            errors.append("'comment_replies' must be an array")
        else:
            for i, item in enumerate(cr):
                errors.extend(_validate_comment_reply(item, i))

    # -- plan_alignment (optional) --
    if "plan_alignment" in data:
        pa = data["plan_alignment"]
        if not isinstance(pa, dict):
            errors.append("'plan_alignment' must be an object")
        else:
            for key in ("requirements_met", "requirements_missing", "out_of_scope"):
                if key in pa and not isinstance(pa[key], list):
                    errors.append(f"plan_alignment.{key}: must be an array")

    return (len(errors) == 0, errors)


def _validate_file_comment(item: object, index: int) -> list:
    """Validate a single file_comments entry."""
    errors: list = []
    prefix = f"file_comments[{index}]"

    if not isinstance(item, dict):
        return [f"{prefix}: must be an object"]

    required = {
        "file": str,
        "line_start": int,
        "line_end": int,
        "severity": str,
        "title": str,
        "comment": str,
        "code_snippet": str,
    }
    for field, expected_type in required.items():
        if field not in item:
            errors.append(f"{prefix}: missing required field '{field}'")
        elif not isinstance(item[field], expected_type):
            # Allow int-like floats (JSON has no int type)
            if expected_type is int and isinstance(item[field], float) and item[field] == int(item[field]):
                continue
            errors.append(f"{prefix}.{field}: expected {expected_type.__name__}, got {type(item[field]).__name__}")

    if "severity" in item and isinstance(item["severity"], str):
        if item["severity"] not in _VALID_SEVERITIES:
            errors.append(
                f"{prefix}.severity: must be one of {sorted(_VALID_SEVERITIES)}, "
                f"got '{item['severity']}'"
            )

    return errors


def _validate_review_summary(rs: dict) -> list:
    """Validate the review_summary object."""
    errors: list = []

    if "lgtm" not in rs:
        errors.append("review_summary: missing required field 'lgtm'")
    elif not isinstance(rs["lgtm"], bool):
        errors.append(f"review_summary.lgtm: expected bool, got {type(rs['lgtm']).__name__}")

    if "summary" not in rs:
        errors.append("review_summary: missing required field 'summary'")
    elif not isinstance(rs["summary"], str):
        errors.append(f"review_summary.summary: expected str, got {type(rs['summary']).__name__}")

    if "checklist" not in rs:
        errors.append("review_summary: missing required field 'checklist'")
    elif not isinstance(rs["checklist"], list):
        errors.append("review_summary.checklist: must be an array")
    else:
        for i, item in enumerate(rs["checklist"]):
            errors.extend(_validate_checklist_item(item, i))

    return errors


def _validate_comment_reply(item: object, index: int) -> list:
    """Validate a single comment_replies entry."""
    errors: list = []
    prefix = f"comment_replies[{index}]"

    if not isinstance(item, dict):
        return [f"{prefix}: must be an object"]

    required = {"comment_id": int, "reply": str}
    for field, expected_type in required.items():
        if field not in item:
            errors.append(f"{prefix}: missing required field '{field}'")
        elif not isinstance(item[field], expected_type):
            if expected_type is int and isinstance(item[field], float) and item[field] == int(item[field]):
                continue
            errors.append(f"{prefix}.{field}: expected {expected_type.__name__}, got {type(item[field]).__name__}")

    return errors


def _validate_checklist_item(item: object, index: int) -> list:
    """Validate a single checklist entry."""
    errors: list = []
    prefix = f"review_summary.checklist[{index}]"

    if not isinstance(item, dict):
        return [f"{prefix}: must be an object"]

    required = {"item": str, "passed": bool, "finding_ref": str}
    for field, expected_type in required.items():
        if field not in item:
            errors.append(f"{prefix}: missing required field '{field}'")
        elif not isinstance(item[field], expected_type):
            errors.append(f"{prefix}.{field}: expected {expected_type.__name__}, got {type(item[field]).__name__}")

    return errors
