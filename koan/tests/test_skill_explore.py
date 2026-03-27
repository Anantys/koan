"""Tests for the /explore and /noexplore skill handlers."""

from pathlib import Path
from unittest.mock import MagicMock

import yaml
import pytest

from app.skills import SkillContext


def _make_ctx(command_name, koan_root, args=""):
    """Create a minimal SkillContext for testing."""
    ctx = MagicMock(spec=SkillContext)
    ctx.command_name = command_name
    ctx.koan_root = Path(koan_root)
    ctx.instance_dir = Path(koan_root) / "instance"
    ctx.args = args
    return ctx


def _write_config(koan_root, config):
    """Write a projects.yaml file."""
    path = Path(koan_root) / "projects.yaml"
    path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _read_config(koan_root):
    """Read projects.yaml back."""
    path = Path(koan_root) / "projects.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Show status (no args)
# ---------------------------------------------------------------------------

class TestShowStatus:
    """Tests for /explore with no args — show exploration status."""

    def test_show_status_all_enabled(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": True},
                "backend": {"path": "/tmp/backend", "exploration": True},
            }
        })
        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        assert "🔭" in result
        assert "koan: ON" in result
        assert "backend: ON" in result

    def test_show_status_mixed(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": True},
                "backend": {"path": "/tmp/backend", "exploration": False},
            }
        })
        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        assert "✅ koan: ON" in result
        assert "❌ backend: OFF" in result

    def test_show_status_inherits_defaults(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "defaults": {"exploration": False},
            "projects": {
                "koan": {"path": "/tmp/koan"},
                "backend": {"path": "/tmp/backend", "exploration": True},
            }
        })
        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        assert "❌ koan: OFF" in result
        assert "✅ backend: ON" in result

    def test_show_status_default_is_true(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "myproject": {"path": "/tmp/myproject"},
            }
        })
        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        assert "✅ myproject: ON" in result

    def test_noexplore_no_args_shows_status(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": True},
            }
        })
        ctx = _make_ctx("noexplore", tmp_path)
        result = handle(ctx)

        assert "🔭" in result
        assert "koan: ON" in result

    def test_show_status_sorted_alphabetically(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "zebra": {"path": "/tmp/z"},
                "alpha": {"path": "/tmp/a"},
                "middle": {"path": "/tmp/m"},
            }
        })
        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        lines = result.split("\n")
        project_lines = [l for l in lines if ": ON" in l or ": OFF" in l]
        names = [l.strip().split()[-2].rstrip(":") for l in project_lines]
        assert names == ["alpha", "middle", "zebra"]

    def test_show_status_includes_usage_hints(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {"koan": {"path": "/tmp/koan"}}
        })
        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        assert "/explore <project>" in result
        assert "/noexplore <project>" in result


# ---------------------------------------------------------------------------
# Enable exploration
# ---------------------------------------------------------------------------

class TestEnableExploration:
    """Tests for /explore <project>."""

    def test_enable_exploration(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "backend": {"path": "/tmp/backend", "exploration": False},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="backend")
        result = handle(ctx)

        assert "enabled" in result.lower()
        assert "backend" in result
        config = _read_config(tmp_path)
        assert config["projects"]["backend"]["exploration"] is True

    def test_enable_already_enabled(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": True},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="koan")
        result = handle(ctx)

        assert "already" in result.lower()
        assert "enabled" in result.lower()

    def test_enable_case_insensitive(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "Backend": {"path": "/tmp/backend", "exploration": False},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="backend")
        result = handle(ctx)

        assert "enabled" in result.lower()
        assert "Backend" in result  # Uses canonical name

    def test_enable_overrides_default_false(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "defaults": {"exploration": False},
            "projects": {
                "koan": {"path": "/tmp/koan"},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="koan")
        result = handle(ctx)

        assert "enabled" in result.lower()
        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is True
        # defaults section should be untouched
        assert config["defaults"]["exploration"] is False

    def test_enable_unknown_project(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan"},
                "backend": {"path": "/tmp/backend"},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="nonexistent")
        result = handle(ctx)

        assert "❌" in result
        assert "nonexistent" in result
        assert "koan" in result
        assert "backend" in result

    def test_enable_creates_entry_for_none_project(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "minimal": None,
            }
        })
        # Default for None entry is exploration=True, so let's test with
        # a defaults that sets exploration=False
        _write_config(tmp_path, {
            "defaults": {"exploration": False},
            "projects": {
                "minimal": None,
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="minimal")
        result = handle(ctx)

        assert "enabled" in result.lower()
        config = _read_config(tmp_path)
        assert config["projects"]["minimal"]["exploration"] is True


# ---------------------------------------------------------------------------
# Disable exploration
# ---------------------------------------------------------------------------

class TestDisableExploration:
    """Tests for /noexplore <project>."""

    def test_disable_exploration(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": True},
            }
        })
        ctx = _make_ctx("noexplore", tmp_path, args="koan")
        result = handle(ctx)

        assert "disabled" in result.lower()
        assert "koan" in result
        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is False

    def test_disable_already_disabled(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "backend": {"path": "/tmp/backend", "exploration": False},
            }
        })
        ctx = _make_ctx("noexplore", tmp_path, args="backend")
        result = handle(ctx)

        assert "already" in result.lower()
        assert "disabled" in result.lower()

    def test_disable_case_insensitive(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "MyProject": {"path": "/tmp/p", "exploration": True},
            }
        })
        ctx = _make_ctx("noexplore", tmp_path, args="MYPROJECT")
        result = handle(ctx)

        assert "disabled" in result.lower()
        assert "MyProject" in result

    def test_disable_unknown_project(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {"koan": {"path": "/tmp/koan"}}
        })
        ctx = _make_ctx("noexplore", tmp_path, args="ghost")
        result = handle(ctx)

        assert "❌" in result
        assert "ghost" in result
        assert "koan" in result

    def test_disable_on_unconfigured_project(self, tmp_path):
        """Project with no exploration key defaults to True, so disabling writes False."""
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan"},
            }
        })
        ctx = _make_ctx("noexplore", tmp_path, args="koan")
        result = handle(ctx)

        assert "disabled" in result.lower()
        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is False


