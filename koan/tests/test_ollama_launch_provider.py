"""Tests for OllamaLaunchProvider (app.provider.ollama_launch)."""

import os
from unittest.mock import patch, MagicMock

import pytest

from app.provider.ollama_launch import OllamaLaunchProvider


# ---------------------------------------------------------------------------
# Basic properties
# ---------------------------------------------------------------------------

class TestOllamaLaunchBasics:
    """Basic provider properties and identity."""

    def setup_method(self):
        self.provider = OllamaLaunchProvider()

    def test_name(self):
        assert self.provider.name == "ollama-launch"

    def test_binary(self):
        assert self.provider.binary() == "ollama"

    def test_shell_command(self):
        assert self.provider.shell_command() == "ollama launch claude"

    def test_is_available_when_ollama_exists(self):
        with patch("app.provider.ollama_launch.shutil.which", return_value="/usr/bin/ollama"):
            assert self.provider.is_available() is True

    def test_is_available_when_missing(self):
        with patch("app.provider.ollama_launch.shutil.which", return_value=None):
            assert self.provider.is_available() is False

    def test_get_env_empty(self):
        assert self.provider.get_env() == {}

    def test_check_quota_always_available(self):
        available, detail = self.provider.check_quota_available("/some/path")
        assert available is True
        assert detail == ""


# ---------------------------------------------------------------------------
# Configuration resolution
# ---------------------------------------------------------------------------

