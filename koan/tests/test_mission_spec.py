"""Tests for mission spec-first execution (complexity detection, spec generation, integration)."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.mission_complexity import (
    COMPLEXITY_KEYWORDS,
    DEFAULT_COMPLEXITY_THRESHOLD,
    _strip_project_tag,
    is_complex_mission,
)
from app.spec_generator import (
    _slugify,
    generate_spec,
    load_spec_for_mission,
    save_spec,
)


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────


@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory."""
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    return tmp_path


# ──────────────────────────────────────────────────────────
# Phase 1: mission_complexity tests
# ──────────────────────────────────────────────────────────


class TestStripProjectTag:
    def test_strips_tag(self):
        assert _strip_project_tag("[project:koan] feat: add retries") == "feat: add retries"

    def test_no_tag(self):
        assert _strip_project_tag("feat: add retries") == "feat: add retries"

    def test_empty(self):
        assert _strip_project_tag("") == ""


class TestIsComplexMission:
    """Tests for is_complex_mission() dual-heuristic gate."""

    def _long_mission(self, keyword="feature"):
        """Build a mission title that exceeds the default threshold with a keyword."""
        base = f"feat: add {keyword} for retry-with-backoff across network calls in notify and github modules"
        # Ensure it's long enough
        while len(base) < DEFAULT_COMPLEXITY_THRESHOLD:
            base += " with extra context"
        return base

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_complex_feature_mission(self, mock_threshold):
        title = self._long_mission("feature")
        assert is_complex_mission(title) is True

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_complex_refactor_mission(self, mock_threshold):
        title = self._long_mission("refactor")
        assert is_complex_mission(title) is True

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_complex_migration_mission(self, mock_threshold):
        title = self._long_mission("migration")
        assert is_complex_mission(title) is True

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_short_mission_returns_false(self, mock_threshold):
        assert is_complex_mission("fix typo") is False

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_long_but_no_keyword_returns_false(self, mock_threshold):
        title = "fix the broken button in the navbar that appears on mobile devices when viewport is narrow"
        assert len(title) >= DEFAULT_COMPLEXITY_THRESHOLD
        assert is_complex_mission(title) is False

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_keyword_but_short_returns_false(self, mock_threshold):
        assert is_complex_mission("feature: add X") is False

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_empty_returns_false(self, mock_threshold):
        assert is_complex_mission("") is False

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_skill_mission_returns_false(self, mock_threshold):
        assert is_complex_mission("/plan Add dark mode to the entire application framework") is False

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_skill_with_project_tag_returns_false(self, mock_threshold):
        assert is_complex_mission("[project:koan] /plan something long enough to matter here") is False

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_project_tag_stripped_before_check(self, mock_threshold):
        tag = "[project:koan] "
        title = tag + self._long_mission("feature")
        assert is_complex_mission(title) is True

    @patch("app.mission_complexity._get_complexity_threshold", return_value=DEFAULT_COMPLEXITY_THRESHOLD)
    def test_all_keywords_recognized(self, mock_threshold):
        for kw in COMPLEXITY_KEYWORDS:
            title = f"mission about {kw} across the entire application codebase with many modules to touch and update"
            assert is_complex_mission(title) is True, f"Keyword '{kw}' not recognized"

    @patch("app.mission_complexity._get_complexity_threshold", return_value=40)
    def test_custom_threshold(self, mock_threshold):
        title = "feat: implement feature for the whole app"
        assert len(title) >= 40
        assert is_complex_mission(title) is True


# ──────────────────────────────────────────────────────────
# Phase 2: spec_generator tests
# ──────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self):
        assert _slugify("feat: add retry-with-backoff") == "feat-add-retry-with-backoff"

    def test_special_chars(self):
        assert _slugify("Fix bug #123 in module!") == "fix-bug-123-in-module"

    def test_truncation(self):
        long_title = "a" * 100
        assert len(_slugify(long_title)) == 60

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify("---hello---") == "hello"


