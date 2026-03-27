"""Tests for provider check_quota_available implementations."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.provider.base import CLIProvider
from app.provider.claude import ClaudeProvider
from app.provider.copilot import CopilotProvider
from app.provider.local import LocalLLMProvider


class TestBaseProviderQuota:
    """Tests for CLIProvider.check_quota_available() base implementation."""

    def test_base_always_returns_available(self):
        """Base implementation returns (True, '') — no quota concept."""

        class StubProvider(CLIProvider):
            name = "stub"
            def binary(self): return "stub"
            def build_prompt_args(self, p): return []
            def build_tool_args(self, a=None, d=None): return []
            def build_model_args(self, m="", f=""): return []
            def build_output_args(self, f=""): return []
            def build_max_turns_args(self, m=0): return []
            def build_mcp_args(self, c=None): return []

        ok, detail = StubProvider().check_quota_available("/tmp")
        assert ok is True
        assert detail == ""


class TestClaudeProviderQuota:
    """Tests for ClaudeProvider.check_quota_available()."""

    def setup_method(self):
        self.provider = ClaudeProvider()

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_quota_available(self, mock_detect, mock_run):
        """When usage shows quota available, returns (True, '')."""
        mock_run.return_value = MagicMock(
            stdout="Tokens used: 1000/50000",
            stderr="",
            returncode=0,
        )

        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""
        mock_run.assert_called_once()
        # Verify 'claude usage' command
        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "usage"]

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=True)
    def test_quota_exhausted(self, mock_detect, mock_run):
        """When quota is exhausted, returns (False, combined_output)."""
        mock_run.return_value = MagicMock(
            stdout="Tokens used: 50000/50000\nQuota exceeded!",
            stderr="Rate limit exceeded",
            returncode=0,
        )

        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is False
        assert "Rate limit exceeded" in detail
        assert "Quota exceeded!" in detail

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15))
    def test_timeout_returns_optimistic(self, mock_run):
        """On timeout, proceed optimistically (True, '')."""
        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""

    @patch("subprocess.run", side_effect=FileNotFoundError("claude not found"))
    def test_binary_not_found_returns_optimistic(self, mock_run):
        """When CLI binary is missing, proceed optimistically."""
        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""

    @patch("subprocess.run", side_effect=OSError("disk error"))
    def test_os_error_returns_optimistic(self, mock_run):
        """On generic OS error, proceed optimistically."""
        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_passes_cwd(self, mock_detect, mock_run):
        """Verify project_path is forwarded as cwd."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        self.provider.check_quota_available("/my/custom/path")
        kwargs = mock_run.call_args[1]
        assert kwargs["cwd"] == "/my/custom/path"

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_captures_output(self, mock_detect, mock_run):
        """Verify subprocess captures stdout/stderr."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        self.provider.check_quota_available("/tmp")
        kwargs = mock_run.call_args[1]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_custom_timeout(self, mock_detect, mock_run):
        """Custom timeout parameter is respected."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        self.provider.check_quota_available("/tmp", timeout=30)
        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 30

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=True)
    def test_combines_stderr_and_stdout(self, mock_detect, mock_run):
        """Combined output includes both stderr and stdout."""
        mock_run.return_value = MagicMock(
            stdout="stdout data",
            stderr="stderr data",
            returncode=0,
        )

        ok, detail = self.provider.check_quota_available("/tmp")
        assert ok is False
        # detect_quota_exhaustion receives combined stderr + stdout
        combined = mock_detect.call_args[0][0]
        assert "stderr data" in combined
        assert "stdout data" in combined

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_handles_none_stdout(self, mock_detect, mock_run):
        """When stdout or stderr is None, doesn't crash."""
        mock_run.return_value = MagicMock(
            stdout=None,
            stderr=None,
            returncode=0,
        )

        ok, detail = self.provider.check_quota_available("/tmp")
        assert ok is True


class TestLocalProviderQuota:
    """Local/Ollama providers have no quota concept."""

    def test_local_always_available(self):
        """LocalLLMProvider inherits base (True, '')."""
        provider = LocalLLMProvider()
        ok, detail = provider.check_quota_available("/tmp")
        assert ok is True
        assert detail == ""


