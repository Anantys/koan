"""Tests for projects_merged.py."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.projects_merged import (
    get_all_projects,
    refresh_projects,
    get_warnings,
    invalidate_cache,
    get_all_github_urls,
    get_all_github_urls_cache,
    get_github_url,
    set_all_github_urls,
    set_github_url,
    get_github_url_cache,
    clear_github_url_cache,
)


@pytest.fixture(autouse=True)
def clean_cache():
    """Reset module caches before each test."""
    invalidate_cache()
    clear_github_url_cache()
    yield
    invalidate_cache()
    clear_github_url_cache()


@pytest.fixture
def koan_root(tmp_path):
    """Create a KOAN_ROOT with workspace/ directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return tmp_path


def _write_projects_yaml(root, content):
    """Write a projects.yaml file."""
    (root / "projects.yaml").write_text(content)


class TestGetAllProjects:
    def test_workspace_only(self, koan_root):
        """Workspace projects discovered when no yaml."""
        ws = koan_root / "workspace"
        (ws / "alpha").mkdir()
        (ws / "beta").mkdir()

        result = get_all_projects(str(koan_root))
        assert len(result) == 2
        assert result[0][0] == "alpha"
        assert result[1][0] == "beta"

    def test_yaml_only(self, koan_root):
        """Yaml-only projects still work."""
        proj_dir = koan_root / "myapp"
        proj_dir.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  myapp:
    path: "{proj_dir}"
""")
        result = get_all_projects(str(koan_root))
        assert len(result) == 1
        assert result[0][0] == "myapp"

    def test_mixed_sources(self, koan_root):
        """Yaml and workspace projects merge."""
        proj_dir = koan_root / "yaml-proj"
        proj_dir.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  yaml-proj:
    path: "{proj_dir}"
""")
        ws = koan_root / "workspace"
        (ws / "ws-proj").mkdir()

        result = get_all_projects(str(koan_root))
        names = [n for n, _ in result]
        assert "yaml-proj" in names
        assert "ws-proj" in names

    def test_yaml_wins_on_duplicate(self, koan_root):
        """When same name exists in both, yaml path wins."""
        yaml_dir = koan_root / "yaml-path"
        yaml_dir.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  myproj:
    path: "{yaml_dir}"
""")
        ws = koan_root / "workspace"
        (ws / "myproj").mkdir()

        result = get_all_projects(str(koan_root))
        assert len(result) == 1
        assert result[0][0] == "myproj"
        assert result[0][1] == str(yaml_dir)

    def test_duplicate_warning(self, koan_root):
        """Duplicate with different paths generates a warning."""
        yaml_dir = koan_root / "yaml-path"
        yaml_dir.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  myproj:
    path: "{yaml_dir}"
""")
        ws = koan_root / "workspace"
        (ws / "myproj").mkdir()

        refresh_projects(str(koan_root))
        warnings = get_warnings()
        assert len(warnings) == 1
        assert "Duplicate" in warnings[0]
        assert "myproj" in warnings[0]

    def test_no_warning_when_paths_identical(self, koan_root):
        """Duplicate with identical paths (yaml path == workspace path) emits no warning."""
        ws = koan_root / "workspace"
        (ws / "myproj").mkdir()
        ws_path = str(ws / "myproj")
        # yaml entry points to the exact same path as workspace discovery
        _write_projects_yaml(koan_root, f"""
projects:
  myproj:
    path: "{ws_path}"
""")

        refresh_projects(str(koan_root))
        warnings = get_warnings()
        assert not any("Duplicate" in w for w in warnings)

    def test_workspace_with_yaml_override_no_path(self, koan_root):
        """Yaml entry without path uses workspace path for the project."""
        ws = koan_root / "workspace"
        (ws / "myproj").mkdir()
        _write_projects_yaml(koan_root, """
projects:
  myproj:
    models:
      mission: "opus"
""")
        result = get_all_projects(str(koan_root))
        assert len(result) == 1
        assert result[0][0] == "myproj"
        # Path comes from workspace
        assert "workspace" in result[0][1]


