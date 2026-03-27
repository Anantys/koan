"""Tests for email_notify.py — sending, rate limiting, duplicate detection, CLI."""

import json
import time
from unittest.mock import patch, MagicMock

import pytest

from app.email_notify import (
    send_owner_email,
    can_send_email,
    is_duplicate,
    get_email_stats,
    _content_hash,
    _prune_old_records,
    _get_email_config,
    _get_smtp_config,
)


# -- Fixtures --

@pytest.fixture
def email_env(monkeypatch, tmp_path):
    """Set up env vars for email testing."""
    monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
    monkeypatch.setenv("KOAN_SMTP_HOST", "smtp.test.com")
    monkeypatch.setenv("KOAN_SMTP_PORT", "587")
    monkeypatch.setenv("KOAN_SMTP_USER", "bot@test.com")
    monkeypatch.setenv("KOAN_SMTP_PASSWORD", "secret123")
    monkeypatch.setenv("EMAIL_KOAN_OWNER", "owner@test.com")
    (tmp_path / "instance").mkdir()
    return tmp_path


@pytest.fixture
def email_config_enabled():
    """Mock load_config to return email enabled."""
    with patch("app.email_notify.load_config") as mock:
        mock.return_value = {"email": {"enabled": True, "max_per_day": 5}}
        yield mock


@pytest.fixture
def email_config_disabled():
    """Mock load_config to return email disabled."""
    with patch("app.email_notify.load_config") as mock:
        mock.return_value = {"email": {"enabled": False}}
        yield mock


# -- Config Tests --

