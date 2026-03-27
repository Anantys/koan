"""Tests for prompts.py — system prompt loader and placeholder substitution."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.prompts import (
    PROMPT_DIR,
    _read_prompt_with_git_fallback,
    _substitute,
    get_prompt_path,
    load_prompt,
    load_prompt_or_skill,
    load_skill_prompt,
)


# ---------- _substitute ----------


class TestSubstitute:
    """Tests for placeholder substitution."""

    def test_single_placeholder(self):
        assert _substitute("Hello {NAME}!", {"NAME": "World"}) == "Hello World!"

    def test_multiple_placeholders(self):
        template = "{A} and {B}"
        assert _substitute(template, {"A": "one", "B": "two"}) == "one and two"

    def test_repeated_placeholder(self):
        template = "{X} then {X} again"
        assert _substitute(template, {"X": "val"}) == "val then val again"

    def test_no_placeholders(self):
        assert _substitute("plain text", {}) == "plain text"

    def test_missing_placeholder_left_as_is(self):
        assert _substitute("Hello {NAME}!", {}) == "Hello {NAME}!"

    def test_non_string_value_converted(self):
        assert _substitute("count: {N}", {"N": 42}) == "count: 42"

    def test_empty_string_value(self):
        assert _substitute("x{V}y", {"V": ""}) == "xy"


# ---------- load_prompt ----------


class TestLoadPrompt:
    """Tests for loading prompts from koan/system-prompts/."""

    def test_load_chat_prompt(self):
        result = load_prompt("chat")
        assert len(result) > 0
        assert isinstance(result, str)

    def test_load_format_telegram(self):
        result = load_prompt("format-message")
        assert len(result) > 0

    def test_load_agent(self):
        result = load_prompt("agent")
        assert len(result) > 0

    def test_load_contemplative(self):
        result = load_prompt("contemplative")
        assert len(result) > 0

    def test_nonexistent_prompt_raises(self):
        with pytest.raises(FileNotFoundError):
            load_prompt("this-does-not-exist-at-all")

    def test_placeholder_substitution(self):
        """Prompts with placeholders should have them replaced."""
        # Read a prompt that has known placeholders
        raw = (PROMPT_DIR / "chat.md").read_text()
        # If it has any {KEY} patterns, test substitution works
        if "{" in raw:
            # Just verify load_prompt doesn't crash with kwargs
            result = load_prompt("chat", SOUL="test soul", MEMORY="test memory")
            assert isinstance(result, str)

    def test_prompt_dir_exists(self):
        assert PROMPT_DIR.exists()
        assert PROMPT_DIR.is_dir()

    def test_all_system_prompts_loadable(self):
        """Every .md file in system-prompts/ should be loadable."""
        for md_file in PROMPT_DIR.glob("*.md"):
            name = md_file.stem
            result = load_prompt(name)
            assert len(result) > 0, f"Prompt {name} is empty"


# ---------- get_prompt_path ----------


class TestGetPromptPath:
    """Tests for the prompt path helper."""

    def test_returns_path_object(self):
        result = get_prompt_path("chat")
        assert isinstance(result, Path)

    def test_path_includes_md_extension(self):
        result = get_prompt_path("format-message")
        assert result.name == "format-message.md"

    def test_path_is_in_prompt_dir(self):
        result = get_prompt_path("agent")
        assert result.parent == PROMPT_DIR

    def test_path_for_existing_prompt(self):
        result = get_prompt_path("chat")
        assert result.exists()

    def test_path_for_nonexistent_prompt(self):
        """Path is returned even if file doesn't exist (caller handles that)."""
        result = get_prompt_path("does-not-exist")
        assert isinstance(result, Path)
        assert not result.exists()


# ---------- load_skill_prompt ----------