class TestProjectLimit:
    def test_limit_enforced(self, koan_root):
        """Projects exceeding 50 are truncated."""
        ws = koan_root / "workspace"
        for i in range(55):
            (ws / f"proj-{i:03d}").mkdir()

        result = get_all_projects(str(koan_root))
        assert len(result) == 50

        warnings = get_warnings()
        assert any("55 projects" in w for w in warnings)


class TestCaching:
    def test_cache_hit(self, koan_root):
        """Second call returns cached result without re-scanning."""
        ws = koan_root / "workspace"
        (ws / "proj").mkdir()

        result1 = get_all_projects(str(koan_root))
        # Second call without changes — should return cached result
        result2 = get_all_projects(str(koan_root))
        assert result1 == result2

    def test_invalidate_and_rescan(self, koan_root):
        """invalidate_cache() forces re-scan."""
        ws = koan_root / "workspace"
        (ws / "proj").mkdir()

        result1 = get_all_projects(str(koan_root))
        assert len(result1) == 1

        (ws / "proj").rmdir()
        invalidate_cache()
        result2 = get_all_projects(str(koan_root))
        assert len(result2) == 0

    def test_refresh_updates_cache(self, koan_root):
        """refresh_projects() updates the cache."""
        ws = koan_root / "workspace"
        (ws / "proj1").mkdir()

        get_all_projects(str(koan_root))
        (ws / "proj2").mkdir()

        result = refresh_projects(str(koan_root))
        assert len(result) == 2

    def test_workspace_change_invalidates_cache(self, koan_root):
        """Adding a project to workspace/ invalidates cache automatically."""
        ws = koan_root / "workspace"
        (ws / "proj1").mkdir()

        result1 = get_all_projects(str(koan_root))
        assert len(result1) == 1

        # Adding a new directory changes workspace/ mtime
        (ws / "proj2").mkdir()
        # Ensure mtime differs (filesystem resolution can be 1s on Linux)
        import os
        os.utime(str(ws), (ws.stat().st_atime + 1, ws.stat().st_mtime + 1))

        # get_all_projects should detect the mtime change and re-scan
        result2 = get_all_projects(str(koan_root))
        assert len(result2) == 2
        names = [n for n, _ in result2]
        assert "proj1" in names
        assert "proj2" in names

    def test_workspace_mtime_no_change_cache_hit(self, koan_root):
        """When workspace/ mtime unchanged, cache is used."""
        ws = koan_root / "workspace"
        (ws / "proj").mkdir()

        result1 = get_all_projects(str(koan_root))
        # Modify a file inside the workspace project (doesn't change ws/ mtime)
        (ws / "proj" / "README.md").write_text("hello")
        result2 = get_all_projects(str(koan_root))
        assert result1 == result2

    def test_no_workspace_dir_cache_works(self, tmp_path):
        """Cache works even when workspace/ doesn't exist."""
        proj_dir = tmp_path / "myapp"
        proj_dir.mkdir()
        _write_projects_yaml(tmp_path, f"""
projects:
  myapp:
    path: "{proj_dir}"
""")
        result1 = get_all_projects(str(tmp_path))
        result2 = get_all_projects(str(tmp_path))
        assert result1 == result2 == [("myapp", str(proj_dir))]

    def test_workspace_created_after_cache_invalidates(self, tmp_path):
        """Creating workspace/ after initial cache triggers rescan."""
        proj_dir = tmp_path / "myapp"
        proj_dir.mkdir()
        _write_projects_yaml(tmp_path, f"""
projects:
  myapp:
    path: "{proj_dir}"
""")
        result1 = get_all_projects(str(tmp_path))
        assert len(result1) == 1

        # Create workspace/ with a new project
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "ws-proj").mkdir()

        result2 = get_all_projects(str(tmp_path))
        assert len(result2) == 2
        names = [n for n, _ in result2]
        assert "myapp" in names
        assert "ws-proj" in names


class TestGithubUrlCache:
    def test_set_and_get(self):
        set_github_url("myproj", "https://github.com/me/myproj")
        assert get_github_url("myproj") == "https://github.com/me/myproj"

    def test_get_missing(self):
        assert get_github_url("nonexistent") is None

    def test_get_cache_dict(self):
        set_github_url("a", "url-a")
        set_github_url("b", "url-b")
        cache = get_github_url_cache()
        assert cache == {"a": "url-a", "b": "url-b"}

    def test_clear(self):
        set_github_url("a", "url-a")
        clear_github_url_cache()
        assert get_github_url("a") is None


