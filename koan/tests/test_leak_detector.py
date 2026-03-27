"""Tests for the credential leak detector."""

import io
from unittest.mock import patch

import pytest

from app.leak_detector import redact_secrets, scan_and_redact


# Helper: build test secrets dynamically to avoid GitHub push protection
# flagging them as real credentials in the test file itself.
def _fake(prefix, length=30):
    """Build a fake secret with the given prefix and padding."""
    pad = "A" * (length - len(prefix))
    return prefix + pad


# ---------------------------------------------------------------------------
# redact_secrets — pattern coverage
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    def test_empty_input(self):
        assert redact_secrets("") == ("", [])
        assert redact_secrets(None) == (None, [])

    def test_clean_text(self):
        text = "Just a normal PR description with no secrets."
        clean, detected = redact_secrets(text)
        assert clean == text
        assert detected == []

    # -- Stripe keys --

    def test_stripe_live_secret_key(self):
        key = _fake("sk_" + "live_", 30)
        text = f"key: {key}"
        clean, detected = redact_secrets(text)
        assert "[REDACTED]" in clean
        assert "sk_live_" not in clean
        assert "Stripe live key" in detected

    def test_stripe_test_key(self):
        key = _fake("pk_" + "test_", 28)
        text = key
        clean, detected = redact_secrets(text)
        assert "[REDACTED]" in clean
        assert "Stripe test publishable key" in detected

    # -- OpenAI / Anthropic --

    def test_openai_key(self):
        key = "sk-" + "a" * 40
        text = f"export OPENAI_API_KEY={key}"
        clean, detected = redact_secrets(text)
        assert "sk-" not in clean
        assert "OpenAI/Anthropic API key" in detected

    # -- GitHub tokens --

    def test_github_pat(self):
        key = _fake("ghp_", 40)
        text = f"token: {key}"
        clean, detected = redact_secrets(text)
        assert "ghp_" not in clean
        assert "GitHub personal access token" in detected

    def test_github_oauth(self):
        key = _fake("gho_", 40)
        text = key
        clean, detected = redact_secrets(text)
        assert "gho_" not in clean
        assert "GitHub OAuth token" in detected

    def test_github_fine_grained_pat(self):
        key = "github_pat_" + "X" * 22
        text = key
        clean, detected = redact_secrets(text)
        assert "github_pat_" not in clean
        assert "GitHub fine-grained PAT" in detected

    # -- AWS --

    def test_aws_access_key(self):
        key = "AKIA" + "X" * 16
        text = f"AWS key is {key}"
        clean, detected = redact_secrets(text)
        assert "AKIA" not in clean
        assert "AWS access key ID" in detected

    # -- JWT --

    def test_jwt_token(self):
        header = "eyJhbGciOiJIUzI1NiJ9"
        payload = "eyJ1c2VyIjoiYWRtaW4ifQ"
        sig = "dGVzdHNpZ25hdHVyZQ"
        text = f"Bearer {header}.{payload}.{sig}"
        clean, detected = redact_secrets(text)
        assert "eyJ" not in clean
        assert "JWT token" in detected

    # -- Database URLs --

    def test_postgres_url(self):
        text = "DATABASE_URL=postgres://user:s3cret@db.host.example.test:5432/mydb"
        clean, detected = redact_secrets(text)
        assert "s3cret" not in clean
        assert "Database connection string" in detected

    def test_mongodb_srv(self):
        text = "mongodb+srv://admin:p4ssword@cluster.example.test/db?retryWrites=true"
        clean, detected = redact_secrets(text)
        assert "p4ssword" not in clean
        assert "Database connection string" in detected

    def test_redis_url(self):
        text = "redis://default:mysecret@cache.example.test:6379"
        clean, detected = redact_secrets(text)
        assert "mysecret" not in clean
        assert "Database connection string" in detected

    # -- PEM keys --

    def test_pem_private_key(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBogIBAAJBALsamplekeydata...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        clean, detected = redact_secrets(text)
        assert "BEGIN RSA PRIVATE KEY" not in clean
        assert "PEM private key" in detected

    def test_ec_private_key(self):
        text = (
            "-----BEGIN EC PRIVATE KEY-----\n"
            "MHQCAQEEIFsampledata...\n"
            "-----END EC PRIVATE KEY-----"
        )
        clean, detected = redact_secrets(text)
        assert "PEM private key" in detected

    # -- Generic credential assignments --

    def test_password_assignment(self):
        text = "password=super_secret_password_value"
        clean, detected = redact_secrets(text)
        assert "super_secret_password_value" not in clean
        assert "Credential assignment" in detected

    def test_token_assignment(self):
        text = "token: abcdefghijklmnopqrstuvwxyz"
        clean, detected = redact_secrets(text)
        assert "Credential assignment" in detected

    def test_api_key_assignment(self):
        text = 'api_key="my_super_secret_key_12345"'
        clean, detected = redact_secrets(text)
        assert "Credential assignment" in detected

    # -- Telegram bot tokens --

    def test_telegram_bot_token(self):
        token = "12345678:" + "A" * 35
        text = token
        clean, detected = redact_secrets(text)
        assert "Telegram bot token" in detected

    # -- Slack tokens --

    def test_slack_bot_token(self):
        token = "xoxb-" + "1234567890abcdefghij"
        text = token
        clean, detected = redact_secrets(text)
        assert "Slack token" in detected

    def test_slack_user_token(self):
        token = "xoxp-" + "1234567890abcdefghij"
        text = token
        clean, detected = redact_secrets(text)
        assert "Slack token" in detected

    # -- Multiple secrets --

    def test_multiple_secrets_all_redacted(self):
        aws = "AKIA" + "X" * 16
        ghp = _fake("ghp_", 40)
        text = (
            f"AWS: {aws}\n"
            f"GitHub: {ghp}\n"
            "DB: postgres://user:pass@host.example.test:5432/db"
        )
        clean, detected = redact_secrets(text)
        assert "AKIA" not in clean
        assert "ghp_" not in clean
        assert "postgres://" not in clean
        assert len(detected) == 3

    # -- No false positives for short strings --

    def test_short_sk_not_matched(self):
        """sk- with less than 32 chars should not trigger."""
        text = "sk-short"
        clean, detected = redact_secrets(text)
        assert clean == text
        assert detected == []

    def test_normal_text_with_password_word(self):
        """The word 'password' without an assignment should not trigger."""
        text = "Please reset your password in the settings page."
        clean, detected = redact_secrets(text)
        assert clean == text
        assert detected == []


# ---------------------------------------------------------------------------
# scan_and_redact — logging behavior
# ---------------------------------------------------------------------------


class TestScanAndRedact:
    def test_logs_to_stderr_on_detection(self):
        key = _fake("ghp_", 40)
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            result = scan_and_redact(key, context="PR body")
            assert "[REDACTED]" in result
            assert "PR body" in mock_err.getvalue()
            assert "GitHub personal access token" in mock_err.getvalue()

    def test_no_log_when_clean(self):
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            result = scan_and_redact("clean text", context="test")
            assert result == "clean text"
            assert mock_err.getvalue() == ""

    def test_context_omitted(self):
        key = "AKIA" + "X" * 16
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            scan_and_redact(key)
            assert "[leak-detector]" in mock_err.getvalue()


# ---------------------------------------------------------------------------
# Integration: github.py wrappers call redaction
# ---------------------------------------------------------------------------


class TestGitHubIntegration:
    @patch("app.github.run_gh", return_value="https://github.com/o/r/pull/1")
    def test_pr_create_redacts_body(self, mock_gh):
        from app.github import pr_create

        key = _fake("ghp_", 40)
        url = pr_create(
            title="Fix bug",
            body=f"secret: {key}",
        )
        call_args = mock_gh.call_args
        body_arg = call_args[0][call_args[0].index("--body") + 1]
        assert "ghp_" not in body_arg
        assert "[REDACTED]" in body_arg
        assert url == "https://github.com/o/r/pull/1"

    @patch("app.github.run_gh", return_value="https://github.com/o/r/issues/1")
    def test_issue_create_redacts_body(self, mock_gh):
        from app.github import issue_create

        url = issue_create(
            title="Bug report",
            body="DB: postgres://admin:secret@db.example.test:5432/prod",
        )
        call_args = mock_gh.call_args
        body_arg = call_args[0][call_args[0].index("--body") + 1]
        assert "postgres://" not in body_arg
        assert "[REDACTED]" in body_arg


# ---------------------------------------------------------------------------
# Integration: journal.py calls redaction
# ---------------------------------------------------------------------------


class TestJournalIntegration:
    def test_append_to_journal_redacts_content(self, tmp_path):
        from app.journal import append_to_journal

        instance_dir = tmp_path / "instance"
        instance_dir.mkdir()

        aws_key = "AKIA" + "X" * 16
        append_to_journal(
            instance_dir,
            "test-project",
            f"Debug output: {aws_key}\n",
        )

        journal_dir = instance_dir / "journal"
        files = list(journal_dir.rglob("*.md"))
        assert len(files) == 1

        content = files[0].read_text()
        assert "AKIA" not in content
        assert "[REDACTED]" in content
