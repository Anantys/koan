"""Tests for the /add_project skill handler."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

# Load handler via spec since directory name has a hyphen
_handler_path = (
    Path(__file__).resolve().parent.parent
    / "skills" / "core" / "add_project" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("add_project_handler", str(_handler_path))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
sys.modules["add_project_handler"] = _mod

# Re-export symbols for cleaner usage
handle = _mod.handle
_parse_args = _mod._parse_args
_normalize_github_url = _mod._normalize_github_url
_extract_owner_repo = _mod._extract_owner_repo

# Patch prefix for the dynamically loaded module
P = "add_project_handler"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def instance_dir(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    return inst


@pytest.fixture
def koan_root(tmp_path, instance_dir):
    return tmp_path


@pytest.fixture
def workspace_dir(koan_root):
    ws = koan_root / "workspace"
    ws.mkdir()
    return ws


def _make_ctx(koan_root, instance_dir, args=""):
    messages = []
    return SimpleNamespace(
        koan_root=koan_root,
        instance_dir=instance_dir,
        command_name="add_project",
        args=args,
        send_message=lambda msg: messages.append(msg),
        handle_chat=None,
        _messages=messages,
    )


# ---------------------------------------------------------------------------
# _normalize_github_url
# ---------------------------------------------------------------------------

class TestNormalizeGithubUrl:
    def test_https_url(self):
        assert _normalize_github_url("https://github.com/owner/repo") == \
            "https://github.com/owner/repo"

    def test_https_url_with_git_suffix(self):
        assert _normalize_github_url("https://github.com/owner/repo.git") == \
            "https://github.com/owner/repo"

    def test_https_url_trailing_slash(self):
        assert _normalize_github_url("https://github.com/owner/repo/") == \
            "https://github.com/owner/repo"

    def test_ssh_url(self):
        assert _normalize_github_url("git@github.com:owner/repo.git") == \
            "https://github.com/owner/repo"

    def test_ssh_url_no_git(self):
        assert _normalize_github_url("git@github.com:owner/repo") == \
            "https://github.com/owner/repo"

    def test_short_form(self):
        assert _normalize_github_url("owner/repo") == \
            "https://github.com/owner/repo"

    def test_invalid_url_returns_none(self):
        assert _normalize_github_url("not-a-url") is None

    def test_empty_returns_none(self):
        assert _normalize_github_url("") is None

    def test_http_url(self):
        assert _normalize_github_url("http://github.com/owner/repo") == \
            "https://github.com/owner/repo"

    def test_dots_in_name(self):
        assert _normalize_github_url("owner/repo.js") == \
            "https://github.com/owner/repo.js"

    def test_hyphens_and_underscores(self):
        assert _normalize_github_url("my-org/my_repo-name") == \
            "https://github.com/my-org/my_repo-name"


# ---------------------------------------------------------------------------
# _extract_owner_repo
# ---------------------------------------------------------------------------

class TestExtractOwnerRepo:
    def test_from_https(self):
        assert _extract_owner_repo("https://github.com/alice/bob") == \
            ("alice", "bob")

    def test_returns_none_for_invalid(self):
        assert _extract_owner_repo("not-a-url") == (None, None)

    def test_from_normalized_url(self):
        """Works on URLs already normalized (no .git suffix)."""
        assert _extract_owner_repo("https://github.com/perl/perl5") == \
            ("perl", "perl5")

    def test_strips_git_suffix(self):
        """Also handles URLs with .git suffix."""
        assert _extract_owner_repo("https://github.com/perl/perl5.git") == \
            ("perl", "perl5")


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_url_only(self):
        url, name = _parse_args("https://github.com/owner/repo")
        assert url == "https://github.com/owner/repo"
        assert name is None

    def test_url_with_name(self):
        url, name = _parse_args("https://github.com/owner/repo myname")
        assert url == "https://github.com/owner/repo"
        assert name == "myname"

    def test_short_form(self):
        url, name = _parse_args("owner/repo")
        assert url == "https://github.com/owner/repo"
        assert name is None

    def test_short_form_with_name(self):
        url, name = _parse_args("owner/repo custom-name")
        assert url == "https://github.com/owner/repo"
        assert name == "custom-name"

    def test_ssh_url(self):
        url, name = _parse_args("git@github.com:owner/repo.git")
        assert url == "https://github.com/owner/repo"
        assert name is None


# ---------------------------------------------------------------------------
# handle — no args / invalid
# ---------------------------------------------------------------------------

class TestHandleNoArgs:
    def test_empty_args_returns_usage(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="")
        result = handle(ctx)
        assert "Usage:" in result

    def test_whitespace_only_returns_usage(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="   ")
        result = handle(ctx)
        assert "Usage:" in result


class TestHandleInvalidUrl:
    def test_invalid_url(self, koan_root, instance_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="not-a-url")
        result = handle(ctx)
        assert "Could not parse" in result


# ---------------------------------------------------------------------------
# handle — project already exists
# ---------------------------------------------------------------------------

class TestHandleAlreadyExists:
    def test_existing_project(self, koan_root, instance_dir, workspace_dir):
        (workspace_dir / "repo").mkdir()
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)
        assert "already exists" in result


# ---------------------------------------------------------------------------
# handle — invalid project name
# ---------------------------------------------------------------------------

class TestHandleInvalidName:
    def test_dot_start(self, koan_root, instance_dir, workspace_dir):
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo .hidden")
        result = handle(ctx)
        assert "Invalid project name" in result


# ---------------------------------------------------------------------------
# handle — clone failure
# ---------------------------------------------------------------------------

class TestHandleCloneFail:
    @patch(f"{P}._git_clone")
    def test_clone_error(self, mock_clone, koan_root, instance_dir, workspace_dir):
        mock_clone.side_effect = RuntimeError("remote not found")
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)
        assert "Clone failed" in result
        assert "remote not found" in result


# ---------------------------------------------------------------------------
# handle — successful clone with push access
# ---------------------------------------------------------------------------

class TestHandleSuccessWithPush:
    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_clone_with_push(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)

        assert "added to workspace" in result
        assert "owner/repo" in result
        assert "Fork" not in result

    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_clone_with_custom_name(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo myapp")
        result = handle(ctx)

        assert "myapp" in result
        assert (workspace_dir / "myapp").is_dir()

    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_creates_workspace_dir_if_missing(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir
    ):
        """workspace/ is created automatically if it doesn't exist."""
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)

        assert "added to workspace" in result
        assert (koan_root / "workspace" / "repo").is_dir()


