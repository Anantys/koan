"""
Tests for the setup wizard web app.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def temp_koan_root():
    """Create a temporary KOAN_ROOT for testing."""
    temp_dir = tempfile.mkdtemp()
    old_root = os.environ.get("KOAN_ROOT")
    os.environ["KOAN_ROOT"] = temp_dir

    # Create instance.example structure
    instance_example = Path(temp_dir) / "instance.example"
    instance_example.mkdir(parents=True)
    (instance_example / "config.yaml").write_text("# Config\n")
    (instance_example / "soul.md").write_text("# Soul\n")
    (instance_example / "missions.md").write_text("# Missions\n")

    # Create env.example
    env_example = Path(temp_dir) / "env.example"
    env_example.write_text("""KOAN_ROOT=/path/to/this/repo
KOAN_TELEGRAM_TOKEN=your-bot-token
KOAN_TELEGRAM_CHAT_ID=your-chat-id
KOAN_PROJECT_PATH=/path/to/your/project
""")

    yield temp_dir

    # Cleanup
    if old_root:
        os.environ["KOAN_ROOT"] = old_root
    else:
        del os.environ["KOAN_ROOT"]
    shutil.rmtree(temp_dir)


@pytest.fixture
def wizard_app(temp_koan_root):
    """Create a test client for the wizard app."""
    # Need to import after setting KOAN_ROOT
    from app.setup_wizard import app, KOAN_ROOT, INSTANCE_DIR, ENV_FILE

    # Update module-level paths to match temp dir
    import app.setup_wizard as wizard_module
    wizard_module.KOAN_ROOT = Path(temp_koan_root)
    wizard_module.INSTANCE_DIR = Path(temp_koan_root) / "instance"
    wizard_module.INSTANCE_EXAMPLE = Path(temp_koan_root) / "instance.example"
    wizard_module.ENV_FILE = Path(temp_koan_root) / ".env"
    wizard_module.ENV_EXAMPLE = Path(temp_koan_root) / "env.example"
    wizard_module.CONFIG_FILE = Path(temp_koan_root) / "instance" / "config.yaml"

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, Path(temp_koan_root)


class TestInstallationStatus:
    """Tests for get_installation_status()."""

    def test_fresh_install(self, wizard_app):
        """Fresh install should have no instance, no env, no venv."""
        client, root = wizard_app

        response = client.get("/api/status")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["instance_exists"] is False
        assert data["env_exists"] is False
        assert data["telegram_configured"] is False
        assert data["projects_configured"] is False

    def test_after_init(self, wizard_app):
        """After init, instance and env should exist."""
        client, root = wizard_app

        # Run init
        response = client.post("/step/welcome/init")
        assert response.status_code == 200

        # Check status
        response = client.get("/api/status")
        data = json.loads(response.data)
        assert data["instance_exists"] is True
        assert data["env_exists"] is True


class TestWizardRoutes:
    """Tests for wizard navigation."""

    def test_index_redirects_to_welcome(self, wizard_app):
        """Index should redirect to welcome step on fresh install."""
        client, root = wizard_app

        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/step/welcome" in response.location

    def test_welcome_page_loads(self, wizard_app):
        """Welcome page should load successfully."""
        client, root = wizard_app

        response = client.get("/step/welcome")
        assert response.status_code == 200
        assert b"Prerequisites" in response.data

    def test_telegram_page_loads(self, wizard_app):
        """Telegram page should load after init."""
        client, root = wizard_app

        # Init first
        client.post("/step/welcome/init")

        response = client.get("/step/telegram")
        assert response.status_code == 200
        assert b"Telegram Bot" in response.data

    def test_projects_page_loads(self, wizard_app):
        """Projects page should load."""
        client, root = wizard_app

        # Init first
        client.post("/step/welcome/init")

        response = client.get("/step/projects")
        assert response.status_code == 200
        assert b"Add Projects" in response.data


class TestTelegramVerification:
    """Tests for Telegram token verification."""

    def test_verify_empty_token(self, wizard_app):
        """Empty token should fail."""
        client, root = wizard_app

        response = client.post(
            "/step/telegram/verify",
            json={"token": ""},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is False
        assert "required" in data["error"].lower()

    @patch("app.setup_wizard.get_chat_id_from_updates", return_value=None)
    @patch("app.setup_wizard.verify_telegram_token")
    def test_verify_valid_token(self, mock_verify, mock_chat, wizard_app):
        """Valid token should return bot info."""
        client, root = wizard_app
        mock_verify.return_value = {
            "valid": True,
            "username": "TestBot",
            "first_name": "Test"
        }

        response = client.post(
            "/step/telegram/verify",
            json={"token": "123456:ABC"},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is True
        assert data["username"] == "TestBot"

    @patch("app.setup_wizard.verify_telegram_token")
    def test_verify_invalid_token(self, mock_verify, wizard_app):
        """Invalid token should fail gracefully."""
        client, root = wizard_app
        mock_verify.return_value = {"valid": False, "error": "Bad token"}

        response = client.post(
            "/step/telegram/verify",
            json={"token": "invalid"},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is False


class TestTelegramSave:
    """Tests for saving Telegram configuration."""

    def test_save_telegram_config(self, wizard_app):
        """Should save token and chat ID to .env."""
        client, root = wizard_app

        # Init first
        client.post("/step/welcome/init")

        # Save telegram config
        response = client.post(
            "/step/telegram/save",
            json={"token": "123456:ABC", "chat_id": "987654"},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is True

        # Verify .env was updated
        env_content = (root / ".env").read_text()
        assert "KOAN_TELEGRAM_TOKEN=123456:ABC" in env_content
        assert "KOAN_TELEGRAM_CHAT_ID=987654" in env_content


class TestProjectValidation:
    """Tests for project path validation."""

    def test_validate_empty_path(self, wizard_app):
        """Empty path should fail."""
        client, root = wizard_app

        response = client.post(
            "/step/projects/validate",
            json={"path": ""},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is False
        assert "required" in data["error"].lower()

    def test_validate_nonexistent_path(self, wizard_app):
        """Non-existent path should fail."""
        client, root = wizard_app

        response = client.post(
            "/step/projects/validate",
            json={"path": "/nonexistent/path/12345"},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is False
        assert "not exist" in data["error"].lower()

    def test_validate_valid_path(self, wizard_app):
        """Valid directory should pass."""
        client, root = wizard_app

        # Create a test project directory
        project_dir = root / "test-project"
        project_dir.mkdir()

        response = client.post(
            "/step/projects/validate",
            json={"path": str(project_dir)},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is True
        assert data["absolute_path"] == str(project_dir)
        assert data["has_claude_md"] is False
        assert data["is_git_repo"] is False

    def test_validate_path_with_claude_md(self, wizard_app):
        """Directory with CLAUDE.md should be detected."""
        client, root = wizard_app

        project_dir = root / "test-project"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# Project")

        response = client.post(
            "/step/projects/validate",
            json={"path": str(project_dir)},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is True
        assert data["has_claude_md"] is True

    def test_validate_git_repo(self, wizard_app):
        """Git repo should be detected."""
        client, root = wizard_app

        project_dir = root / "test-project"
        project_dir.mkdir()
        (project_dir / ".git").mkdir()

        response = client.post(
            "/step/projects/validate",
            json={"path": str(project_dir)},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is True
        assert data["is_git_repo"] is True


    @pytest.mark.skipif(os.getuid() == 0, reason="root ignores file permissions")
    def test_validate_non_writable_path(self, wizard_app):
        """Non-writable directory should fail validation."""
        client, root = wizard_app

        project_dir = root / "readonly-project"
        project_dir.mkdir()
        project_dir.chmod(0o444)

        try:
            response = client.post(
                "/step/projects/validate",
                json={"path": str(project_dir)},
                content_type="application/json"
            )
            data = json.loads(response.data)
            assert data["valid"] is False
            assert "not writable" in data["error"].lower()
        finally:
            project_dir.chmod(0o755)


class TestProjectSave:
    """Tests for saving project configuration to projects.yaml."""

    def test_save_single_project(self, wizard_app):
        """Single project should create projects.yaml."""
        client, root = wizard_app

        # Init and create project dir
        client.post("/step/welcome/init")
        project_dir = root / "my-project"
        project_dir.mkdir()

        response = client.post(
            "/step/projects/save",
            json={"projects": [{"name": "myproject", "path": str(project_dir)}]},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is True

        import yaml
        projects_yaml = root / "projects.yaml"
        assert projects_yaml.exists()
        config = yaml.safe_load(projects_yaml.read_text())
        assert "myproject" in config["projects"]
        assert config["projects"]["myproject"]["path"] == str(project_dir)

    def test_save_multiple_projects(self, wizard_app):
        """Multiple projects should create projects.yaml with all entries."""
        client, root = wizard_app

        # Init and create project dirs
        client.post("/step/welcome/init")
        project1 = root / "project1"
        project2 = root / "project2"
        project1.mkdir()
        project2.mkdir()

        response = client.post(
            "/step/projects/save",
            json={
                "projects": [
                    {"name": "proj1", "path": str(project1)},
                    {"name": "proj2", "path": str(project2)}
                ]
            },
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is True

        import yaml
        projects_yaml = root / "projects.yaml"
        assert projects_yaml.exists()
        config = yaml.safe_load(projects_yaml.read_text())
        assert "proj1" in config["projects"]
        assert "proj2" in config["projects"]
        assert config["projects"]["proj1"]["path"] == str(project1)
        assert config["projects"]["proj2"]["path"] == str(project2)


class TestSetupExecution:
    """Tests for running make setup."""

    @patch("app.setup_wizard.run_make_setup")
    def test_run_setup_success(self, mock_setup, wizard_app):
        """Successful setup should return ok."""
        client, root = wizard_app
        mock_setup.return_value = (True, "Dependencies installed")

        response = client.post("/step/ready/setup")
        data = json.loads(response.data)
        assert data["ok"] is True

    @patch("app.setup_wizard.run_make_setup")
    def test_run_setup_failure(self, mock_setup, wizard_app):
        """Failed setup should return error."""
        client, root = wizard_app
        mock_setup.return_value = (False, "pip install failed")

        response = client.post("/step/ready/setup")
        data = json.loads(response.data)
        assert data["ok"] is False
        assert "pip install failed" in data["output"]


# ===================================================================
# Helper function unit tests (no Flask client needed)
# ===================================================================


class TestCreateInstanceDir:
    """Tests for create_instance_dir() helper."""

    def test_already_exists(self, temp_koan_root):
        """Returns True immediately when instance/ already exists."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.INSTANCE_DIR.mkdir()
        assert wiz.create_instance_dir() is True

    def test_copies_from_example(self, temp_koan_root):
        """Copies instance.example/ to instance/ and returns True."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.INSTANCE_EXAMPLE = Path(temp_koan_root) / "instance.example"
        assert not wiz.INSTANCE_DIR.exists()
        assert wiz.create_instance_dir() is True
        assert wiz.INSTANCE_DIR.exists()
        assert (wiz.INSTANCE_DIR / "config.yaml").exists()

    def test_no_example_returns_false(self, temp_koan_root):
        """Returns False when instance.example/ doesn't exist."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.INSTANCE_EXAMPLE = Path(temp_koan_root) / "no-such-dir"
        assert wiz.create_instance_dir() is False


