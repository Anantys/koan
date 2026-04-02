"""Tests for the /brainstorm core skill — handler + runner."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Import handler functions
# ---------------------------------------------------------------------------

import importlib.util

HANDLER_PATH = Path(__file__).parent.parent / "skills" / "core" / "brainstorm" / "handler.py"
SKILL_DIR = Path(__file__).parent.parent / "skills" / "core" / "brainstorm"


def _load_handler():
    """Load the brainstorm handler module."""
    spec = importlib.util.spec_from_file_location("brainstorm_handler", str(HANDLER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def handler():
    return _load_handler()


@pytest.fixture
def ctx(tmp_path):
    """Create a basic SkillContext for tests."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    missions_path = instance_dir / "missions.md"
    missions_path.write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name="brainstorm",
        args="",
        send_message=MagicMock(),
    )


# ---------------------------------------------------------------------------
# handle() — usage / routing
# ---------------------------------------------------------------------------

class TestHandleRouting:
    def test_no_args_returns_usage(self, handler, ctx):
        result = handler.handle(ctx)
        assert "Usage:" in result
        assert "/brainstorm" in result
        assert "--tag" in result

    def test_routes_to_brainstorm(self, handler, ctx):
        ctx.args = "Improve caching strategy"
        with patch.object(handler, "_queue_brainstorm", return_value="queued") as mock:
            handler.handle(ctx)
            mock.assert_called_once()

    def test_routes_with_tag(self, handler, ctx):
        ctx.args = "Improve caching --tag prompt-caching"
        with patch.object(handler, "_queue_brainstorm", return_value="queued") as mock:
            handler.handle(ctx)
            mock.assert_called_once()
            # The mission text should contain --tag
            call_args = mock.call_args[0]
            assert "--tag prompt-caching" in call_args[2]  # mission_text

    def test_routes_project_prefixed(self, handler, ctx):
        ctx.args = "koan Improve caching"
        with patch.object(handler, "_queue_brainstorm", return_value="queued") as mock, \
             patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            handler.handle(ctx)
            mock.assert_called_once()

    def test_empty_topic_returns_error(self, handler, ctx):
        ctx.args = "   "
        result = handler.handle(ctx)
        assert "Usage:" in result


# ---------------------------------------------------------------------------
# _extract_tag
# ---------------------------------------------------------------------------

class TestExtractTag:
    def test_no_tag(self, handler):
        tag, remaining = handler._extract_tag("Improve caching")
        assert tag is None
        assert remaining == "Improve caching"

    def test_tag_at_end(self, handler):
        tag, remaining = handler._extract_tag("Improve caching --tag prompt-caching")
        assert tag == "prompt-caching"
        assert remaining == "Improve caching"

    def test_tag_in_middle(self, handler):
        tag, remaining = handler._extract_tag("Improve --tag cache-fix caching strategy")
        assert tag == "cache-fix"
        assert "caching strategy" in remaining

    def test_tag_with_hyphenated_value(self, handler):
        tag, remaining = handler._extract_tag("Topic --tag my-long-tag")
        assert tag == "my-long-tag"


# ---------------------------------------------------------------------------
# _parse_project_arg
# ---------------------------------------------------------------------------

class TestParseProjectArg:
    def test_no_project_prefix(self, handler):
        with patch("app.utils.get_known_projects", return_value=[]):
            project, topic = handler._parse_project_arg("Improve caching")
            assert project is None
            assert topic == "Improve caching"

    def test_project_tag_format(self, handler):
        project, topic = handler._parse_project_arg("[project:koan] Improve caching")
        assert project == "koan"
        assert topic == "Improve caching"

    def test_project_name_prefix(self, handler):
        with patch("app.utils.get_known_projects",
                    return_value=[("koan", "/path")]):
            project, topic = handler._parse_project_arg("koan Improve caching")
            assert project == "koan"
            assert topic == "Improve caching"

    def test_unknown_project_treated_as_topic(self, handler):
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            project, topic = handler._parse_project_arg("webapp Improve caching")
            assert project is None
            assert topic == "webapp Improve caching"