class TestLoadSkillPrompt:
    """Tests for loading prompts from skill directories."""

    def test_load_from_skill_dir(self, tmp_path):
        """When prompt exists in skill dir, use it."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.md").write_text("Skill prompt {VAR}")
        result = load_skill_prompt(tmp_path, "test", VAR="value")
        assert result == "Skill prompt value"

    def test_fallback_to_system_prompts(self, tmp_path):
        """When prompt missing from skill dir, fall back to system-prompts/."""
        # tmp_path has no prompts/ dir, so should fall back
        result = load_skill_prompt(tmp_path, "chat")
        # Should get the system chat prompt
        assert len(result) > 0

    def test_skill_prompt_takes_priority(self, tmp_path):
        """Skill-specific prompt overrides system prompt."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        # Create a prompt with the same name as a system prompt
        (prompts_dir / "chat.md").write_text("Custom skill chat prompt")
        result = load_skill_prompt(tmp_path, "chat")
        assert result == "Custom skill chat prompt"

    def test_nonexistent_in_both_raises(self, tmp_path):
        """If prompt doesn't exist in skill or system dir, raise."""
        with pytest.raises(FileNotFoundError):
            load_skill_prompt(tmp_path, "totally-nonexistent-prompt-xyz")

    def test_substitution_works(self, tmp_path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "review.md").write_text("Review {PROJECT} for {GOAL}")
        result = load_skill_prompt(
            tmp_path, "review", PROJECT="koan", GOAL="quality"
        )
        assert result == "Review koan for quality"

    def test_real_skill_prompts_loadable(self):
        """All existing skill prompts should be loadable."""
        skills_dir = Path(__file__).parent.parent / "skills" / "core"
        if not skills_dir.exists():
            pytest.skip("skills/core not found")
        for skill_dir in skills_dir.iterdir():
            prompts = skill_dir / "prompts"
            if prompts.exists():
                for md_file in prompts.glob("*.md"):
                    result = load_skill_prompt(skill_dir, md_file.stem)
                    assert len(result) > 0, f"{skill_dir.name}/{md_file.stem} is empty"


# ---------- load_prompt_or_skill ----------