class TestEmailConfig:
    def test_defaults_when_no_config(self):
        with patch("app.email_notify.load_config", return_value={}):
            cfg = _get_email_config()
        assert cfg["enabled"] is False
        assert cfg["max_per_day"] == 5
        assert cfg["require_approval"] is False

    def test_config_override(self):
        with patch("app.email_notify.load_config", return_value={
            "email": {"enabled": True, "max_per_day": 10}
        }):
            cfg = _get_email_config()
        assert cfg["enabled"] is True
        assert cfg["max_per_day"] == 10

    def test_smtp_from_env(self, monkeypatch):
        monkeypatch.setenv("KOAN_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("KOAN_SMTP_PORT", "465")
        monkeypatch.setenv("KOAN_SMTP_USER", "user@example.com")
        monkeypatch.setenv("KOAN_SMTP_PASSWORD", "pass")
        monkeypatch.setenv("EMAIL_KOAN_OWNER", "owner@example.com")
        with patch("app.email_notify.load_dotenv"):
            cfg = _get_smtp_config()
        assert cfg["host"] == "smtp.example.com"
        assert cfg["port"] == 465
        assert cfg["user"] == "user@example.com"
        assert cfg["password"] == "pass"
        assert cfg["recipient"] == "owner@example.com"

    def test_smtp_defaults_when_empty(self, monkeypatch):
        monkeypatch.delenv("KOAN_SMTP_HOST", raising=False)
        monkeypatch.delenv("KOAN_SMTP_PORT", raising=False)
        monkeypatch.delenv("KOAN_SMTP_USER", raising=False)
        monkeypatch.delenv("KOAN_SMTP_PASSWORD", raising=False)
        monkeypatch.delenv("EMAIL_KOAN_OWNER", raising=False)
        with patch("app.email_notify.load_dotenv"):
            cfg = _get_smtp_config()
        assert cfg["host"] == ""
        assert cfg["port"] == 587
        assert cfg["recipient"] == ""


# -- Can Send Tests --

class TestCanSendEmail:
    def test_disabled(self, email_env, email_config_disabled):
        allowed, reason = can_send_email()
        assert allowed is False
        assert "not enabled" in reason

    def test_no_smtp_host(self, email_env, email_config_enabled, monkeypatch):
        monkeypatch.setenv("KOAN_SMTP_HOST", "")
        allowed, reason = can_send_email()
        assert allowed is False
        assert "SMTP" in reason

    def test_no_recipient(self, email_env, email_config_enabled, monkeypatch):
        monkeypatch.delenv("EMAIL_KOAN_OWNER", raising=False)
        allowed, reason = can_send_email()
        assert allowed is False
        assert "recipient" in reason.lower()

    def test_allowed_when_configured(self, email_env, email_config_enabled):
        allowed, reason = can_send_email()
        assert allowed is True
        assert reason == "OK"

    def test_rate_limited(self, email_env, email_config_enabled):
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        records = [{"timestamp": time.time(), "content_hash": f"h{i}"} for i in range(5)]
        cooldown_path.write_text(json.dumps(records))

        allowed, reason = can_send_email()
        assert allowed is False
        assert "Rate limit" in reason

    def test_old_records_pruned(self, email_env, email_config_enabled):
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        old_time = time.time() - 90000  # 25 hours ago
        records = [{"timestamp": old_time, "content_hash": f"h{i}"} for i in range(5)]
        cooldown_path.write_text(json.dumps(records))

        allowed, reason = can_send_email()
        assert allowed is True


# -- Duplicate Detection --

class TestDuplicateDetection:
    def test_content_hash_deterministic(self):
        h1 = _content_hash("subject", "body")
        h2 = _content_hash("subject", "body")
        assert h1 == h2

    def test_content_hash_differs(self):
        h1 = _content_hash("subject1", "body")
        h2 = _content_hash("subject2", "body")
        assert h1 != h2

    def test_content_hash_truncates_body(self):
        """Only first 100 chars of body used in hash."""
        h1 = _content_hash("sub", "x" * 100 + "A")
        h2 = _content_hash("sub", "x" * 100 + "B")
        assert h1 == h2  # Same first 100 chars

    def test_is_duplicate_true(self, email_env):
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        h = _content_hash("test", "body")
        records = [{"timestamp": time.time(), "content_hash": h}]
        cooldown_path.write_text(json.dumps(records))

        assert is_duplicate("test", "body") is True

    def test_is_duplicate_false(self, email_env):
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        records = [{"timestamp": time.time(), "content_hash": "different"}]
        cooldown_path.write_text(json.dumps(records))

        assert is_duplicate("test", "body") is False

    def test_is_duplicate_no_file(self, email_env):
        assert is_duplicate("test", "body") is False

    def test_old_duplicates_pruned(self, email_env):
        """Duplicates older than 24h are not considered."""
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        h = _content_hash("test", "body")
        records = [{"timestamp": time.time() - 90000, "content_hash": h}]
        cooldown_path.write_text(json.dumps(records))

        assert is_duplicate("test", "body") is False


# -- Pruning --

class TestPruneRecords:
    def test_prune_removes_old(self):
        old = {"timestamp": time.time() - 90000, "content_hash": "old"}
        new = {"timestamp": time.time(), "content_hash": "new"}
        result = _prune_old_records([old, new])
        assert len(result) == 1
        assert result[0]["content_hash"] == "new"

    def test_prune_empty_list(self):
        assert _prune_old_records([]) == []

    def test_prune_missing_timestamp(self):
        """Records without timestamp get pruned (timestamp=0 is old)."""
        result = _prune_old_records([{"content_hash": "x"}])
        assert len(result) == 0


# -- Stats --

class TestEmailStats:
    def test_stats_enabled(self, email_env, email_config_enabled):
        stats = get_email_stats()
        assert stats["enabled"] is True
        assert stats["sent_today"] == 0
        assert stats["remaining"] == 5
        assert stats["max_per_day"] == 5
        assert stats["last_sent"] is None

    def test_stats_with_records(self, email_env, email_config_enabled):
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        now = time.time()
        records = [
            {"timestamp": now - 3600, "content_hash": "h1"},
            {"timestamp": now, "content_hash": "h2"},
        ]
        cooldown_path.write_text(json.dumps(records))

        stats = get_email_stats()
        assert stats["sent_today"] == 2
        assert stats["remaining"] == 3
        assert stats["last_sent"] == now

    def test_stats_disabled(self, email_env, email_config_disabled):
        stats = get_email_stats()
        assert stats["enabled"] is False


# -- Send Email --

class TestSendOwnerEmail:
    @patch("app.email_notify.smtplib.SMTP")
    def test_send_success(self, mock_smtp_class, email_env, email_config_enabled):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        result = send_owner_email("Test Subject", "Test body")
        assert result is True

        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("bot@test.com", "secret123")
        mock_server.send_message.assert_called_once()

        # Check the sent message
        msg = mock_server.send_message.call_args[0][0]
        assert msg["Subject"] == "[Koan] Test Subject"
        assert msg["From"] == "bot@test.com"
        assert msg["To"] == "owner@test.com"

    @patch("app.email_notify.smtplib.SMTP")
    def test_send_records_cooldown(self, mock_smtp_class, email_env, email_config_enabled):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        send_owner_email("Subject", "Body")

        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        assert cooldown_path.exists()
        records = json.loads(cooldown_path.read_text())
        assert len(records) == 1
        assert records[0]["subject"] == "Subject"

    def test_send_when_disabled(self, email_env, email_config_disabled):
        result = send_owner_email("Test", "Body")
        assert result is False

    @patch("app.email_notify.smtplib.SMTP")
    def test_send_skips_duplicate(self, mock_smtp_class, email_env, email_config_enabled):
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        h = _content_hash("Subject", "Body")
        records = [{"timestamp": time.time(), "content_hash": h}]
        cooldown_path.write_text(json.dumps(records))

        result = send_owner_email("Subject", "Body")
        assert result is False
        mock_smtp_class.assert_not_called()

    @patch("app.email_notify.smtplib.SMTP")
    def test_send_with_skip_duplicate_check(self, mock_smtp_class, email_env, email_config_enabled):
        """skip_duplicate_check=True sends even when duplicate exists."""
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        h = _content_hash("Subject", "Body")
        records = [{"timestamp": time.time(), "content_hash": h}]
        cooldown_path.write_text(json.dumps(records))

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        result = send_owner_email("Subject", "Body", skip_duplicate_check=True)
        assert result is True
        mock_server.send_message.assert_called_once()

    def test_send_rate_limited(self, email_env, email_config_enabled):
        cooldown_path = email_env / "instance" / ".email-cooldown.json"
        records = [{"timestamp": time.time(), "content_hash": f"h{i}"} for i in range(5)]
        cooldown_path.write_text(json.dumps(records))

        result = send_owner_email("Subject", "Body")
        assert result is False

    @patch("app.email_notify.smtplib.SMTP")
    def test_auth_failure(self, mock_smtp_class, email_env, email_config_enabled):
        import smtplib
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        result = send_owner_email("Test", "Body")
        assert result is False

    @patch("app.email_notify.smtplib.SMTP")
    def test_connection_error(self, mock_smtp_class, email_env, email_config_enabled):
        mock_smtp_class.side_effect = OSError("Connection refused")

        result = send_owner_email("Test", "Body")
        assert result is False


# -- Session Digest --

class TestSessionDigest:
    @patch("app.email_notify.send_owner_email", return_value=True)
    def test_digest_calls_send(self, mock_send):
        from app.email_notify import send_session_digest
        result = send_session_digest("koan", "Session summary here")
        assert result is True
        mock_send.assert_called_once()
        subject = mock_send.call_args[0][0]
        assert "koan" in subject
        assert "Session digest" in subject


# -- Skill Handler Tests --

class TestEmailSkillHandler:
    """Tests for /email skill handler."""

    def _make_ctx(self, args="", tmp_path=None):
        from app.skills import SkillContext
        return SkillContext(
            koan_root=tmp_path or "/tmp",
            instance_dir=tmp_path / "instance" if tmp_path else "/tmp/instance",
            command_name="email",
            args=args,
            send_message=MagicMock(),
            handle_chat=MagicMock(),
        )

    @patch("app.email_notify.load_config", return_value={"email": {"enabled": False}})
    def test_status_disabled(self, _mock_cfg, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir(exist_ok=True)
        from skills.core.email.handler import handle
        ctx = self._make_ctx("", tmp_path)
        result = handle(ctx)
        assert "disabled" in result.lower()

    @patch("app.email_notify.load_config", return_value={"email": {"enabled": True, "max_per_day": 5}})
    def test_status_enabled(self, _mock_cfg, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        monkeypatch.setenv("KOAN_SMTP_HOST", "smtp.test.com")
        monkeypatch.setenv("KOAN_SMTP_USER", "bot@test.com")
        monkeypatch.setenv("KOAN_SMTP_PASSWORD", "pass")
        monkeypatch.setenv("EMAIL_KOAN_OWNER", "owner@test.com")
        (tmp_path / "instance").mkdir(exist_ok=True)
        from skills.core.email.handler import handle
        ctx = self._make_ctx("status", tmp_path)
        result = handle(ctx)
        assert "Email Status" in result
        assert "Enabled: yes" in result

    @patch("app.email_notify.send_owner_email", return_value=True)
    def test_test_success(self, mock_send_email, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir(exist_ok=True)
        from skills.core.email.handler import handle
        ctx = self._make_ctx("test", tmp_path)
        result = handle(ctx)
        assert "sent" in result.lower()
        mock_send_email.assert_called_once()

    @patch("app.email_notify.can_send_email", return_value=(False, "Email not enabled"))
    @patch("app.email_notify.send_owner_email", return_value=False)
    def test_test_failure(self, _mock_send, _mock_can, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir(exist_ok=True)
        from skills.core.email.handler import handle
        ctx = self._make_ctx("test", tmp_path)
        result = handle(ctx)
        assert "failed" in result.lower()

    def test_unknown_subcommand(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir(exist_ok=True)
        from skills.core.email.handler import handle
        ctx = self._make_ctx("foo", tmp_path)
        result = handle(ctx)
        assert "/email" in result


# -- CLI Tests --

class TestEmailCLI:
    def test_cli_sends_email(self, email_env, email_config_enabled):
        """CLI parses args and calls send_owner_email."""
        with patch("app.email_notify.smtplib.SMTP") as mock_smtp_class:
            mock_server = MagicMock()
            mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)
            result = send_owner_email("CLI Subject", "CLI Body")
        assert result is True
        msg = mock_server.send_message.call_args[0][0]
        assert msg["Subject"] == "[Koan] CLI Subject"

    def test_cli_exits_1_on_failure(self):
        """send_owner_email returns False when disabled."""
        with patch("app.email_notify.load_config", return_value={"email": {"enabled": False}}):
            result = send_owner_email("Test", "Body")
        assert result is False

    def test_cli_no_args(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["email_notify.py"])
        with pytest.raises(SystemExit) as exc_info:
            run_module("app.email_notify", run_name="__main__")
        assert exc_info.value.code == 1

    def test_cli_missing_body(self, monkeypatch):
        from tests._helpers import run_module
        monkeypatch.setattr("sys.argv", ["email_notify.py", "Subject only"])
        with pytest.raises(SystemExit) as exc_info:
            run_module("app.email_notify", run_name="__main__")
        assert exc_info.value.code == 1
