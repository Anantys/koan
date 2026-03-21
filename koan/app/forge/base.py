"""Base class and feature constants for forge provider abstraction.

Mirrors the CLIProvider pattern in koan/app/provider/ — each forge platform
(GitHub, GitLab, Gitea/Codeberg) subclasses ForgeProvider and implements
the operations it supports.  Unsupported operations raise NotImplementedError.
"""

import shutil
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

FEATURE_PR = "pr"
FEATURE_ISSUES = "issues"
FEATURE_NOTIFICATIONS = "notifications"
FEATURE_CI_STATUS = "ci_status"
FEATURE_REACTIONS = "reactions"
FEATURE_PR_REVIEW_COMMENTS = "pr_review_comments"

ALL_FEATURES = (
    FEATURE_PR,
    FEATURE_ISSUES,
    FEATURE_NOTIFICATIONS,
    FEATURE_CI_STATUS,
    FEATURE_REACTIONS,
    FEATURE_PR_REVIEW_COMMENTS,
)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class ForgeProvider(ABC):
    """Abstract base class for Git forge platform integrations.

    Subclasses implement platform-specific PR/issue/CI operations.
    All methods raise NotImplementedError by default — subclasses override
    only what they support and declare support via supports().
    """

    name: str = ""

    # ------------------------------------------------------------------
    # CLI availability
    # ------------------------------------------------------------------

    def cli_name(self) -> str:
        """Return the primary CLI binary name (e.g. 'gh', 'glab', 'tea')."""
        raise NotImplementedError

    def is_cli_available(self) -> bool:
        """Return True if the CLI binary is installed on PATH."""
        try:
            return shutil.which(self.cli_name()) is not None
        except NotImplementedError:
            return False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def auth_env(self) -> Dict[str, str]:
        """Return environment variables for authenticated CLI/API calls.

        Designed to be merged into subprocess.run() env:
            env = {**os.environ, **forge.auth_env()}
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------

    def parse_pr_url(self, url: str) -> Tuple[str, str, str]:
        """Extract (owner, repo, pr_number) from a forge PR/MR URL.

        Raises:
            ValueError: If the URL does not match the expected pattern.
        """
        raise NotImplementedError

    def parse_issue_url(self, url: str) -> Tuple[str, str, str]:
        """Extract (owner, repo, issue_number) from a forge issue URL.

        Raises:
            ValueError: If the URL does not match the expected pattern.
        """
        raise NotImplementedError

    def search_pr_url(self, text: str) -> Tuple[str, str, str]:
        """Search for a PR/MR URL anywhere in text and return parsed components.

        Raises:
            ValueError: If no PR/MR URL is found in text.
        """
        raise NotImplementedError

    def search_issue_url(self, text: str) -> Tuple[str, str, str]:
        """Search for an issue URL anywhere in text and return parsed components.

        Raises:
            ValueError: If no issue URL is found in text.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # PR / MR operations
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
        """Create a pull/merge request and return its URL.

        Raises:
            NotImplementedError: If the forge does not support PR creation.
        """
        raise NotImplementedError

    def pr_view(
        self,
        repo: str,
        number: int,
        cwd: Optional[str] = None,
    ) -> Dict:
        """Fetch PR/MR details as a dict.

        Raises:
            NotImplementedError: If the forge does not support PR viewing.
        """
        raise NotImplementedError

    def pr_diff(
        self,
        repo: str,
        number: int,
        cwd: Optional[str] = None,
    ) -> str:
        """Return the unified diff for a PR/MR.

        Raises:
            NotImplementedError: If the forge does not support PR diffs.
        """
        raise NotImplementedError

    def list_merged_prs(
        self,
        repo: str,
        cwd: Optional[str] = None,
    ) -> List[str]:
        """Return a list of recently merged PR branch names.

        Raises:
            NotImplementedError: If the forge does not support PR listing.
        """
        raise NotImplementedError

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
        """Create an issue and return its URL.

        Raises:
            NotImplementedError: If the forge does not support issue creation.
        """
        raise NotImplementedError

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
        """Call the forge REST API and return raw response text.

        Args:
            endpoint: API path (relative to the forge base URL).
            method: HTTP method.
            data: Optional JSON payload.
            cwd: Working directory for CLI fallback.

        Raises:
            NotImplementedError: If the forge does not support direct API access.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # CI / Status
    # ------------------------------------------------------------------

    def get_ci_status(
        self,
        repo: str,
        branch: str,
        cwd: Optional[str] = None,
    ) -> Dict:
        """Return CI status information for a branch.

        Returns a dict with at least 'status' key ('success', 'failure',
        'pending', 'unknown').

        Raises:
            NotImplementedError: If the forge does not support CI status checks.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Repository introspection
    # ------------------------------------------------------------------

    def get_web_url(
        self,
        repo: str,
        url_type: str,
        number: int,
    ) -> str:
        """Build the web URL for a PR, MR, or issue.

        Args:
            repo: Repository in owner/repo format.
            url_type: One of 'pull', 'issues', 'merge_request'.
            number: PR/issue number.

        Raises:
            NotImplementedError: If the forge does not support URL construction.
        """
        raise NotImplementedError

    def detect_fork(self, project_path: str) -> Optional[str]:
        """Detect if the repo is a fork and return the parent slug (owner/repo).

        Returns None if not a fork, cannot be determined, or on error.

        Raises:
            NotImplementedError: If the forge does not support fork detection.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Feature matrix
    # ------------------------------------------------------------------

    def supports(self, feature: str) -> bool:
        """Return True if this forge implementation supports the given feature.

        Feature names are defined as FEATURE_* constants in this module.
        Callers should check supports() before calling optional methods to
        provide user-friendly messages instead of propagating NotImplementedError.
        """
        return False