# ---------------------------------------------------------------------------
# handle — clone + fork (no push access)
# ---------------------------------------------------------------------------

class TestHandleFork:
    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=False)
    @patch(f"{P}._create_fork_and_configure", return_value="myuser/repo")
    @patch("app.projects_merged.refresh_projects")
    def test_fork_when_no_push(
        self, mock_refresh, mock_fork, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)

        assert "Fork" in result
        assert "myuser/repo" in result
        assert "origin=fork, upstream=original" in result
        mock_fork.assert_called_once()

    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=False)
    @patch(f"{P}._create_fork_and_configure", side_effect=RuntimeError("fork failed"))
    @patch("app.projects_merged.refresh_projects")
    def test_fork_failure_still_adds_project(
        self, mock_refresh, mock_fork, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        """If fork creation fails, the project is still registered."""
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)

        assert "added to workspace" in result
        assert any("Fork creation failed" in m for m in ctx._messages)

    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", side_effect=RuntimeError("gh failed"))
    @patch(f"{P}._create_fork_and_configure", return_value="me/repo")
    @patch("app.projects_merged.refresh_projects")
    def test_push_check_exception_triggers_fork(
        self, mock_refresh, mock_fork, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        """If push check raises, treat as no access."""
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)

        mock_fork.assert_called_once()


# ---------------------------------------------------------------------------
# handle — project cache refresh
# ---------------------------------------------------------------------------

