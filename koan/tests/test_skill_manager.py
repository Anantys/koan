"""Tests for app/skill_manager.py — skill source management."""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.skill_manager import (
    SkillSource,
    _count_skills_in_dir,
    _data_to_source,
    _extract_scope_from_url,
    _parse_manifest,
    _remove_dir,
    _serialize_manifest,
    compare_versions,
    install_skill_source,
    list_sources,
    load_manifest,
    normalize_git_url,
    parse_version,
    remove_skill_source,
    save_manifest,
    update_all_sources,
    update_skill_source,
    validate_scope,
)


# ---------------------------------------------------------------------------
# normalize_git_url
# ---------------------------------------------------------------------------

class TestNormalizeGitUrl:
    def test_github_shorthand(self):
        assert normalize_git_url("myorg/koan-skills") == "https://github.com/myorg/koan-skills.git"

    def test_full_https_url_unchanged(self):
        url = "https://github.com/myorg/koan-skills.git"
        assert normalize_git_url(url) == url

    def test_ssh_url_unchanged(self):
        url = "git@github.com:myorg/koan-skills.git"
        assert normalize_git_url(url) == url

    def test_strips_whitespace(self):
        assert normalize_git_url("  myorg/repo  ") == "https://github.com/myorg/repo.git"


# ---------------------------------------------------------------------------
# _extract_scope_from_url
# ---------------------------------------------------------------------------

class TestExtractScopeFromUrl:
    def test_github_url_with_prefix(self):
        assert _extract_scope_from_url("https://github.com/myorg/koan-skills-ops.git") == "ops"

    def test_github_url_no_prefix(self):
        assert _extract_scope_from_url("https://github.com/team/deploy-skills.git") == "deploy-skills"

    def test_ssh_url(self):
        assert _extract_scope_from_url("git@github.com:team/koan-skills-analytics.git") == "analytics"

    def test_shorthand(self):
        assert _extract_scope_from_url("myorg/koan-skills-ops") == "ops"

    def test_simple_repo_name(self):
        assert _extract_scope_from_url("https://github.com/org/tools.git") == "tools"

    def test_trailing_slash(self):
        assert _extract_scope_from_url("https://github.com/org/koan-skills-ops/") == "ops"


# ---------------------------------------------------------------------------
# validate_scope
# ---------------------------------------------------------------------------

class TestValidateScope:
    def test_valid_scope(self):
        assert validate_scope("ops") is None

    def test_valid_scope_with_hyphen(self):
        assert validate_scope("my-team") is None

    def test_reserved_core(self):
        assert validate_scope("core") is not None
        assert "reserved" in validate_scope("core")

    def test_empty_scope(self):
        assert validate_scope("") is not None

    def test_invalid_chars(self):
        assert validate_scope("my scope") is not None
        assert validate_scope("../escape") is not None


# ---------------------------------------------------------------------------
# Manifest parsing / serialization
# ---------------------------------------------------------------------------

class TestManifestParsing:
    def test_parse_empty(self):
        assert _parse_manifest("") == {}

    def test_parse_sources_header_only(self):
        assert _parse_manifest("sources:\n") == {}

    def test_parse_single_source(self):
        content = textwrap.dedent("""\
            sources:
              ops:
                url: "https://github.com/myorg/koan-skills-ops.git"
                ref: "main"
                installed_at: "2026-02-07T12:00:00"
                updated_at: "2026-02-07T12:00:00"
        """)
        result = _parse_manifest(content)
        assert "ops" in result
        assert result["ops"].url == "https://github.com/myorg/koan-skills-ops.git"
        assert result["ops"].ref == "main"
        assert result["ops"].installed_at == "2026-02-07T12:00:00"

    def test_parse_multiple_sources(self):
        content = textwrap.dedent("""\
            sources:
              ops:
                url: "https://github.com/myorg/ops.git"
                ref: "main"
              analytics:
                url: "https://github.com/myorg/analytics.git"
                ref: "v1.2.0"
        """)
        result = _parse_manifest(content)
        assert len(result) == 2
        assert result["analytics"].ref == "v1.2.0"

    def test_parse_without_quotes(self):
        content = textwrap.dedent("""\
            sources:
              ops:
                url: https://github.com/myorg/ops.git
                ref: main
        """)
        result = _parse_manifest(content)
        assert result["ops"].url == "https://github.com/myorg/ops.git"

    def test_parse_with_comments(self):
        content = textwrap.dedent("""\
            # Installed skills
            sources:
              # Team ops skills
              ops:
                url: "https://github.com/myorg/ops.git"
                ref: "main"
        """)
        result = _parse_manifest(content)
        assert "ops" in result