# ---------------------------------------------------------------------------
# _queue_brainstorm — mission queuing
# ---------------------------------------------------------------------------

class TestQueueBrainstorm:
    def test_queues_mission(self, handler, ctx):
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path/koan")]):
            result = handler._queue_brainstorm(
                ctx, "koan", "/brainstorm Improve caching", "Improve caching",
            )
            assert "queued" in result.lower()
            missions = (ctx.instance_dir / "missions.md").read_text()
            assert "/brainstorm Improve caching" in missions
            assert "[project:koan]" in missions

    def test_unknown_project_returns_error(self, handler, ctx):
        with patch("app.utils.get_known_projects", return_value=[("koan", "/path")]):
            result = handler._queue_brainstorm(
                ctx, "unknown", "/brainstorm Topic", "Topic",
            )
            assert "not found" in result

    def test_tag_preserved_in_mission(self, handler, ctx):
        with patch("app.utils.get_known_projects", return_value=[("koan", "/p")]):
            handler._queue_brainstorm(
                ctx, "koan",
                "/brainstorm Improve caching --tag prompt-caching",
                "Improve caching",
            )
            missions = (ctx.instance_dir / "missions.md").read_text()
            assert "--tag prompt-caching" in missions


# ---------------------------------------------------------------------------
# SKILL.md — structure validation
# ---------------------------------------------------------------------------