# ---------------------------------------------------------------------------
# Bulk toggle
# ---------------------------------------------------------------------------

class TestBulkToggle:
    """Tests for /explore all and /explore none."""

    def test_explore_all(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": False},
                "backend": {"path": "/tmp/backend", "exploration": False},
                "web": {"path": "/tmp/web", "exploration": True},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="all")
        result = handle(ctx)

        assert "enabled" in result.lower()
        assert "2 project" in result  # 2 changed (web was already True)
        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is True
        assert config["projects"]["backend"]["exploration"] is True

    def test_explore_none(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": True},
                "backend": {"path": "/tmp/backend", "exploration": True},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="none")
        result = handle(ctx)

        assert "disabled" in result.lower()
        assert "2 project" in result
        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is False
        assert config["projects"]["backend"]["exploration"] is False

    def test_explore_all_already_enabled(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {"path": "/tmp/koan", "exploration": True},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="all")
        result = handle(ctx)

        assert "already" in result.lower()

    def test_explore_none_already_disabled(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "backend": {"path": "/tmp/backend", "exploration": False},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="none")
        result = handle(ctx)

        assert "already" in result.lower()

    def test_explore_all_with_none_entries(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "defaults": {"exploration": False},
            "projects": {
                "koan": None,
                "backend": {"path": "/tmp/backend", "exploration": False},
            }
        })
        ctx = _make_ctx("explore", tmp_path, args="all")
        result = handle(ctx)

        assert "enabled" in result.lower()
        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is True
        assert config["projects"]["backend"]["exploration"] is True


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    """Tests for error handling."""

    def test_no_projects_yaml(self, tmp_path):
        from skills.core.explore.handler import handle

        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        assert "❌" in result
        assert "projects.yaml" in result.lower()

    def test_empty_projects(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {"defaults": {}})
        ctx = _make_ctx("explore", tmp_path)
        result = handle(ctx)

        assert "❌" in result
        assert "No projects" in result

    def test_whitespace_args_treated_as_no_args(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {"koan": {"path": "/tmp/koan"}}
        })
        ctx = _make_ctx("explore", tmp_path, args="   ")
        result = handle(ctx)

        # Should show status, not error
        assert "🔭" in result
        assert "koan" in result


# ---------------------------------------------------------------------------
# Persistence verification
# ---------------------------------------------------------------------------

class TestPersistence:
    """Verify changes survive a round-trip through projects.yaml."""

    def test_roundtrip_enable(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "defaults": {"exploration": False},
            "projects": {
                "koan": {"path": "/tmp/koan"},
            }
        })

        # Enable
        ctx = _make_ctx("explore", tmp_path, args="koan")
        handle(ctx)

        # Verify by reading back
        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is True
        # defaults untouched
        assert config["defaults"]["exploration"] is False

    def test_roundtrip_disable(self, tmp_path):
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "backend": {"path": "/tmp/backend", "exploration": True},
            }
        })

        # Disable
        ctx = _make_ctx("noexplore", tmp_path, args="backend")
        handle(ctx)

        # Verify
        config = _read_config(tmp_path)
        assert config["projects"]["backend"]["exploration"] is False

    def test_other_config_preserved(self, tmp_path):
        """Toggling exploration doesn't lose other project settings."""
        from skills.core.explore.handler import handle

        _write_config(tmp_path, {
            "projects": {
                "koan": {
                    "path": "/tmp/koan",
                    "exploration": False,
                    "cli_provider": "claude",
                    "models": {"mission": "opus"},
                }
            }
        })

        ctx = _make_ctx("explore", tmp_path, args="koan")
        handle(ctx)

        config = _read_config(tmp_path)
        assert config["projects"]["koan"]["exploration"] is True
        assert config["projects"]["koan"]["cli_provider"] == "claude"
        assert config["projects"]["koan"]["models"]["mission"] == "opus"