class TestManifestSerialization:
    def test_serialize_empty(self):
        assert _serialize_manifest({}) == "sources:\n"

    def test_serialize_single_source(self):
        sources = {
            "ops": SkillSource(
                scope="ops",
                url="https://github.com/myorg/ops.git",
                ref="main",
                installed_at="2026-02-07T12:00:00",
                updated_at="2026-02-07T12:00:00",
            )
        }
        result = _serialize_manifest(sources)
        assert "ops:" in result
        assert "https://github.com/myorg/ops.git" in result
        assert "2026-02-07T12:00:00" in result

    def test_roundtrip(self):
        sources = {
            "ops": SkillSource(
                scope="ops",
                url="https://github.com/myorg/ops.git",
                ref="main",
                installed_at="2026-02-07T12:00:00",
                updated_at="2026-02-07T12:00:00",
            ),
            "analytics": SkillSource(
                scope="analytics",
                url="https://github.com/team/analytics.git",
                ref="v1.2.0",
                installed_at="2026-02-06T10:00:00",
                updated_at="2026-02-07T08:00:00",
            ),
        }
        serialized = _serialize_manifest(sources)
        parsed = _parse_manifest(serialized)
        assert len(parsed) == 2
        assert parsed["ops"].url == sources["ops"].url
        assert parsed["analytics"].ref == sources["analytics"].ref


class TestManifestIO:
    def test_load_nonexistent(self, tmp_path):
        assert load_manifest(tmp_path) == {}

    def test_save_and_load(self, tmp_path):
        sources = {
            "ops": SkillSource(
                scope="ops",
                url="https://github.com/myorg/ops.git",
                ref="main",
                installed_at="2026-02-07T12:00:00",
            )
        }
        save_manifest(tmp_path, sources)
        loaded = load_manifest(tmp_path)
        assert "ops" in loaded
        assert loaded["ops"].url == "https://github.com/myorg/ops.git"

    def test_save_creates_file(self, tmp_path):
        save_manifest(tmp_path, {})
        assert (tmp_path / "skills.yaml").exists()


# ---------------------------------------------------------------------------
# Version utilities
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_simple_version(self):
        assert parse_version("1.2.3") == (1, 2, 3, "")

    def test_prerelease(self):
        assert parse_version("1.0.0-alpha") == (1, 0, 0, "alpha")

    def test_zero_version(self):
        assert parse_version("0.0.0") == (0, 0, 0, "")

    def test_invalid_version(self):
        assert parse_version("not-a-version") is None

    def test_partial_version(self):
        assert parse_version("1.2") is None

    def test_whitespace(self):
        assert parse_version("  1.0.0  ") == (1, 0, 0, "")


class TestCompareVersions:
    def test_equal(self):
        assert compare_versions("1.0.0", "1.0.0") == 0

    def test_major_greater(self):
        assert compare_versions("2.0.0", "1.0.0") == 1

    def test_major_less(self):
        assert compare_versions("1.0.0", "2.0.0") == -1

    def test_minor_greater(self):
        assert compare_versions("1.1.0", "1.0.0") == 1

    def test_patch_greater(self):
        assert compare_versions("1.0.1", "1.0.0") == 1

    def test_prerelease_less_than_release(self):
        assert compare_versions("1.0.0-alpha", "1.0.0") == -1

    def test_release_greater_than_prerelease(self):
        assert compare_versions("1.0.0", "1.0.0-beta") == 1

    def test_invalid_version_returns_zero(self):
        assert compare_versions("invalid", "1.0.0") == 0

    def test_prerelease_comparison(self):
        assert compare_versions("1.0.0-alpha", "1.0.0-beta") == -1


# ---------------------------------------------------------------------------
# install_skill_source
# ---------------------------------------------------------------------------

