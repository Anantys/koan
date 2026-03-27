"""Tests for OpenAI Codex CLI provider (app.provider.codex)."""

import os
from unittest.mock import patch, MagicMock

import pytest

from app.provider.codex import CodexProvider
from app.cli_provider import (
    CodexProvider as FacadeCodex,
    get_provider,
    get_provider_name,
    reset_provider,
    build_full_command,
)


# ---------------------------------------------------------------------------
# Package structure
# ---------------------------------------------------------------------------

class TestCodexPackageStructure:
    """Verify Codex provider is properly registered and re-exported."""

    def test_import_from_provider_package(self):
        from app.provider import CodexProvider
        assert CodexProvider.name == "codex"

    def test_import_from_codex_module(self):
        from app.provider.codex import CodexProvider
        assert CodexProvider().binary() == "codex"

    def test_facade_reexports_codex(self):
        """cli_provider.py re-exports CodexProvider."""
        from app.provider import CodexProvider as Package
        assert FacadeCodex is Package

    def test_codex_in_provider_registry(self):
        from app.provider import _PROVIDERS
        assert "codex" in _PROVIDERS

    def test_registry_creates_codex_instance(self):
        from app.provider import _PROVIDERS
        provider = _PROVIDERS["codex"]()
        assert isinstance(provider, CodexProvider)
        assert provider.name == "codex"


# ---------------------------------------------------------------------------
# CodexProvider basics
# ---------------------------------------------------------------------------

class TestCodexProvider:
    """Tests for CodexProvider flag generation."""

    def setup_method(self):
        self.provider = CodexProvider()

    def test_binary(self):
        assert self.provider.binary() == "codex"

    def test_name(self):
        assert self.provider.name == "codex"

    # -- Prompt args --

    def test_prompt_args(self):
        result = self.provider.build_prompt_args("hello world")
        assert result == ["exec", "hello world"]

    def test_prompt_args_multiline(self):
        result = self.provider.build_prompt_args("line1\nline2")
        assert result == ["exec", "line1\nline2"]

    # -- Tool args (no-op for Codex) --

    def test_tool_args_allowed_ignored(self):
        result = self.provider.build_tool_args(allowed_tools=["Bash", "Read"])
        assert result == []

    def test_tool_args_disallowed_ignored(self):
        result = self.provider.build_tool_args(disallowed_tools=["Bash", "Edit", "Write"])
        assert result == []

    def test_tool_args_empty(self):
        assert self.provider.build_tool_args() == []

    # -- Model args --

    def test_model_args(self):
        result = self.provider.build_model_args(model="gpt-5.4")
        assert result == ["--model", "gpt-5.4"]

    def test_model_args_empty(self):
        assert self.provider.build_model_args() == []

    def test_model_args_fallback_ignored(self):
        """Codex has no --fallback-model; fallback is silently ignored."""
        result = self.provider.build_model_args(model="gpt-5.4", fallback="gpt-5.4-mini")
        assert result == ["--model", "gpt-5.4"]
        assert "--fallback-model" not in result

    def test_model_args_only_fallback(self):
        """When only fallback is specified, nothing is emitted."""
        result = self.provider.build_model_args(fallback="gpt-5.4-mini")
        assert result == []

    # -- Output args (no-op) --

    def test_output_args_json(self):
        """Codex output format is a no-op (uses plain text for Kōan compat)."""
        assert self.provider.build_output_args("json") == []

    def test_output_args_empty(self):
        assert self.provider.build_output_args() == []

    # -- Max turns (no-op) --

    def test_max_turns_args(self):
        assert self.provider.build_max_turns_args(3) == []

    def test_max_turns_args_zero(self):
        assert self.provider.build_max_turns_args(0) == []

    # -- MCP args (no-op) --

    def test_mcp_args(self):
        result = self.provider.build_mcp_args(["config1.json"])
        assert result == []

    def test_mcp_args_empty(self):
        assert self.provider.build_mcp_args() == []
        assert self.provider.build_mcp_args([]) == []

    # -- Plugin args (no-op) --

    def test_plugin_args_ignored(self):
        assert self.provider.build_plugin_args(["/tmp/plugins"]) == []

    def test_plugin_args_none(self):
        assert self.provider.build_plugin_args(None) == []
        assert self.provider.build_plugin_args([]) == []

    # -- Permission args --

    def test_permission_args_yolo(self):
        """skip_permissions=True maps to --yolo."""
        assert self.provider.build_permission_args(True) == ["--yolo"]

    def test_permission_args_full_auto(self):
        """skip_permissions=False maps to --full-auto."""
        assert self.provider.build_permission_args(False) == ["--full-auto"]


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------

