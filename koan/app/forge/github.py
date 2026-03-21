"""GitHub forge implementation — thin delegation wrapper over app.github.

GitHubForge delegates all operations to the existing app.github and
app.github_url_parser modules.  No logic is duplicated here — app.github
remains the single implementation source.  Phase 1 introduces zero behavior
changes; GitHubForge is purely additive.

Supports GitHub Enterprise via the base_url parameter.
"""

import json
import subprocess
from typing import Dict, List, Optional, Tuple

from app.forge.base import ForgeProvider


class GitHubForge(ForgeProvider):
    """Forge implementation for GitHub (including GitHub Enterprise).

    Delegates to app.github and app.github_url_parser — no logic duplication.
    """

    name = "github"

    def __init__(self, base_url: str = "https://github.com"):
        """Create a GitHubForge instance.

        Args:
            base_url: Base URL for the GitHub instance.  Defaults to
                      "https://github.com".  Set to your GitHub Enterprise
                      URL (e.g. "https://github.example.com") for GHE support.
        """
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # CLI availability
    # ------------------------------------------------------------------

    def cli_name(self) -> str:
        return "gh"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def auth_env(self) -> Dict[str, str]:
        from app.github_auth import get_gh_env
        return get_gh_env()

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------

    def parse_pr_url(self, url: str) -> Tuple[str, str, str]:
        from app.github_url_parser import parse_pr_url
        return parse_pr_url(url)

    def parse_issue_url(self, url: str) -> Tuple[str, str, str]:
        from app.github_url_parser import parse_issue_url
        return parse_issue_url(url)

    def search_pr_url(self, text: str) -> Tuple[str, str, str]:
        from app.github_url_parser import search_pr_url
        return search_pr_url(text)

    def search_issue_url(self, text: str) -> Tuple[str, str, str]:
        from app.github_url_parser import search_issue_url
        return search_issue_url(text)

    # ------------------------------------------------------------------
    # PR operations
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
        return pr_create(title=title, body=body, draft=draft, base=base,
                         repo=repo, head=head, cwd=cwd)

    def pr_view(
        self,
        repo: str,
        number: int,
        cwd: Optional[str] = None,
    ) -> Dict:
        from app.github import run_gh
        output = run_gh(
            "pr", "view", str(number),
            "--repo", repo,
            "--json", "number,title,body,state,headRefName,baseRefName,url",
            cwd=cwd,
        )
        try:
            return json.loads(output)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"Failed to parse PR view output for {repo}#{number}: {exc}"
            ) from exc

    def pr_diff(
        self,
        repo: str,
        number: int,
        cwd: Optional[str] = None,
    ) -> str:
        from app.github import run_gh
        return run_gh("pr", "diff", str(number), "--repo", repo, cwd=cwd)

    def list_merged_prs(
        self,
        repo: str,
        cwd: Optional[str] = None,
    ) -> List[str]:
        from app.github import run_gh
        output = run_gh(
            "pr", "list",
            "--repo", repo,
            "--state", "merged",
            "--json", "headRefName",
            "--jq", ".[].headRefName",
            cwd=cwd,
        )
        return [line for line in output.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # Issue operations
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
    # API access
    # ------------------------------------------------------------------

    def run_api(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[Dict] = None,
        cwd: Optional[str] = None,
    ) -> str:
        from app.github import api
        input_data = None
        if data:
            input_data = json.dumps(data)
        return api(endpoint=endpoint, method=method,
                   input_data=input_data, cwd=cwd)

    # ------------------------------------------------------------------
    # CI / Status
    # ------------------------------------------------------------------

    def get_ci_status(
        self,
        repo: str,
        branch: str,
        cwd: Optional[str] = None,
    ) -> Dict:
        from app.github import run_gh
        try:
            output = run_gh(
                "api",
                f"repos/{repo}/commits/{branch}/status",
                "--jq", '{"status": .state, "total": .total_count}',
                cwd=cwd,
            )
            return json.loads(output)
        except (RuntimeError, json.JSONDecodeError, subprocess.SubprocessError,
                OSError):
            return {"status": "unknown"}

    # ------------------------------------------------------------------
    # Repository introspection
    # ------------------------------------------------------------------

    def get_web_url(
        self,
        repo: str,
        url_type: str,
        number: int,
    ) -> str:
        # url_type: 'pull' -> /pull/N, 'issues' -> /issues/N
        path_map = {
            "pull": "pull",
            "pr": "pull",
            "issues": "issues",
            "issue": "issues",
        }
        path = path_map.get(url_type, url_type)
        return f"{self.base_url}/{repo}/{path}/{number}"

    def detect_fork(self, project_path: str) -> Optional[str]:
        from app.github import detect_parent_repo
        return detect_parent_repo(project_path)

    # ------------------------------------------------------------------
    # Feature matrix
    # ------------------------------------------------------------------

    # Features actually implemented by GitHubForge.
    _SUPPORTED_FEATURES = frozenset({
        "pr",
        "issues",
        "ci_status",
        "pr_review_comments",
    })

    def supports(self, feature: str) -> bool:
        return feature in self._SUPPORTED_FEATURES