class TestInstallSkillSource:
    def _make_skill_dir(self, parent, name):
        """Create a minimal skill directory with a SKILL.md."""
        skill_dir = parent / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent(f"""\
            ---
            name: {name}
            description: Test skill
            version: 1.0.0
            commands:
              - name: {name}
                description: Test command
            ---
        """))

    @patch("app.skill_manager._run_git")
    def test_install_success(self, mock_git, tmp_path):
        """Successful install clones repo and tracks in manifest."""
        def fake_clone(*args, cwd=None, timeout=60):
            # Simulate git clone by creating the target directory
            for i, arg in enumerate(args):
                if arg == "clone":
                    target = Path(args[-1])
                    target.mkdir(parents=True, exist_ok=True)
                    self._make_skill_dir(target, "deploy")
                    return 0, "", ""
            return 1, "", "error"

        mock_git.side_effect = fake_clone
        ok, msg = install_skill_source(tmp_path, "https://github.com/myorg/ops.git", scope="ops")
        assert ok
        assert "1 skill" in msg
        assert "ops" in msg

        # Verify manifest was created
        sources = load_manifest(tmp_path)
        assert "ops" in sources
        assert sources["ops"].url == "https://github.com/myorg/ops.git"

    @patch("app.skill_manager._run_git")
    def test_install_auto_scope(self, mock_git, tmp_path):
        """Scope is derived from URL when not provided."""
        def fake_clone(*args, cwd=None, timeout=60):
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            self._make_skill_dir(target, "deploy")
            return 0, "", ""

        mock_git.side_effect = fake_clone
        ok, msg = install_skill_source(tmp_path, "https://github.com/myorg/koan-skills-ops.git")
        assert ok
        sources = load_manifest(tmp_path)
        assert "ops" in sources

    def test_install_reserved_scope(self, tmp_path):
        """Cannot install into 'core' scope."""
        ok, msg = install_skill_source(tmp_path, "https://github.com/x/y.git", scope="core")
        assert not ok
        assert "reserved" in msg

    @patch("app.skill_manager._run_git")
    def test_install_duplicate_scope(self, mock_git, tmp_path):
        """Cannot install same scope twice."""
        # Pre-populate manifest
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://example.com/ops.git")
        })
        ok, msg = install_skill_source(tmp_path, "https://github.com/other/ops.git", scope="ops")
        assert not ok
        assert "already installed" in msg

    @patch("app.skill_manager._run_git")
    def test_install_no_skills_found(self, mock_git, tmp_path):
        """Install fails if no SKILL.md files found."""
        def fake_clone(*args, cwd=None, timeout=60):
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "README.md").write_text("No skills here")
            return 0, "", ""

        mock_git.side_effect = fake_clone
        ok, msg = install_skill_source(tmp_path, "https://github.com/x/y.git", scope="empty")
        assert not ok
        assert "No SKILL.md" in msg
        # Directory should be cleaned up
        assert not (tmp_path / "skills" / "empty").exists()

    @patch("app.skill_manager._run_git")
    def test_install_git_failure(self, mock_git, tmp_path):
        """Install fails gracefully on git error."""
        mock_git.return_value = (1, "", "fatal: repo not found")
        ok, msg = install_skill_source(tmp_path, "https://github.com/x/y.git", scope="ops")
        assert not ok
        assert "clone failed" in msg

    @patch("app.skill_manager._run_git")
    def test_install_with_ref(self, mock_git, tmp_path):
        """Install with a specific ref/tag."""
        def fake_clone(*args, cwd=None, timeout=60):
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            self._make_skill_dir(target, "deploy")
            return 0, "", ""

        mock_git.side_effect = fake_clone
        ok, msg = install_skill_source(
            tmp_path, "https://github.com/myorg/ops.git",
            scope="ops", ref="v1.0.0"
        )
        assert ok
        sources = load_manifest(tmp_path)
        assert sources["ops"].ref == "v1.0.0"
        # Verify --branch v1.0.0 was passed
        call_args = mock_git.call_args_list[0]
        assert "v1.0.0" in call_args[0]

    @patch("app.skill_manager._run_git")
    def test_install_github_shorthand(self, mock_git, tmp_path):
        """GitHub shorthand is expanded to full URL."""
        def fake_clone(*args, cwd=None, timeout=60):
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            self._make_skill_dir(target, "deploy")
            return 0, "", ""

        mock_git.side_effect = fake_clone
        ok, msg = install_skill_source(tmp_path, "myorg/koan-skills-ops", scope="ops")
        assert ok
        sources = load_manifest(tmp_path)
        assert sources["ops"].url == "https://github.com/myorg/koan-skills-ops.git"

    def test_install_existing_directory(self, tmp_path):
        """Cannot install if directory already exists."""
        (tmp_path / "skills" / "ops").mkdir(parents=True)
        ok, msg = install_skill_source(tmp_path, "https://github.com/x/y.git", scope="ops")
        assert not ok
        assert "already exists" in msg