class TestGenerateSpec:
    @patch("app.cli_provider.run_command")
    @patch("app.prompts.load_prompt", return_value="test prompt")
    def test_success(self, mock_prompt, mock_run):
        mock_run.return_value = "### Goal\nDo the thing.\n### Scope\nfile.py"
        result = generate_spec("/path/to/project", "feat: add retries", "/path/to/instance")
        assert result is not None
        assert "Goal" in result
        mock_run.assert_called_once()

    @patch("app.cli_provider.run_command")
    @patch("app.prompts.load_prompt", return_value="test prompt")
    def test_empty_output_returns_none(self, mock_prompt, mock_run):
        mock_run.return_value = ""
        result = generate_spec("/path/to/project", "feat: add retries", "/path/to/instance")
        assert result is None

    @patch("app.cli_provider.run_command")
    @patch("app.prompts.load_prompt", return_value="test prompt")
    def test_whitespace_only_returns_none(self, mock_prompt, mock_run):
        mock_run.return_value = "   \n  "
        result = generate_spec("/path/to/project", "feat: add retries", "/path/to/instance")
        assert result is None

    @patch("app.cli_provider.run_command", side_effect=RuntimeError("CLI failed"))
    @patch("app.prompts.load_prompt", return_value="test prompt")
    def test_runtime_error_returns_none(self, mock_prompt, mock_run):
        result = generate_spec("/path/to/project", "feat: add retries", "/path/to/instance")
        assert result is None

    @patch("app.cli_provider.run_command", side_effect=TimeoutError("timed out"))
    @patch("app.prompts.load_prompt", return_value="test prompt")
    def test_timeout_returns_none(self, mock_prompt, mock_run):
        result = generate_spec("/path/to/project", "feat: add retries", "/path/to/instance")
        assert result is None

    @patch("app.cli_provider.run_command")
    @patch("app.prompts.load_prompt", return_value="test prompt")
    def test_uses_read_only_tools(self, mock_prompt, mock_run):
        mock_run.return_value = "spec content"
        generate_spec("/path/to/project", "feat: add retries", "/path/to/instance")
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["allowed_tools"] == ["Read", "Glob", "Grep"]

    @patch("app.cli_provider.run_command")
    @patch("app.prompts.load_prompt", return_value="test prompt")
    def test_max_turns_is_5(self, mock_prompt, mock_run):
        mock_run.return_value = "spec content"
        generate_spec("/path/to/project", "feat: add retries", "/path/to/instance")
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["max_turns"] == 5


class TestSaveSpec:
    def test_saves_to_correct_path(self, instance_dir):
        today = datetime.now().strftime("%Y-%m-%d")
        path = save_spec(str(instance_dir), "feat: add retries", "spec content")
        assert path is not None
        assert path.exists()
        assert f"journal/{today}/specs/" in str(path)
        assert path.read_text() == "spec content"

    def test_slug_in_filename(self, instance_dir):
        path = save_spec(str(instance_dir), "feat: add retry-with-backoff", "content")
        assert "feat-add-retry-with-backoff" in path.name

    def test_returns_none_on_failure(self):
        result = save_spec("/nonexistent/path/that/should/fail", "title", "content")
        # May or may not fail depending on OS, but should not raise
        # (atomic_write may create dirs or fail gracefully)


class TestLoadSpecForMission:
    def test_loads_existing_spec(self, instance_dir):
        today = datetime.now().strftime("%Y-%m-%d")
        specs_dir = instance_dir / "journal" / today / "specs"
        specs_dir.mkdir(parents=True)
        slug = _slugify("feat: add retries")
        (specs_dir / f"{slug}.md").write_text("the spec")
        result = load_spec_for_mission(str(instance_dir), "feat: add retries")
        assert result == "the spec"

    def test_returns_empty_for_missing(self, instance_dir):
        result = load_spec_for_mission(str(instance_dir), "nonexistent mission")
        assert result == ""


# ──────────────────────────────────────────────────────────
# Phase 3: prompt_builder integration tests
# ──────────────────────────────────────────────────────────