class TestCodexBuildCommand:
    """Tests for CodexProvider.build_command() — full command assembly."""

    def setup_method(self):
        self.provider = CodexProvider()

    def test_minimal(self):
        cmd = self.provider.build_command(prompt="hello")
        # Default: codex --full-auto exec "hello"
        assert cmd[0] == "codex"
        assert "--full-auto" in cmd
        assert "exec" in cmd
        assert "hello" in cmd

    def test_with_skip_permissions(self):
        cmd = self.provider.build_command(prompt="hello", skip_permissions=True)
        assert cmd[0] == "codex"
        assert "--yolo" in cmd
        assert "--full-auto" not in cmd
        assert "exec" in cmd
        assert "hello" in cmd

    def test_with_model(self):
        cmd = self.provider.build_command(prompt="do stuff", model="gpt-5.4")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "gpt-5.4"

    def test_model_before_exec(self):
        """Global flags (--model) must appear before 'exec' subcommand."""
        cmd = self.provider.build_command(prompt="do stuff", model="gpt-5.4")
        model_idx = cmd.index("--model")
        exec_idx = cmd.index("exec")
        assert model_idx < exec_idx

    def test_yolo_before_exec(self):
        """Permission flags must appear before 'exec'."""
        cmd = self.provider.build_command(prompt="hello", skip_permissions=True)
        yolo_idx = cmd.index("--yolo")
        exec_idx = cmd.index("exec")
        assert yolo_idx < exec_idx

    def test_system_prompt_prepended(self):
        """System prompt is prepended to user prompt (no native flag)."""
        cmd = self.provider.build_command(
            prompt="do the thing",
            system_prompt="You are helpful.",
        )
        # Find the prompt argument (after 'exec')
        exec_idx = cmd.index("exec")
        prompt_text = cmd[exec_idx + 1]
        assert prompt_text.startswith("You are helpful.")
        assert "do the thing" in prompt_text

    def test_fallback_ignored(self):
        """Fallback model is silently ignored."""
        cmd = self.provider.build_command(
            prompt="hello", model="gpt-5.4", fallback="gpt-5.4-mini",
        )
        assert "--fallback-model" not in cmd

    def test_tools_ignored(self):
        """Tool args are silently ignored."""
        cmd = self.provider.build_command(
            prompt="hello",
            allowed_tools=["Bash", "Read"],
            disallowed_tools=["Write"],
        )
        assert "--allowedTools" not in cmd
        assert "--disallowedTools" not in cmd
        assert "--allow-tool" not in cmd

    def test_max_turns_ignored(self):
        cmd = self.provider.build_command(prompt="hello", max_turns=5)
        assert "--max-turns" not in cmd

    def test_mcp_ignored(self):
        cmd = self.provider.build_command(prompt="hello", mcp_configs=["mcp.json"])
        assert "--mcp-config" not in cmd

    def test_plugin_dirs_ignored(self):
        cmd = self.provider.build_command(
            prompt="hello", plugin_dirs=["/tmp/koan-plugins"],
        )
        assert "--plugin-dir" not in cmd

    def test_full_command_shape(self):
        """Full command with all parameters produces correct shape."""
        cmd = self.provider.build_command(
            prompt="implement feature X",
            allowed_tools=["Bash", "Read", "Write"],
            disallowed_tools=["Edit"],
            model="gpt-5.4",
            fallback="gpt-5.4-mini",
            output_format="json",
            max_turns=25,
            mcp_configs=["mcp.json"],
            plugin_dirs=["/tmp/plugins"],
            skip_permissions=True,
            system_prompt="Be concise.",
        )
        assert cmd[0] == "codex"
        assert "--yolo" in cmd
        assert "--model" in cmd
        assert "exec" in cmd
        # Prompt should contain both system prompt and user prompt
        exec_idx = cmd.index("exec")
        prompt_text = cmd[exec_idx + 1]
        assert "Be concise." in prompt_text
        assert "implement feature X" in prompt_text


