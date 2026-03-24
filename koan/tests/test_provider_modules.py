"""Tests for the CLI provider abstraction layer.

Covers: base.py, claude.py, copilot.py, local.py, ollama_launch.py, __init__.py
These modules had zero test coverage despite being used throughout the codebase.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.provider.base import CLIProvider, CLAUDE_TOOLS, TOOL_NAME_MAP
from app.provider.claude import ClaudeProvider
from app.provider.copilot import CopilotProvider
from app.provider.local import LocalLLMProvider
from app.provider.ollama_launch import OllamaLaunchProvider


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify that tool name constants are sane."""

    def test_claude_tools_contains_expected(self):
        expected = {"Bash", "Read", "Write", "Glob", "Grep", "Edit", "Skill"}
        assert CLAUDE_TOOLS == expected

    def test_tool_name_map_keys_are_claude_tools(self):
        assert set(TOOL_NAME_MAP.keys()) == CLAUDE_TOOLS

    def test_tool_name_map_values_are_strings(self):
        for k, v in TOOL_NAME_MAP.items():
            assert isinstance(v, str)
            assert v  # not empty


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class TestCLIProviderBase:
    """Test CLIProvider base class behavior."""

    def test_build_system_prompt_args_default_empty(self):
        """Base class returns empty — signals no native support."""
        p = CLIProvider()
        assert p.build_system_prompt_args("some prompt") == []

    def test_build_plugin_args_default_empty(self):
        p = CLIProvider()
        assert p.build_plugin_args(["/path/to/plugin"]) == []

    def test_build_permission_args_default_empty(self):
        p = CLIProvider()
        assert p.build_permission_args(skip_permissions=True) == []

    def test_check_quota_default_always_available(self):
        p = CLIProvider()
        available, detail = p.check_quota_available("/some/path")
        assert available is True
        assert detail == ""

    def test_shell_command_defaults_to_binary(self):
        p = CLIProvider()
        p.binary = lambda: "test-bin"
        assert p.shell_command() == "test-bin"

    def test_is_available_uses_shutil_which(self):
        p = CLIProvider()
        p.binary = lambda: "definitely-not-installed-binary-xyz"
        assert p.is_available() is False

    @patch("shutil.which", return_value="/usr/bin/fake")
    def test_is_available_true_when_found(self, mock_which):
        p = CLIProvider()
        p.binary = lambda: "fake"
        assert p.is_available() is True

    def test_abstract_methods_raise(self):
        p = CLIProvider()
        with pytest.raises(NotImplementedError):
            p.binary()
        with pytest.raises(NotImplementedError):
            p.build_prompt_args("hello")
        with pytest.raises(NotImplementedError):
            p.build_tool_args()
        with pytest.raises(NotImplementedError):
            p.build_model_args()
        with pytest.raises(NotImplementedError):
            p.build_output_args()
        with pytest.raises(NotImplementedError):
            p.build_max_turns_args()
        with pytest.raises(NotImplementedError):
            p.build_mcp_args()