# ---------------------------------------------------------------------------
# update_skill_source
# ---------------------------------------------------------------------------

class TestUpdateSkillSource:
    @patch("app.skill_manager._run_git")
    def test_update_success(self, mock_git, tmp_path):
        """Successful update pulls and updates timestamp."""
        # Setup: manifest + directory
        save_manifest(tmp_path, {
            "ops": SkillSource(
                scope="ops",
                url="https://github.com/myorg/ops.git",
                ref="main",
                installed_at="2026-02-06T12:00:00",
                updated_at="2026-02-06T12:00:00",
            )
        })
        skills_dir = tmp_path / "skills" / "ops" / "deploy"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("---\nname: deploy\n---")

        mock_git.return_value = (0, "Updating abc123..def456", "")
        ok, msg = update_skill_source(tmp_path, "ops")
        assert ok
        assert "Updated" in msg
        assert "Restart" in msg

        # Verify timestamp was updated
        sources = load_manifest(tmp_path)
        assert sources["ops"].updated_at != "2026-02-06T12:00:00"

    @patch("app.skill_manager._run_git")
    def test_update_already_current(self, mock_git, tmp_path):
        """Update when already up to date."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/x.git")
        })
        (tmp_path / "skills" / "ops").mkdir(parents=True)

        mock_git.return_value = (0, "Already up to date.", "")
        ok, msg = update_skill_source(tmp_path, "ops")
        assert ok
        assert "already up to date" in msg

    def test_update_unknown_scope(self, tmp_path):
        """Update fails for untracked scope."""
        save_manifest(tmp_path, {})
        ok, msg = update_skill_source(tmp_path, "nonexistent")
        assert not ok
        assert "not found" in msg

    @patch("app.skill_manager._run_git")
    def test_update_git_failure(self, mock_git, tmp_path):
        """Update handles git pull failure."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/x.git")
        })
        (tmp_path / "skills" / "ops").mkdir(parents=True)

        mock_git.return_value = (1, "", "error: merge conflict")
        ok, msg = update_skill_source(tmp_path, "ops")
        assert not ok
        assert "pull failed" in msg

    def test_update_missing_directory(self, tmp_path):
        """Update fails when directory was deleted."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/x.git")
        })
        ok, msg = update_skill_source(tmp_path, "ops")
        assert not ok
        assert "missing" in msg


# ---------------------------------------------------------------------------
# update_all_sources
# ---------------------------------------------------------------------------

class TestUpdateAllSources:
    def test_no_sources(self, tmp_path):
        ok, msg = update_all_sources(tmp_path)
        assert ok
        assert "No installed" in msg

    @patch("app.skill_manager._run_git")
    def test_update_all(self, mock_git, tmp_path):
        """Updates all tracked sources."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/x.git"),
            "analytics": SkillSource(scope="analytics", url="https://github.com/y.git"),
        })
        (tmp_path / "skills" / "ops").mkdir(parents=True)
        (tmp_path / "skills" / "analytics").mkdir(parents=True)

        mock_git.return_value = (0, "Already up to date.", "")
        ok, msg = update_all_sources(tmp_path)
        assert ok
        assert "ops" in msg
        assert "analytics" in msg


# ---------------------------------------------------------------------------
# remove_skill_source
# ---------------------------------------------------------------------------

class TestRemoveSkillSource:
    def test_remove_tracked(self, tmp_path):
        """Remove a tracked skill source."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/x.git")
        })
        (tmp_path / "skills" / "ops").mkdir(parents=True)
        (tmp_path / "skills" / "ops" / "file.txt").write_text("test")

        ok, msg = remove_skill_source(tmp_path, "ops")
        assert ok
        assert not (tmp_path / "skills" / "ops").exists()
        assert "ops" not in load_manifest(tmp_path)

    def test_remove_untracked_directory(self, tmp_path):
        """Remove a directory even if not in manifest."""
        (tmp_path / "skills" / "manual").mkdir(parents=True)
        ok, msg = remove_skill_source(tmp_path, "manual")
        assert ok
        assert not (tmp_path / "skills" / "manual").exists()

    def test_remove_nonexistent(self, tmp_path):
        """Remove fails for unknown scope."""
        ok, msg = remove_skill_source(tmp_path, "ghost")
        assert not ok
        assert "not found" in msg

    def test_remove_cleans_manifest(self, tmp_path):
        """Manifest is cleaned after removal."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/x.git"),
            "analytics": SkillSource(scope="analytics", url="https://github.com/y.git"),
        })
        (tmp_path / "skills" / "ops").mkdir(parents=True)

        ok, msg = remove_skill_source(tmp_path, "ops")
        assert ok
        sources = load_manifest(tmp_path)
        assert "ops" not in sources
        assert "analytics" in sources