class TestHandleCacheRefresh:
    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_refresh_projects_called(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        handle(ctx)

        mock_refresh.assert_called_once_with(str(koan_root))

    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects", side_effect=Exception("boom"))
    def test_refresh_failure_does_not_crash(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        result = handle(ctx)

        assert "added to workspace" in result


# ---------------------------------------------------------------------------
# handle — send_message notifications
# ---------------------------------------------------------------------------

class TestHandleNotifications:
    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_cloning_message_sent(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        handle(ctx)

        assert any("Cloning" in m for m in ctx._messages)

    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=False)
    @patch(f"{P}._create_fork_and_configure", return_value="me/repo")
    @patch("app.projects_merged.refresh_projects")
    def test_fork_message_sent(
        self, mock_refresh, mock_fork, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
        handle(ctx)

        assert any("No push access" in m for m in ctx._messages)


# ---------------------------------------------------------------------------
# _check_push_access
# ---------------------------------------------------------------------------

class TestCheckPushAccess:
    @patch("app.github.run_gh", return_value="ADMIN")
    def test_admin_returns_true(self, _):
        assert _mod._check_push_access("owner", "repo") is True

    @patch("app.github.run_gh", return_value="WRITE")
    def test_write_returns_true(self, _):
        assert _mod._check_push_access("owner", "repo") is True

    @patch("app.github.run_gh", return_value="MAINTAIN")
    def test_maintain_returns_true(self, _):
        assert _mod._check_push_access("owner", "repo") is True

    @patch("app.github.run_gh", return_value="READ")
    def test_read_returns_false(self, _):
        assert _mod._check_push_access("owner", "repo") is False

    @patch("app.github.run_gh", return_value="")
    def test_none_returns_false(self, _):
        assert _mod._check_push_access("owner", "repo") is False


# ---------------------------------------------------------------------------
# _create_fork_and_configure
# ---------------------------------------------------------------------------

class TestCreateForkAndConfigure:
    @patch(f"{P}._get_gh_username", return_value="myuser")
    @patch("app.github.run_gh", return_value="")
    @patch(f"{P}.run_git_strict")
    def test_creates_fork_and_reconfigures(self, mock_git, mock_gh, mock_user):
        result = _mod._create_fork_and_configure(
            "upstream-owner", "repo", "/tmp/project"
        )

        assert result == "myuser/repo"
        mock_git.assert_any_call(
            "remote", "rename", "origin", "upstream", cwd="/tmp/project"
        )
        mock_git.assert_any_call(
            "remote", "add", "origin",
            "https://github.com/myuser/repo.git",
            cwd="/tmp/project",
        )

    @patch(f"{P}._get_gh_username", return_value="myuser")
    @patch("app.github.run_gh", side_effect=RuntimeError("already exists"))
    @patch(f"{P}.run_git_strict")
    def test_fork_already_exists_is_ok(self, mock_git, mock_gh, mock_user):
        result = _mod._create_fork_and_configure(
            "upstream-owner", "repo", "/tmp/project"
        )
        assert result == "myuser/repo"

    @patch(f"{P}._get_gh_username", return_value=None)
    @patch("app.github.run_gh", return_value="")
    def test_no_username_raises(self, mock_gh, mock_user):
        with pytest.raises(RuntimeError, match="Cannot determine"):
            _mod._create_fork_and_configure(
                "upstream-owner", "repo", "/tmp/project"
            )

    @patch(f"{P}._get_gh_username", return_value="myuser")
    @patch("app.github.run_gh", side_effect=RuntimeError("forbidden"))
    def test_fork_api_error_raises(self, mock_gh, mock_user):
        with pytest.raises(RuntimeError, match="forbidden"):
            _mod._create_fork_and_configure(
                "upstream-owner", "repo", "/tmp/project"
            )


# ---------------------------------------------------------------------------
# handle — name derived from URL
# ---------------------------------------------------------------------------

class TestHandleNameDerivation:
    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_name_from_repo(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(koan_root, instance_dir, args="alice/my-project")
        result = handle(ctx)

        assert "my-project" in result
        assert (workspace_dir / "my-project").is_dir()

    @patch(f"{P}._git_clone")
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_name_from_url_with_git_suffix(
        self, mock_refresh, mock_push, mock_clone,
        koan_root, instance_dir, workspace_dir
    ):
        def fake_clone(url, target):
            Path(target).mkdir(parents=True)

        mock_clone.side_effect = fake_clone
        ctx = _make_ctx(
            koan_root, instance_dir,
            args="https://github.com/alice/my-project.git",
        )
        result = handle(ctx)

        assert "my-project" in result


# ---------------------------------------------------------------------------
# handle — clone URL correctness
# ---------------------------------------------------------------------------

class TestHandleCloneUrl:
    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_correct_clone_url(
        self, mock_refresh, mock_push,
        koan_root, instance_dir, workspace_dir
    ):
        clone_urls = []

        def capture_clone(url, target):
            clone_urls.append(url)
            Path(target).mkdir(parents=True)

        with patch(f"{P}._git_clone", side_effect=capture_clone):
            ctx = _make_ctx(koan_root, instance_dir, args="owner/repo")
            handle(ctx)

        assert clone_urls == ["https://github.com/owner/repo.git"]

    @patch(f"{P}._check_push_access", return_value=True)
    @patch("app.projects_merged.refresh_projects")
    def test_ssh_url_converted_to_https_for_clone(
        self, mock_refresh, mock_push,
        koan_root, instance_dir, workspace_dir
    ):
        clone_urls = []

        def capture_clone(url, target):
            clone_urls.append(url)
            Path(target).mkdir(parents=True)

        with patch(f"{P}._git_clone", side_effect=capture_clone):
            ctx = _make_ctx(
                koan_root, instance_dir,
                args="git@github.com:owner/repo.git",
            )
            handle(ctx)

        assert clone_urls == ["https://github.com/owner/repo.git"]


# ---------------------------------------------------------------------------
# Registry — command and backward-compat alias
# ---------------------------------------------------------------------------

class TestRegistryDiscovery:
    def test_registry_discovers_add_project(self):
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("add_project")
        assert skill is not None, "Command '/add_project' not found in registry"
        assert skill.name == "add_project"

    def test_underscore_alias(self):
        """The underscore alias resolves correctly."""
        from app.skills import build_registry
        registry = build_registry()
        skill = registry.find_by_command("add_project")
        assert skill is not None, "Alias 'add_project' not found"
        assert skill.name == "add_project"
