"""Tests for koan/app/projects_config.py — project configuration loader."""

import pytest
from pathlib import Path
from unittest.mock import patch

from app.projects_config import (
    load_projects_config,
    get_projects_from_config,
    get_project_config,
    get_project_auto_merge,
    get_project_cli_provider,
    get_project_exploration,
    get_project_max_open_prs,
    get_project_models,
    get_project_github_reply_authorized_users,
    get_project_submit_to_repository,
    get_project_tools,
    resolve_base_branch,
    validate_project_paths,
    _validate_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def koan_root(tmp_path):
    """A temporary KOAN_ROOT with no projects.yaml."""
    return str(tmp_path)


def _write_yaml(koan_root, content):
    """Write projects.yaml content to the given root."""
    Path(koan_root, "projects.yaml").write_text(content)


def _minimal_config(koan_root, extra=""):
    """Write a minimal valid projects.yaml."""
    _write_yaml(koan_root, f"""
projects:
  myapp:
    path: "/tmp/myapp"
{extra}""")


# ---------------------------------------------------------------------------
# load_projects_config
# ---------------------------------------------------------------------------


class TestLoadProjectsConfig:
    """Tests for load_projects_config()."""

    def test_returns_none_when_no_file(self, koan_root):
        assert load_projects_config(koan_root) is None

    def test_returns_none_for_empty_file(self, koan_root):
        _write_yaml(koan_root, "")
        assert load_projects_config(koan_root) is None

    def test_loads_minimal_config(self, koan_root):
        _minimal_config(koan_root)
        config = load_projects_config(koan_root)
        assert config is not None
        assert "projects" in config
        assert "myapp" in config["projects"]

    def test_loads_config_with_defaults(self, koan_root):
        _write_yaml(koan_root, """
defaults:
  git_auto_merge:
    enabled: true
    base_branch: main
projects:
  app:
    path: /tmp/app
""")
        config = load_projects_config(koan_root)
        assert config["defaults"]["git_auto_merge"]["enabled"] is True

    def test_raises_on_invalid_yaml(self, koan_root):
        _write_yaml(koan_root, ":\n  invalid: [yaml\n  unclosed")
        with pytest.raises(ValueError, match="Invalid YAML"):
            load_projects_config(koan_root)

    def test_raises_when_not_a_dict(self, koan_root):
        _write_yaml(koan_root, "- this is a list\n- not a dict")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_projects_config(koan_root)

    def test_missing_projects_section_is_valid(self, koan_root):
        """projects: section is optional — defaults-only yaml is valid."""
        _write_yaml(koan_root, "defaults:\n  enabled: true\n")
        config = load_projects_config(koan_root)
        assert config is not None
        assert "defaults" in config
        assert config.get("projects") is None

    def test_empty_projects_is_valid(self, koan_root):
        """Empty projects: dict is valid — workspace will provide projects."""
        _write_yaml(koan_root, "projects: {}")
        config = load_projects_config(koan_root)
        assert config is not None
        assert config["projects"] == {}

    def test_raises_when_projects_not_a_dict(self, koan_root):
        _write_yaml(koan_root, "projects:\n  - item1\n  - item2")
        with pytest.raises(ValueError, match="must be a mapping"):
            load_projects_config(koan_root)

    def test_allows_project_with_no_config(self, koan_root):
        """Project with no config (None value) is valid — workspace override."""
        _write_yaml(koan_root, "projects:\n  myapp:")
        config = load_projects_config(koan_root)
        assert "myapp" in config["projects"]

    def test_allows_project_missing_path(self, koan_root):
        """Project without path is valid — workspace provides the path."""
        _write_yaml(koan_root, "projects:\n  myapp:\n    other: value")
        config = load_projects_config(koan_root)
        assert config["projects"]["myapp"]["other"] == "value"

    def test_raises_when_path_is_empty(self, koan_root):
        _write_yaml(koan_root, 'projects:\n  myapp:\n    path: ""')
        with pytest.raises(ValueError, match="invalid path"):
            load_projects_config(koan_root)

    def test_raises_when_project_is_not_dict(self, koan_root):
        _write_yaml(koan_root, "projects:\n  myapp: just-a-string")
        with pytest.raises(ValueError, match="must be a mapping"):
            load_projects_config(koan_root)

    def test_raises_when_defaults_is_not_dict(self, koan_root):
        _write_yaml(koan_root, "defaults: not-a-dict\nprojects:\n  a:\n    path: /tmp/a")
        with pytest.raises(ValueError, match="'defaults' must be a mapping"):
            load_projects_config(koan_root)

    def test_raises_when_too_many_projects(self, koan_root):
        projects = "\n".join(
            f"  p{i}:\n    path: /tmp/p{i}" for i in range(51)
        )
        _write_yaml(koan_root, f"projects:\n{projects}")
        with pytest.raises(ValueError, match="Max 50 projects"):
            load_projects_config(koan_root)

    def test_50_projects_is_ok(self, koan_root):
        projects = "\n".join(
            f"  p{i}:\n    path: /tmp/p{i}" for i in range(50)
        )
        _write_yaml(koan_root, f"projects:\n{projects}")
        config = load_projects_config(koan_root)
        assert len(config["projects"]) == 50

    def test_multiple_projects(self, koan_root):
        _write_yaml(koan_root, """
projects:
  frontend:
    path: /tmp/frontend
  backend:
    path: /tmp/backend
  koan:
    path: /tmp/koan
""")
        config = load_projects_config(koan_root)
        assert len(config["projects"]) == 3


# ---------------------------------------------------------------------------
# validate_project_paths
# ---------------------------------------------------------------------------


class TestValidateProjectPaths:
    """Tests for validate_project_paths() — filesystem checks."""

    def test_valid_paths(self, tmp_path):
        project_dir = tmp_path / "myapp"
        project_dir.mkdir()
        config = {"projects": {"myapp": {"path": str(project_dir)}}}
        assert validate_project_paths(config) is None

    def test_missing_path(self):
        config = {"projects": {"myapp": {"path": "/nonexistent/path/xyz"}}}
        result = validate_project_paths(config)
        assert result is not None
        assert "does not exist" in result
        assert "myapp" in result

    def test_empty_projects(self):
        config = {"projects": {}}
        assert validate_project_paths(config) is None

    def test_multiple_projects_one_missing(self, tmp_path):
        good_dir = tmp_path / "good"
        good_dir.mkdir()
        config = {
            "projects": {
                "good": {"path": str(good_dir)},
                "bad": {"path": "/nonexistent/xyz"},
            }
        }
        result = validate_project_paths(config)
        assert "bad" in result


# ---------------------------------------------------------------------------
# get_projects_from_config
# ---------------------------------------------------------------------------


class TestGetProjectsFromConfig:
    """Tests for get_projects_from_config()."""

    def test_extracts_name_path_tuples(self):
        config = {
            "projects": {
                "koan": {"path": "/home/koan"},
                "web": {"path": "/home/web"},
            }
        }
        result = get_projects_from_config(config)
        assert ("koan", "/home/koan") in result
        assert ("web", "/home/web") in result

    def test_sorted_alphabetically(self):
        config = {
            "projects": {
                "zebra": {"path": "/z"},
                "alpha": {"path": "/a"},
                "middle": {"path": "/m"},
            }
        }
        result = get_projects_from_config(config)
        assert result[0][0] == "alpha"
        assert result[1][0] == "middle"
        assert result[2][0] == "zebra"

    def test_case_insensitive_sort(self):
        config = {
            "projects": {
                "Bravo": {"path": "/b"},
                "alpha": {"path": "/a"},
            }
        }
        result = get_projects_from_config(config)
        assert result[0][0] == "alpha"
        assert result[1][0] == "Bravo"

    def test_strips_path_whitespace(self):
        config = {"projects": {"app": {"path": "  /tmp/app  "}}}
        result = get_projects_from_config(config)
        assert result[0] == ("app", "/tmp/app")

    def test_empty_projects(self):
        config = {"projects": {}}
        assert get_projects_from_config(config) == []

    def test_missing_projects_key(self):
        config = {}
        assert get_projects_from_config(config) == []

    def test_single_project(self):
        config = {"projects": {"solo": {"path": "/solo"}}}
        result = get_projects_from_config(config)
        assert result == [("solo", "/solo")]


# ---------------------------------------------------------------------------
# get_project_config
# ---------------------------------------------------------------------------


class TestGetProjectConfig:
    """Tests for get_project_config() — merged defaults + project overrides."""

    def test_project_inherits_defaults(self):
        config = {
            "defaults": {
                "git_auto_merge": {"enabled": True, "base_branch": "main"},
            },
            "projects": {
                "app": {"path": "/app"},
            },
        }
        result = get_project_config(config, "app")
        assert result["git_auto_merge"]["enabled"] is True
        assert result["git_auto_merge"]["base_branch"] == "main"

    def test_project_overrides_defaults(self):
        config = {
            "defaults": {
                "git_auto_merge": {"enabled": True, "base_branch": "main", "strategy": "squash"},
            },
            "projects": {
                "app": {
                    "path": "/app",
                    "git_auto_merge": {"base_branch": "staging"},
                },
            },
        }
        result = get_project_config(config, "app")
        # Overridden
        assert result["git_auto_merge"]["base_branch"] == "staging"
        # Inherited
        assert result["git_auto_merge"]["enabled"] is True
        assert result["git_auto_merge"]["strategy"] == "squash"

    def test_no_defaults_section(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "git_auto_merge": {"enabled": False},
                },
            },
        }
        result = get_project_config(config, "app")
        assert result["git_auto_merge"]["enabled"] is False

    def test_unknown_project_returns_defaults(self):
        config = {
            "defaults": {"git_auto_merge": {"enabled": True}},
            "projects": {},
        }
        result = get_project_config(config, "nonexistent")
        assert result["git_auto_merge"]["enabled"] is True

    def test_path_excluded_from_merged_config(self):
        config = {
            "projects": {
                "app": {"path": "/app", "git_auto_merge": {"enabled": True}},
            },
        }
        result = get_project_config(config, "app")
        assert "path" not in result

    def test_project_only_keys_included(self):
        config = {
            "defaults": {},
            "projects": {
                "app": {"path": "/app", "custom_key": "custom_value"},
            },
        }
        result = get_project_config(config, "app")
        assert result["custom_key"] == "custom_value"

    def test_scalar_defaults_overridden(self):
        config = {
            "defaults": {"cli_provider": "claude"},
            "projects": {
                "app": {"path": "/app", "cli_provider": "copilot"},
            },
        }
        result = get_project_config(config, "app")
        assert result["cli_provider"] == "copilot"

    def test_none_defaults_handled(self):
        config = {
            "defaults": None,
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_config(config, "app")
        assert isinstance(result, dict)

    def test_none_project_handled(self):
        config = {
            "defaults": {"git_auto_merge": {"enabled": True}},
            "projects": {"app": None},
        }
        # get_project_config for a project with None config
        result = get_project_config(config, "app")
        assert result["git_auto_merge"]["enabled"] is True


# ---------------------------------------------------------------------------
# get_project_auto_merge
# ---------------------------------------------------------------------------


class TestGetProjectAutoMerge:
    """Tests for get_project_auto_merge()."""

    def test_returns_defaults_when_no_override(self):
        config = {
            "defaults": {
                "git_auto_merge": {
                    "enabled": True,
                    "base_branch": "main",
                    "strategy": "squash",
                    "rules": [{"pattern": "koan/*"}],
                },
            },
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_auto_merge(config, "app")
        assert result["enabled"] is True
        assert result["base_branch"] == "main"
        assert result["strategy"] == "squash"
        assert len(result["rules"]) == 1

    def test_project_override(self):
        config = {
            "defaults": {
                "git_auto_merge": {"enabled": True, "strategy": "squash"},
            },
            "projects": {
                "app": {
                    "path": "/app",
                    "git_auto_merge": {"strategy": "merge", "base_branch": "develop"},
                },
            },
        }
        result = get_project_auto_merge(config, "app")
        assert result["enabled"] is True  # inherited
        assert result["strategy"] == "merge"  # overridden
        assert result["base_branch"] == "develop"  # overridden

    def test_sensible_defaults_when_nothing_configured(self):
        config = {"projects": {"app": {"path": "/app"}}}
        result = get_project_auto_merge(config, "app")
        assert result["enabled"] is False
        assert result["base_branch"] == "main"
        assert result["strategy"] == "squash"
        assert result["rules"] == []

    def test_unknown_project_uses_defaults(self):
        config = {
            "defaults": {"git_auto_merge": {"enabled": True}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_auto_merge(config, "unknown")
        assert result["enabled"] is True


# ---------------------------------------------------------------------------
# get_project_exploration
# ---------------------------------------------------------------------------


class TestGetProjectExploration:
    """Tests for get_project_exploration() — per-project exploration flag."""

    def test_defaults_to_true_when_key_missing(self):
        config = {"projects": {"app": {"path": "/app"}}}
        assert get_project_exploration(config, "app") is True

    def test_explicit_false_returns_false(self):
        config = {"projects": {"app": {"path": "/app", "exploration": False}}}
        assert get_project_exploration(config, "app") is False

    def test_explicit_true_returns_true(self):
        config = {"projects": {"app": {"path": "/app", "exploration": True}}}
        assert get_project_exploration(config, "app") is True

    def test_defaults_section_override(self):
        config = {
            "defaults": {"exploration": False},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_exploration(config, "app") is False

    def test_project_overrides_defaults(self):
        config = {
            "defaults": {"exploration": False},
            "projects": {"app": {"path": "/app", "exploration": True}},
        }
        assert get_project_exploration(config, "app") is True

    def test_string_false_coerced(self):
        config = {"projects": {"app": {"path": "/app", "exploration": "false"}}}
        assert get_project_exploration(config, "app") is False

    def test_string_no_coerced(self):
        config = {"projects": {"app": {"path": "/app", "exploration": "no"}}}
        assert get_project_exploration(config, "app") is False

    def test_string_zero_coerced(self):
        config = {"projects": {"app": {"path": "/app", "exploration": "0"}}}
        assert get_project_exploration(config, "app") is False

    def test_string_true_coerced(self):
        config = {"projects": {"app": {"path": "/app", "exploration": "true"}}}
        assert get_project_exploration(config, "app") is True

    def test_int_zero_returns_false(self):
        config = {"projects": {"app": {"path": "/app", "exploration": 0}}}
        assert get_project_exploration(config, "app") is False

    def test_int_one_returns_true(self):
        config = {"projects": {"app": {"path": "/app", "exploration": 1}}}
        assert get_project_exploration(config, "app") is True

    def test_unknown_project_returns_default_true(self):
        config = {"projects": {"app": {"path": "/app"}}}
        assert get_project_exploration(config, "unknown") is True

    def test_unknown_project_inherits_defaults_false(self):
        config = {
            "defaults": {"exploration": False},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_exploration(config, "unknown") is False

    def test_none_project_config_returns_default(self):
        config = {
            "defaults": {"exploration": True},
            "projects": {"app": None},
        }
        assert get_project_exploration(config, "app") is True


# ---------------------------------------------------------------------------
# get_project_max_open_prs
# ---------------------------------------------------------------------------


class TestGetProjectMaxOpenPrs:
    """Tests for get_project_max_open_prs() — per-project PR limit."""

    def test_defaults_to_zero_when_key_missing(self):
        config = {"projects": {"app": {"path": "/app"}}}
        assert get_project_max_open_prs(config, "app") == 0

    def test_explicit_zero_returns_zero(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": 0}}}
        assert get_project_max_open_prs(config, "app") == 0

    def test_positive_int_returns_value(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": 10}}}
        assert get_project_max_open_prs(config, "app") == 10

    def test_string_int_coerced(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": "5"}}}
        assert get_project_max_open_prs(config, "app") == 5

    def test_negative_returns_zero(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": -1}}}
        assert get_project_max_open_prs(config, "app") == 0

    def test_none_returns_zero(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": None}}}
        assert get_project_max_open_prs(config, "app") == 0

    def test_invalid_string_returns_zero(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": "abc"}}}
        assert get_project_max_open_prs(config, "app") == 0

    def test_float_coerced_to_int(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": 3.7}}}
        assert get_project_max_open_prs(config, "app") == 3

    def test_empty_string_returns_zero(self):
        config = {"projects": {"app": {"path": "/app", "max_open_prs": ""}}}
        assert get_project_max_open_prs(config, "app") == 0

    def test_defaults_section_applies(self):
        config = {
            "defaults": {"max_open_prs": 10},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_max_open_prs(config, "app") == 10

    def test_project_overrides_defaults(self):
        config = {
            "defaults": {"max_open_prs": 10},
            "projects": {"app": {"path": "/app", "max_open_prs": 3}},
        }
        assert get_project_max_open_prs(config, "app") == 3

    def test_unknown_project_returns_default_zero(self):
        config = {"projects": {"app": {"path": "/app"}}}
        assert get_project_max_open_prs(config, "unknown") == 0

    def test_unknown_project_inherits_defaults(self):
        config = {
            "defaults": {"max_open_prs": 5},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_max_open_prs(config, "unknown") == 5

    def test_none_project_config_returns_default(self):
        config = {
            "defaults": {"max_open_prs": 8},
            "projects": {"app": None},
        }
        assert get_project_max_open_prs(config, "app") == 8


# ---------------------------------------------------------------------------
# get_project_github_reply_authorized_users
# ---------------------------------------------------------------------------


class TestGetProjectGithubReplyAuthorizedUsers:
    """Tests for get_project_github_reply_authorized_users()."""

    def test_returns_none_when_unset(self):
        config = {"projects": {"app": {"path": "/app", "github": {}}}}
        assert get_project_github_reply_authorized_users(config, "app") is None

    def test_returns_list_when_set(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "github": {"reply_authorized_users": ["alice", "bob"]},
                }
            }
        }
        assert get_project_github_reply_authorized_users(config, "app") == ["alice", "bob"]

    def test_returns_empty_list_when_explicitly_empty(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "github": {"reply_authorized_users": []},
                }
            }
        }
        assert get_project_github_reply_authorized_users(config, "app") == []

    def test_defaults_inherited_via_get_project_config(self):
        config = {
            "defaults": {"github": {"reply_authorized_users": ["team"]}},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_github_reply_authorized_users(config, "app") == ["team"]

    def test_invalid_type_returns_none(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "github": {"reply_authorized_users": "alice"},
                }
            }
        }
        assert get_project_github_reply_authorized_users(config, "app") is None


# ---------------------------------------------------------------------------
# Integration: load + get
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end tests: load_projects_config → get_projects_from_config."""

    def test_full_workflow(self, koan_root):
        _write_yaml(koan_root, """
defaults:
  git_auto_merge:
    enabled: false
    base_branch: main
    strategy: squash

projects:
  koan:
    path: /home/koan
    git_auto_merge:
      enabled: false
      strategy: merge

  backend:
    path: /home/backend
    git_auto_merge:
      base_branch: staging

  frontend:
    path: /home/frontend
""")
        config = load_projects_config(koan_root)

        # Projects extraction
        projects = get_projects_from_config(config)
        assert len(projects) == 3
        assert projects[0][0] == "backend"  # sorted
        assert projects[1][0] == "frontend"
        assert projects[2][0] == "koan"

        # Koan auto-merge
        koan_am = get_project_auto_merge(config, "koan")
        assert koan_am["enabled"] is False
        assert koan_am["strategy"] == "merge"
        assert koan_am["base_branch"] == "main"  # inherited

        # Backend auto-merge
        backend_am = get_project_auto_merge(config, "backend")
        assert backend_am["base_branch"] == "staging"
        assert backend_am["strategy"] == "squash"  # inherited

        # Frontend auto-merge (pure defaults)
        frontend_am = get_project_auto_merge(config, "frontend")
        assert frontend_am["enabled"] is False
        assert frontend_am["base_branch"] == "main"
        assert frontend_am["strategy"] == "squash"

    def test_minimal_config(self, koan_root):
        _write_yaml(koan_root, """
projects:
  solo:
    path: /home/solo
""")
        config = load_projects_config(koan_root)
        projects = get_projects_from_config(config)
        assert projects == [("solo", "/home/solo")]

        am = get_project_auto_merge(config, "solo")
        assert am["enabled"] is False

    def test_projects_yaml_with_comments(self, koan_root):
        _write_yaml(koan_root, """
# This is a comment
defaults:
  # Another comment
  git_auto_merge:
    enabled: true
    base_branch: main

projects:
  myapp:
    path: /tmp/myapp
    # Per-project override
    git_auto_merge:
      strategy: rebase
""")
        config = load_projects_config(koan_root)
        assert config is not None
        am = get_project_auto_merge(config, "myapp")
        assert am["strategy"] == "rebase"
        assert am["enabled"] is True  # inherited from defaults


# ---------------------------------------------------------------------------
# get_known_projects integration with projects.yaml
# ---------------------------------------------------------------------------


class TestGetKnownProjectsWithYaml:
    """Tests for get_known_projects() when projects.yaml exists."""

    def test_projects_yaml_takes_priority(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "envproject:/env/path")

        # Write projects.yaml
        (tmp_path / "projects.yaml").write_text("""
projects:
  yamlproject:
    path: /yaml/path
""")
        from app.utils import get_known_projects
        result = get_known_projects()
        assert len(result) == 1
        assert result[0] == ("yamlproject", "/yaml/path")

    def test_falls_back_to_env_when_no_yaml(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "envproject:/env/path")

        # No projects.yaml
        from app.utils import get_known_projects
        result = get_known_projects()
        assert len(result) == 1
        assert result[0] == ("envproject", "/env/path")

    def test_falls_back_on_invalid_yaml(self, tmp_path, monkeypatch):
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "fallback:/fallback")

        # Write broken projects.yaml
        (tmp_path / "projects.yaml").write_text("not valid: [yaml")
        from app.utils import get_known_projects
        result = get_known_projects()
        assert len(result) == 1
        assert result[0] == ("fallback", "/fallback")

    def test_defaults_only_yaml_returns_no_projects(self, tmp_path, monkeypatch):
        """projects.yaml with only defaults: and no projects: is valid; returns [] projects."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.setenv("KOAN_PROJECTS", "fallback:/fallback")

        # Valid YAML with only defaults: — no longer a schema error
        (tmp_path / "projects.yaml").write_text("defaults:\n  enabled: true\n")
        from app.utils import get_known_projects
        result = get_known_projects()
        # No projects configured — empty list (no fallback to KOAN_PROJECTS)
        assert result == []

    def test_legacy_project_path_no_longer_supported(self, tmp_path, monkeypatch):
        """KOAN_PROJECT_PATH is no longer a fallback — returns empty list."""
        from app import utils
        monkeypatch.setattr(utils, "KOAN_ROOT", tmp_path)
        monkeypatch.delenv("KOAN_PROJECTS", raising=False)
        monkeypatch.setenv("KOAN_PROJECT_PATH", "/legacy/path")

        # No projects.yaml
        from app.utils import get_known_projects
        result = get_known_projects()
        assert result == []


# ---------------------------------------------------------------------------
# get_auto_merge_config integration with projects.yaml
# ---------------------------------------------------------------------------


class TestAutoMergeConfigWithYaml:
    """Tests for get_auto_merge_config() when projects.yaml exists."""

    def test_reads_from_projects_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        (tmp_path / "projects.yaml").write_text("""
defaults:
  git_auto_merge:
    enabled: true
    strategy: squash
projects:
  myapp:
    path: /tmp/myapp
    git_auto_merge:
      strategy: merge
""")
        from app.config import get_auto_merge_config
        result = get_auto_merge_config({}, "myapp")
        assert result["strategy"] == "merge"
        assert result["enabled"] is True

    def test_falls_back_to_config_yaml_global(self, tmp_path, monkeypatch):
        """Without projects.yaml, config.yaml global settings are used (projects: section ignored)."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        # No projects.yaml

        config = {
            "git_auto_merge": {"enabled": True, "base_branch": "main", "strategy": "squash"},
            "projects": {"app": {"git_auto_merge": {"strategy": "rebase"}}},
        }
        from app.config import get_auto_merge_config
        result = get_auto_merge_config(config, "app")
        # config.yaml projects: section is ignored — global "squash" used
        assert result["strategy"] == "squash"
        assert result["enabled"] is True

    def test_unknown_project_in_yaml_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))

        (tmp_path / "projects.yaml").write_text("""
projects:
  known:
    path: /tmp/known
""")
        config = {
            "git_auto_merge": {"enabled": True, "strategy": "squash"},
        }
        from app.config import get_auto_merge_config
        # 'unknown' is not in projects.yaml — falls back to config.yaml
        result = get_auto_merge_config(config, "unknown")
        assert result["enabled"] is True
        assert result["strategy"] == "squash"


# ---------------------------------------------------------------------------
# get_project_cli_provider
# ---------------------------------------------------------------------------


class TestGetProjectCliProvider:
    """Tests for get_project_cli_provider() — per-project CLI provider."""

    def test_returns_project_provider(self):
        config = {
            "defaults": {"cli_provider": "claude"},
            "projects": {"app": {"path": "/app", "cli_provider": "copilot"}},
        }
        assert get_project_cli_provider(config, "app") == "copilot"

    def test_inherits_default_provider(self):
        config = {
            "defaults": {"cli_provider": "claude"},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_cli_provider(config, "app") == "claude"

    def test_returns_empty_when_not_configured(self):
        config = {
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_cli_provider(config, "app") == ""

    def test_unknown_project_returns_default(self):
        config = {
            "defaults": {"cli_provider": "local"},
            "projects": {"app": {"path": "/app"}},
        }
        assert get_project_cli_provider(config, "unknown") == "local"

    def test_normalizes_to_lowercase(self):
        config = {
            "projects": {"app": {"path": "/app", "cli_provider": "Claude"}},
        }
        assert get_project_cli_provider(config, "app") == "claude"

    def test_strips_whitespace(self):
        config = {
            "projects": {"app": {"path": "/app", "cli_provider": "  copilot  "}},
        }
        assert get_project_cli_provider(config, "app") == "copilot"


# ---------------------------------------------------------------------------
# get_project_models
# ---------------------------------------------------------------------------


class TestGetProjectModels:
    """Tests for get_project_models() — per-project model overrides."""

    def test_returns_project_models(self):
        config = {
            "defaults": {"models": {"mission": "opus", "chat": "sonnet"}},
            "projects": {"app": {"path": "/app", "models": {"mission": "haiku"}}},
        }
        result = get_project_models(config, "app")
        # Project override merged with defaults
        assert result["mission"] == "haiku"
        assert result["chat"] == "sonnet"

    def test_inherits_defaults_when_no_project_models(self):
        config = {
            "defaults": {"models": {"mission": "opus"}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_models(config, "app")
        assert result["mission"] == "opus"

    def test_returns_empty_when_not_configured(self):
        config = {
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_models(config, "app")
        assert result == {}

    def test_handles_non_dict_models(self):
        config = {
            "projects": {"app": {"path": "/app", "models": "invalid"}},
        }
        result = get_project_models(config, "app")
        assert result == {}

    def test_project_overrides_specific_keys(self):
        config = {
            "defaults": {"models": {"mission": "opus", "chat": "opus", "lightweight": "haiku"}},
            "projects": {
                "small": {"path": "/small", "models": {"mission": "sonnet"}},
            },
        }
        result = get_project_models(config, "small")
        assert result["mission"] == "sonnet"
        assert result["chat"] == "opus"
        assert result["lightweight"] == "haiku"

    def test_unknown_project_returns_defaults(self):
        config = {
            "defaults": {"models": {"mission": "opus"}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_models(config, "unknown")
        assert result["mission"] == "opus"


# ---------------------------------------------------------------------------
# get_project_tools
# ---------------------------------------------------------------------------


class TestGetProjectTools:
    """Tests for get_project_tools() — per-project tool restrictions."""

    def test_returns_project_tools(self):
        config = {
            "defaults": {
                "tools": {"mission": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]},
            },
            "projects": {
                "readonly": {
                    "path": "/readonly",
                    "tools": {"mission": ["Read", "Glob", "Grep"]},
                },
            },
        }
        result = get_project_tools(config, "readonly")
        assert result["mission"] == ["Read", "Glob", "Grep"]

    def test_inherits_default_tools(self):
        config = {
            "defaults": {"tools": {"mission": ["Read", "Bash"]}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_tools(config, "app")
        assert result["mission"] == ["Read", "Bash"]

    def test_returns_empty_when_not_configured(self):
        config = {
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_tools(config, "app")
        assert result == {}

    def test_handles_non_dict_tools(self):
        config = {
            "projects": {"app": {"path": "/app", "tools": "invalid"}},
        }
        result = get_project_tools(config, "app")
        assert result == {}

    def test_chat_tools_override(self):
        config = {
            "defaults": {"tools": {"chat": ["Read", "Glob", "Grep"]}},
            "projects": {
                "app": {
                    "path": "/app",
                    "tools": {"chat": ["Read"]},
                },
            },
        }
        result = get_project_tools(config, "app")
        assert result["chat"] == ["Read"]

    def test_mixed_mission_and_chat(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "tools": {
                        "mission": ["Read", "Glob", "Grep"],
                        "chat": ["Read"],
                    },
                },
            },
        }
        result = get_project_tools(config, "app")
        assert result["mission"] == ["Read", "Glob", "Grep"]
        assert result["chat"] == ["Read"]


# ---------------------------------------------------------------------------
# Per-project config integration with config.py
# ---------------------------------------------------------------------------


class TestPerProjectModelConfig:
    """Integration tests for get_model_config() with per-project overrides."""

    def test_project_model_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
defaults:
  models:
    mission: "opus"
    chat: "opus"
projects:
  small-lib:
    path: /tmp/small-lib
    models:
      mission: "sonnet"
""")
        from app.config import get_model_config

        with patch("app.config._load_config", return_value={"models": {"mission": "opus", "chat": "opus"}}):
            result = get_model_config("small-lib")
        assert result["mission"] == "sonnet"
        assert result["chat"] == "opus"

    def test_no_project_override_uses_global(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  app:
    path: /tmp/app
""")
        from app.config import get_model_config

        with patch("app.config._load_config", return_value={"models": {"mission": "opus"}}):
            result = get_model_config("app")
        assert result["mission"] == "opus"

    def test_empty_project_name_uses_global(self):
        from app.config import get_model_config

        with patch("app.config._load_config", return_value={"models": {"mission": "opus"}}):
            result = get_model_config("")
        assert result["mission"] == "opus"

    def test_unknown_project_uses_global(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  app:
    path: /tmp/app
""")
        from app.config import get_model_config

        with patch("app.config._load_config", return_value={"models": {"mission": "opus"}}):
            result = get_model_config("nonexistent")
        assert result["mission"] == "opus"


class TestPerProjectToolConfig:
    """Integration tests for get_mission_tools()/get_chat_tools() with per-project overrides."""

    def test_project_mission_tools_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  readonly:
    path: /tmp/readonly
    tools:
      mission: ["Read", "Glob", "Grep"]
""")
        from app.config import get_mission_tools

        with patch("app.config._load_config", return_value={}):
            result = get_mission_tools("readonly")
        assert result == "Read,Glob,Grep"

    def test_project_chat_tools_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  restricted:
    path: /tmp/restricted
    tools:
      chat: ["Read"]
""")
        from app.config import get_chat_tools

        with patch("app.config._load_config", return_value={}):
            result = get_chat_tools("restricted")
        assert result == "Read"

    def test_no_project_override_uses_global(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  app:
    path: /tmp/app
""")
        from app.config import get_mission_tools

        with patch("app.config._load_config", return_value={}):
            result = get_mission_tools("app")
        assert result == "Read,Glob,Grep,Edit,Write,Bash"

    def test_empty_project_name_uses_global(self):
        from app.config import get_mission_tools

        with patch("app.config._load_config", return_value={}):
            result = get_mission_tools("")
        assert result == "Read,Glob,Grep,Edit,Write,Bash"

    def test_defaults_section_tools_inherited(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
defaults:
  tools:
    mission: ["Read", "Bash"]
projects:
  app:
    path: /tmp/app
""")
        from app.config import get_mission_tools

        with patch("app.config._load_config", return_value={}):
            result = get_mission_tools("app")
        assert result == "Read,Bash"


class TestPerProjectFlagsForRole:
    """Integration tests for get_claude_flags_for_role() with per-project overrides."""

    def test_project_model_used_in_flags(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  small:
    path: /tmp/small
    models:
      mission: "sonnet"
""")
        from app.config import get_claude_flags_for_role
        from app.provider import reset_provider

        reset_provider()
        with patch("app.config._load_config", return_value={}):
            result = get_claude_flags_for_role("mission", project_name="small")
        assert "--model" in result
        assert "sonnet" in result

    def test_no_project_uses_global_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        # No projects.yaml
        from app.config import get_claude_flags_for_role
        from app.provider import reset_provider

        reset_provider()
        with patch("app.config._load_config", return_value={"models": {"mission": "opus"}}):
            result = get_claude_flags_for_role("mission", project_name="nonexistent")
        assert "opus" in result


# ---------------------------------------------------------------------------
# resolve_base_branch
# ---------------------------------------------------------------------------


class TestResolveBaseBranch:
    """Tests for resolve_base_branch() — resolves base branch from projects.yaml."""

    def test_returns_main_by_default(self, monkeypatch):
        """No KOAN_ROOT set — returns 'main'."""
        monkeypatch.delenv("KOAN_ROOT", raising=False)
        assert resolve_base_branch("app") == "main"

    def test_returns_main_when_no_projects_yaml(self, tmp_path, monkeypatch):
        """KOAN_ROOT set but no projects.yaml — returns 'main'."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        assert resolve_base_branch("app") == "main"

    def test_returns_configured_base_branch(self, tmp_path, monkeypatch):
        """projects.yaml has base_branch configured — returns it."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  backend:
    path: /tmp/backend
    git_auto_merge:
      base_branch: staging
""")
        assert resolve_base_branch("backend") == "staging"

    def test_inherits_default_base_branch(self, tmp_path, monkeypatch):
        """defaults.git_auto_merge.base_branch inherited when project doesn't override."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
defaults:
  git_auto_merge:
    base_branch: develop
projects:
  app:
    path: /tmp/app
""")
        assert resolve_base_branch("app") == "develop"

    def test_project_overrides_default_base_branch(self, tmp_path, monkeypatch):
        """Project-level base_branch overrides defaults."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
defaults:
  git_auto_merge:
    base_branch: develop
projects:
  app:
    path: /tmp/app
    git_auto_merge:
      base_branch: release
""")
        assert resolve_base_branch("app") == "release"

    def test_unknown_project_returns_default(self, tmp_path, monkeypatch):
        """Unknown project inherits defaults — returns 'main' if no default set."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  app:
    path: /tmp/app
""")
        assert resolve_base_branch("nonexistent") == "main"

    def test_unknown_project_inherits_default_branch(self, tmp_path, monkeypatch):
        """Unknown project inherits defaults.git_auto_merge.base_branch."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
defaults:
  git_auto_merge:
    base_branch: develop
projects:
  app:
    path: /tmp/app
""")
        assert resolve_base_branch("nonexistent") == "develop"

    def test_empty_koan_root_returns_main(self, monkeypatch):
        """KOAN_ROOT set to empty string — returns 'main'."""
        monkeypatch.setenv("KOAN_ROOT", "")
        assert resolve_base_branch("app") == "main"

    def test_invalid_yaml_returns_main(self, tmp_path, monkeypatch):
        """Broken projects.yaml — returns 'main' gracefully."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("not: [valid yaml")
        assert resolve_base_branch("app") == "main"

    def test_no_auto_merge_section_returns_main(self, tmp_path, monkeypatch):
        """Project exists but has no git_auto_merge config."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  app:
    path: /tmp/app
    cli_provider: claude
""")
        assert resolve_base_branch("app") == "main"

    def test_auto_detection_with_project_path(self, tmp_path, monkeypatch):
        """When project_path is provided and no explicit config, auto-detect from remote."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
defaults:
  git_auto_merge:
    base_branch: main
projects:
  myrepo:
    path: /tmp/myrepo
""")
        # Mock auto-detection to return "master"
        monkeypatch.setattr(
            "app.git_prep.detect_remote_default_branch",
            lambda remote, path: "master",
        )
        monkeypatch.setattr(
            "app.git_prep.get_upstream_remote",
            lambda path, name, root: "origin",
        )
        assert resolve_base_branch("myrepo", "/tmp/myrepo") == "master"

    def test_auto_detection_skipped_without_project_path(self, tmp_path, monkeypatch):
        """Without project_path, auto-detection is skipped — falls back to config."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
defaults:
  git_auto_merge:
    base_branch: main
projects:
  myrepo:
    path: /tmp/myrepo
""")
        # Even if detect would return "master", without project_path it's not called
        assert resolve_base_branch("myrepo") == "main"

    def test_explicit_project_config_overrides_detection(self, tmp_path, monkeypatch):
        """Explicit project-level base_branch trumps auto-detection."""
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "projects.yaml").write_text("""
projects:
  myrepo:
    path: /tmp/myrepo
    git_auto_merge:
      base_branch: develop
""")
        # detect_remote_default_branch should NOT be called
        def fail_detect(*a, **kw):
            raise AssertionError("Should not be called")
        monkeypatch.setattr(
            "app.git_prep.detect_remote_default_branch", fail_detect,
        )
        assert resolve_base_branch("myrepo", "/tmp/myrepo") == "develop"


# ---------------------------------------------------------------------------
# get_project_submit_to_repository (additional tests in test_projects_config)
# ---------------------------------------------------------------------------


class TestGetProjectSubmitToRepository:
    """Tests for get_project_submit_to_repository() — fork submission config."""

    def test_returns_empty_when_not_configured(self):
        config = {"projects": {"app": {"path": "/app"}}}
        assert get_project_submit_to_repository(config, "app") == {}

    def test_returns_repo_and_remote(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": {
                        "repo": "upstream/app",
                        "remote": "upstream",
                    },
                }
            }
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "upstream/app", "remote": "upstream"}

    def test_returns_repo_only(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": {"repo": "upstream/app"},
                }
            }
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "upstream/app"}

    def test_returns_remote_only(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": {"remote": "upstream"},
                }
            }
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"remote": "upstream"}

    def test_inherits_from_defaults(self):
        config = {
            "defaults": {
                "submit_to_repository": {"repo": "default/repo", "remote": "upstream"},
            },
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "default/repo", "remote": "upstream"}

    def test_project_overrides_defaults(self):
        config = {
            "defaults": {
                "submit_to_repository": {"repo": "default/repo", "remote": "upstream"},
            },
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": {"repo": "custom/repo", "remote": "origin"},
                }
            },
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "custom/repo", "remote": "origin"}

    def test_invalid_type_returns_empty(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": "invalid",
                }
            }
        }
        assert get_project_submit_to_repository(config, "app") == {}

    def test_unknown_project_returns_default(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "up/stream"}},
            "projects": {"app": {"path": "/app"}},
        }
        result = get_project_submit_to_repository(config, "unknown")
        assert result == {"repo": "up/stream"}

    def test_empty_values_excluded(self):
        config = {
            "projects": {
                "app": {
                    "path": "/app",
                    "submit_to_repository": {"repo": "", "remote": "upstream"},
                }
            }
        }
        result = get_project_submit_to_repository(config, "app")
        # Empty repo should not appear in result
        assert "repo" not in result
        assert result == {"remote": "upstream"}

    def test_none_project_config_returns_default(self):
        config = {
            "defaults": {"submit_to_repository": {"repo": "up/stream"}},
            "projects": {"app": None},
        }
        result = get_project_submit_to_repository(config, "app")
        assert result == {"repo": "up/stream"}
