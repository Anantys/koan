"""Base class for forge (Git hosting platform) abstraction.

Mirrors the CLIProvider pattern in koan/app/provider/ — a ForgeProvider
knows how to interact with a specific Git hosting platform (GitHub, GitLab,
Gitea/Codeberg, etc.).

Each method raises NotImplementedError by default. Platform implementations
override the methods they support and call super() for the rest.
"""

import shutil
from abc import ABC
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Feature flag constants
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

    A forge provider knows how to:
    - Authenticate CLI tools for the platform
    - Parse platform-specific PR/MR and issue URLs
    - Create PRs/MRs and issues
    - Query CI status
    - Detect forks
    - Report which features are supported

    GitHub is the default and fully-supported forge.  Other forges start
    with core operations and gain features incrementally.
    """

    #: Human-readable forge name (e.g. "github", "gitlab", "gitea")
    name: str = ""

    def __init__(self, base_url: str = ""):
        """Initialise the forge provider.

        Args:
            base_url: Base URL for the forge instance.  Used for self-hosted
                instances (e.g. ``https://gitlab.company.com``).  Leave empty
                for the public cloud instance of each forge.
        """
        self.base_url = base_url

    # ------------------------------------------------------------------
    # CLI availability
    # ------------------------------------------------------------------

    def cli_name(self) -> str:
        """Return the primary CLI binary name for this forge (e.g. 'gh')."""
        raise NotImplementedError

    def is_cli_available(self) -> bool:
        """Return True if the forge CLI tool is installed and on PATH."""
        try:
            return shutil.which(self.cli_name()) is not None
        except NotImplementedError:
            return False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def auth_env(self) -> Dict[str, str]:
        """Return environment variables required for authenticated CLI calls.

        Returns:
            Dict of env vars to merge into subprocess env (e.g.
            ``{"GH_TOKEN": "ghp_..."}``).  Empty dict if no auth needed.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------

    def parse_pr_url(self, url: str) -> Tuple[str, str, str]:
        """Extract (owner, repo, pr_number) from a platform PR/MR URL.

        Args:
            url: Full PR or merge-request URL for this forge.

        Returns:
            Tuple of (owner, repo, pr_number) as strings.

        Raises:
            ValueError: If the URL doesn't match this forge's PR pattern.
        """
        raise NotImplementedError

    def parse_issue_url(self, url: str) -> Tuple[str, str, str]:
        """Extract (owner, repo, issue_number) from a platform issue URL.

        Args:
            url: Full issue URL for this forge.

        Returns:
            Tuple of (owner, repo, issue_number) as strings.

        Raises:
            ValueError: If the URL doesn't match this forge's issue pattern.
        """
        raise NotImplementedError

    def search_pr_url(self, text: str) -> Tuple[str, str, str]:
        """Search for a PR URL anywhere in text.

        Args:
            text: Text that may contain a PR/MR URL.

        Returns:
            Tuple of (owner, repo, pr_number) as strings.

        Raises:
            ValueError: If no PR URL is found in text.
        """
        raise NotImplementedError

    def search_issue_url(self, text: str) -> Tuple[str, str, str]:
        """Search for an issue URL anywhere in text.

        Args:
            text: Text that may contain an issue URL.

        Returns:
            Tuple of (owner, repo, issue_number) as strings.

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
        """Create a pull/merge request.

        Args:
            title: PR title.
            body: PR body (markdown).
            draft: If True, create as a draft.
            base: Target branch.
            repo: Repository in ``owner/repo`` format.
            head: Branch with changes.
            cwd: Working directory (inside a git repo).

        Returns:
            URL of the newly created PR/MR.
        """
        raise NotImplementedError

    def pr_view(self, repo: str, number: str, cwd: Optional[str] = None) -> dict:
        """Fetch PR/MR details as a dict.

        Args:
            repo: Repository in ``owner/repo`` format.
            number: PR/MR number as string.
            cwd: Optional working directory.

        Returns:
            Dict of PR metadata (title, body, state, etc.).
        """
        raise NotImplementedError

    def pr_diff(self, repo: str, number: str, cwd: Optional[str] = None) -> str:
        """Fetch the unified diff for a PR/MR.

        Args:
            repo: Repository in ``owner/repo`` format.
            number: PR/MR number as string.
            cwd: Optional working directory.

        Returns:
            Unified diff string.
        """
        raise NotImplementedError

    def list_merged_prs(self, repo: str, cwd: Optional[str] = None) -> List[str]:
        """List merged PR branch names for the repository.

        Used by git_sync to detect branches that have been squash-merged.

        Args:
            repo: Repository in ``owner/repo`` format.
            cwd: Optional working directory.

        Returns:
            List of head branch names that have been merged.
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
        """Create an issue.

        Args:
            title: Issue title.
            body: Issue body (markdown).
            labels: Optional list of label names.
            cwd: Working directory.

        Returns:
            URL of the newly created issue.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # API access
    # ------------------------------------------------------------------

    def run_api(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        """Call the forge REST API.

        Args:
            endpoint: API path (e.g. ``repos/owner/repo/pulls/1/comments``).
            method: HTTP method (default GET).
            data: Optional request body string.
            cwd: Optional working directory.

        Returns:
            Response body string.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # CI / status
    # ------------------------------------------------------------------

    def get_ci_status(self, repo: str, branch: str, cwd: Optional[str] = None) -> dict:
        """Return CI status for a branch.

        Args:
            repo: Repository in ``owner/repo`` format.
            branch: Branch name.
            cwd: Optional working directory.

        Returns:
            Dict with at least ``{"status": "pending"|"success"|"failure"|"unknown"}``.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Repository introspection
    # ------------------------------------------------------------------

    def get_web_url(
        self,
        repo: str,
        url_type: str = "",
        number: Optional[str] = None,
    ) -> str:
        """Build a web URL for a repository resource.

        Args:
            repo: Repository in ``owner/repo`` format.
            url_type: Resource type: ``"pull"``, ``"issues"``, or ``""`` for
                the repo root.
            number: Resource number (PR/issue number) as string.

        Returns:
            Full HTTPS URL string.
        """
        raise NotImplementedError

    def detect_fork(self, project_path: str) -> Optional[str]:
        """Detect if the local repo is a fork and return the parent slug.

        Args:
            project_path: Path to the local git repository.

        Returns:
            Parent ``owner/repo`` slug, or ``None`` if not a fork.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Feature matrix
    # ------------------------------------------------------------------

    def supports(self, feature: str) -> bool:
        """Return True if this forge implementation supports the given feature.

        Feature names are the FEATURE_* constants defined in this module:
        ``"pr"``, ``"issues"``, ``"notifications"``, ``"ci_status"``,
        ``"reactions"``, ``"pr_review_comments"``.

        Base implementation returns False for all features — subclasses must
        opt in explicitly.  This ensures new forge implementations don't
        silently claim support they don't have.

        Args:
            feature: Feature name string.

        Returns:
            True if the feature is supported, False otherwise.
        """
        return False