class TestCopilotProviderQuota:
    """Tests for CopilotProvider.check_quota_available() — minimal probe."""

    def setup_method(self):
        # Create a CopilotProvider with mocked binary availability
        with patch("app.provider.copilot.shutil.which",
                    side_effect=lambda x: "/usr/local/bin/copilot" if x == "copilot" else None):
            self.provider = CopilotProvider()

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_quota_available(self, mock_detect, mock_run):
        """Returns (True, '') when probe succeeds without quota signals."""
        mock_run.return_value = MagicMock(
            stdout="ok",
            stderr="",
            returncode=0,
        )

        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""
        mock_run.assert_called_once()
        # Verify probe command
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "copilot"
        assert "-p" in cmd
        assert "ok" in cmd

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=True)
    def test_quota_exhausted(self, mock_detect, mock_run):
        """Returns (False, combined_output) when probe hits rate limit."""
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="HTTP 429: too many requests\nRetry-After: 300",
            returncode=1,
        )

        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is False
        assert "429" in detail
        assert "too many requests" in detail

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15))
    def test_timeout_returns_optimistic(self, mock_run):
        """On timeout, proceed optimistically (True, '')."""
        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""

    @patch("subprocess.run", side_effect=FileNotFoundError("copilot not found"))
    def test_binary_not_found_returns_optimistic(self, mock_run):
        """When CLI binary is missing, proceed optimistically."""
        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""

    @patch("subprocess.run", side_effect=OSError("disk error"))
    def test_os_error_returns_optimistic(self, mock_run):
        """On generic OS error, proceed optimistically."""
        ok, detail = self.provider.check_quota_available("/tmp/project")
        assert ok is True
        assert detail == ""

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_passes_cwd(self, mock_detect, mock_run):
        """Verify project_path is forwarded as cwd."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        self.provider.check_quota_available("/my/custom/path")
        kwargs = mock_run.call_args[1]
        assert kwargs["cwd"] == "/my/custom/path"

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_custom_timeout(self, mock_detect, mock_run):
        """Custom timeout parameter is respected."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        self.provider.check_quota_available("/tmp", timeout=30)
        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 30

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=True)
    def test_combines_stderr_and_stdout(self, mock_detect, mock_run):
        """Combined output includes both stderr and stdout."""
        mock_run.return_value = MagicMock(
            stdout="stdout data",
            stderr="stderr data",
            returncode=0,
        )

        ok, detail = self.provider.check_quota_available("/tmp")
        assert ok is False
        combined = mock_detect.call_args[0][0]
        assert "stderr data" in combined
        assert "stdout data" in combined

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_handles_none_stdout(self, mock_detect, mock_run):
        """When stdout or stderr is None, doesn't crash."""
        mock_run.return_value = MagicMock(
            stdout=None,
            stderr=None,
            returncode=0,
        )

        ok, detail = self.provider.check_quota_available("/tmp")
        assert ok is True

    @patch("subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_nonzero_exit_no_pattern_proceeds_optimistically(self, mock_detect, mock_run):
        """Non-zero exit without quota pattern → proceed optimistically."""
        mock_run.return_value = MagicMock(
            stdout="Some error occurred",
            stderr="connection refused",
            returncode=1,
        )

        ok, detail = self.provider.check_quota_available("/tmp")
        assert ok is True
        assert detail == ""

    def test_gh_mode_probe_command(self):
        """In gh mode, probe command uses 'gh copilot -p ok'."""
        with patch("app.provider.copilot.shutil.which",
                    side_effect=lambda x: "/usr/bin/gh" if x == "gh" else None):
            provider = CopilotProvider()

        with patch("subprocess.run") as mock_run, \
             patch("app.quota_handler.detect_quota_exhaustion", return_value=False):
            mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
            provider.check_quota_available("/tmp")

            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "gh"
            assert "copilot" in cmd
            assert "-p" in cmd
            assert "ok" in cmd