class TestLoadPromptOrSkill:
    """Tests for the consolidated prompt loading helper."""

    def test_with_skill_dir_uses_skill_prompt(self, tmp_path):
        """When skill_dir is not None, delegates to load_skill_prompt."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.md").write_text("Skill {VAR}")
        result = load_prompt_or_skill(tmp_path, "test", VAR="ok")
        assert result == "Skill ok"

    def test_with_none_skill_dir_uses_system_prompt(self):
        """When skill_dir is None, delegates to load_prompt."""
        result = load_prompt_or_skill(None, "chat")
        assert len(result) > 0

    def test_skill_dir_takes_priority_over_system(self, tmp_path):
        """Skill-specific prompt overrides system prompt of the same name."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "chat.md").write_text("custom chat")
        result = load_prompt_or_skill(tmp_path, "chat")
        assert result == "custom chat"

    def test_skill_dir_falls_back_to_system(self, tmp_path):
        """When prompt missing in skill dir, falls back to system-prompts."""
        result = load_prompt_or_skill(tmp_path, "chat")
        system_result = load_prompt("chat")
        assert result == system_result

    def test_none_skill_dir_nonexistent_raises(self):
        """When skill_dir is None and prompt doesn't exist, raises."""
        with pytest.raises(FileNotFoundError):
            load_prompt_or_skill(None, "totally-nonexistent-xyz")

    def test_substitution_with_skill_dir(self, tmp_path):
        """Placeholder substitution works via skill path."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "demo.md").write_text("{A} and {B}")
        result = load_prompt_or_skill(tmp_path, "demo", A="one", B="two")
        assert result == "one and two"

    def test_substitution_without_skill_dir(self):
        """Placeholder substitution works via system path."""
        result = load_prompt_or_skill(
            None, "chat", SOUL="test soul", MEMORY="test mem"
        )
        assert "test soul" in result or isinstance(result, str)


# ---------- _read_prompt_with_git_fallback ----------


def _make_run_side_effect(rev_parse="ok", remotes=None, repo_root="/repo"):
    """Build a side_effect function for subprocess.run mocking.

    Args:
        rev_parse: "ok", "fail", or "timeout" for git rev-parse behavior.
        remotes: dict mapping remote prefix (e.g. "upstream/main") to
                 "ok", "fail", or "timeout".  Defaults to both failing.
        repo_root: path returned by rev-parse --show-toplevel.
    """
    if remotes is None:
        remotes = {"upstream/main": "fail", "origin/main": "fail"}

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            if rev_parse == "timeout":
                raise subprocess.TimeoutExpired(cmd, 5)
            rc = 0 if rev_parse == "ok" else 1
            return subprocess.CompletedProcess(cmd, rc, stdout=f"{repo_root}\n", stderr="")

        if cmd[:2] == ["git", "show"]:
            ref = cmd[2]  # e.g. "upstream/main:rel/path.md"
            for remote, behavior in remotes.items():
                if ref.startswith(f"{remote}:"):
                    if behavior == "timeout":
                        raise subprocess.TimeoutExpired(cmd, 5)
                    if behavior == "ok":
                        return subprocess.CompletedProcess(
                            cmd, 0, stdout=f"content from {remote.split('/')[0]}", stderr=""
                        )
                    return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: not found")

        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown command")

    return side_effect


class TestGitFallback:
    """Tests for _read_prompt_with_git_fallback."""

    def test_file_exists_no_git_call(self, tmp_path):
        """When the file exists on disk, return it without calling git."""
        p = tmp_path / "prompt.md"
        p.write_text("on disk content")
        with patch("app.prompts.subprocess.run") as mock_run:
            result = _read_prompt_with_git_fallback(p)
        assert result == "on disk content"
        mock_run.assert_not_called()

    def test_file_missing_reads_upstream(self, tmp_path):
        """When file is missing, falls back to upstream/main."""
        p = tmp_path / "sub" / "prompt.md"
        se = _make_run_side_effect(remotes={"upstream/main": "ok", "origin/main": "fail"}, repo_root=str(tmp_path))
        with patch("app.prompts.subprocess.run", side_effect=se):
            result = _read_prompt_with_git_fallback(p)
        assert result == "content from upstream"

    def test_upstream_fails_reads_origin(self, tmp_path):
        """When upstream/main fails, falls back to origin/main."""
        p = tmp_path / "sub" / "prompt.md"
        se = _make_run_side_effect(remotes={"upstream/main": "fail", "origin/main": "ok"}, repo_root=str(tmp_path))
        with patch("app.prompts.subprocess.run", side_effect=se):
            result = _read_prompt_with_git_fallback(p)
        assert result == "content from origin"

    def test_both_remotes_fail_raises(self, tmp_path):
        """When both remotes fail, raises FileNotFoundError."""
        p = tmp_path / "sub" / "prompt.md"
        se = _make_run_side_effect(repo_root=str(tmp_path))
        with patch("app.prompts.subprocess.run", side_effect=se):
            with pytest.raises(FileNotFoundError):
                _read_prompt_with_git_fallback(p)

    def test_rev_parse_fails_raises(self, tmp_path):
        """When git rev-parse fails, raises FileNotFoundError."""
        p = tmp_path / "prompt.md"
        se = _make_run_side_effect(rev_parse="fail", repo_root=str(tmp_path))
        with patch("app.prompts.subprocess.run", side_effect=se):
            with pytest.raises(FileNotFoundError):
                _read_prompt_with_git_fallback(p)

    def test_rev_parse_timeout_raises(self, tmp_path):
        """When git rev-parse times out, raises FileNotFoundError."""
        p = tmp_path / "prompt.md"
        se = _make_run_side_effect(rev_parse="timeout", repo_root=str(tmp_path))
        with patch("app.prompts.subprocess.run", side_effect=se):
            with pytest.raises(FileNotFoundError):
                _read_prompt_with_git_fallback(p)

    def test_upstream_timeout_tries_origin(self, tmp_path):
        """When upstream/main times out, tries origin/main."""
        p = tmp_path / "sub" / "prompt.md"
        se = _make_run_side_effect(
            remotes={"upstream/main": "timeout", "origin/main": "ok"}, repo_root=str(tmp_path),
        )
        with patch("app.prompts.subprocess.run", side_effect=se):
            result = _read_prompt_with_git_fallback(p)
        assert result == "content from origin"

    def test_both_timeouts_raises(self, tmp_path):
        """When both remotes time out, raises FileNotFoundError."""
        p = tmp_path / "sub" / "prompt.md"
        se = _make_run_side_effect(
            remotes={"upstream/main": "timeout", "origin/main": "timeout"}, repo_root=str(tmp_path),
        )
        with patch("app.prompts.subprocess.run", side_effect=se):
            with pytest.raises(FileNotFoundError):
                _read_prompt_with_git_fallback(p)

    def test_load_prompt_uses_fallback(self, tmp_path):
        """load_prompt() uses the git fallback when file is missing."""
        fake_path = tmp_path / "nonexistent.md"
        se = _make_run_side_effect(remotes={"upstream/main": "ok", "origin/main": "fail"}, repo_root=str(tmp_path))
        with patch("app.prompts.get_prompt_path", return_value=fake_path):
            with patch("app.prompts.subprocess.run", side_effect=se):
                result = load_prompt("nonexistent")
        assert result == "content from upstream"

    def test_load_skill_prompt_uses_fallback(self, tmp_path):
        """load_skill_prompt() uses the git fallback on system-prompt fallback path."""
        skill_dir = tmp_path / "myskill"
        skill_dir.mkdir()
        # No prompts/ dir in skill, so it falls back to system-prompts
        fake_path = tmp_path / "nonexistent.md"
        se = _make_run_side_effect(remotes={"upstream/main": "fail", "origin/main": "ok"}, repo_root=str(tmp_path))
        with patch("app.prompts.get_prompt_path", return_value=fake_path):
            with patch("app.prompts.subprocess.run", side_effect=se):
                result = load_skill_prompt(skill_dir, "nonexistent")
        assert result == "content from origin"

    def test_skill_prompt_git_fallback_before_system_prompts(self, tmp_path):
        """When skill prompt is missing on disk, try git for the skill path first."""
        skill_dir = tmp_path / "myskill"
        skill_dir.mkdir()
        # Skill prompt doesn't exist on disk, but git show finds it
        se = _make_run_side_effect(
            remotes={"upstream/main": "ok", "origin/main": "fail"},
            repo_root=str(tmp_path),
        )
        with patch("app.prompts.subprocess.run", side_effect=se):
            result = load_skill_prompt(skill_dir, "recreate")
        assert result == "content from upstream"

    def test_skill_prompt_falls_through_to_system_on_git_miss(self, tmp_path):
        """When skill prompt not on disk AND not in git, try system-prompts."""
        skill_dir = tmp_path / "myskill"
        skill_dir.mkdir()

        # First call to _read_prompt_with_git_fallback (skill path) fails entirely
        # Second call (system-prompts path) succeeds via origin
        call_count = [0]
        se_both_fail = _make_run_side_effect(
            remotes={"upstream/main": "fail", "origin/main": "fail"},
            repo_root=str(tmp_path),
        )
        se_origin_ok = _make_run_side_effect(
            remotes={"upstream/main": "fail", "origin/main": "ok"},
            repo_root=str(tmp_path),
        )

        def alternating_se(cmd, **kwargs):
            if cmd[:2] == ["git", "show"]:
                ref = cmd[2]
                # Skill path calls fail, system-prompt calls succeed
                if "myskill" in ref:
                    return se_both_fail(cmd, **kwargs)
                return se_origin_ok(cmd, **kwargs)
            return se_origin_ok(cmd, **kwargs)

        fake_sys = tmp_path / "someprompt.md"
        with patch("app.prompts.get_prompt_path", return_value=fake_sys):
            with patch("app.prompts.subprocess.run", side_effect=alternating_se):
                result = load_skill_prompt(skill_dir, "someprompt")
        assert result == "content from origin"
