"""Issue tracker configuration helpers.

Reads issue tracker settings from config.yaml (global) and projects.yaml
(per-project override) for the PR review enrichment feature.

Config schema in config.yaml:
    issue_tracker:
      type: jira          # "jira" or "github"
      base_url: "https://your-org.atlassian.net"   # JIRA only
      email: "bot@your-org.com"                    # JIRA only
      api_token: ""       # JIRA only — store here or leave empty to disable

Per-project override in projects.yaml:
    projects:
      my-project:
        issue_tracker:
          type: jira
          base_url: "https://my-org.atlassian.net"
          email: "bot@my-org.com"
          api_token: "my-api-token"

When type is "github", no additional credentials are needed — the existing
gh CLI auth is used.

Returns None (not an empty dict) when the section is absent or the config
is incomplete — callers use None as the "feature disabled" signal.
"""

import sys
from typing import Optional


def get_issue_tracker_config(
    global_config: dict,
    project_name: Optional[str] = None,
    projects_config: Optional[dict] = None,
) -> Optional[dict]:
    """Get the effective issue tracker config for a project.

    Merges global config.yaml ``issue_tracker`` section with per-project
    overrides from projects.yaml when project_name and projects_config are
    provided.

    Args:
        global_config: Parsed config.yaml dict (from load_config()).
        project_name: Optional project name for per-project override lookup.
        projects_config: Parsed projects.yaml dict (from load_projects_config()).

    Returns:
        A dict with at minimum ``{"type": "jira"|"github"}`` on success.
        None when the feature is disabled or the config is incomplete.
    """
    # Start with global defaults
    global_tracker = (global_config or {}).get("issue_tracker") or {}

    # Apply per-project override (shallow merge, same pattern as projects_config.py)
    if project_name and projects_config:
        from app.projects_config import get_project_config
        project_cfg = get_project_config(projects_config, project_name)
        project_tracker = project_cfg.get("issue_tracker") or {}
        merged = {**global_tracker, **project_tracker}
    else:
        merged = dict(global_tracker)

    tracker_type = merged.get("type", "").strip().lower()
    if not tracker_type:
        # No issue_tracker section configured — feature disabled
        return None

    if tracker_type == "jira":
        api_token = merged.get("api_token") or ""
        if not api_token:
            print(
                "[issue_tracker_config] JIRA issue tracker configured but "
                "'api_token' is missing or empty — feature disabled.",
                file=sys.stderr,
            )
            return None
        base_url = merged.get("base_url", "").strip()
        email = merged.get("email", "").strip()
        if not base_url or not email:
            print(
                "[issue_tracker_config] JIRA issue tracker configured but "
                "'base_url' or 'email' is missing — feature disabled.",
                file=sys.stderr,
            )
            return None
        return {
            "type": "jira",
            "base_url": base_url,
            "email": email,
            "api_token": api_token,
        }

    if tracker_type == "github":
        # GitHub type requires no credentials — gh CLI auth is used
        return {"type": "github"}

    print(
        f"[issue_tracker_config] Unknown issue tracker type '{tracker_type}' "
        "— feature disabled.",
        file=sys.stderr,
    )
    return None