class TestSkillMd:
    def test_skill_md_parses(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(SKILL_DIR / "SKILL.md")
        assert skill is not None
        assert skill.name == "brainstorm"
        assert skill.scope == "core"
        assert len(skill.commands) == 1
        assert skill.commands[0].name == "brainstorm"

    def test_no_worker_flag(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(SKILL_DIR / "SKILL.md")
        assert skill.worker is False

    def test_github_enabled(self):
        from app.skills import parse_skill_md
        skill = parse_skill_md(SKILL_DIR / "SKILL.md")
        assert skill.github_enabled is True

    def test_skill_registered_in_registry(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("brainstorm")
        assert skill is not None
        assert skill.name == "brainstorm"

    def test_skill_handler_exists(self):
        assert HANDLER_PATH.exists()


# ---------------------------------------------------------------------------
# Decompose prompt
# ---------------------------------------------------------------------------

PROMPT_PATH = SKILL_DIR / "prompts" / "decompose.md"


class TestDecomposePrompt:
    def test_prompt_file_exists(self):
        assert PROMPT_PATH.exists()

    def test_prompt_has_placeholder(self):
        content = PROMPT_PATH.read_text()
        assert "{TOPIC}" in content

    def test_prompt_requests_json(self):
        content = PROMPT_PATH.read_text()
        assert "JSON" in content
        assert "master_summary" in content
        assert "issues" in content


# ---------------------------------------------------------------------------
# brainstorm_runner — unit tests
# ---------------------------------------------------------------------------

from skills.core.brainstorm.brainstorm_runner import (
    _generate_tag,
    _parse_decomposition,
    _build_master_body,
    _extract_master_title,
    _apply_sub_replacements,
    _replace_sub_placeholders,
)


class TestGenerateTag:
    def test_basic_topic(self):
        tag = _generate_tag("Improve caching strategy for API responses")
        assert tag == "improve-caching-strategy-api"

    def test_strips_stop_words(self):
        tag = _generate_tag("Add the new feature to the system")
        assert "the" not in tag.split("-")
        assert "to" not in tag.split("-")

    def test_max_four_words(self):
        tag = _generate_tag("one two three four five six seven")
        assert len(tag.split("-")) <= 4

    def test_empty_topic(self):
        tag = _generate_tag("the a an is")
        assert tag == "brainstorm"

    def test_kebab_case(self):
        tag = _generate_tag("Prompt Caching Strategy")
        assert "-" in tag
        assert tag == tag.lower()


class TestParseDecomposition:
    def test_valid_json(self):
        raw = json.dumps({
            "master_summary": "Overview of the initiative.",
            "issues": [
                {"title": "Issue 1", "body": "Body 1"},
                {"title": "Issue 2", "body": "Body 2"},
                {"title": "Issue 3", "body": "Body 3"},
            ]
        })
        data = _parse_decomposition(raw)
        assert len(data["issues"]) == 3
        assert data["master_summary"] == "Overview of the initiative."

    def test_json_with_markdown_fences(self):
        raw = "```json\n" + json.dumps({
            "master_summary": "Summary",
            "issues": [{"title": "T", "body": "B"}],
        }) + "\n```"
        data = _parse_decomposition(raw)
        assert len(data["issues"]) == 1

    def test_json_with_preamble(self):
        raw = "Here is the decomposition:\n\n" + json.dumps({
            "master_summary": "S",
            "issues": [{"title": "T", "body": "B"}],
        })
        data = _parse_decomposition(raw)
        assert len(data["issues"]) == 1

    def test_empty_output_raises(self):
        with pytest.raises(ValueError, match="Empty output"):
            _parse_decomposition("")

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON"):
            _parse_decomposition("Just some text without JSON")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            _parse_decomposition("{invalid json}")

    def test_missing_issues_key_raises(self):
        with pytest.raises(ValueError, match="Missing 'issues'"):
            _parse_decomposition(json.dumps({"master_summary": "x"}))

    def test_missing_title_raises(self):
        with pytest.raises(ValueError, match="missing 'title' or 'body'"):
            _parse_decomposition(json.dumps({
                "master_summary": "x",
                "issues": [{"body": "no title"}],
            }))

    def test_default_master_summary(self):
        data = _parse_decomposition(json.dumps({
            "issues": [{"title": "T", "body": "B"}],
        }))
        assert data["master_summary"] == ""


class TestBuildMasterBody:
    def test_contains_task_list(self):
        issues = [("1", "Title One", "url1"), ("2", "Title Two", "url2")]
        body = _build_master_body("Topic", "Summary", issues, "owner", "repo")
        assert "- [ ] #1" in body
        assert "- [ ] #2" in body
        assert "Title One" in body
        assert "Title Two" in body

    def test_contains_topic(self):
        body = _build_master_body("My topic", "", [("1", "T", "u")], "o", "r")
        assert "My topic" in body

    def test_contains_summary(self):
        body = _build_master_body("T", "My summary", [("1", "T", "u")], "o", "r")
        assert "My summary" in body

    def test_footer(self):
        body = _build_master_body("T", "", [("1", "T", "u")], "o", "r")
        assert "Koan /brainstorm" in body


class TestApplySubReplacements:
    def test_replaces_sub_placeholders(self):
        mapping = {1: "42", 2: "43", 3: "44"}
        text = "Depends on SUB-1 and SUB-2. See also SUB-3."
        result = _apply_sub_replacements(text, mapping)
        assert result == "Depends on #42 and #43. See also #44."

    def test_leaves_unknown_placeholders(self):
        mapping = {1: "42"}
        text = "Depends on SUB-1 and SUB-5."
        result = _apply_sub_replacements(text, mapping)
        assert "#42" in result
        assert "SUB-5" in result

    def test_no_placeholders_unchanged(self):
        mapping = {1: "42"}
        text = "No cross-references here."
        result = _apply_sub_replacements(text, mapping)
        assert result == text

    def test_multiple_occurrences_of_same_placeholder(self):
        mapping = {1: "99"}
        text = "SUB-1 is needed before SUB-1 can be tested."
        result = _apply_sub_replacements(text, mapping)
        assert result == "#99 is needed before #99 can be tested."

    def test_preserves_existing_hash_references(self):
        """Real GitHub #N references in the text should not be touched."""
        mapping = {1: "42"}
        text = "This fixes #10. Depends on SUB-1."
        result = _apply_sub_replacements(text, mapping)
        assert "#10" in result
        assert "#42" in result


class TestReplaceSubPlaceholders:
    def test_calls_issue_edit_for_changed_bodies(self):
        created = [("42", "Title A", "url1"), ("43", "Title B", "url2")]
        original = [
            {"title": "Title A", "body": "Depends on SUB-2."},
            {"title": "Title B", "body": "No deps."},
        ]
        with patch("skills.core.brainstorm.brainstorm_runner.issue_edit") as mock_edit:
            _replace_sub_placeholders(created, original, "/fake")
            # Only issue 42 had a placeholder that changed
            mock_edit.assert_called_once_with("42", "Depends on #43.", cwd="/fake")

    def test_skips_edit_when_no_placeholders(self):
        created = [("10", "T", "u")]
        original = [{"title": "T", "body": "No placeholders here."}]
        with patch("skills.core.brainstorm.brainstorm_runner.issue_edit") as mock_edit:
            _replace_sub_placeholders(created, original, "/fake")
            mock_edit.assert_not_called()

    def test_handles_edit_failure_gracefully(self):
        created = [("42", "T", "u"), ("43", "T2", "u2")]
        original = [
            {"title": "T", "body": "See SUB-2"},
            {"title": "T2", "body": "See SUB-1"},
        ]
        with patch("skills.core.brainstorm.brainstorm_runner.issue_edit",
                    side_effect=RuntimeError("API error")):
            # Should not raise — errors are caught and logged
            _replace_sub_placeholders(created, original, "/fake")


class TestExtractMasterTitle:
    def test_short_topic(self):
        assert _extract_master_title("Fix caching") == "Fix caching"

    def test_long_topic_truncated(self):
        long = "A" * 200
        result = _extract_master_title(long)
        assert len(result) <= 100
        assert result.endswith("...")

    def test_first_sentence(self):
        result = _extract_master_title("Fix caching. Then do more stuff.")
        assert result == "Fix caching"

    def test_empty_topic(self):
        assert _extract_master_title("") == "Brainstorm"


# ---------------------------------------------------------------------------
# skill_dispatch integration
# ---------------------------------------------------------------------------

class TestSkillDispatch:
    def test_brainstorm_in_skill_runners(self):
        from app.skill_dispatch import _SKILL_RUNNERS
        assert "brainstorm" in _SKILL_RUNNERS
        assert _SKILL_RUNNERS["brainstorm"] == "skills.core.brainstorm.brainstorm_runner"

    def test_build_brainstorm_cmd_basic(self):
        from app.skill_dispatch import _build_brainstorm_cmd
        import sys
        base_cmd = [sys.executable, "-m", "skills.core.brainstorm.brainstorm_runner"]
        cmd = _build_brainstorm_cmd(base_cmd, "Improve caching", "/project/path")
        assert "--project-path" in cmd
        assert "/project/path" in cmd
        assert "--topic" in cmd
        assert "Improve caching" in cmd
        assert "--tag" not in cmd

    def test_build_brainstorm_cmd_with_tag(self):
        from app.skill_dispatch import _build_brainstorm_cmd
        import sys
        base_cmd = [sys.executable, "-m", "skills.core.brainstorm.brainstorm_runner"]
        cmd = _build_brainstorm_cmd(
            base_cmd, "Improve caching --tag prompt-caching", "/p",
        )
        assert "--tag" in cmd
        assert "prompt-caching" in cmd
        # Topic should not contain --tag
        topic_idx = cmd.index("--topic")
        topic_value = cmd[topic_idx + 1]
        assert "--tag" not in topic_value

    def test_is_skill_mission(self):
        from app.skill_dispatch import is_skill_mission
        assert is_skill_mission("/brainstorm Improve caching")
        assert is_skill_mission("[project:koan] /brainstorm Topic")

    def test_parse_skill_mission(self):
        from app.skill_dispatch import parse_skill_mission
        project, cmd, args = parse_skill_mission(
            "[project:koan] /brainstorm Improve caching --tag cache"
        )
        assert project == "koan"
        assert cmd == "brainstorm"
        assert "Improve caching --tag cache" in args