# ---------------------------------------------------------------------------
# build_extra_flags
# ---------------------------------------------------------------------------

class TestCodexExtraFlags:
    """Tests for build_extra_flags() used by get_claude_flags_for_role."""

    def setup_method(self):
        self.provider = CodexProvider()

    def test_with_model(self):
        result = self.provider.build_extra_flags(model="gpt-5.4")
        assert result == ["--model", "gpt-5.4"]

    def test_with_disallowed_tools(self):
        """Disallowed tools are silently ignored."""
        result = self.provider.build_extra_flags(disallowed_tools=["Bash"])
        assert result == []

    def test_combined(self):
        result = self.provider.build_extra_flags(
            model="gpt-5.4", fallback="gpt-5.4-mini", disallowed_tools=["Bash"],
        )
        assert result == ["--model", "gpt-5.4"]


# ---------------------------------------------------------------------------
# Provider selection via env var / config
# ---------------------------------------------------------------------------

class TestCodexProviderSelection:
    """Tests for selecting Codex via KOAN_CLI_PROVIDER."""

    def setup_method(self):
        reset_provider()

    def teardown_method(self):
        reset_provider()

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "codex", "KOAN_ROOT": "/tmp"})
    def test_env_var_selects_codex(self):
        assert get_provider_name() == "codex"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "codex", "KOAN_ROOT": "/tmp"})
    def test_get_provider_returns_codex(self):
        provider = get_provider()
        assert isinstance(provider, CodexProvider)
        assert provider.name == "codex"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "codex", "KOAN_ROOT": "/tmp"})
    def test_build_full_command_uses_codex(self):
        cmd = build_full_command(prompt="hello")
        assert cmd[0] == "codex"
        assert "exec" in cmd


# ---------------------------------------------------------------------------
# check_quota_available
# ---------------------------------------------------------------------------

class TestCodexQuotaCheck:
    """Tests for CodexProvider.check_quota_available()."""

    def setup_method(self):
        self.provider = CodexProvider()

    @patch("subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr="",
        )
        available, detail = self.provider.check_quota_available("/tmp/project")
        assert available is True
        assert detail == ""

    @patch("subprocess.run")
    def test_timeout_proceeds_optimistically(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=15)
        available, detail = self.provider.check_quota_available("/tmp/project")
        assert available is True

    @patch("subprocess.run")
    def test_generic_error_proceeds_optimistically(self, mock_run):
        mock_run.side_effect = OSError("codex not found")
        available, detail = self.provider.check_quota_available("/tmp/project")
        assert available is True


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

class TestCodexIsAvailable:
    """Tests for CodexProvider.is_available()."""

    def setup_method(self):
        self.provider = CodexProvider()

    @patch("app.provider.codex.shutil.which", return_value="/usr/local/bin/codex")
    def test_available(self, mock_which):
        assert self.provider.is_available() is True

    @patch("app.provider.codex.shutil.which", return_value=None)
    def test_not_available(self, mock_which):
        assert self.provider.is_available() is False