class TestCreateEnvFile:
    """Tests for create_env_file() helper."""

    def test_already_exists(self, temp_koan_root):
        """Returns True immediately when .env already exists."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("existing")
        assert wiz.create_env_file() is True

    def test_copies_from_example(self, temp_koan_root):
        """Copies env.example to .env and returns True."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_EXAMPLE = Path(temp_koan_root) / "env.example"
        assert not wiz.ENV_FILE.exists()
        assert wiz.create_env_file() is True
        assert wiz.ENV_FILE.exists()

    def test_no_example_returns_false(self, temp_koan_root):
        """Returns False when env.example doesn't exist."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_EXAMPLE = Path(temp_koan_root) / "no-such-file"
        assert wiz.create_env_file() is False


class TestUpdateEnvVar:
    """Tests for update_env_var() helper."""

    def test_no_env_file(self, temp_koan_root):
        """Returns False when .env doesn't exist."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        assert wiz.update_env_var("KEY", "val") is False

    def test_updates_existing_var(self, temp_koan_root):
        """Replaces existing KEY=old with KEY=new."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("FOO=old\nBAR=keep\n")
        assert wiz.update_env_var("FOO", "new") is True
        content = wiz.ENV_FILE.read_text()
        assert "FOO=new" in content
        assert "BAR=keep" in content
        assert "FOO=old" not in content

    def test_uncomments_var(self, temp_koan_root):
        """Replaces '# KEY=placeholder' with 'KEY=value'."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("# KOAN_TOKEN=your-token\n")
        assert wiz.update_env_var("KOAN_TOKEN", "real-token") is True
        content = wiz.ENV_FILE.read_text()
        assert "KOAN_TOKEN=real-token" in content
        assert "# KOAN_TOKEN" not in content

    def test_appends_new_var(self, temp_koan_root):
        """Appends KEY=value when key doesn't exist yet."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("EXISTING=yes\n")
        assert wiz.update_env_var("NEW_VAR", "hello") is True
        content = wiz.ENV_FILE.read_text()
        assert "NEW_VAR=hello" in content
        assert "EXISTING=yes" in content


class TestGetEnvVar:
    """Tests for get_env_var() helper."""

    def test_no_env_file(self, temp_koan_root):
        """Returns None when .env doesn't exist."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        assert wiz.get_env_var("KEY") is None

    def test_reads_existing_var(self, temp_koan_root):
        """Reads value of existing key."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("MY_VAR=hello\nOTHER=world\n")
        assert wiz.get_env_var("MY_VAR") == "hello"
        assert wiz.get_env_var("OTHER") == "world"

    def test_strips_quotes(self, temp_koan_root):
        """Strips surrounding quotes from values."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text('DOUBLE="quoted"\nSINGLE=\'quoted\'\n')
        assert wiz.get_env_var("DOUBLE") == "quoted"
        assert wiz.get_env_var("SINGLE") == "quoted"

    def test_missing_var_returns_none(self, temp_koan_root):
        """Returns None for non-existent key."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("FOO=bar\n")
        assert wiz.get_env_var("MISSING") is None

    def test_skips_comments(self, temp_koan_root):
        """Commented-out vars are not returned."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("# TOKEN=old\nTOKEN=real\n")
        assert wiz.get_env_var("TOKEN") == "real"

    def test_value_with_equals(self, temp_koan_root):
        """Values containing = are preserved (split on first = only)."""
        import app.setup_wizard as wiz
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.ENV_FILE.write_text("URL=https://example.com?a=1&b=2\n")
        assert wiz.get_env_var("URL") == "https://example.com?a=1&b=2"