# ---------------------------------------------------------------------------
# list_sources
# ---------------------------------------------------------------------------

class TestListSources:
    def test_empty(self, tmp_path):
        result = list_sources(tmp_path)
        assert "No external" in result
        assert "install" in result

    def test_with_sources(self, tmp_path):
        save_manifest(tmp_path, {
            "ops": SkillSource(
                scope="ops",
                url="https://github.com/myorg/ops.git",
                ref="main",
                updated_at="2026-02-07T12:00:00",
            )
        })
        result = list_sources(tmp_path)
        assert "ops" in result
        assert "https://github.com/myorg/ops.git" in result
        assert "main" in result

    def test_with_skills_count(self, tmp_path):
        """Shows skill count from actual directory."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/x.git")
        })
        # Create 2 skills in the directory
        for name in ["deploy", "oncall"]:
            d = tmp_path / "skills" / "ops" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\n---")

        result = list_sources(tmp_path)
        assert "2 skills" in result


# ---------------------------------------------------------------------------
# SkillSource dataclass
# ---------------------------------------------------------------------------

class TestSkillSource:
    def test_default_values(self):
        src = SkillSource(scope="ops", url="https://example.com/ops.git")
        assert src.ref == "main"
        assert src.installed_at == ""
        assert src.updated_at == ""

    def test_all_fields(self):
        src = SkillSource(
            scope="ops",
            url="https://example.com/ops.git",
            ref="v1.0.0",
            installed_at="2026-02-07",
            updated_at="2026-02-07",
        )
        assert src.scope == "ops"
        assert src.ref == "v1.0.0"


# ---------------------------------------------------------------------------
# Install: fallback clone path (--branch fails, plain clone + checkout)
# ---------------------------------------------------------------------------

class TestInstallFallbackClone:
    """Tests for the fallback clone logic when --branch ref fails."""

    def _make_skill_dir(self, parent, name):
        skill_dir = parent / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---")

    @patch("app.skill_manager._run_git")
    def test_branch_fails_plain_clone_succeeds(self, mock_git, tmp_path):
        """When --branch ref fails, falls back to plain clone."""
        call_count = 0

        def fake_git(*args, cwd=None, timeout=60):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: clone --branch v2.0 fails
                return 1, "", "fatal: Remote branch v2.0 not found"
            # Second call: plain clone succeeds
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            self._make_skill_dir(target, "deploy")
            return 0, "", ""

        mock_git.side_effect = fake_git
        ok, msg = install_skill_source(
            tmp_path, "https://github.com/org/ops.git",
            scope="ops", ref="main",
        )
        assert ok
        assert "1 skill" in msg
        assert call_count == 2

    @patch("app.skill_manager._run_git")
    def test_branch_fails_plain_clone_with_custom_ref_checkout(self, mock_git, tmp_path):
        """When --branch fails for a custom ref, falls back and checkouts ref."""
        call_count = 0

        def fake_git(*args, cwd=None, timeout=60):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First: clone --branch v2.0 fails
                return 1, "", "fatal: Remote branch v2.0 not found"
            if call_count == 2:
                # Second: plain clone succeeds
                target = Path(args[-1])
                target.mkdir(parents=True, exist_ok=True)
                self._make_skill_dir(target, "deploy")
                return 0, "", ""
            if call_count == 3:
                # Third: checkout v2.0 succeeds
                return 0, "", ""
            return 1, "", "unexpected"

        mock_git.side_effect = fake_git
        ok, msg = install_skill_source(
            tmp_path, "https://github.com/org/ops.git",
            scope="ops", ref="v2.0",
        )
        assert ok
        assert call_count == 3
        # Verify checkout was called with "v2.0"
        third_call = mock_git.call_args_list[2]
        assert "checkout" in third_call[0]
        assert "v2.0" in third_call[0]

    @patch("app.skill_manager._run_git")
    def test_checkout_failure_cleans_up(self, mock_git, tmp_path):
        """When checkout of custom ref fails after plain clone, directory is removed."""
        call_count = 0

        def fake_git(*args, cwd=None, timeout=60):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 1, "", "branch not found"
            if call_count == 2:
                target = Path(args[-1])
                target.mkdir(parents=True, exist_ok=True)
                self._make_skill_dir(target, "deploy")
                return 0, "", ""
            if call_count == 3:
                # Checkout fails
                return 1, "", "error: pathspec 'v9.9' did not match"
            return 1, "", "unexpected"

        mock_git.side_effect = fake_git
        ok, msg = install_skill_source(
            tmp_path, "https://github.com/org/ops.git",
            scope="ops", ref="v9.9",
        )
        assert not ok
        assert "checkout" in msg.lower()
        # Directory should be cleaned up
        assert not (tmp_path / "skills" / "ops").exists()

    @patch("app.skill_manager._run_git")
    def test_install_multiple_skills_plural(self, mock_git, tmp_path):
        """Plural message when installing multiple skills."""
        def fake_clone(*args, cwd=None, timeout=60):
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            for name in ["deploy", "oncall", "monitor"]:
                skill_dir = target / name
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---")
            return 0, "", ""

        mock_git.side_effect = fake_clone
        ok, msg = install_skill_source(
            tmp_path, "https://github.com/org/ops.git", scope="ops",
        )
        assert ok
        assert "3 skills" in msg

    @patch("app.skill_manager._run_git")
    def test_install_invalid_scope_name(self, mock_git, tmp_path):
        """Install with scope containing spaces fails validation."""
        ok, msg = install_skill_source(
            tmp_path, "https://github.com/org/ops.git",
            scope="my scope",
        )
        assert not ok
        assert "Invalid" in msg


# ---------------------------------------------------------------------------
# Manifest parsing edge cases
# ---------------------------------------------------------------------------

class TestManifestParsingEdgeCases:
    def test_parse_unrecognized_top_level_after_sources(self):
        """Parser stops at unrecognized top-level line after sources block."""
        content = textwrap.dedent("""\
            sources:
              ops:
                url: "https://github.com/myorg/ops.git"
                ref: "main"
            other_section:
              key: value
        """)
        result = _parse_manifest(content)
        assert len(result) == 1
        assert result["ops"].url == "https://github.com/myorg/ops.git"

    def test_parse_no_sources_header(self):
        """Content without 'sources:' header returns empty dict."""
        content = "something_else:\n  key: value\n"
        assert _parse_manifest(content) == {}

    def test_parse_source_missing_url(self):
        """Source with no URL defaults to empty string."""
        content = textwrap.dedent("""\
            sources:
              ops:
                ref: "v1.0"
        """)
        result = _parse_manifest(content)
        assert result["ops"].url == ""
        assert result["ops"].ref == "v1.0"

    def test_parse_source_missing_ref(self):
        """Source with no ref defaults to 'main'."""
        content = textwrap.dedent("""\
            sources:
              ops:
                url: "https://github.com/org/ops.git"
        """)
        result = _parse_manifest(content)
        assert result["ops"].ref == "main"

    def test_parse_only_empty_lines_and_comments(self):
        """Content with only blank lines and comments returns empty."""
        content = "# comment\n\n# another comment\n\n"
        assert _parse_manifest(content) == {}

    def test_parse_scope_with_hyphen_and_underscore(self):
        """Scope names with hyphens and underscores are valid."""
        content = textwrap.dedent("""\
            sources:
              my-team_ops:
                url: "https://github.com/org/ops.git"
        """)
        result = _parse_manifest(content)
        assert "my-team_ops" in result

    def test_parse_field_value_with_single_quotes(self):
        """Field values wrapped in single quotes are stripped."""
        content = textwrap.dedent("""\
            sources:
              ops:
                url: 'https://github.com/org/ops.git'
                ref: 'v1.0'
        """)
        result = _parse_manifest(content)
        assert result["ops"].url == "https://github.com/org/ops.git"
        assert result["ops"].ref == "v1.0"


# ---------------------------------------------------------------------------
# Manifest serialization edge cases
# ---------------------------------------------------------------------------

class TestSerializationEdgeCases:
    def test_serialize_without_timestamps(self):
        """Sources with empty timestamps omit those fields."""
        sources = {
            "ops": SkillSource(scope="ops", url="https://example.com/ops.git"),
        }
        result = _serialize_manifest(sources)
        assert "installed_at" not in result
        assert "updated_at" not in result

    def test_serialize_sorted_by_scope(self):
        """Scopes are serialized in alphabetical order."""
        sources = {
            "zebra": SkillSource(scope="zebra", url="https://example.com/z.git"),
            "alpha": SkillSource(scope="alpha", url="https://example.com/a.git"),
        }
        result = _serialize_manifest(sources)
        alpha_pos = result.index("alpha:")
        zebra_pos = result.index("zebra:")
        assert alpha_pos < zebra_pos


# ---------------------------------------------------------------------------
# update_all_sources: partial failure
# ---------------------------------------------------------------------------

class TestUpdateAllPartialFailure:
    @patch("app.skill_manager._run_git")
    def test_partial_failure_reports_both(self, mock_git, tmp_path):
        """When one source fails and another succeeds, both are reported."""
        save_manifest(tmp_path, {
            "alpha": SkillSource(scope="alpha", url="https://github.com/a.git"),
            "beta": SkillSource(scope="beta", url="https://github.com/b.git"),
        })
        (tmp_path / "skills" / "alpha").mkdir(parents=True)
        (tmp_path / "skills" / "beta").mkdir(parents=True)

        call_count = 0

        def fake_git(*args, cwd=None, timeout=60):
            nonlocal call_count
            call_count += 1
            if "alpha" in str(cwd):
                return 0, "Already up to date.", ""
            return 1, "", "error: merge conflict"

        mock_git.side_effect = fake_git
        ok, msg = update_all_sources(tmp_path)
        assert not ok  # any_failure = True
        assert "✅" in msg
        assert "❌" in msg
        assert "alpha" in msg
        assert "beta" in msg


# ---------------------------------------------------------------------------
# _extract_scope_from_url: additional edge cases
# ---------------------------------------------------------------------------

class TestExtractScopeEdgeCases:
    def test_empty_url(self):
        """Empty URL returns 'custom'."""
        assert _extract_scope_from_url("") == "custom"

    def test_koan_skill_singular_prefix(self):
        """'koan-skill-' prefix (singular) is stripped."""
        result = _extract_scope_from_url("https://github.com/org/koan-skill-deploy.git")
        assert result == "deploy"

    def test_koan_prefix(self):
        """'koan-' prefix is stripped."""
        result = _extract_scope_from_url("https://github.com/org/koan-monitoring.git")
        assert result == "monitoring"

    def test_skills_prefix(self):
        """'skills-' prefix is stripped."""
        result = _extract_scope_from_url("https://github.com/org/skills-infra.git")
        assert result == "infra"

    def test_only_prefix_no_suffix(self):
        """When 'koan-skills-' prefix matches exactly (no suffix), the guard
        prevents stripping, so the next prefix 'koan-' is tried instead."""
        # "koan-skills-" starts with "koan-skills-" but len guard fails (==, not >).
        # Falls through to "koan-" prefix which succeeds → "skills-".
        result = _extract_scope_from_url("https://github.com/org/koan-skills-.git")
        assert result == "skills-"

    def test_ssh_protocol_url(self):
        """ssh:// protocol URLs are preserved and scope extracted."""
        result = _extract_scope_from_url("ssh://git@github.com/org/koan-skills-ops.git")
        assert result == "ops"