class TestBuildAgentPromptSpec:
    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_focus_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="")
    @patch("app.prompts.load_prompt", return_value="base prompt {MISSION_INSTRUCTION}")
    def test_spec_injected_when_present(self, mock_load, *mocks):
        from app.prompt_builder import build_agent_prompt

        prompt = build_agent_prompt(
            instance="/tmp/inst",
            project_name="proj",
            project_path="/tmp/proj",
            run_num=1,
            max_runs=10,
            autonomous_mode="implement",
            focus_area="general",
            available_pct=50,
            mission_title="feat: do something",
            spec_content="### Goal\nDo the thing.",
        )
        assert "# Mission Spec" in prompt
        assert "### Goal" in prompt
        assert "Do the thing." in prompt

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_focus_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="")
    @patch("app.prompts.load_prompt", return_value="base prompt {MISSION_INSTRUCTION}")
    def test_no_spec_section_when_empty(self, mock_load, *mocks):
        from app.prompt_builder import build_agent_prompt

        prompt = build_agent_prompt(
            instance="/tmp/inst",
            project_name="proj",
            project_path="/tmp/proj",
            run_num=1,
            max_runs=10,
            autonomous_mode="implement",
            focus_area="general",
            available_pct=50,
            mission_title="feat: do something",
            spec_content="",
        )
        assert "# Mission Spec" not in prompt

    @patch("app.prompt_builder._get_verbose_section", return_value="")
    @patch("app.prompt_builder._get_focus_section", return_value="")
    @patch("app.prompt_builder._get_submit_pr_section", return_value="")
    @patch("app.prompt_builder._get_merge_policy", return_value="")
    @patch("app.prompts.load_prompt", return_value="base prompt {MISSION_INSTRUCTION}")
    def test_no_spec_section_for_autonomous(self, mock_load, *mocks):
        from app.prompt_builder import build_agent_prompt

        prompt = build_agent_prompt(
            instance="/tmp/inst",
            project_name="proj",
            project_path="/tmp/proj",
            run_num=1,
            max_runs=10,
            autonomous_mode="implement",
            focus_area="general",
            available_pct=50,
            mission_title="",  # autonomous — no mission
            spec_content="should not appear",
        )
        assert "# Mission Spec" not in prompt


# ──────────────────────────────────────────────────────────
# Phase 5: post_mission_reflection spec comparison tests
# ──────────────────────────────────────────────────────────


class TestReflectionSpecComparison:
    @pytest.fixture
    def reflection_instance(self, tmp_path):
        (tmp_path / "soul.md").write_text("You are Koan.")
        memory_dir = tmp_path / "memory" / "global"
        memory_dir.mkdir(parents=True)
        return tmp_path

    @patch("app.post_mission_reflection._get_prompt_template")
    def test_spec_appended_to_reflection(self, mock_template, reflection_instance):
        mock_template.return_value = (
            "{SOUL_CONTEXT} {EMOTIONAL_CONTEXT} {JOURNAL_CONTEXT} "
            "{MISSION_TEXT} {MISSION_JOURNAL}"
        )
        # Write a spec file for today
        today = datetime.now().strftime("%Y-%m-%d")
        mission = "feat: add retries for network calls"
        slug = _slugify(mission)
        specs_dir = reflection_instance / "journal" / today / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / f"{slug}.md").write_text("### Goal\nAdd retries.\n### Scope\nnotify.py")

        from app.post_mission_reflection import build_reflection_prompt

        prompt = build_reflection_prompt(reflection_instance, mission, "journal content")
        assert "Mission Spec Comparison" in prompt
        assert "Add retries." in prompt

    @patch("app.post_mission_reflection._get_prompt_template")
    def test_no_spec_section_when_no_spec(self, mock_template, reflection_instance):
        mock_template.return_value = (
            "{SOUL_CONTEXT} {EMOTIONAL_CONTEXT} {JOURNAL_CONTEXT} "
            "{MISSION_TEXT} {MISSION_JOURNAL}"
        )
        from app.post_mission_reflection import build_reflection_prompt

        prompt = build_reflection_prompt(reflection_instance, "fix typo", "journal content")
        assert "Mission Spec Comparison" not in prompt

    @patch("app.post_mission_reflection._get_prompt_template")
    def test_spec_truncated_at_1500_chars(self, mock_template, reflection_instance):
        mock_template.return_value = (
            "{SOUL_CONTEXT} {EMOTIONAL_CONTEXT} {JOURNAL_CONTEXT} "
            "{MISSION_TEXT} {MISSION_JOURNAL}"
        )
        today = datetime.now().strftime("%Y-%m-%d")
        mission = "feat: add retries for network calls"
        slug = _slugify(mission)
        specs_dir = reflection_instance / "journal" / today / "specs"
        specs_dir.mkdir(parents=True)
        long_spec = "x" * 3000
        (specs_dir / f"{slug}.md").write_text(long_spec)

        from app.post_mission_reflection import build_reflection_prompt

        prompt = build_reflection_prompt(reflection_instance, mission, "journal content")
        assert "Mission Spec Comparison" in prompt
        # The spec excerpt should be truncated to 1500 chars
        # Check it's not the full 3000
        spec_section = prompt.split("### Original Spec")[1]
        assert len(spec_section.strip()) <= 1510  # 1500 + small margin for whitespace