class TestVerifyTelegramToken:
    """Tests for verify_telegram_token() helper (mocked HTTP)."""

    @patch("urllib.request.urlopen")
    def test_valid_token(self, mock_urlopen, temp_koan_root):
        """Valid token returns bot username and first_name."""
        import app.setup_wizard as wiz
        response_data = json.dumps({
            "ok": True,
            "result": {"username": "MyBot", "first_name": "Bot"}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = wiz.verify_telegram_token("123:ABC")
        assert result["valid"] is True
        assert result["username"] == "MyBot"

    @patch("urllib.request.urlopen")
    def test_api_returns_not_ok(self, mock_urlopen, temp_koan_root):
        """API returning ok=false results in valid=False."""
        import app.setup_wizard as wiz
        response_data = json.dumps({"ok": False}).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = wiz.verify_telegram_token("bad:token")
        assert result["valid"] is False

    @patch("urllib.request.urlopen", side_effect=Exception("Connection refused"))
    def test_network_error(self, mock_urlopen, temp_koan_root):
        """Network error returns valid=False with error message."""
        import app.setup_wizard as wiz
        result = wiz.verify_telegram_token("123:ABC")
        assert result["valid"] is False
        assert "Connection refused" in result["error"]


class TestGetChatIdFromUpdates:
    """Tests for get_chat_id_from_updates() helper (mocked HTTP)."""

    @patch("urllib.request.urlopen")
    def test_returns_chat_id(self, mock_urlopen, temp_koan_root):
        """Extracts chat ID from first message update."""
        import app.setup_wizard as wiz
        response_data = json.dumps({
            "ok": True,
            "result": [
                {"message": {"chat": {"id": 12345}}}
            ]
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        assert wiz.get_chat_id_from_updates("123:ABC") == "12345"

    @patch("urllib.request.urlopen")
    def test_no_updates_returns_none(self, mock_urlopen, temp_koan_root):
        """No updates available returns None."""
        import app.setup_wizard as wiz
        response_data = json.dumps({"ok": True, "result": []}).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        assert wiz.get_chat_id_from_updates("123:ABC") is None

    @patch("urllib.request.urlopen")
    def test_update_without_message_skipped(self, mock_urlopen, temp_koan_root):
        """Updates without message.chat.id are skipped."""
        import app.setup_wizard as wiz
        response_data = json.dumps({
            "ok": True,
            "result": [
                {"callback_query": {}},
                {"message": {"chat": {"id": 99999}}}
            ]
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        assert wiz.get_chat_id_from_updates("123:ABC") == "99999"

    @patch("urllib.request.urlopen", side_effect=OSError("timeout"))
    def test_network_error_returns_none(self, mock_urlopen, temp_koan_root):
        """Network error returns None silently."""
        import app.setup_wizard as wiz
        assert wiz.get_chat_id_from_updates("123:ABC") is None


class TestRunMakeSetup:
    """Tests for run_make_setup() helper."""

    @patch("subprocess.run")
    def test_success(self, mock_run, temp_koan_root):
        """Successful make setup returns (True, output)."""
        import app.setup_wizard as wiz
        wiz.KOAN_ROOT = Path(temp_koan_root)
        mock_run.return_value = MagicMock(
            returncode=0, stdout="OK\n", stderr=""
        )
        success, output = wiz.run_make_setup()
        assert success is True
        assert "OK" in output

    @patch("subprocess.run")
    def test_failure(self, mock_run, temp_koan_root):
        """Failed make setup returns (False, combined output)."""
        import app.setup_wizard as wiz
        wiz.KOAN_ROOT = Path(temp_koan_root)
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error: pip failed"
        )
        success, output = wiz.run_make_setup()
        assert success is False
        assert "pip failed" in output

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="make", timeout=300))
    def test_timeout(self, mock_run, temp_koan_root):
        """Timeout returns (False, timeout message)."""
        import app.setup_wizard as wiz
        wiz.KOAN_ROOT = Path(temp_koan_root)
        success, output = wiz.run_make_setup()
        assert success is False
        assert "timed out" in output.lower()

    @patch("subprocess.run", side_effect=FileNotFoundError("make not found"))
    def test_command_not_found(self, mock_run, temp_koan_root):
        """Missing make binary returns (False, error)."""
        import app.setup_wizard as wiz
        wiz.KOAN_ROOT = Path(temp_koan_root)
        success, output = wiz.run_make_setup()
        assert success is False
        assert "make not found" in output


class TestLoadWizardProjects:
    """Tests for _load_wizard_projects() helper."""

    @patch("app.utils.get_known_projects", return_value=[("proj1", "/a"), ("proj2", "/b")])
    def test_loads_projects(self, mock_gkp, temp_koan_root):
        """Returns list of dicts with name and path."""
        import app.setup_wizard as wiz
        result = wiz._load_wizard_projects()
        assert len(result) == 2
        assert result[0] == {"name": "proj1", "path": "/a"}

    @patch("app.utils.get_known_projects", side_effect=OSError("config broken"))
    def test_exception_returns_empty(self, mock_gkp, temp_koan_root):
        """Exception is caught, returns empty list."""
        import app.setup_wizard as wiz
        result = wiz._load_wizard_projects()
        assert result == []


class TestInstallationStatusHelpers:
    """Tests for get_installation_status() edge cases."""

    def test_telegram_configured_detection(self, temp_koan_root):
        """Detects real Telegram config vs placeholder values."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.KOAN_ROOT = Path(temp_koan_root)

        wiz.ENV_FILE.write_text(
            "KOAN_TELEGRAM_TOKEN=123456:real\n"
            "KOAN_TELEGRAM_CHAT_ID=99999\n"
        )
        status = wiz.get_installation_status()
        assert status["telegram_configured"] is True

    def test_placeholder_token_not_configured(self, temp_koan_root):
        """Placeholder values are detected as unconfigured."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.KOAN_ROOT = Path(temp_koan_root)

        wiz.ENV_FILE.write_text(
            "KOAN_TELEGRAM_TOKEN=your-bot-token\n"
            "KOAN_TELEGRAM_CHAT_ID=your-chat-id\n"
        )
        status = wiz.get_installation_status()
        assert status["telegram_configured"] is False

    def test_projects_configured_via_yaml(self, temp_koan_root):
        """Detects projects.yaml as configured."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.KOAN_ROOT = Path(temp_koan_root)

        wiz.ENV_FILE.write_text("SOMETHING=yes\n")
        (Path(temp_koan_root) / "projects.yaml").write_text("projects: {}\n")
        status = wiz.get_installation_status()
        assert status["projects_configured"] is True

    def test_projects_configured_via_env(self, temp_koan_root):
        """Detects KOAN_PROJECTS env var as configured."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.KOAN_ROOT = Path(temp_koan_root)

        wiz.ENV_FILE.write_text("KOAN_PROJECTS=/real/path\n")
        status = wiz.get_installation_status()
        assert status["projects_configured"] is True

    def test_projects_placeholder_not_configured(self, temp_koan_root):
        """Placeholder project paths are detected as unconfigured."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.KOAN_ROOT = Path(temp_koan_root)

        wiz.ENV_FILE.write_text("KOAN_PROJECTS=/path/to/project\n")
        status = wiz.get_installation_status()
        assert status["projects_configured"] is False

    @patch("shutil.which", return_value="/usr/bin/claude")
    def test_claude_installed_detection(self, mock_which, temp_koan_root):
        """Detects Claude CLI as installed."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.KOAN_ROOT = Path(temp_koan_root)

        status = wiz.get_installation_status()
        assert status["claude_installed"] is True

    def test_venv_detection(self, temp_koan_root):
        """Detects .venv directory existence."""
        import app.setup_wizard as wiz
        wiz.INSTANCE_DIR = Path(temp_koan_root) / "instance"
        wiz.ENV_FILE = Path(temp_koan_root) / ".env"
        wiz.KOAN_ROOT = Path(temp_koan_root)

        (Path(temp_koan_root) / ".venv").mkdir()
        status = wiz.get_installation_status()
        assert status["venv_exists"] is True


# ===================================================================
# Route edge case tests
# ===================================================================


class TestRouteEdgeCases:
    """Tests for route edge cases not covered above."""

    def test_index_redirects_to_telegram_after_init(self, wizard_app):
        """Index redirects to telegram step when instance exists but telegram not configured."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        # Claude is not in PATH during test, so redirect will still go to welcome
        # due to claude_installed check. Patch it.
        with patch("app.setup_wizard.get_installation_status") as mock_status:
            mock_status.return_value = {
                "instance_exists": True,
                "env_exists": True,
                "venv_exists": True,
                "claude_installed": True,
                "telegram_configured": False,
                "projects_configured": False,
            }
            response = client.get("/", follow_redirects=False)
            assert response.status_code == 302
            assert "/step/telegram" in response.location

    def test_index_redirects_to_projects(self, wizard_app):
        """Index redirects to projects when telegram configured but projects not."""
        client, root = wizard_app
        with patch("app.setup_wizard.get_installation_status") as mock_status:
            mock_status.return_value = {
                "instance_exists": True,
                "env_exists": True,
                "venv_exists": True,
                "claude_installed": True,
                "telegram_configured": True,
                "projects_configured": False,
            }
            response = client.get("/", follow_redirects=False)
            assert response.status_code == 302
            assert "/step/projects" in response.location

    def test_index_redirects_to_ready(self, wizard_app):
        """Index redirects to ready when everything configured."""
        client, root = wizard_app
        with patch("app.setup_wizard.get_installation_status") as mock_status:
            mock_status.return_value = {
                "instance_exists": True,
                "env_exists": True,
                "venv_exists": True,
                "claude_installed": True,
                "telegram_configured": True,
                "projects_configured": True,
            }
            response = client.get("/", follow_redirects=False)
            assert response.status_code == 302
            assert "/step/ready" in response.location

    def test_telegram_page_masks_configured_token(self, wizard_app):
        """Telegram page shows masked token when already configured."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        import app.setup_wizard as wiz
        wiz.update_env_var("KOAN_TELEGRAM_TOKEN", "1234567890:ABCDEFghijklmnopqrstuvwxyz")
        wiz.update_env_var("KOAN_TELEGRAM_CHAT_ID", "99999")

        response = client.get("/step/telegram")
        assert response.status_code == 200
        # The full token should NOT appear in the response
        assert b"1234567890:ABCDEFghijklmnopqrstuvwxyz" not in response.data

    def test_save_telegram_missing_token(self, wizard_app):
        """Save telegram with missing token returns error."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        response = client.post(
            "/step/telegram/save",
            json={"token": "", "chat_id": "99999"},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is False
        assert "required" in data["error"].lower()

    def test_save_telegram_missing_chat_id(self, wizard_app):
        """Save telegram with missing chat_id returns error."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        response = client.post(
            "/step/telegram/save",
            json={"token": "123:ABC", "chat_id": ""},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is False
        assert "required" in data["error"].lower()

    def test_save_projects_empty_list(self, wizard_app):
        """Saving empty project list returns error."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        response = client.post(
            "/step/projects/save",
            json={"projects": []},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is False
        assert "at least one" in data["error"].lower()

    def test_save_projects_invalid_path(self, wizard_app):
        """Saving project with invalid path returns error."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        response = client.post(
            "/step/projects/save",
            json={"projects": [{"name": "bad", "path": "/no/such/dir"}]},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is False
        assert "Invalid path" in data["error"]

    def test_save_projects_name_defaults_to_dirname(self, wizard_app):
        """Project without explicit name uses directory name."""
        client, root = wizard_app
        client.post("/step/welcome/init")
        project_dir = root / "awesome-project"
        project_dir.mkdir()

        response = client.post(
            "/step/projects/save",
            json={"projects": [{"path": str(project_dir)}]},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["ok"] is True

        import yaml
        config = yaml.safe_load((root / "projects.yaml").read_text())
        assert "awesome-project" in config["projects"]

    def test_ready_page_loads(self, wizard_app):
        """Ready page loads with project list."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        with patch("app.setup_wizard._load_wizard_projects", return_value=[]):
            response = client.get("/step/ready")
            assert response.status_code == 200

    def test_finish_setup_all_configured(self, wizard_app):
        """Finish setup returns ok when everything is configured."""
        client, root = wizard_app
        with patch("app.setup_wizard.get_installation_status") as mock_status:
            mock_status.return_value = {
                "instance_exists": True,
                "env_exists": True,
                "venv_exists": True,
                "claude_installed": True,
                "telegram_configured": True,
                "projects_configured": True,
            }
            response = client.post("/step/ready/finish")
            data = json.loads(response.data)
            assert data["ok"] is True
            assert "terminal1" in data["launch_commands"]
            assert "terminal2" in data["launch_commands"]

    def test_finish_setup_incomplete(self, wizard_app):
        """Finish setup returns not ok when config is incomplete."""
        client, root = wizard_app
        with patch("app.setup_wizard.get_installation_status") as mock_status:
            mock_status.return_value = {
                "instance_exists": True,
                "env_exists": True,
                "venv_exists": False,
                "claude_installed": False,
                "telegram_configured": False,
                "projects_configured": False,
            }
            response = client.post("/step/ready/finish")
            data = json.loads(response.data)
            assert data["ok"] is False

    @patch("app.setup_wizard.verify_telegram_token")
    @patch("app.setup_wizard.get_chat_id_from_updates", return_value="54321")
    def test_verify_valid_token_fetches_chat_id(self, mock_chat, mock_verify, wizard_app):
        """Valid token verification also fetches chat ID from updates."""
        client, root = wizard_app
        mock_verify.return_value = {
            "valid": True,
            "username": "TestBot",
            "first_name": "Test"
        }

        response = client.post(
            "/step/telegram/verify",
            json={"token": "123456:ABC"},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is True
        assert data["chat_id"] == "54321"
        mock_chat.assert_called_once_with("123456:ABC")

    def test_validate_file_not_directory(self, wizard_app):
        """Validate project rejects a file (not a directory)."""
        client, root = wizard_app
        file_path = root / "not-a-dir.txt"
        file_path.write_text("content")

        response = client.post(
            "/step/projects/validate",
            json={"path": str(file_path)},
            content_type="application/json"
        )
        data = json.loads(response.data)
        assert data["valid"] is False
        assert "not a directory" in data["error"].lower()

    def test_projects_yaml_has_defaults(self, wizard_app):
        """Saved projects.yaml includes defaults section."""
        client, root = wizard_app
        client.post("/step/welcome/init")
        project_dir = root / "myproj"
        project_dir.mkdir()

        client.post(
            "/step/projects/save",
            json={"projects": [{"name": "myproj", "path": str(project_dir)}]},
            content_type="application/json"
        )

        import yaml
        config = yaml.safe_load((root / "projects.yaml").read_text())
        assert "defaults" in config
        assert config["defaults"]["git_auto_merge"]["enabled"] is False

    def test_projects_yaml_sorted_alphabetically(self, wizard_app):
        """Projects in YAML are sorted alphabetically by name."""
        client, root = wizard_app
        client.post("/step/welcome/init")

        dirs = {}
        for name in ["zebra", "alpha", "middle"]:
            d = root / name
            d.mkdir()
            dirs[name] = d

        client.post(
            "/step/projects/save",
            json={"projects": [
                {"name": "zebra", "path": str(dirs["zebra"])},
                {"name": "alpha", "path": str(dirs["alpha"])},
                {"name": "middle", "path": str(dirs["middle"])},
            ]},
            content_type="application/json"
        )

        import yaml
        config = yaml.safe_load((root / "projects.yaml").read_text())
        keys = list(config["projects"].keys())
        assert keys == sorted(keys)