# ---------------------------------------------------------------------------
# normalize_git_url: additional edge cases
# ---------------------------------------------------------------------------

class TestNormalizeGitUrlEdgeCases:
    def test_ssh_protocol_url_unchanged(self):
        url = "ssh://git@github.com/org/repo.git"
        assert normalize_git_url(url) == url

    def test_http_url_unchanged(self):
        url = "http://internal.git.example.com/repo.git"
        assert normalize_git_url(url) == url

    def test_single_word_not_shorthand(self):
        """A single word without '/' is not treated as GitHub shorthand."""
        # "myrepo" doesn't match owner/repo pattern
        assert normalize_git_url("myrepo") == "myrepo"

    def test_complex_github_shorthand(self):
        """Dotted org/repo names still match shorthand."""
        assert normalize_git_url("my.org/my.repo") == "https://github.com/my.org/my.repo.git"


# ---------------------------------------------------------------------------
# _data_to_source helper
# ---------------------------------------------------------------------------

class TestDataToSource:
    def test_minimal_data(self):
        """Source from data with no fields uses defaults."""
        src = _data_to_source("test", {})
        assert src.scope == "test"
        assert src.url == ""
        assert src.ref == "main"
        assert src.installed_at == ""
        assert src.updated_at == ""

    def test_full_data(self):
        data = {
            "url": "https://example.com/repo.git",
            "ref": "v2.0",
            "installed_at": "2026-01-01",
            "updated_at": "2026-02-01",
        }
        src = _data_to_source("ops", data)
        assert src.url == "https://example.com/repo.git"
        assert src.ref == "v2.0"
        assert src.installed_at == "2026-01-01"

    def test_extra_fields_ignored(self):
        """Unknown fields in data dict are silently ignored."""
        data = {"url": "https://example.com/r.git", "unknown_field": "value"}
        src = _data_to_source("ops", data)
        assert src.url == "https://example.com/r.git"


