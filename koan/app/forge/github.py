"""GitHub forge implementation.

GitHubForge is a thin delegation wrapper over the existing koan/app/github.py
functions — it does NOT duplicate logic.  All implementation stays in
app.github and app.github_auth; GitHubForge simply forwards calls through
the ForgeProvider interface.

Supports GitHub.com and GitHub Enterprise (via the base_url parameter).
"""

import json
import subprocess
from typing import Dict, List, Optional, Tuple

from app.forge.base import (
    ALL_FEATURES,
    ForgeProvider,
)


class GitHubForge(ForgeProvider):
    """ForgeProvider implementation for GitHub (github.com and GitHub Enterprise).

    All methods delegate to app.github / app.github_auth — no logic is
    duplicated here.  This wrapper is intentionally thin so that Phase 1
    introduces zero behaviour changes; the existing modules remain the
    single source of truth during the transition period.

    Args:
        base_url: GitHub instance base URL.  Defaults to
            ``"https://github.com"`` for GitHub.com.  Pass e.g.
            ``"https://github.company.com"`` for GitHub Enterprise.
    """

    name = "github"

    def __init__(self, base_url: str = "https://github.com"):
        super().__init__(base_url=base_url)

    # ------------------------------------------------------------------
    # CLI availability
    # ------------------------------------------------------------------

    def cli_name(self) -> str:
        return "gh"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def auth_env(self) -> Dict[str, str]:
        """Return env dict with GH_TOKEN if GITHUB_USER is configured."""
        from app.github_auth import get_gh_env
        return get_gh_env()

    # ------------------------------------------------------------------
    # URL parsing — delegates to app.github_url_parser
    # ------------------------------------------------------------------

    def parse_pr_url(self, url: str) -> Tuple[str, str, str]:
        from app.github_url_parser import parse_pr_url
        return parse_pr_url(url)

    def parse_issue_url(self, url: str) -> Tuple[str, str, str]:
        from app.github_url_parser import parse_issue_url
        return parse_issue_url(url)

    def search_pr_url(self, url: str) -> Tuple[str, str, str]:
        from app.github_url_parser import search_pr_url
        return search_pr_url(url)

    def search_issue_url(self, url: str) -> Tuple[str, str, str]:
        from app.github_url_parser import search_issue_url
        return search_issue_url(url)

    # ------------------------------------------------------------------
    # PR operations — delegates to app.github
    # ------------------------------------------------------------------

    def pr_create(
        self,
        title: str,
        body: str,
        draft: bool = True,
        base: Optional[str] = None,
        repo: Optional[str] = None,
        head: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        from app.github import pr_create
        return pr_create(
            title=title, body=body, draft=draft,
            base=base, repo=repo, head=head, cwd=cwd,
        )

    def pr_view(self, repo: str, number: str, cwd: Optional[str] = None) -> dict:
        from app.github import run_gh
        output = run_gh(
            "pr", "view", number,
            "--repo", repo,
            "--json", "title,body,state,headRefName,baseRefName,url,number",
            cwd=cwd,
        )
        try:
            return json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return {"raw": output}

    def pr_diff(self, repo: str, number: str, cwd: Optional[str] = None) -> str:
        from app.github import run_gh
        return run_gh("pr", "diff", number, "--repo", repo, cwd=cwd)

    def list_merged_prs(self, repo: str, cwd: Optional[str] = None) -> List[str]:
        from app.github import run_gh
        output = run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", "merged",
            "--json", "headRefName",
            "--jq", "[.[].headRefName]",
            cwd=cwd,
        )
        try:
            return json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return []

    # ------------------------------------------------------------------
    # Issue operations — delegates to app.github
    # ------------------------------------------------------------------

    def issue_create(
        self,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
        cwd: Optional[str] = None,
    ) -> str:
        from app.github import issue_create
        return issue_create(title=title, body=body, labels=labels, cwd=cwd)

    # ------------------------------------------------------------------
    # API access — delegates to app.github
    # ------------------------------------------------------------------

    def run_api(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        from app.github import api
        return api(
            endpoint=endpoint, method=method,
            input_data=data, cwd=cwd,
        )

    # ------------------------------------------------------------------
    # CI / status
    # ------------------------------------------------------------------

    def get_ci_status(self, repo: str, branch: str, cwd: Optional[str] = None) -> dict:
        from app.github import run_gh
        try:
            output = run_gh(
                "api",
                f"repos/{repo}/commits/{branch}/status",
                "--jq", "{status: .state, total: .total_count}",
                cwd=cwd,
                timeout=15,
            )
            return json.loads(output)
        except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError, OSError):
            return {"status": "unknown"}

    # ------------------------------------------------------------------
    # Repository introspection
    # ------------------------------------------------------------------

    def get_web_url(
        self,
        repo: str,
        url_type: str = "",
        number: Optional[str] = None,
    ) -> str:
        base = self.base_url.rstrip("/")
        if not url_type or not number:
            return f"{base}/{repo}"
        if url_type == "pull":
            return f"{base}/{repo}/pull/{number}"
        if url_type == "issues":
            return f"{base}/{repo}/issues/{number}"
        return f"{base}/{repo}"

    def detect_fork(self, project_path: str) -> Optional[str]:
        from app.github import detect_parent_repo
        return detect_parent_repo(project_path)

    # ------------------------------------------------------------------
    # Feature matrix — GitHub supports everything
    # ------------------------------------------------------------------

    def supports(self, feature: str) -> bool:
        return feature in ALL_FEATURES
