"""Tests for security audit trail module."""

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from app.security_audit import (
    AUTH_DENY,
    AUTH_GRANT,
    CONFIG_CHANGE,
    GIT_OPERATION,
    MISSION_COMPLETE,
    MISSION_FAIL,
    MISSION_START,
    SUBPROCESS_EXEC,
    _redact_list,
    _redact_secrets,
    _truncate,
    log_event,
    read_recent_events,
)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class TestRedactSecrets:
    def test_anthropic_key(self):
        assert "<REDACTED>" in _redact_secrets("key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz")

    def test_github_pat(self):
        assert _redact_secrets("ghp_" + "a" * 36) == "<REDACTED>"

    def test_github_app_token(self):
        assert _redact_secrets("ghs_" + "x" * 36) == "<REDACTED>"

    def test_github_fine_grained(self):
        assert _redact_secrets("github_pat_" + "x" * 22) == "<REDACTED>"

    def test_slack_token(self):
        assert "<REDACTED>" in _redact_secrets("xoxb-123-456-abc")

    def test_aws_access_key(self):
        assert "<REDACTED>" in _redact_secrets("key=AKIAIOSFODNN7EXAMPLE")

    def test_postgres_connection_string(self):
        result = _redact_secrets("db=postgres://user:pass@host:5432/mydb")
        assert "postgres://" not in result
        assert "<REDACTED>" in result

    def test_mysql_connection_string(self):
        result = _redact_secrets("db=mysql://root:secret@localhost/app")
        assert "mysql://" not in result
        assert "<REDACTED>" in result

    def test_ssh_private_key_header(self):
        result = _redact_secrets("found -----BEGIN RSA PRIVATE KEY----- in file")
        assert "BEGIN RSA PRIVATE KEY" not in result
        assert "<REDACTED>" in result

    def test_ssh_openssh_key_header(self):
        result = _redact_secrets("-----BEGIN OPENSSH PRIVATE KEY-----")
        assert "<REDACTED>" in result

    def test_bearer_auth_header(self):
        result = _redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5c")
        assert "eyJhbG" not in result
        assert "<REDACTED>" in result

    def test_basic_auth_header(self):
        result = _redact_secrets("Authorization: Basic dXNlcjpwYXNzd29yZA==")
        assert "dXNlcjpwYXNz" not in result
        assert "<REDACTED>" in result

    def test_aws_secret_env_var(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY")
        assert "<REDACTED>" in _redact_secrets("key=wJalrXUtnFEMI/K7MDENG/bPxRfiCY")

    def test_slack_token_env_var(self, monkeypatch):
        monkeypatch.setenv("SLACK_TOKEN", "xoxp-my-slack-token")
        assert "<REDACTED>" in _redact_secrets("token=xoxp-my-slack-token")

    def test_no_secret(self):
        assert _redact_secrets("hello world") == "hello world"

    def test_env_var_value(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "my-secret-key-value")
        assert _redact_secrets("using my-secret-key-value here") == "using <REDACTED> here"

    def test_mixed_content(self):
        text = "cmd --token ghp_" + "a" * 36 + " --verbose"
        result = _redact_secrets(text)
        assert "<REDACTED>" in result
        assert "--verbose" in result


class TestRedactList:
    def test_redacts_each_item(self):
        args = ["cmd", "--key", "sk-" + "a" * 20, "safe"]
        result = _redact_list(args)
        assert result[0] == "cmd"
        assert result[3] == "safe"
        assert "<REDACTED>" in result[2]


class TestTruncate:
    def test_short_string(self):
        assert _truncate("hello") == "hello"

    def test_long_string(self):
        result = _truncate("x" * 3000)
        assert len(result) == 2000
        assert result.endswith("...")


# ---------------------------------------------------------------------------
# Event logging
# ---------------------------------------------------------------------------

class TestLogEvent:
    def test_writes_valid_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        log_event(MISSION_START, details={"mission": "test task", "project": "demo"})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        assert audit_file.exists()
        data = json.loads(audit_file.read_text().strip())
        assert data["event_type"] == MISSION_START
        assert data["details"]["mission"] == "test task"
        assert data["result"] == "success"
        assert "timestamp" in data

    def test_creates_audit_dir_lazily(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        log_event(MISSION_COMPLETE, details={"mission": "done"})
        assert (tmp_path / "instance" / "audit").is_dir()

    def test_actor_field(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        log_event(AUTH_GRANT, actor={"type": "github", "id": "user1"})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        data = json.loads(audit_file.read_text().strip())
        assert data["actor"] == {"type": "github", "id": "user1"}

    def test_no_actor_omits_field(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        log_event(MISSION_FAIL, details={"mission": "failed"})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        data = json.loads(audit_file.read_text().strip())
        assert "actor" not in data

    def test_noop_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": False, "max_size_mb": 10, "redact_patterns": []})

        log_event(MISSION_START, details={"mission": "should not log"})

        audit_dir = tmp_path / "instance" / "audit"
        assert not audit_dir.exists() or not (audit_dir / "security.jsonl").exists()

    def test_redacts_secrets_in_details(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        secret = "ghp_" + "a" * 36
        log_event(SUBPROCESS_EXEC, details={"cmd": f"gh auth --token {secret}"})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        content = audit_file.read_text()
        assert secret not in content
        assert "<REDACTED>" in content

    def test_redacts_secrets_in_lists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        secret = "sk-" + "a" * 40
        log_event(SUBPROCESS_EXEC, details={"cmd": ["run", "--key", secret]})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        content = audit_file.read_text()
        assert secret not in content

    def test_truncates_long_values(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        long_text = "x" * 5000
        log_event(MISSION_START, details={"mission": long_text})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        data = json.loads(audit_file.read_text().strip())
        assert len(data["details"]["mission"]) == 2000

    def test_graceful_on_readonly_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("KOAN_ROOT", "/nonexistent/readonly/path")
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        # Should not raise
        log_event(MISSION_START, details={"mission": "test"})
        captured = capsys.readouterr()
        assert "Failed to log event" in captured.err

    def test_multiple_events_append(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        log_event(MISSION_START, details={"mission": "first"})
        log_event(MISSION_COMPLETE, details={"mission": "first"})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        lines = [l for l in audit_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == MISSION_START
        assert json.loads(lines[1])["event_type"] == MISSION_COMPLETE

    def test_unicode_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        log_event(MISSION_START, details={"mission": "tâche française 🚀"})

        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        data = json.loads(audit_file.read_text().strip())
        assert "tâche française 🚀" in data["details"]["mission"]


class TestConcurrentWrites:
    def test_threads_dont_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        errors = []

        def writer(n):
            try:
                for i in range(10):
                    log_event(MISSION_START, details={"mission": f"thread-{n}-{i}"})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        audit_file = tmp_path / "instance" / "audit" / "security.jsonl"
        lines = [l for l in audit_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 40
        # Each line is valid JSON
        for line in lines:
            json.loads(line)


class TestRotation:
    def test_rotation_triggered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        # Use tiny threshold to trigger rotation
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 0, "redact_patterns": []})

        audit_dir = tmp_path / "instance" / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = audit_dir / "security.jsonl"
        # Pre-fill with data to exceed threshold
        audit_file.write_text("x" * 1000 + "\n")

        with patch("app.log_rotation.rotate_log") as mock_rotate:
            log_event(MISSION_START, details={"mission": "test"})
            mock_rotate.assert_called_once_with(audit_file)


# ---------------------------------------------------------------------------
# Reading events
# ---------------------------------------------------------------------------

class TestReadRecentEvents:
    def test_reads_last_n(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        monkeypatch.setattr("app.security_audit._get_audit_config",
                            lambda: {"enabled": True, "max_size_mb": 10, "redact_patterns": []})

        for i in range(10):
            log_event(MISSION_START, details={"mission": f"task-{i}"})

        events = read_recent_events(count=3)
        assert len(events) == 3
        assert events[0]["details"]["mission"] == "task-7"
        assert events[2]["details"]["mission"] == "task-9"

    def test_empty_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KOAN_ROOT", str(tmp_path))
        (tmp_path / "instance").mkdir()
        assert read_recent_events() == []

    def test_missing_koan_root(self, monkeypatch):
        monkeypatch.delenv("KOAN_ROOT", raising=False)
        assert read_recent_events() == []