# ---------------------------------------------------------------------------
# _count_skills_in_dir / _remove_dir helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_count_skills_nonexistent_dir(self, tmp_path):
        assert _count_skills_in_dir(tmp_path / "nonexistent") == 0

    def test_count_skills_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert _count_skills_in_dir(d) == 0

    def test_count_skills_nested(self, tmp_path):
        """Counts SKILL.md in nested subdirectories."""
        for name in ["a", "b", "c"]:
            sd = tmp_path / "skills" / name
            sd.mkdir(parents=True)
            (sd / "SKILL.md").write_text(f"---\nname: {name}\n---")
        assert _count_skills_in_dir(tmp_path / "skills") == 3

    def test_remove_dir_nonexistent(self, tmp_path):
        """_remove_dir on nonexistent path is a no-op."""
        _remove_dir(tmp_path / "ghost")  # should not raise

    def test_remove_dir_is_file(self, tmp_path):
        """_remove_dir on a file is a no-op (only removes directories)."""
        f = tmp_path / "file.txt"
        f.write_text("data")
        _remove_dir(f)  # should not raise, should not delete
        assert f.exists()

    def test_remove_dir_with_contents(self, tmp_path):
        """_remove_dir removes directory and all contents."""
        d = tmp_path / "target"
        d.mkdir()
        (d / "sub").mkdir()
        (d / "sub" / "file.txt").write_text("content")
        _remove_dir(d)
        assert not d.exists()