class TestBuildCommand:
    """Test CLIProvider.build_command() orchestration."""

    def _make_provider(self):
        """Create a concrete provider for testing build_command."""
        p = ClaudeProvider()
        return p

    def test_basic_command(self):
        p = self._make_provider()
        cmd = p.build_command(prompt="hello world")
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "hello world" in cmd

    def test_with_all_options(self):
        p = self._make_provider()
        cmd = p.build_command(
            prompt="do something",
            allowed_tools=["Bash", "Read"],
            model="opus",
            fallback="sonnet",
            output_format="json",
            max_turns=10,
            skip_permissions=True,
            system_prompt="You are helpful.",
        )
        assert "--dangerously-skip-permissions" in cmd
        assert "--append-system-prompt" in cmd
        assert "You are helpful." in cmd
        assert "--allowedTools" in cmd
        assert "--model" in cmd
        assert "opus" in cmd
        assert "--fallback-model" in cmd
        assert "sonnet" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--max-turns" in cmd
        assert "10" in cmd

    def test_system_prompt_fallback_prepend(self):
        """When provider doesn't support system prompt, it's prepended to user prompt."""
        p = LocalLLMProvider()
        # LocalLLMProvider doesn't override build_system_prompt_args,
        # so it returns [] → base class signals no native support.
        with patch.object(p, "_get_base_url", return_value="http://localhost:11434/v1"):
            with patch.object(p, "_get_api_key", return_value=""):
                with patch.object(p, "_get_default_model", return_value="test-model"):
                    cmd = p.build_command(
                        prompt="do X",
                        system_prompt="Be concise.",
                    )
        # System prompt should be prepended to user prompt
        prompt_idx = cmd.index("-p") + 1
        assert cmd[prompt_idx].startswith("Be concise.")
        assert "do X" in cmd[prompt_idx]

    def test_system_prompt_native_claude(self):
        """Claude uses --append-system-prompt for system prompts."""
        p = ClaudeProvider()
        cmd = p.build_command(prompt="do X", system_prompt="Be helpful.")
        assert "--append-system-prompt" in cmd
        # The user prompt should NOT have system prompt prepended
        prompt_idx = cmd.index("-p") + 1
        assert cmd[prompt_idx] == "do X"

    def test_build_extra_flags(self):
        p = ClaudeProvider()
        flags = p.build_extra_flags(
            model="opus",
            fallback="sonnet",
            disallowed_tools=["Write"],
        )
        assert "--model" in flags
        assert "opus" in flags
        assert "--fallback-model" in flags
        assert "--disallowedTools" in flags
        assert "Write" in flags


# ---------------------------------------------------------------------------
# ClaudeProvider
# ---------------------------------------------------------------------------


class TestClaudeProvider:

    def test_name(self):
        assert ClaudeProvider.name == "claude"

    def test_binary(self):
        assert ClaudeProvider().binary() == "claude"

    def test_permission_args(self):
        p = ClaudeProvider()
        assert p.build_permission_args(False) == []
        assert p.build_permission_args(True) == ["--dangerously-skip-permissions"]

    def test_system_prompt_args(self):
        p = ClaudeProvider()
        assert p.build_system_prompt_args("") == []
        assert p.build_system_prompt_args("prompt") == ["--append-system-prompt", "prompt"]

    def test_prompt_args(self):
        p = ClaudeProvider()
        assert p.build_prompt_args("hello") == ["-p", "hello"]

    def test_tool_args_allowed(self):
        p = ClaudeProvider()
        args = p.build_tool_args(allowed_tools=["Bash", "Read"])
        assert args == ["--allowedTools", "Bash,Read"]

    def test_tool_args_disallowed(self):
        p = ClaudeProvider()
        args = p.build_tool_args(disallowed_tools=["Write", "Edit"])
        assert args == ["--disallowedTools", "Write", "Edit"]

    def test_tool_args_both(self):
        p = ClaudeProvider()
        args = p.build_tool_args(
            allowed_tools=["Bash"],
            disallowed_tools=["Write"],
        )
        assert "--allowedTools" in args
        assert "--disallowedTools" in args

    def test_tool_args_none(self):
        p = ClaudeProvider()
        assert p.build_tool_args() == []

    def test_model_args(self):
        p = ClaudeProvider()
        assert p.build_model_args() == []
        assert p.build_model_args("opus") == ["--model", "opus"]
        assert p.build_model_args("opus", "sonnet") == [
            "--model", "opus", "--fallback-model", "sonnet"
        ]

    def test_model_args_same_fallback_skipped(self):
        """Fallback is skipped when same as primary model."""
        p = ClaudeProvider()
        assert p.build_model_args("opus", "opus") == ["--model", "opus"]

    def test_output_args(self):
        p = ClaudeProvider()
        assert p.build_output_args() == []
        assert p.build_output_args("json") == ["--output-format", "json"]

    def test_max_turns_args(self):
        p = ClaudeProvider()
        assert p.build_max_turns_args() == []
        assert p.build_max_turns_args(0) == []
        assert p.build_max_turns_args(5) == ["--max-turns", "5"]

    def test_mcp_args(self):
        p = ClaudeProvider()
        assert p.build_mcp_args() == []
        assert p.build_mcp_args(["config.json"]) == ["--mcp-config", "config.json"]

    def test_plugin_args(self):
        p = ClaudeProvider()
        assert p.build_plugin_args() == []
        assert p.build_plugin_args(["/a", "/b"]) == [
            "--plugin-dir", "/a", "--plugin-dir", "/b"
        ]

    @patch("subprocess.run")
    def test_check_quota_available(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Session: 25%", stderr="", returncode=0
        )
        p = ClaudeProvider()
        available, detail = p.check_quota_available("/tmp")
        assert available is True
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_check_quota_exhausted(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="Your rate limit will reset", returncode=1
        )
        p = ClaudeProvider()
        with patch("app.quota_handler.detect_quota_exhaustion", return_value=True):
            available, detail = p.check_quota_available("/tmp")
        assert available is False
        assert "rate limit" in detail

    @patch("subprocess.run", side_effect=TimeoutError)
    def test_check_quota_timeout_optimistic(self, mock_run):
        """Timeout should default to optimistic (available)."""
        p = ClaudeProvider()
        with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("cmd", 15)):
            available, _ = p.check_quota_available("/tmp")
        assert available is True


