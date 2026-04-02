"""Tests for issue_tracker_config.py — issue tracker configuration helpers."""

import pytest

from app.issue_tracker_config import get_issue_tracker_config


class TestGetIssueTrackerConfig:
    """Tests for get_issue_tracker_config()."""

    # -- absent / empty config — defaults to GitHub ----------------------------

    def test_defaults_to_github_when_section_absent(self):
        """Defaults to GitHub when no issue_tracker section in config."""
        result = get_issue_tracker_config({})
        assert result == {"type": "github"}

    def test_defaults_to_github_when_section_is_none(self):
        """Defaults to GitHub when issue_tracker section is None."""
        result = get_issue_tracker_config({"issue_tracker": None})
        assert result == {"type": "github"}

    def test_defaults_to_github_when_type_missing(self):
        """Defaults to GitHub when type key is absent."""
        result = get_issue_tracker_config({"issue_tracker": {"base_url": "x"}})
        assert result == {"type": "github"}

    def test_defaults_to_github_when_type_empty_string(self):
        """Defaults to GitHub when type is empty string."""
        result = get_issue_tracker_config({"issue_tracker": {"type": ""}})
        assert result == {"type": "github"}

    def test_returns_none_for_unknown_type(self):
        """Returns None (with warning) for unknown tracker type."""
        result = get_issue_tracker_config({"issue_tracker": {"type": "gitlab"}})
        assert result is None

    # -- JIRA configuration ---------------------------------------------------

    def test_returns_jira_config_when_complete(self):
        """Returns full JIRA config when all required fields are present."""
        config = {
            "issue_tracker": {
                "type": "jira",
                "base_url": "https://my-org.atlassian.net",
                "email": "bot@my-org.com",
                "api_token": "secret-token",
            }
        }
        result = get_issue_tracker_config(config)
        assert result is not None
        assert result["type"] == "jira"
        assert result["base_url"] == "https://my-org.atlassian.net"
        assert result["email"] == "bot@my-org.com"
        assert result["api_token"] == "secret-token"

    def test_returns_none_for_jira_missing_api_token(self):
        """Returns None when api_token is absent for JIRA."""
        config = {
            "issue_tracker": {
                "type": "jira",
                "base_url": "https://my-org.atlassian.net",
                "email": "bot@my-org.com",
            }
        }
        assert get_issue_tracker_config(config) is None

    def test_returns_none_for_jira_empty_api_token(self):
        """Returns None when api_token is empty string for JIRA."""
        config = {
            "issue_tracker": {
                "type": "jira",
                "base_url": "https://my-org.atlassian.net",
                "email": "bot@my-org.com",
                "api_token": "",
            }
        }
        assert get_issue_tracker_config(config) is None

    def test_returns_none_for_jira_missing_base_url(self):
        """Returns None when base_url is absent for JIRA."""
        config = {
            "issue_tracker": {
                "type": "jira",
                "email": "bot@my-org.com",
                "api_token": "secret-token",
            }
        }
        assert get_issue_tracker_config(config) is None

    def test_returns_none_for_jira_missing_email(self):
        """Returns None when email is absent for JIRA."""
        config = {
            "issue_tracker": {
                "type": "jira",
                "base_url": "https://my-org.atlassian.net",
                "api_token": "secret-token",
            }
        }
        assert get_issue_tracker_config(config) is None

    def test_jira_type_case_insensitive(self):
        """JIRA type matching is case-insensitive."""
        config = {
            "issue_tracker": {
                "type": "JIRA",
                "base_url": "https://my-org.atlassian.net",
                "email": "bot@my-org.com",
                "api_token": "secret-token",
            }
        }
        result = get_issue_tracker_config(config)
        assert result is not None
        assert result["type"] == "jira"

    # -- GitHub configuration -------------------------------------------------

    def test_returns_github_config(self):
        """Returns minimal GitHub config (no credentials needed)."""
        config = {"issue_tracker": {"type": "github"}}
        result = get_issue_tracker_config(config)
        assert result is not None
        assert result["type"] == "github"

    def test_github_type_case_insensitive(self):
        """GitHub type matching is case-insensitive."""
        config = {"issue_tracker": {"type": "GitHub"}}
        result = get_issue_tracker_config(config)
        assert result is not None
        assert result["type"] == "github"

    # -- per-project override -------------------------------------------------

    def test_per_project_override_changes_type(self):
        """Project-level override can change the tracker type."""
        global_config = {
            "issue_tracker": {
                "type": "github",
            }
        }
        # Simulate a projects.yaml where the project overrides to JIRA
        projects_config = {
            "projects": {
                "my-project": {
                    "issue_tracker": {
                        "type": "jira",
                        "base_url": "https://proj.atlassian.net",
                        "email": "bot@proj.com",
                        "api_token": "proj-token",
                    }
                }
            }
        }
        result = get_issue_tracker_config(
            global_config,
            project_name="my-project",
            projects_config=projects_config,
        )
        assert result is not None
        assert result["type"] == "jira"
        assert result["base_url"] == "https://proj.atlassian.net"

    def test_project_without_override_inherits_global(self):
        """Project with no issue_tracker override inherits global config."""
        global_config = {"issue_tracker": {"type": "github"}}
        projects_config = {
            "projects": {
                "other-project": {
                    "path": "/some/path",
                }
            }
        }
        result = get_issue_tracker_config(
            global_config,
            project_name="other-project",
            projects_config=projects_config,
        )
        assert result is not None
        assert result["type"] == "github"

    def test_no_project_name_uses_global(self):
        """When project_name is None, returns global config only."""
        global_config = {"issue_tracker": {"type": "github"}}
        result = get_issue_tracker_config(global_config, project_name=None)
        assert result is not None
        assert result["type"] == "github"

    def test_project_override_merges_fields(self):
        """Project override shallow-merges with global defaults."""
        global_config = {
            "issue_tracker": {
                "type": "jira",
                "base_url": "https://global.atlassian.net",
                "email": "global@example.com",
                "api_token": "global-token",
            }
        }
        # Project overrides only the base_url
        projects_config = {
            "projects": {
                "proj": {
                    "issue_tracker": {
                        "base_url": "https://proj.atlassian.net",
                    }
                }
            }
        }
        result = get_issue_tracker_config(
            global_config,
            project_name="proj",
            projects_config=projects_config,
        )
        assert result is not None
        assert result["type"] == "jira"
        assert result["base_url"] == "https://proj.atlassian.net"
        assert result["email"] == "global@example.com"
        assert result["api_token"] == "global-token"