class TestOllamaLaunchConfig:
    """Config resolution: env var > config.yaml > default."""

    def setup_method(self):
        self.provider = OllamaLaunchProvider()

    def test_model_from_env_var(self):
        with patch.dict(os.environ, {"KOAN_OLLAMA_LAUNCH_MODEL": "llama3.3"}):
            assert self.provider._get_default_model() == "llama3.3"

    def test_model_from_config(self):
        config = {"ollama_launch": {"model": "qwen2.5-coder:14b"}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", return_value=config):
                assert self.provider._get_default_model() == "qwen2.5-coder:14b"

    def test_model_default_empty(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", return_value={}):
                assert self.provider._get_default_model() == ""

    def test_env_var_takes_priority_over_config(self):
        config = {"ollama_launch": {"model": "from-config"}}
        with patch.dict(os.environ, {"KOAN_OLLAMA_LAUNCH_MODEL": "from-env"}):
            with patch("app.utils.load_config", return_value=config):
                assert self.provider._get_default_model() == "from-env"

    def test_config_error_returns_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", side_effect=Exception("no config")):
                assert self.provider._get_default_model() == ""


# ---------------------------------------------------------------------------
# Flag building (Claude Code flags, passed after --)
# ---------------------------------------------------------------------------

class TestOllamaLaunchFlags:
    """Individual flag builders for Claude Code arguments."""

    def setup_method(self):
        self.provider = OllamaLaunchProvider()

    def test_prompt_args(self):
        assert self.provider.build_prompt_args("hello") == ["-p", "hello"]

    def test_tool_args_allowed(self):
        result = self.provider.build_tool_args(allowed_tools=["Bash", "Read"])
        assert result == ["--allowedTools", "Bash,Read"]

    def test_tool_args_disallowed(self):
        result = self.provider.build_tool_args(disallowed_tools=["Edit", "Write"])
        assert result == ["--disallowedTools", "Edit", "Write"]

    def test_tool_args_empty(self):
        assert self.provider.build_tool_args() == []

    def test_tool_args_both(self):
        result = self.provider.build_tool_args(
            allowed_tools=["Bash"], disallowed_tools=["Write"]
        )
        assert "--allowedTools" in result
        assert "--disallowedTools" in result

    def test_model_args_not_added(self):
        """Model is handled by ollama --model flag, not Claude --model."""
        assert self.provider.build_model_args(model="opus") == []
        assert self.provider.build_model_args(fallback="sonnet") == []

    def test_output_args_json(self):
        assert self.provider.build_output_args("json") == ["--output-format", "json"]

    def test_output_args_empty(self):
        assert self.provider.build_output_args() == []

    def test_max_turns_args(self):
        assert self.provider.build_max_turns_args(5) == ["--max-turns", "5"]

    def test_max_turns_args_zero(self):
        assert self.provider.build_max_turns_args(0) == []

    def test_mcp_args(self):
        result = self.provider.build_mcp_args(["config.json"])
        assert result == ["--mcp-config", "config.json"]

    def test_mcp_args_multiple(self):
        result = self.provider.build_mcp_args(["a.json", "b.json"])
        assert result == ["--mcp-config", "a.json", "b.json"]

    def test_mcp_args_empty(self):
        assert self.provider.build_mcp_args() == []
        assert self.provider.build_mcp_args([]) == []

    def test_plugin_args(self):
        result = self.provider.build_plugin_args(["/path/to/plugin"])
        assert result == ["--plugin-dir", "/path/to/plugin"]

    def test_plugin_args_multiple(self):
        result = self.provider.build_plugin_args(["/a", "/b"])
        assert result == ["--plugin-dir", "/a", "--plugin-dir", "/b"]

    def test_plugin_args_empty(self):
        assert self.provider.build_plugin_args() == []
        assert self.provider.build_plugin_args([]) == []


# ---------------------------------------------------------------------------
# Full command building
# ---------------------------------------------------------------------------

class TestOllamaLaunchBuildCommand:
    """Test complete command construction with -- separator."""

    def setup_method(self):
        self.provider = OllamaLaunchProvider()

    def test_minimal_command(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", return_value={}):
                cmd = self.provider.build_command(prompt="hello")
        assert cmd[:3] == ["ollama", "launch", "claude"]
        assert "--" in cmd
        sep_idx = cmd.index("--")
        # After --, we should have Claude flags
        after_sep = cmd[sep_idx + 1:]
        assert "-p" in after_sep
        assert "hello" in after_sep

    def test_command_with_model(self):
        cmd = self.provider.build_command(prompt="test", model="llama3.3")
        assert cmd[:3] == ["ollama", "launch", "claude"]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "llama3.3"
        # --model must be BEFORE the -- separator
        sep_idx = cmd.index("--")
        assert model_idx < sep_idx

    def test_command_with_default_model_from_config(self):
        config = {"ollama_launch": {"model": "qwen2.5-coder:14b"}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", return_value=config):
                cmd = self.provider.build_command(prompt="test")
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "qwen2.5-coder:14b"

    def test_command_model_arg_overrides_config(self):
        """Explicit model parameter overrides config default."""
        config = {"ollama_launch": {"model": "from-config"}}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", return_value=config):
                cmd = self.provider.build_command(prompt="test", model="explicit")
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "explicit"

    def test_command_no_model_when_empty(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", return_value={}):
                cmd = self.provider.build_command(prompt="test")
        # No --model before the separator
        sep_idx = cmd.index("--")
        before_sep = cmd[:sep_idx]
        assert "--model" not in before_sep

    def test_full_command_structure(self):
        """Complete command with all options."""
        cmd = self.provider.build_command(
            prompt="do the thing",
            allowed_tools=["Bash", "Read"],
            disallowed_tools=["Write"],
            model="llama3.3",
            output_format="json",
            max_turns=5,
            mcp_configs=["mcp.json"],
            plugin_dirs=["/plugins"],
        )
        # Verify structure: ollama launch claude --model X -- <claude flags>
        assert cmd[:3] == ["ollama", "launch", "claude"]
        sep_idx = cmd.index("--")

        before_sep = cmd[:sep_idx]
        after_sep = cmd[sep_idx + 1:]

        # Model is in ollama part (before --)
        assert "--model" in before_sep
        assert "llama3.3" in before_sep

        # Claude flags are after --
        assert "-p" in after_sep
        assert "do the thing" in after_sep
        assert "--allowedTools" in after_sep
        assert "--disallowedTools" in after_sep
        assert "--output-format" in after_sep
        assert "--max-turns" in after_sep
        assert "--mcp-config" in after_sep
        assert "--plugin-dir" in after_sep

    def test_fallback_model_ignored(self):
        """Fallback model is not supported — ollama serves one model."""
        cmd = self.provider.build_command(
            prompt="test", model="llama3.3", fallback="phi3"
        )
        assert "--fallback-model" not in cmd

    def test_separator_always_present(self):
        """The -- separator is always in the command, even without model."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOAN_OLLAMA_LAUNCH_MODEL", None)
            with patch("app.utils.load_config", return_value={}):
                cmd = self.provider.build_command(prompt="hello")
        assert "--" in cmd


# ---------------------------------------------------------------------------
# Provider registry integration
# ---------------------------------------------------------------------------

class TestOllamaLaunchRegistry:
    """Test that the provider is properly registered."""

    def test_registered_in_providers(self):
        from app.provider import _PROVIDERS
        assert "ollama-launch" in _PROVIDERS

    def test_provider_class_in_registry(self):
        from app.provider import _PROVIDERS
        assert _PROVIDERS["ollama-launch"] is OllamaLaunchProvider

    def test_get_provider_returns_instance(self):
        from app.provider import get_provider, reset_provider
        reset_provider()
        with patch("app.utils.get_cli_provider_env", return_value="ollama-launch"):
            provider = get_provider()
        assert isinstance(provider, OllamaLaunchProvider)
        reset_provider()

    def test_get_provider_name_from_env(self):
        from app.provider import get_provider_name, reset_provider
        reset_provider()
        with patch("app.utils.get_cli_provider_env", return_value="ollama-launch"):
            assert get_provider_name() == "ollama-launch"
        reset_provider()

    def test_get_provider_name_from_config(self):
        from app.provider import get_provider_name, reset_provider
        reset_provider()
        config = {"cli_provider": "ollama-launch"}
        with patch("app.utils.get_cli_provider_env", return_value=""):
            with patch("app.utils.load_config", return_value=config):
                assert get_provider_name() == "ollama-launch"
        reset_provider()

    def test_facade_reexports(self):
        """cli_provider.py facade exports OllamaLaunchProvider."""
        from app.cli_provider import OllamaLaunchProvider as Facade
        from app.provider import OllamaLaunchProvider as Package
        assert Facade is Package

    def test_import_from_module(self):
        from app.provider.ollama_launch import OllamaLaunchProvider
        assert OllamaLaunchProvider().binary() == "ollama"


# ---------------------------------------------------------------------------
# PID manager integration
# ---------------------------------------------------------------------------

class TestOllamaLaunchPidManager:
    """Verify pid_manager treats ollama-launch correctly."""

    def test_needs_ollama_false_for_ollama_launch(self):
        """ollama-launch manages its own server — no separate ollama serve."""
        from app.pid_manager import _needs_ollama
        assert _needs_ollama("ollama-launch") is False

    def test_needs_ollama_true_for_local(self):
        from app.pid_manager import _needs_ollama
        assert _needs_ollama("local") is True

    def test_needs_ollama_true_for_ollama(self):
        from app.pid_manager import _needs_ollama
        assert _needs_ollama("ollama") is True

    def test_needs_ollama_false_for_claude(self):
        from app.pid_manager import _needs_ollama
        assert _needs_ollama("claude") is False

    def test_status_processes_exclude_ollama(self):
        """ollama-launch should not show ollama in status processes."""
        from app.pid_manager import get_status_processes
        from pathlib import Path
        with patch("app.pid_manager._detect_provider", return_value="ollama-launch"):
            procs = get_status_processes(Path("/fake"))
        assert "ollama" not in procs
        assert "run" in procs
        assert "awake" in procs


# ---------------------------------------------------------------------------
# CLI exec compatibility
# ---------------------------------------------------------------------------

class TestOllamaLaunchCliExec:
    """Verify stdin-based prompt passing works with ollama-launch."""

    def test_uses_stdin_passing(self):
        """ollama-launch supports stdin prompt passing like claude."""
        from app.cli_exec import _uses_stdin_passing
        with patch("app.provider.get_provider_name", return_value="ollama-launch"):
            assert _uses_stdin_passing() is True


# ---------------------------------------------------------------------------
# Extra flags (build_extra_flags)
# ---------------------------------------------------------------------------

class TestOllamaLaunchExtraFlags:
    """Test build_extra_flags for additional flag injection."""

    def setup_method(self):
        self.provider = OllamaLaunchProvider()

    def test_extra_flags_with_disallowed_tools(self):
        result = self.provider.build_extra_flags(disallowed_tools=["Bash"])
        assert "--disallowedTools" in result
        assert "Bash" in result

    def test_extra_flags_model_not_added(self):
        """Model is handled separately in build_command, not extra_flags."""
        result = self.provider.build_extra_flags(model="opus")
        assert "--model" not in result

    def test_extra_flags_empty(self):
        result = self.provider.build_extra_flags()
        assert result == []