# ---------------------------------------------------------------------------
# CopilotProvider
# ---------------------------------------------------------------------------


class TestCopilotProvider:

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_standalone_mode(self, mock_which):
        p = CopilotProvider()
        assert p.binary() == "copilot"
        assert p.shell_command() == "copilot"
        assert p._is_gh_mode is False

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/gh" if x == "gh" else None)
    def test_gh_mode(self, mock_which):
        p = CopilotProvider()
        assert p.binary() == "gh"
        assert p.shell_command() == "gh copilot"
        assert p._is_gh_mode is True

    @patch("shutil.which", return_value=None)
    def test_not_available(self, mock_which):
        p = CopilotProvider()
        assert p.is_available() is False

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_prompt_args_standalone(self, mock_which):
        p = CopilotProvider()
        assert p.build_prompt_args("hello") == ["-p", "hello"]

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/gh" if x == "gh" else None)
    def test_prompt_args_gh_mode(self, mock_which):
        p = CopilotProvider()
        assert p.build_prompt_args("hello") == ["copilot", "-p", "hello"]

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_tool_args_all_tools_shortcut(self, mock_which):
        """When all CLAUDE_TOOLS are allowed, use --allow-all-tools."""
        p = CopilotProvider()
        args = p.build_tool_args(allowed_tools=list(CLAUDE_TOOLS))
        assert "--allow-all-tools" in args

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_tool_args_specific_tools(self, mock_which):
        p = CopilotProvider()
        args = p.build_tool_args(allowed_tools=["Bash", "Read"])
        assert "--allow-tool" in args
        assert "shell" in args  # Bash → shell
        assert "read_file" in args  # Read → read_file

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_tool_args_disallowed_inversion(self, mock_which):
        """Disallowed tools are converted to allowed = ALL - disallowed."""
        p = CopilotProvider()
        args = p.build_tool_args(disallowed_tools=["Write"])
        # Should allow all tools except Write
        assert "--allow-tool" in args
        copilot_names = [args[i + 1] for i in range(len(args)) if args[i] == "--allow-tool"]
        assert "write_file" not in copilot_names
        assert "shell" in copilot_names  # Bash is allowed

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_tool_args_disallowed_ignored_when_allowed_present(self, mock_which):
        """If allowed_tools is present, disallowed is ignored."""
        p = CopilotProvider()
        args = p.build_tool_args(
            allowed_tools=["Bash"],
            disallowed_tools=["Write"],
        )
        # Only Bash should appear as allowed
        copilot_names = [args[i + 1] for i in range(len(args)) if args[i] == "--allow-tool"]
        assert copilot_names == ["shell"]

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_model_args_no_fallback(self, mock_which):
        """Copilot silently ignores fallback model."""
        p = CopilotProvider()
        args = p.build_model_args("opus", "sonnet")
        assert args == ["--model", "opus"]

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_output_args_not_supported(self, mock_which):
        """Copilot doesn't support output format; returns empty."""
        p = CopilotProvider()
        assert p.build_output_args("json") == []

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_max_turns_not_supported(self, mock_which):
        p = CopilotProvider()
        assert p.build_max_turns_args(10) == []

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_mcp_args(self, mock_which):
        p = CopilotProvider()
        assert p.build_mcp_args(["c.json"]) == ["--mcp-config", "c.json"]

    @patch("subprocess.run")
    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_check_quota_sends_tiny_prompt(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        p = CopilotProvider()
        available, _ = p.check_quota_available("/tmp")
        assert available is True
        # Verify it sent a real prompt (not a usage command)
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "-p" in cmd
        assert "ok" in cmd


# ---------------------------------------------------------------------------
# LocalLLMProvider
# ---------------------------------------------------------------------------


class TestLocalLLMProvider:

    def test_name(self):
        assert LocalLLMProvider.name == "local"

    def test_binary_is_python(self):
        p = LocalLLMProvider()
        assert p.binary() == sys.executable

    def test_shell_command(self):
        p = LocalLLMProvider()
        assert "app.local_llm_runner" in p.shell_command()

    def test_prompt_args(self):
        p = LocalLLMProvider()
        args = p.build_prompt_args("hello")
        assert args == ["-m", "app.local_llm_runner", "-p", "hello"]

    def test_tool_args(self):
        p = LocalLLMProvider()
        args = p.build_tool_args(allowed_tools=["Bash"], disallowed_tools=["Write"])
        assert "--allowed-tools" in args
        assert "Bash" in args[args.index("--allowed-tools") + 1]
        assert "--disallowed-tools" in args

    def test_model_args_explicit(self):
        p = LocalLLMProvider()
        assert p.build_model_args("glm4") == ["--model", "glm4"]

    @patch.dict(os.environ, {"KOAN_LOCAL_LLM_MODEL": "default-model"}, clear=False)
    def test_model_args_default_from_env(self):
        p = LocalLLMProvider()
        args = p.build_model_args()  # No explicit model
        assert args == ["--model", "default-model"]

    def test_model_args_empty_no_config(self):
        """No model configured → empty args."""
        p = LocalLLMProvider()
        with patch.object(p, "_get_config", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                args = p.build_model_args()
        assert args == []

    def test_output_args(self):
        p = LocalLLMProvider()
        assert p.build_output_args("json") == ["--output-format", "json"]

    def test_max_turns_args(self):
        p = LocalLLMProvider()
        assert p.build_max_turns_args(5) == ["--max-turns", "5"]

    def test_mcp_not_supported(self):
        p = LocalLLMProvider()
        assert p.build_mcp_args(["config.json"]) == []

    @patch.dict(os.environ, {"KOAN_LOCAL_LLM_MODEL": "test-model"}, clear=False)
    def test_is_available_with_model(self):
        p = LocalLLMProvider()
        assert p.is_available() is True

    def test_is_available_without_model(self):
        p = LocalLLMProvider()
        with patch.object(p, "_get_config", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                assert p.is_available() is False

    def test_build_command_includes_base_url(self):
        p = LocalLLMProvider()
        with patch.object(p, "_get_base_url", return_value="http://localhost:1234/v1"):
            with patch.object(p, "_get_api_key", return_value=""):
                with patch.object(p, "_get_default_model", return_value="my-model"):
                    cmd = p.build_command(prompt="hello")
        assert "--base-url" in cmd
        assert "http://localhost:1234/v1" in cmd

    def test_build_command_includes_api_key(self):
        p = LocalLLMProvider()
        with patch.object(p, "_get_base_url", return_value="http://localhost:1234/v1"):
            with patch.object(p, "_get_api_key", return_value="sk-test"):
                with patch.object(p, "_get_default_model", return_value="my-model"):
                    cmd = p.build_command(prompt="hello")
        assert "--api-key" in cmd
        assert "sk-test" in cmd

    def test_build_command_no_api_key(self):
        p = LocalLLMProvider()
        with patch.object(p, "_get_base_url", return_value="http://localhost:1234/v1"):
            with patch.object(p, "_get_api_key", return_value=""):
                with patch.object(p, "_get_default_model", return_value="my-model"):
                    cmd = p.build_command(prompt="hello")
        assert "--api-key" not in cmd

    @patch.dict(os.environ, {
        "KOAN_LOCAL_LLM_BASE_URL": "http://env-url:5000/v1",
        "KOAN_LOCAL_LLM_MODEL": "env-model",
        "KOAN_LOCAL_LLM_API_KEY": "env-key",
    }, clear=False)
    def test_env_overrides_config(self):
        """Env vars take priority over config.yaml."""
        p = LocalLLMProvider()
        with patch.object(p, "_get_config", return_value={
            "base_url": "http://config-url/v1",
            "model": "config-model",
            "api_key": "config-key",
        }):
            assert p._get_base_url() == "http://env-url:5000/v1"
            assert p._get_default_model() == "env-model"
            assert p._get_api_key() == "env-key"


# ---------------------------------------------------------------------------
# OllamaLaunchProvider
# ---------------------------------------------------------------------------


class TestOllamaLaunchProvider:

    def test_name(self):
        assert OllamaLaunchProvider.name == "ollama-launch"

    def test_binary(self):
        assert OllamaLaunchProvider().binary() == "ollama"

    def test_shell_command(self):
        assert OllamaLaunchProvider().shell_command() == "ollama launch claude"

    def test_prompt_args(self):
        p = OllamaLaunchProvider()
        assert p.build_prompt_args("hello") == ["-p", "hello"]

    def test_tool_args_uses_claude_style(self):
        """OllamaLaunch passes through to Claude, so uses Claude tool names."""
        p = OllamaLaunchProvider()
        args = p.build_tool_args(allowed_tools=["Bash", "Read"])
        assert args == ["--allowedTools", "Bash,Read"]

    def test_tool_args_disallowed(self):
        p = OllamaLaunchProvider()
        args = p.build_tool_args(disallowed_tools=["Write"])
        assert args == ["--disallowedTools", "Write"]

    def test_model_args_empty(self):
        """Model is handled in build_command, not build_model_args."""
        p = OllamaLaunchProvider()
        assert p.build_model_args("opus") == []

    def test_output_args(self):
        p = OllamaLaunchProvider()
        assert p.build_output_args("json") == ["--output-format", "json"]

    def test_max_turns_args(self):
        p = OllamaLaunchProvider()
        assert p.build_max_turns_args(10) == ["--max-turns", "10"]

    def test_mcp_args(self):
        p = OllamaLaunchProvider()
        assert p.build_mcp_args(["c.json"]) == ["--mcp-config", "c.json"]

    def test_plugin_args(self):
        p = OllamaLaunchProvider()
        assert p.build_plugin_args(["/a"]) == ["--plugin-dir", "/a"]

    def test_build_command_structure(self):
        """Command should be: ollama launch claude --model X -- <claude-flags>."""
        p = OllamaLaunchProvider()
        with patch.object(p, "_get_default_model", return_value="qwen2.5-coder:14b"):
            cmd = p.build_command(
                prompt="do something",
                max_turns=5,
            )
        # Ollama part
        assert cmd[0] == "ollama"
        assert cmd[1] == "launch"
        assert cmd[2] == "claude"
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "qwen2.5-coder:14b"
        # Separator
        assert "--" in cmd
        sep_idx = cmd.index("--")
        # Claude flags come after separator
        after_sep = cmd[sep_idx + 1:]
        assert "-p" in after_sep
        assert "do something" in after_sep
        assert "--max-turns" in after_sep
        assert "5" in after_sep

    def test_build_command_no_model(self):
        """If no model configured, omit --model from ollama part."""
        p = OllamaLaunchProvider()
        with patch.object(p, "_get_default_model", return_value=""):
            cmd = p.build_command(prompt="hi")
        # Should still have the separator
        assert "--" in cmd
        # No --model before separator
        sep_idx = cmd.index("--")
        before_sep = cmd[:sep_idx]
        assert "--model" not in before_sep

    def test_build_command_explicit_model_overrides_default(self):
        p = OllamaLaunchProvider()
        with patch.object(p, "_get_default_model", return_value="default-model"):
            cmd = p.build_command(prompt="hi", model="override-model")
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "override-model"

    def test_check_quota_always_available(self):
        p = OllamaLaunchProvider()
        available, detail = p.check_quota_available("/tmp")
        assert available is True
        assert detail == ""

    def test_get_env_empty(self):
        p = OllamaLaunchProvider()
        assert p.get_env() == {}

    @patch("shutil.which", return_value="/usr/bin/ollama")
    def test_is_available(self, mock_which):
        p = OllamaLaunchProvider()
        assert p.is_available() is True

    @patch("shutil.which", return_value=None)
    def test_not_available(self, mock_which):
        p = OllamaLaunchProvider()
        assert p.is_available() is False


# ---------------------------------------------------------------------------
# Provider registry (__init__.py)
# ---------------------------------------------------------------------------


class TestProviderRegistry:

    def setup_method(self):
        """Reset cached provider before each test."""
        from app.provider import reset_provider
        reset_provider()

    def teardown_method(self):
        from app.provider import reset_provider
        reset_provider()

    def test_all_providers_registered(self):
        from app.provider import _PROVIDERS
        assert "claude" in _PROVIDERS
        assert "copilot" in _PROVIDERS
        assert "local" in _PROVIDERS
        assert "ollama-launch" in _PROVIDERS

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_get_provider_claude(self, mock_name):
        from app.provider import get_provider
        p = get_provider()
        assert isinstance(p, ClaudeProvider)

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_get_provider_caches(self, mock_name):
        from app.provider import get_provider
        p1 = get_provider()
        p2 = get_provider()
        assert p1 is p2

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_get_provider_invalidates_on_name_change(self, mock_name):
        from app.provider import get_provider
        p1 = get_provider()
        mock_name.return_value = "copilot"
        # Need to manually patch CopilotProvider to avoid shutil.which call
        with patch("shutil.which", return_value=None):
            p2 = get_provider()
        assert p1 is not p2

    def test_get_provider_name_default(self):
        from app.provider import get_provider_name
        with patch("app.utils.get_cli_provider_env", return_value=""):
            with patch("app.utils.load_config", return_value={}):
                name = get_provider_name()
        assert name == "claude"

    def test_get_provider_name_from_env(self):
        from app.provider import get_provider_name
        with patch("app.utils.get_cli_provider_env", return_value="copilot"):
            name = get_provider_name()
        assert name == "copilot"

    def test_get_provider_name_from_config(self):
        from app.provider import get_provider_name
        with patch("app.utils.get_cli_provider_env", return_value=""):
            with patch("app.utils.load_config", return_value={"cli_provider": "local"}):
                name = get_provider_name()
        assert name == "local"

    def test_get_provider_name_invalid_env_falls_through(self):
        from app.provider import get_provider_name
        with patch("app.utils.get_cli_provider_env", return_value="nonexistent"):
            with patch("app.utils.load_config", return_value={}):
                name = get_provider_name()
        assert name == "claude"

    def test_get_provider_name_invalid_config_falls_through(self):
        from app.provider import get_provider_name
        with patch("app.utils.get_cli_provider_env", return_value=""):
            with patch("app.utils.load_config", return_value={"cli_provider": "bogus"}):
                name = get_provider_name()
        assert name == "claude"


class TestConvenienceFunctions:

    def setup_method(self):
        from app.provider import reset_provider
        reset_provider()

    def teardown_method(self):
        from app.provider import reset_provider
        reset_provider()

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_get_cli_binary(self, _):
        from app.provider import get_cli_binary
        assert get_cli_binary() == "claude"

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_build_cli_flags(self, _):
        from app.provider import build_cli_flags
        flags = build_cli_flags(model="opus", disallowed_tools=["Write"])
        assert "--model" in flags
        assert "--disallowedTools" in flags

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_build_tool_flags(self, _):
        from app.provider import build_tool_flags
        flags = build_tool_flags(allowed_tools=["Bash"])
        assert "--allowedTools" in flags

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_build_prompt_flags(self, _):
        from app.provider import build_prompt_flags
        flags = build_prompt_flags("hello")
        assert flags == ["-p", "hello"]

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_build_output_flags(self, _):
        from app.provider import build_output_flags
        assert build_output_flags("json") == ["--output-format", "json"]

    @patch("app.provider.get_provider_name", return_value="claude")
    def test_build_max_turns_flags(self, _):
        from app.provider import build_max_turns_flags
        assert build_max_turns_flags(10) == ["--max-turns", "10"]