class TestEdgeCases:
    def test_no_workspace_no_yaml(self, tmp_path):
        """No workspace, no yaml — returns empty."""
        result = get_all_projects(str(tmp_path))
        assert result == []

    def test_invalid_yaml_still_returns_workspace(self, koan_root):
        """Bad yaml doesn't prevent workspace discovery."""
        (koan_root / "projects.yaml").write_text("{{invalid yaml")
        ws = koan_root / "workspace"
        (ws / "proj").mkdir()

        result = get_all_projects(str(koan_root))
        assert len(result) == 1
        assert result[0][0] == "proj"
        warnings = get_warnings()
        assert any("projects.yaml" in w for w in warnings)

    def test_sorted_output(self, koan_root):
        """Output is sorted case-insensitively."""
        ws = koan_root / "workspace"
        (ws / "Zebra").mkdir()
        (ws / "alpha").mkdir()

        proj_dir = koan_root / "mypath"
        proj_dir.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  Middle:
    path: "{proj_dir}"
""")
        result = get_all_projects(str(koan_root))
        names = [n for n, _ in result]
        assert names == ["alpha", "Middle", "Zebra"]


class TestGetYamlProjectNames:
    """Tests for get_yaml_project_names()."""

    def test_returns_names_with_paths(self, koan_root):
        """Returns names of projects that have path configured."""
        from app.projects_merged import get_yaml_project_names
        proj_dir = koan_root / "myapp"
        proj_dir.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  myapp:
    path: "{proj_dir}"
  overrides_only:
    models:
      mission: "opus"
""")
        names = get_yaml_project_names(str(koan_root))
        assert "myapp" in names
        assert "overrides_only" not in names

    def test_returns_empty_when_no_yaml(self, tmp_path):
        """Returns empty set when projects.yaml doesn't exist."""
        from app.projects_merged import get_yaml_project_names
        assert get_yaml_project_names(str(tmp_path)) == set()

    def test_returns_empty_on_bad_yaml(self, koan_root):
        """Returns empty set on invalid yaml."""
        from app.projects_merged import get_yaml_project_names
        (koan_root / "projects.yaml").write_text("{{bad yaml")
        assert get_yaml_project_names(str(koan_root)) == set()


class TestPopulateWorkspaceGithubUrls:
    """Tests for populate_workspace_github_urls()."""

    def test_discovers_git_remotes(self, koan_root):
        """Discovers github URLs from workspace git repos."""
        from app.projects_merged import populate_workspace_github_urls
        ws = koan_root / "workspace"
        proj = ws / "myproj"
        proj.mkdir()
        (proj / ".git").mkdir()

        with patch("app.utils.get_github_remote", return_value="me/myproj"), \
             patch("app.utils.get_all_github_remotes", return_value=["me/myproj"]):
            count = populate_workspace_github_urls(str(koan_root))

        assert count == 1
        assert get_github_url("myproj") == "me/myproj"

    def test_caches_all_remotes(self, koan_root):
        """Caches ALL remote URLs for cross-owner notification matching."""
        from app.projects_merged import populate_workspace_github_urls
        ws = koan_root / "workspace"
        proj = ws / "myfork"
        proj.mkdir()
        (proj / ".git").mkdir()

        all_remotes = ["me/myfork", "upstream-org/myfork"]
        with patch("app.utils.get_github_remote", return_value="me/myfork"), \
             patch("app.utils.get_all_github_remotes", return_value=all_remotes):
            populate_workspace_github_urls(str(koan_root))

        # Primary URL is origin
        assert get_github_url("myfork") == "me/myfork"
        # All URLs include upstream
        all_urls = get_all_github_urls("myfork")
        assert "me/myfork" in all_urls
        assert "upstream-org/myfork" in all_urls

    def test_skips_yaml_projects(self, koan_root):
        """Skips projects that are in projects.yaml."""
        from app.projects_merged import populate_workspace_github_urls
        proj_dir = koan_root / "yaml-proj"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  yaml-proj:
    path: "{proj_dir}"