# ---------------------------------------------------------------------------
# list_sources edge cases
# ---------------------------------------------------------------------------

class TestListSourcesEdgeCases:
    def test_list_with_missing_directory(self, tmp_path):
        """list_sources counts 0 when skill directory doesn't exist on disk."""
        save_manifest(tmp_path, {
            "ops": SkillSource(
                scope="ops",
                url="https://github.com/org/ops.git",
                ref="main",
            )
        })
        # Don't create the skills/ops directory
        result = list_sources(tmp_path)
        assert "0 skills" in result
        assert "ops" in result

    def test_list_with_single_skill(self, tmp_path):
        """Singular 'skill' when count is 1."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/org/ops.git"),
        })
        sd = tmp_path / "skills" / "ops" / "deploy"
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text("---\nname: deploy\n---")
        result = list_sources(tmp_path)
        assert "1 skill)" in result  # "(1 skill)" not "(1 skills)"

    def test_list_without_updated_at(self, tmp_path):
        """Source without updated_at omits the 'updated:' line."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/org/ops.git"),
        })
        result = list_sources(tmp_path)
        assert "updated:" not in result


# ---------------------------------------------------------------------------
# remove_skill_source: manifest-only (no directory on disk)
# ---------------------------------------------------------------------------

class TestRemoveManifestOnly:
    def test_remove_tracked_but_no_directory(self, tmp_path):
        """Remove succeeds when in manifest but directory was already deleted."""
        save_manifest(tmp_path, {
            "ops": SkillSource(scope="ops", url="https://github.com/org/ops.git"),
        })
        # Don't create the directory
        ok, msg = remove_skill_source(tmp_path, "ops")
        assert ok
        assert "Removed" in msg
        assert "ops" not in load_manifest(tmp_path)


# ---------------------------------------------------------------------------
# compare_versions: additional edge cases
# ---------------------------------------------------------------------------

class TestCompareVersionsEdgeCases:
    def test_both_invalid(self):
        """Two invalid versions return 0."""
        assert compare_versions("abc", "xyz") == 0

    def test_large_version_numbers(self):
        assert compare_versions("100.200.300", "100.200.301") == -1

    def test_prerelease_alphabetical(self):
        """Pre-release strings are compared lexicographically."""
        assert compare_versions("1.0.0-alpha", "1.0.0-alpha") == 0
        assert compare_versions("1.0.0-alpha.1", "1.0.0-alpha.2") == -1