""")
        ws = koan_root / "workspace"
        ws_proj = ws / "ws-proj"
        ws_proj.mkdir()
        (ws_proj / ".git").mkdir()

        with patch("app.utils.get_github_remote", return_value="me/proj"), \
             patch("app.utils.get_all_github_remotes", return_value=["me/proj"]):
            count = populate_workspace_github_urls(str(koan_root))

        assert count == 1
        assert get_github_url("ws-proj") == "me/proj"
        assert get_github_url("yaml-proj") is None

    def test_skips_already_cached(self, koan_root):
        """Skips projects that already have a cached URL."""
        from app.projects_merged import populate_workspace_github_urls
        ws = koan_root / "workspace"
        proj = ws / "myproj"
        proj.mkdir()
        (proj / ".git").mkdir()
        set_github_url("myproj", "existing/url")

        with patch("app.utils.get_github_remote") as mock_remote, \
             patch("app.utils.get_all_github_remotes") as mock_all:
            populate_workspace_github_urls(str(koan_root))
            mock_remote.assert_not_called()
            mock_all.assert_not_called()

    def test_skips_non_git_dirs(self, koan_root):
        """Skips workspace directories that aren't git repos."""
        from app.projects_merged import populate_workspace_github_urls
        ws = koan_root / "workspace"
        (ws / "not-git").mkdir()

        with patch("app.utils.get_github_remote") as mock_remote:
            count = populate_workspace_github_urls(str(koan_root))

        assert count == 0
        mock_remote.assert_not_called()

    def test_skips_no_github_remote(self, koan_root):
        """Skips repos with no github remote."""
        from app.projects_merged import populate_workspace_github_urls
        ws = koan_root / "workspace"
        proj = ws / "myproj"
        proj.mkdir()
        (proj / ".git").mkdir()

        with patch("app.utils.get_github_remote", return_value=None), \
             patch("app.utils.get_all_github_remotes", return_value=[]):
            count = populate_workspace_github_urls(str(koan_root))

        assert count == 0
        assert get_github_url("myproj") is None


class TestCacheDifferentRoots:
    """Cache correctly handles different KOAN_ROOT values."""

    def test_different_root_triggers_rescan(self, tmp_path):
        """Switching to a different root triggers a new scan."""
        root1 = tmp_path / "root1"
        root1.mkdir()
        (root1 / "workspace").mkdir()
        (root1 / "workspace" / "proj1").mkdir()

        root2 = tmp_path / "root2"
        root2.mkdir()
        (root2 / "workspace").mkdir()
        (root2 / "workspace" / "proj2").mkdir()

        result1 = get_all_projects(str(root1))
        assert len(result1) == 1
        assert result1[0][0] == "proj1"

        result2 = get_all_projects(str(root2))
        assert len(result2) == 1
        assert result2[0][0] == "proj2"


class TestCaseInsensitiveDuplicates:
    """Case-insensitive duplicate detection."""

    def test_case_insensitive_yaml_wins(self, koan_root):
        """Yaml project 'MyProj' overrides workspace 'myproj'."""
        yaml_dir = koan_root / "yaml-path"
        yaml_dir.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  MyProj:
    path: "{yaml_dir}"
""")
        ws = koan_root / "workspace"
        (ws / "myproj").mkdir()

        result = get_all_projects(str(koan_root))
        # Yaml entry takes precedence
        assert len(result) == 1
        assert result[0][0] == "MyProj"
        assert result[0][1] == str(yaml_dir)


class TestYamlChangeInvalidatesCache:
    """Changes to projects.yaml invalidate the cache."""

    def test_yaml_modified_triggers_rescan(self, koan_root):
        """Modifying projects.yaml triggers a rescan."""
        proj1 = koan_root / "proj1"
        proj1.mkdir()
        _write_projects_yaml(koan_root, f"""
projects:
  proj1:
    path: "{proj1}"
""")
        result1 = get_all_projects(str(koan_root))
        assert len(result1) == 1

        # Modify yaml — add another project
        proj2 = koan_root / "proj2"
        proj2.mkdir()
        import time
        time.sleep(0.01)  # Ensure mtime differs
        _write_projects_yaml(koan_root, f"""
projects:
  proj1:
    path: "{proj1}"
  proj2:
    path: "{proj2}"
""")

        result2 = get_all_projects(str(koan_root))
        assert len(result2) == 2
