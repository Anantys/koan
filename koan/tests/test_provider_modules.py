"""Tests for the CLI provider abstraction layer.

Covers: base.py, claude.py, copilot.py, ollama_launch.py, __init__.py
These modules had zero test coverage despite being used throughout the codebase.
"""

import contextlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from app.provider.base import CLIProvider, CLAUDE_TOOLS, TOOL_NAME_MAP
from app.provider.claude import ClaudeProvider
from app.provider.copilot import CopilotProvider
from app.provider.ollama_launch import OllamaLaunchProvider


@pytest.fixture(autouse=True)
def _non_root_euid():
    """ClaudeProvider drops --dangerously-skip-permissions under root; pin a
    non-root euid so assertions don't depend on who runs the suite."""
    with patch("app.provider.claude.os.geteuid", return_value=1000):
        yield


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
        for v in TOOL_NAME_MAP.values():
            assert isinstance(v, str)
            assert v  # not empty

    def test_side_effect_tools_cover_every_write_capable_tool(self):
        """Read-only roles deny these by name, so the set must stay complete.

        Anything that can mutate state or shell out belongs here; otherwise a
        read-only role silently keeps a write path.
        """
        from app.provider.base import SIDE_EFFECT_TOOLS

        assert set(SIDE_EFFECT_TOOLS) >= {"Bash", "Write", "Edit"}
        # The read tools must NOT be denied — reviews still need them.
        assert not ({"Read", "Glob", "Grep"} & set(SIDE_EFFECT_TOOLS))

    def test_read_only_roles_is_review_only(self):
        """Only review_mode is read-only.

        Deliberately NOT "chat": scripts/wiki_sync_ci.py drives a *write* job
        (Edit/Write plus a git-log Bash rule) on the chat role, so adding it
        here would break that CI fixer.
        """
        from app.provider import READ_ONLY_ROLES

        assert READ_ONLY_ROLES == frozenset({"review_mode"})
        assert "chat" not in READ_ONLY_ROLES
        assert "mission" not in READ_ONLY_ROLES


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

    def test_stdin_prompt_passing_default_enabled(self):
        p = CLIProvider()
        assert p.supports_stdin_prompt_passing() is True

    def test_default_rewrite_prompt_for_stdin(self):
        p = CLIProvider()
        rewritten, prompt = p.rewrite_prompt_for_stdin(
            ["provider", "-p", "hello", "--model", "m"],
            "@stdin",
        )
        assert rewritten == ["provider", "-p", "@stdin", "--model", "m"]
        assert prompt == "hello"

    def test_default_invocation_lock_empty(self):
        p = CLIProvider()
        assert p.invocation_lock_name() == ""

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

    def test_basic_command(self, monkeypatch):
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
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
        # CopilotProvider doesn't override build_system_prompt_args, so it
        # returns [] → base class signals no native support and prepends.
        with patch("shutil.which", return_value="/usr/bin/gh"):
            p = CopilotProvider()
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

    def test_binary(self, monkeypatch):
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        assert ClaudeProvider().binary() == "claude"

    def test_permission_args(self):
        p = ClaudeProvider()
        assert p.build_permission_args(False) == []
        assert p.build_permission_args(True) == ["--dangerously-skip-permissions"]

    def test_read_only_suppresses_skip_permissions(self):
        """A read-only role never gets --dangerously-skip-permissions.

        That flag bypasses ALL permission checks, which would defeat the
        --disallowedTools denial that makes the role read-only in the first
        place. read_only therefore wins over skip_permissions.
        """
        p = ClaudeProvider()
        assert p.build_permission_args(False, read_only=True) == []
        assert p.build_permission_args(True, read_only=True) == []

    def test_supports_tool_denial(self):
        assert ClaudeProvider().supports_tool_denial() is True

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
        assert args == ["--disallowedTools", "Write,Edit"]

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

    def test_check_quota_always_available(self):
        """check_quota_available is a no-op — always returns (True, '')."""
        p = ClaudeProvider()
        available, detail = p.check_quota_available("/tmp")
        assert available is True
        assert detail == ""


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

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/copilot" if x == "copilot" else None)
    def test_stdin_prompt_passing_disabled(self, mock_which):
        p = CopilotProvider()
        assert p.supports_stdin_prompt_passing() is False

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
# Registry — local provider removed
# ---------------------------------------------------------------------------


class TestLocalProviderRemoved:

    def test_local_provider_removed_from_registry(self):
        from app.provider import _PROVIDERS
        assert "local" not in _PROVIDERS
        assert "ollama-launch" in _PROVIDERS  # preserved

    def test_get_provider_by_name_local_raises(self):
        from app.provider import get_provider_by_name
        with pytest.raises(KeyError):
            get_provider_by_name("local")


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

    def test_build_command_with_permissions(self):
        p = OllamaLaunchProvider()
        cmd = p.build_command(prompt="hi", skip_permissions=True)
        sep_idx = cmd.index("--")
        after_sep = cmd[sep_idx + 1:]
        assert "--dangerously-skip-permissions" in after_sep

    def test_build_command_with_system_prompt(self):
        p = OllamaLaunchProvider()
        cmd = p.build_command(prompt="hi", system_prompt="Be helpful.")
        sep_idx = cmd.index("--")
        after_sep = cmd[sep_idx + 1:]
        assert "--append-system-prompt" in after_sep
        assert "Be helpful." in after_sep

    def test_build_command_with_resume(self):
        p = OllamaLaunchProvider()
        cmd = p.build_command(prompt="hi", resume_session_id="sess-123")
        sep_idx = cmd.index("--")
        after_sep = cmd[sep_idx + 1:]
        assert "--resume" in after_sep
        assert "sess-123" in after_sep

    def test_supports_stream_json(self):
        assert OllamaLaunchProvider().supports_stream_json() is True

    def test_supports_system_prompt_file(self):
        assert OllamaLaunchProvider().supports_system_prompt_file() is True

    def test_supports_session_resume(self):
        assert OllamaLaunchProvider().supports_session_resume() is True

    def test_output_args_stream_json(self):
        p = OllamaLaunchProvider()
        args = p.build_output_args("stream-json")
        assert args == ["--output-format", "stream-json", "--verbose"]

    def test_build_effort_args(self):
        p = OllamaLaunchProvider()
        assert p.build_effort_args("high") == ["--effort", "high"]
        assert p.build_effort_args("") == []

    def test_build_thinking_args(self):
        p = OllamaLaunchProvider()
        assert p.build_thinking_args(enabled=True) == ["--effort", "max"]
        assert p.build_thinking_args(enabled=False) == []

    def test_detect_quota_exhaustion(self):
        p = OllamaLaunchProvider()
        assert p.detect_quota_exhaustion(
            stdout_text="", stderr_text="rate limit exceeded", exit_code=1,
        ) is True
        assert p.detect_quota_exhaustion(
            stdout_text="", stderr_text="ok", exit_code=0,
        ) is False

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
        assert "haze" in _PROVIDERS
        assert "grok" in _PROVIDERS
        assert "ollama-launch" in _PROVIDERS

    def test_haze_is_known_provider(self):
        from app.provider import is_known_provider, known_providers
        assert is_known_provider("haze")
        assert "haze" in known_providers()

    def test_grok_is_known_provider(self):
        from app.provider import is_known_provider, known_providers
        assert is_known_provider("grok")
        assert "grok" in known_providers()

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
            with patch("app.utils.load_config", return_value={"cli_provider": "ollama-launch"}):
                name = get_provider_name()
        assert name == "ollama-launch"

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
    def test_get_cli_binary(self, _, monkeypatch):
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
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


class TestBuildFullCommand:
    def test_delegates_to_provider(self):
        from app.provider import build_full_command
        fake_prov = MagicMock()
        fake_prov.build_command.return_value = ["fake-cli", "-p", "hi"]
        with patch("app.provider.get_provider", return_value=fake_prov), \
             patch("app.config.get_skip_permissions", return_value=True):
            cmd = build_full_command(prompt="hi", allowed_tools=["Bash"],
                                     model="m", fallback="fb", max_turns=5)
        assert cmd == ["fake-cli", "-p", "hi"]
        assert fake_prov.build_command.call_args.kwargs["skip_permissions"] is True


class TestGetProviderNameFallback:
    def test_config_load_error_falls_back_to_claude(self, monkeypatch):
        from app.provider import get_provider_name
        monkeypatch.delenv("KOAN_CLI_PROVIDER", raising=False)
        monkeypatch.delenv("CLI_PROVIDER", raising=False)
        with patch("app.utils.load_config", side_effect=RuntimeError("bad")):
            assert get_provider_name() == "claude"


class TestGetCliBinaryName:
    """get_cli_binary_name: KOAN_CLAUDE_CLI_PATH basename, or ''."""

    def test_returns_basename_when_set(self, monkeypatch):
        from app.provider import get_cli_binary_name
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/opt/koan/ollama-claude")
        assert get_cli_binary_name() == "ollama-claude"

    def test_strips_trailing_slash(self, monkeypatch):
        from app.provider import get_cli_binary_name
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/usr/local/bin/my-claude/")
        assert get_cli_binary_name() == "my-claude"

    def test_empty_when_unset(self, monkeypatch):
        from app.provider import get_cli_binary_name
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        assert get_cli_binary_name() == ""


class TestPerInstanceBinaryOverride:
    """CLIProvider(binary_path=...) is honored by binary()/is_available()."""

    def test_each_provider_honors_override(self, monkeypatch):
        from app.provider import (
            ClaudeProvider, CodexProvider, ClineProvider,
            CopilotProvider, OllamaLaunchProvider,
        )
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        for cls in (ClaudeProvider, CodexProvider, ClineProvider,
                    CopilotProvider, OllamaLaunchProvider):
            assert cls(binary_path="/opt/bin/custom").binary() == "/opt/bin/custom"

    def test_override_relative_rooted_at_koan_root(self, monkeypatch):
        from app.provider import CodexProvider
        monkeypatch.setenv("KOAN_ROOT", "/srv/koan")
        assert CodexProvider(binary_path="bin/wrap").binary() == "/srv/koan/bin/wrap"

    def test_default_binary_when_no_override(self, monkeypatch):
        from app.provider import CodexProvider, ClineProvider
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        assert CodexProvider().binary() == "codex"
        assert ClineProvider().binary() == "cline"

    def test_copilot_init_calls_super(self):
        """Regression (PR #2251 #2): CopilotProvider overrides __init__, so it
        must call super().__init__ — otherwise binary() reading _binary_override
        raises AttributeError on EVERY call, including the no-args parity path
        that get_provider() uses."""
        from app.provider import CopilotProvider
        # Default construction (no cli: override) must not raise.
        assert isinstance(CopilotProvider().binary(), str)
        # And the override path resolves the custom binary.
        assert CopilotProvider(binary_path="/opt/bin/cop").binary() == "/opt/bin/cop"

    def test_is_available_uses_overridden_binary(self):
        from app.provider import CodexProvider
        with patch("shutil.which", side_effect=lambda b: b if b == "/opt/bin/custom" else None):
            assert CodexProvider(binary_path="/opt/bin/custom").is_available() is True
            assert CodexProvider().is_available() is False


class TestGetProviderDisplay:
    """get_provider_display: '<name>' or '<name> (<binary>)' like /status."""

    def test_appends_binary_flavor(self, monkeypatch):
        from app.provider import get_provider_display
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/opt/koan/ollama-claude")
        with patch("app.provider.get_provider_name", return_value="claude"):
            assert get_provider_display() == "claude (ollama-claude)"

    def test_no_flavor_when_unset(self, monkeypatch):
        from app.provider import get_provider_display
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        with patch("app.provider.get_provider_name", return_value="claude"):
            assert get_provider_display() == "claude"


class TestProviderCliDisplay:
    """provider_cli_display: custom binary basename, else the provider name.

    The review footer attribution uses this so a pinned review CLI
    (cli.review_mode: claude:/root/.local/bin/claude-deep) surfaces its
    basename (claude-deep) instead of the provider flavor.
    """

    def test_per_role_path_basename(self, monkeypatch):
        from app.provider import ClaudeProvider, provider_cli_display
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        provider = ClaudeProvider(binary_path="/root/.local/bin/claude-deep")
        assert provider_cli_display(provider) == "claude-deep"

    def test_global_env_basename_when_no_path(self, monkeypatch):
        from app.provider import ClaudeProvider, provider_cli_display
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/opt/koan/ollama-claude")
        assert provider_cli_display(ClaudeProvider()) == "ollama-claude"

    def test_per_role_path_wins_over_env(self, monkeypatch):
        from app.provider import ClaudeProvider, provider_cli_display
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/opt/koan/ollama-claude")
        provider = ClaudeProvider(binary_path="/root/.local/bin/claude-deep")
        assert provider_cli_display(provider) == "claude-deep"

    def test_falls_back_to_flavor_when_no_override(self, monkeypatch):
        from app.provider import ClaudeProvider, provider_cli_display
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        assert provider_cli_display(ClaudeProvider()) == "claude"

    def test_non_claude_path_override(self, monkeypatch):
        from app.provider import CodexProvider, provider_cli_display
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        provider = CodexProvider(binary_path="/opt/bin/my-codex")
        assert provider_cli_display(provider) == "my-codex"

    def test_non_claude_default_ignores_env(self, monkeypatch):
        """KOAN_CLAUDE_CLI_PATH is claude-only; codex default stays 'codex'."""
        from app.provider import CodexProvider, provider_cli_display
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/opt/koan/ollama-claude")
        assert provider_cli_display(CodexProvider()) == "codex"

    def test_copilot_default_not_its_gh_fallback(self, monkeypatch):
        """Copilot's binary() falls back to 'gh' with no standalone binary,
        but that's not a user-pinned override — the display must stay the
        flavor name so the footer never reads 'gh'."""
        from app.provider import CopilotProvider, provider_cli_display
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        with patch("shutil.which", return_value=None):
            assert provider_cli_display(CopilotProvider()) == "copilot"

    def test_override_basename_equal_to_flavor_returns_flavor(self, monkeypatch):
        """A path whose basename equals the flavor (e.g. '/usr/bin/claude')
        is not a meaningfully different binary — show the flavor name."""
        from app.provider import ClaudeProvider, provider_cli_display
        monkeypatch.delenv("KOAN_CLAUDE_CLI_PATH", raising=False)
        assert provider_cli_display(ClaudeProvider(binary_path="/usr/bin/claude")) == "claude"

    def test_no_flavor_when_identical(self, monkeypatch):
        from app.provider import get_provider_display
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/usr/bin/claude")
        with patch("app.provider.get_provider_name", return_value="claude"):
            assert get_provider_display() == "claude"

    def test_honors_explicit_name(self, monkeypatch):
        from app.provider import get_provider_display
        monkeypatch.setenv("KOAN_CLAUDE_CLI_PATH", "/x/my-claude")
        # An explicit name (e.g. the "ollama" sentinel) is not overridden.
        assert get_provider_display("ollama") == "ollama (my-claude)"


class TestRunCommand:
    def test_success(self):
        from app.provider import run_command
        result = MagicMock(returncode=0, stdout="hello\n", stderr="")
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.run_cli_with_retry", return_value=result), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            assert run_command("hi", "/tmp", []) == "hello"

    def test_failure_raises(self):
        from app.provider import run_command
        result = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.run_cli_with_retry", return_value=result):
            with pytest.raises(RuntimeError, match="CLI invocation failed") as exc:
                run_command("hi", "/tmp", [])
            msg = str(exc.value)
            assert "exit=1" in msg
            assert "stderr=boom" in msg

    def test_failure_includes_stdout_when_stderr_empty(self):
        from app.provider import run_command
        result = MagicMock(
            returncode=2, stdout="auth token expired\nplease re-login", stderr=""
        )
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.run_cli_with_retry", return_value=result):
            with pytest.raises(RuntimeError, match="CLI invocation failed") as exc:
                run_command("hi", "/tmp", [])
            msg = str(exc.value)
            assert "exit=2" in msg
            assert "stdout=auth token expired" in msg
            assert "stderr=" not in msg

    def test_max_turns_returns_partial_output(self, capsys):
        """When CLI exits non-zero due to max turns, return partial output."""
        from app.provider import run_command
        result = MagicMock(
            returncode=1,
            stdout="partial result here\nError: Reached max turns (10)",
            stderr="",
        )
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.run_cli_with_retry", return_value=result), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command("hi", "/tmp", [])
        assert "partial result here" in out
        assert "max turns limit" in capsys.readouterr().err


class TestRunCommandStreaming:
    def _make_proc(self, stdout_lines, stderr="", returncode=0):
        proc = MagicMock()
        stdout = MagicMock()
        stdout.__iter__ = lambda self: iter(stdout_lines)
        stdout.close = MagicMock()
        proc.stdout = stdout
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = stderr
        proc.returncode = returncode
        proc.wait.return_value = None
        return proc

    def test_happy_path(self, capsys):
        from app.provider import run_command_streaming
        proc = self._make_proc(["line1\n", "line2\n"])
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])
        assert "line1" in out and "line2" in out
        cleanup.assert_called_once()

    def test_popen_uses_errors_replace(self):
        """popen_cli must be called with errors='replace' to survive non-UTF-8."""
        from app.provider import run_command_streaming
        proc = self._make_proc(["ok\n"])
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)) as mock_popen, \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command_streaming("hi", "/tmp", [])
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("errors") == "replace"

    def test_missing_executable_raises_actionable_error(self):
        """A missing provider binary must not crash a skill runner."""
        from app.provider import run_command_streaming

        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="codex"), \
             patch("app.provider.build_full_command", return_value=["codex", "exec", "prompt"]), \
             patch(
                 "app.cli_exec.popen_cli",
                 side_effect=FileNotFoundError(2, "No such file or directory", "codex"),
             ):
            with pytest.raises(RuntimeError, match="CLI executable not found: 'codex'") as exc:
                run_command_streaming("hi", "/tmp", [])

        assert "provider 'codex'" in str(exc.value)
        assert "cli.chat: codex:/absolute/path" in str(exc.value)

    def test_failure_raises(self):
        from app.provider import run_command_streaming
        proc = self._make_proc(["oops\n"], stderr="err", returncode=1)
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            with pytest.raises(RuntimeError, match="err"):
                run_command_streaming("hi", "/tmp", [])

    def test_failure_includes_stdout_when_stderr_empty(self):
        """When stderr is empty, stdout is included in the error."""
        from app.provider import run_command_streaming
        proc = self._make_proc(
            ["Error: context window exceeded\n"], stderr="", returncode=1,
        )
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)):
            with pytest.raises(RuntimeError, match="context window exceeded"):
                run_command_streaming("hi", "/tmp", [])

    def test_max_turns_returns_partial_output(self, capsys):
        """When CLI exits non-zero due to max turns, return partial output."""
        from app.provider import run_command_streaming
        proc = self._make_proc(
            ["partial report\n", "Error: Reached max turns (50)\n"],
            returncode=1,
        )
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [], max_turns=50)
        assert "partial report" in out
        assert "max turns limit" in capsys.readouterr().err

    def test_timeout_raises(self):
        import subprocess as sp
        from app.provider import run_command_streaming
        proc = self._make_proc([])
        proc.wait.side_effect = [sp.TimeoutExpired("fake", 1), None]
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)):
            with pytest.raises(RuntimeError, match="timed out"):
                run_command_streaming("hi", "/tmp", [], timeout=1)
        proc.kill.assert_called_once()

    def test_max_turns_warning_exit_zero(self, capsys):
        """When CLI exits 0 but output mentions max turns, still warn."""
        from app.provider import run_command_streaming
        proc = self._make_proc(["Reached max turns limit\n"])
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command_streaming("hi", "/tmp", [], max_turns=2)
        assert "max turns limit" in capsys.readouterr().err

    def test_stream_json_requests_event_format_for_claude(self):
        """When the provider is Claude, the helper asks for stream-json output.

        This is what keeps the parent watchdog alive on long high-effort
        sessions: the CLI emits a JSON line per turn/tool-use instead of
        buffering until the end.
        """
        import json
        from app.provider import run_command_streaming
        result_event = json.dumps({
            "type": "result", "subtype": "success",
            "result": "the answer",
        })
        proc = self._make_proc([result_event + "\n"])
        cleanup = MagicMock()
        captured_kwargs = {}

        def fake_build(**kwargs):
            captured_kwargs.update(kwargs)
            return ["fake"]

        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="claude"), \
             patch("app.provider.build_full_command", side_effect=fake_build), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command_streaming("hi", "/tmp", [])
        assert captured_kwargs.get("output_format") == "stream-json"

    def test_stream_json_returns_result_event_text(self, capsys):
        """The ``result`` event's text becomes the function's return value.

        Verifies the contract that callers depend on: even with stream-json
        on, what comes back is the same final assistant text a plain
        text-mode run would have produced.
        """
        import json
        from app.provider import run_command_streaming
        events = [
            json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}) + "\n",
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "id": "tu_1"},
            ]}}) + "\n",
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            ]}}) + "\n",
            json.dumps({"type": "result", "subtype": "success",
                        "result": "## Summary\nDone.", "duration_ms": 12000}) + "\n",
        ]
        proc = self._make_proc(events)
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="claude"), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])
        assert out == "## Summary\nDone."
        # The raw JSON should NOT appear in stdout — only human-readable summaries.
        captured = capsys.readouterr().out
        assert '"type": "result"' not in captured
        # But the summaries should be there so the watchdog sees activity.
        assert "tool_use: Edit" in captured
        assert "tool_result" in captured
        assert "result: success" in captured

    def test_stream_json_each_event_emits_a_line_for_watchdog(self, capsys):
        """Per-event summary lines unblock the run.py liveness watchdog.

        This is the whole point of the change: a long-running Claude call
        emits one stdout line per tool use, so the runner's parent (which
        resets its 600s watchdog on every line) sees real activity even
        when no final text has been produced yet.
        """
        import json
        from app.provider import run_command_streaming
        # Five tool_use events with no result yet — simulates the middle
        # of a long high-effort run, the exact case that previously
        # produced ZERO stdout for 10 minutes and got killed.
        events = []
        for i in range(5):
            events.append(
                json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "id": f"tu_{i}"},
                ]}}) + "\n"
            )
        events.append(
            json.dumps({"type": "result", "subtype": "success",
                        "result": "fin"}) + "\n"
        )
        proc = self._make_proc(events)
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="claude"), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command_streaming("hi", "/tmp", [])
        out_lines = capsys.readouterr().out.splitlines()
        tool_lines = [ln for ln in out_lines if "tool_use: Bash" in ln]
        assert len(tool_lines) == 5

    def test_stream_json_max_turns_via_result_event(self, capsys):
        """A ``result`` event with a max-turns subtype is treated like the
        legacy ``Error: Reached max turns`` regex hit — warn and return
        partial output instead of raising."""
        import json
        from app.provider import run_command_streaming
        events = [
            json.dumps({"type": "result", "subtype": "error_max_turns",
                        "result": "partial answer"}) + "\n",
        ]
        proc = self._make_proc(events, returncode=1)
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="claude"), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [], max_turns=3)
        assert "partial answer" in out
        assert "max turns limit" in capsys.readouterr().err

    def test_stream_json_empty_result_falls_back_to_text_blocks(self, capsys):
        """An empty-string ``result`` field must not pin the return value to ``""``.

        The Claude CLI normally populates ``result`` for successful runs,
        but if it ever emits an explicitly-empty string we should treat it
        as "no final text" and surface whatever assistant text blocks the
        stream produced, rather than discarding them.
        """
        import json
        from app.provider import run_command_streaming
        events = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "first chunk"},
            ]}}) + "\n",
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "second chunk"},
            ]}}) + "\n",
            json.dumps({"type": "result", "subtype": "success",
                        "result": ""}) + "\n",
        ]
        proc = self._make_proc(events)
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="claude"), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])
        assert "first chunk" in out
        assert "second chunk" in out

    def test_non_streaming_provider_uses_raw_text(self, capsys):
        """Providers without JSONL progress support use raw stdout."""
        from app.provider import run_command_streaming
        proc = self._make_proc(["plain text result\n"])
        cleanup = MagicMock()
        captured_kwargs = {}

        def fake_build(**kwargs):
            captured_kwargs.update(kwargs)
            return ["fake"]

        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="copilot"), \
             patch("app.provider.build_full_command", side_effect=fake_build), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])
        # No output_format request for providers that cannot stream JSONL.
        assert captured_kwargs.get("output_format") == ""
        assert "plain text result" in out

    def test_codex_provider_requests_jsonl_progress(self):
        """Codex uses --json events so parent liveness sees progress."""
        import json
        from app.provider import run_command_streaming
        events = [
            json.dumps({
                "type": "item_completed",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "codex answer"}],
                },
            }) + "\n",
        ]
        proc = self._make_proc(events)
        cleanup = MagicMock()
        captured_kwargs = {}

        def fake_build(**kwargs):
            captured_kwargs.update(kwargs)
            return ["fake"]

        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="codex"), \
             patch("app.provider.build_full_command", side_effect=fake_build), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])
        assert captured_kwargs.get("output_format") == "stream-json"
        assert out == "codex answer"

    def test_stream_usage_sidecar_persists_usage_snapshot(self, tmp_path):
        """When configured, stream usage is persisted for skill post-processing."""
        import json
        from app.provider import run_command_streaming

        usage_file = tmp_path / "stream-usage.json"
        events = [
            json.dumps({
                "type": "turn.completed",
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input_tokens": 1200,
                    "cached_input_tokens": 900,
                    "output_tokens": 80,
                },
            }) + "\n",
            json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "ok",
            }) + "\n",
        ]
        proc = self._make_proc(events)
        cleanup = MagicMock()

        with patch.dict(os.environ, {"KOAN_STREAM_USAGE_FILE": str(usage_file)}), \
             patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="claude"), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])

        assert out == "ok"
        payload = json.loads(usage_file.read_text())
        assert payload["input_tokens"] == 300
        assert payload["cache_read_input_tokens"] == 900
        assert payload["output_tokens"] == 80

    def test_stream_usage_sidecar_accumulates_across_calls(self, tmp_path):
        """Multiple provider calls in one subprocess sum into the sidecar.

        A skill that makes a main provider call and then a backend review/fix
        gate call must have BOTH counted — overwriting would drop all but the
        last call from the mission's post-mission accounting.
        """
        import json
        from app.provider import run_command_streaming

        usage_file = tmp_path / "stream-usage.json"

        def _events(inp, out):
            return [
                json.dumps({
                    "type": "turn.completed",
                    "model": "claude-sonnet-4-20250514",
                    "usage": {"input_tokens": inp, "output_tokens": out},
                }) + "\n",
                json.dumps({
                    "type": "result", "subtype": "success", "result": "ok",
                }) + "\n",
            ]

        with patch.dict(os.environ, {"KOAN_STREAM_USAGE_FILE": str(usage_file)}), \
             patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="claude"), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            with patch(
                "app.cli_exec.popen_cli",
                return_value=(self._make_proc(_events(1000, 200)), MagicMock()),
            ):
                run_command_streaming("main work", "/tmp", [])
            with patch(
                "app.cli_exec.popen_cli",
                return_value=(self._make_proc(_events(300, 40)), MagicMock()),
            ):
                run_command_streaming("gate review", "/tmp", [])

        payload = json.loads(usage_file.read_text())
        assert payload["input_tokens"] == 1300
        assert payload["output_tokens"] == 240

    def test_codex_turn_complete_returns_last_agent_message(self):
        import json
        from app.provider import run_command_streaming
        events = [
            json.dumps({"type": "turn_started"}) + "\n",
            json.dumps({"type": "agent_message_content_delta", "delta": "partial"}) + "\n",
            json.dumps({
                "type": "turn_complete",
                "last_agent_message": "final codex answer",
            }) + "\n",
        ]
        proc = self._make_proc(events)
        cleanup = MagicMock()

        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="codex"), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])
        assert out == "final codex answer"

    def test_codex_last_message_file_wins_over_event_fallback(self):
        """Codex writes the final answer to --output-last-message; prefer it.

        The live failure mode was a stream full of progress events with no
        extractable result body. The file is Codex's stable final-output API.
        """
        import json
        from app.provider import run_command_streaming

        events = [
            json.dumps({"type": "agent_message"}) + "\n",
            json.dumps({"type": "turn.completed"}) + "\n",
        ]
        proc = self._make_proc(events)
        cleanup = MagicMock()
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            path = cmd[cmd.index("--output-last-message") + 1]
            captured["path"] = path
            with open(path, "w") as f:
                f.write("final answer from file\n")
            return proc, cleanup

        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="codex"), \
             patch("app.provider.build_full_command", return_value=["codex", "exec", "--json", "prompt"]), \
             patch("app.cli_exec.popen_cli", side_effect=fake_popen), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])

        assert out == "final answer from file"
        assert captured["cmd"][-1] == "prompt"
        assert "--output-last-message" in captured["cmd"]
        assert not os.path.exists(captured["path"])

    def test_codex_empty_last_message_file_falls_back_to_events(self):
        import json
        from app.provider import run_command_streaming

        events = [
            json.dumps({
                "type": "turn_complete",
                "last_agent_message": "event fallback answer",
            }) + "\n",
        ]
        proc = self._make_proc(events)
        cleanup = MagicMock()

        def fake_popen(cmd, **kwargs):
            path = cmd[cmd.index("--output-last-message") + 1]
            with open(path, "w") as f:
                f.write("")
            return proc, cleanup

        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.get_provider_name", return_value="codex"), \
             patch("app.provider.build_full_command", return_value=["codex", "exec", "--json", "prompt"]), \
             patch("app.cli_exec.popen_cli", side_effect=fake_popen), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            out = run_command_streaming("hi", "/tmp", [])

        assert out == "event fallback answer"


class TestToolInputPreview:
    """Coverage for the tool-input preview carried in tool_use summaries."""

    def test_bash_command_preview(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "make lint && make test"}}
            ]},
        })
        assert line == "[cli] assistant — tool_use: Bash: make lint && make test"

    def test_read_uses_file_path(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/app/koan/app/run.py"}}
            ]},
        })
        assert line.endswith("tool_use: Read: /app/koan/app/run.py")

    def test_long_command_is_truncated_with_ellipsis(self):
        from app.provider import _tool_input_preview
        long = "echo " + "x" * 200
        out = _tool_input_preview({"command": long}, limit=60)
        assert out.endswith("…")
        assert len(out) <= 61  # 60 chars + ellipsis

    def test_no_input_yields_bare_name(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Grep"}]},
        })
        assert line == "[cli] assistant — tool_use: Grep"

    def test_multiline_command_shows_first_line_with_ellipsis(self):
        from app.provider import _tool_input_preview
        out = _tool_input_preview({"command": "cd /app\nmake test"}, limit=60)
        assert out == "cd /app…"

    def test_comma_preview_does_not_embed_part_delimiter(self):
        # A preview containing ", text: " must NOT survive into the summary
        # verbatim: the ", " part delimiter is neutralized to a bare comma so
        # the display formatter (log_fmt._PART_SEP) can't mis-split it into a
        # spurious `text:` part.
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": 'git commit -m "fix, text: hi"'}}
            ]},
        })
        assert ", text: " not in line
        assert line == (
            '[cli] assistant — tool_use: Bash: git commit -m "fix,text: hi"'
        )


class TestSummarizeStreamEvent:
    """Direct coverage for _summarize_stream_event so changes to the
    rendering don't accidentally silence the watchdog signal."""

    def test_tool_use_renders_name(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Grep"}]},
        })
        assert "tool_use: Grep" in line

    def test_text_block_renders_preview(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "Looking at the failing test"},
            ]},
        })
        assert "Looking at the failing test" in line

    def test_unknown_type_falls_back_to_event_tag(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({"type": "weird"})
        assert "weird" in line

    def test_result_event_renders_duration(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "result", "subtype": "success", "duration_ms": 45000,
        })
        assert "45s" in line

    def test_informational_rate_limit_event_has_no_quota_trigger(self):
        """An 'allowed' rate_limit_event must summarize without tripping ANY
        quota detector.

        Regression: the summary line must not read as quota exhaustion under
        either the strict matcher (stdout) OR the combined detector (used for
        stderr / general text). The earlier '[cli] rate limit ok' wording
        contained the loose 'rate limit' substring, so a summary that leaked
        into a stderr-trusted buffer paused Koan on a successful run.
        """
        from app.provider import _summarize_stream_event
        from app.quota_handler import _strict_quota_match, detect_quota_exhaustion
        line = _summarize_stream_event({
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
        })
        assert _strict_quota_match(line) is False
        assert detect_quota_exhaustion(line) is False

    def test_rejected_rate_limit_event_is_detected_from_summary(self):
        """A 'rejected' rate_limit_event must summarize to a line the quota
        detector recognizes, since the streaming path only sees summaries."""
        from app.provider import _summarize_stream_event
        from app.quota_handler import _strict_quota_match
        line = _summarize_stream_event({
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "rateLimitType": "five_hour",
                "resetsAt": 1779937200,
            },
        })
        assert "rate_limit_rejected" in line
        assert _strict_quota_match(line) is True


class TestClaudeProviderStreamJsonRequiresVerbose:
    def test_stream_json_adds_verbose(self):
        from app.provider.claude import ClaudeProvider
        flags = ClaudeProvider().build_output_args("stream-json")
        assert "--output-format" in flags
        assert "stream-json" in flags
        # --verbose is mandatory for stream-json print mode.
        assert "--verbose" in flags

    def test_plain_format_unchanged(self):
        from app.provider.claude import ClaudeProvider
        assert ClaudeProvider().build_output_args("json") == [
            "--output-format", "json",
        ]
        assert ClaudeProvider().build_output_args("") == []


class TestMaxTurnsWarningAttribution:
    """The warning message must match how max_turns was actually sourced.

    Regression: chat-style callers (ask, github_reply, spec_generator) pass
    hardcoded max_turns=5 to run_command(), but the warning always told users
    to bump ``skill_max_turns`` in config — which is set to 200 and has no
    effect on these callers.
    """

    def _make_proc(self, stdout_lines, stderr="", returncode=0):
        proc = MagicMock()
        stdout = MagicMock()
        stdout.__iter__ = lambda self: iter(stdout_lines)
        stdout.close = MagicMock()
        proc.stdout = stdout
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = stderr
        proc.returncode = returncode
        proc.wait.return_value = None
        return proc

    def test_run_command_with_hardcoded_source_omits_config_key(self, capsys):
        """When max_turns_source=None, warning does not tell user to edit config."""
        from app.provider import run_command
        result = MagicMock(
            returncode=1,
            stdout="partial result\nError: Reached max turns (5)",
            stderr="",
        )
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.run_cli_with_retry", return_value=result), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command("hi", "/tmp", [], max_turns=5, max_turns_source=None)
        err = capsys.readouterr().err
        assert "max turns limit (5)" in err
        assert "skill_max_turns" not in err
        assert "instance/config.yaml" not in err

    def test_run_command_with_named_source_points_to_correct_key(self, capsys):
        """When max_turns_source='skill_max_turns', warning mentions that exact key."""
        from app.provider import run_command
        result = MagicMock(
            returncode=1,
            stdout="partial result\nError: Reached max turns (200)",
            stderr="",
        )
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.run_cli_with_retry", return_value=result), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command(
                "hi", "/tmp", [], max_turns=200,
                max_turns_source="skill_max_turns",
            )
        err = capsys.readouterr().err
        assert "skill_max_turns" in err

    def test_streaming_with_hardcoded_source_omits_config_key(self, capsys):
        """run_command_streaming honors max_turns_source=None the same way."""
        from app.provider import run_command_streaming
        proc = self._make_proc(
            ["partial report\n", "Error: Reached max turns (5)\n"],
            returncode=1,
        )
        cleanup = MagicMock()
        with patch("app.config.get_model_config", return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command_streaming(
                "hi", "/tmp", [], max_turns=5, max_turns_source=None,
            )
        err = capsys.readouterr().err
        assert "max turns limit (5)" in err
        assert "skill_max_turns" not in err


class TestCodexProvider:
    def test_all_build_methods(self):
        from app.provider.codex import CodexProvider
        p = CodexProvider()
        assert p.binary() == "codex"
        with patch("app.provider.codex.shutil.which", return_value="/usr/bin/codex"):
            assert p.is_available() is True
        with patch("app.provider.codex.shutil.which", return_value=None):
            assert p.is_available() is False
        assert p.build_permission_args(True) == [
            "--dangerously-bypass-approvals-and-sandbox"
        ]
        assert p.build_permission_args(False) == ["--sandbox", "workspace-write"]
        assert p.build_prompt_args("hi") == ["exec", "hi"]
        assert p.build_tool_args(allowed_tools=["Bash"]) == []
        assert p.build_model_args(model="m") == ["--model", "m"]
        assert p.build_model_args(model="", fallback="fb") == []
        assert p.supports_stream_json() is True
        assert p.build_output_args("json") == ["--json"]
        assert p.build_output_args("stream-json") == ["--json"]
        assert p.build_max_turns_args(10) == []
        assert p.build_mcp_args(configs=["x"]) == []
        assert p.build_plugin_args(plugin_dirs=["/x"]) == []

    def test_build_command_structure(self):
        from app.provider.codex import CodexProvider
        p = CodexProvider()
        cmd = p.build_command(prompt="hello", model="gpt-5", skip_permissions=True)
        assert (
            cmd[0] == "codex"
            and "--dangerously-bypass-approvals-and-sandbox" in cmd
            and "exec" in cmd
        )

    def test_read_only_pins_sandbox_and_beats_skip_permissions(self):
        """Codex ignores tool arguments, so the sandbox is the only lever.

        build_tool_args() returns [] AND build_command never calls it, so a
        read-only Codex review can only be enforced via --sandbox read-only.
        That must therefore override skip_permissions — honoring the bypass
        would hand the review write access to the live project clone.
        """
        from app.provider.codex import CodexProvider
        p = CodexProvider()
        assert p.build_permission_args(False, read_only=True) == [
            "--sandbox", "read-only",
        ]
        assert p.build_permission_args(True, read_only=True) == [
            "--sandbox", "read-only",
        ]

    def test_read_only_build_command_has_no_write_sandbox(self):
        from app.provider.codex import CodexProvider
        cmd = CodexProvider().build_command(
            prompt="hi", skip_permissions=True, read_only=True,
        )
        assert "read-only" in cmd
        assert "workspace-write" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

    def test_build_command_prepends_system_prompt(self):
        from app.provider.codex import CodexProvider
        cmd = CodexProvider().build_command(prompt="up", system_prompt="sys")
        assert cmd[-1].startswith("sys")

    def test_check_quota_success(self):
        from app.provider.codex import CodexProvider
        r = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch("app.provider.codex.subprocess.run", return_value=r), \
             patch("app.quota_handler.detect_quota_exhaustion", return_value=False):
            ok, msg = CodexProvider().check_quota_available("/tmp")
        assert ok is True

    def test_check_quota_exhausted(self):
        from app.provider.codex import CodexProvider
        r = MagicMock(stdout="", stderr="rate limit exceeded", returncode=1)
        with patch("app.provider.codex.subprocess.run", return_value=r), \
             patch("app.quota_handler.detect_quota_exhaustion", return_value=True):
            ok, msg = CodexProvider().check_quota_available("/tmp")
        assert ok is False

    def test_check_quota_ignores_healthy_rate_limit_telemetry(self):
        """A bare 'rate limit' mention is healthy telemetry, not exhaustion.

        Regression for the codex quota regex tightening: plain 'rate limit'
        (without exceeded/reached/rejected/error) must not pause the daemon.
        """
        from app.provider.codex import CodexProvider
        r = MagicMock(stdout="", stderr="rate limit", returncode=1)
        with patch("app.provider.codex.subprocess.run", return_value=r):
            ok, _ = CodexProvider().check_quota_available("/tmp")
        assert ok is True

    def test_check_quota_timeout_optimistic(self):
        import subprocess as sp
        from app.provider.codex import CodexProvider
        with patch("app.provider.codex.subprocess.run",
                   side_effect=sp.TimeoutExpired("codex", 1)):
            ok, _ = CodexProvider().check_quota_available("/tmp")
        assert ok is True

    def test_check_quota_generic_error_optimistic(self):
        from app.provider.codex import CodexProvider
        with patch("app.provider.codex.subprocess.run",
                   side_effect=OSError("no binary")):
            ok, _ = CodexProvider().check_quota_available("/tmp")
        assert ok is True


# ---------------------------------------------------------------------------
# ClineProvider
# ---------------------------------------------------------------------------


class TestClineProvider:
    def test_all_build_methods(self):
        from app.provider.cline import ClineProvider
        p = ClineProvider()
        assert p.binary() == "cline"
        with patch("app.provider.cline.shutil.which", return_value="/usr/bin/cline"):
            assert p.is_available() is True
        with patch("app.provider.cline.shutil.which", return_value=None):
            assert p.is_available() is False
        assert p.build_permission_args(True) == ["--auto-approve", "true"]
        assert p.build_permission_args(False) == ["--auto-approve", "false"]
        assert p.build_prompt_args("hi") == ["hi"]
        assert p.build_tool_args(allowed_tools=["Bash"]) == []
        assert p.build_model_args(model="m") == ["--model", "m"]
        assert p.build_model_args(model="", fallback="fb") == []
        assert p.supports_stream_json() is True
        assert p.build_output_args("json") == ["--json"]
        assert p.build_output_args("stream-json") == ["--json"]
        assert p.build_max_turns_args(10) == []
        assert p.build_mcp_args(configs=["x"]) == []
        assert p.build_plugin_args(plugin_dirs=["/x"]) == []
        assert p.build_effort_args(effort="high") == []
        assert p.build_thinking_args(enabled=True) == ["--thinking"]
        assert p.build_thinking_args(enabled=False) == []
        assert p.invocation_lock_name() == "cline-cli"

    def test_build_command_structure(self):
        from app.provider.cline import ClineProvider
        p = ClineProvider()
        cmd = p.build_command(prompt="hello", model="claude-sonnet-4", skip_permissions=True)
        assert cmd[0] == "cline"
        assert "--auto-approve" in cmd
        assert "true" in cmd
        assert "--model" in cmd
        assert "claude-sonnet-4" in cmd
        assert cmd[-1] == "hello"

    def test_build_command_ordering(self):
        from app.provider.cline import ClineProvider
        p = ClineProvider()
        cmd = p.build_command(
            prompt="test prompt",
            model="claude-sonnet-4",
            output_format="json",
            skip_permissions=True,
        )
        # Verify order: binary -> permissions -> model -> output -> prompt
        assert cmd[0] == "cline"
        perm_idx = cmd.index("--auto-approve")
        model_idx = cmd.index("--model")
        assert perm_idx < model_idx
        assert cmd[-1] == "test prompt"

    def test_build_command_prepends_system_prompt(self):
        from app.provider.cline import ClineProvider
        cmd = ClineProvider().build_command(prompt="user", system_prompt="system")
        assert cmd[-1].startswith("system")
        assert "user" in cmd[-1]

    def test_check_quota_success(self):
        from app.provider.cline import ClineProvider
        r = MagicMock(stdout="ok", stderr="", returncode=0)
        with patch("app.provider.cline.subprocess.run", return_value=r):
            ok, msg = ClineProvider().check_quota_available("/tmp")
        assert ok is True
        assert msg == ""

    def test_check_quota_exhausted(self):
        from app.provider.cline import ClineProvider
        r = MagicMock(stdout="", stderr="rate limit exceeded", returncode=1)
        with patch("app.provider.cline.subprocess.run", return_value=r):
            ok, msg = ClineProvider().check_quota_available("/tmp")
        assert ok is False
        assert "rate limit" in msg

    def test_check_quota_auth_failure(self):
        from app.provider.cline import ClineProvider
        r = MagicMock(stdout="401 Unauthorized", stderr="", returncode=1)
        with patch("app.provider.cline.subprocess.run", return_value=r):
            ok, msg = ClineProvider().check_quota_available("/tmp")
        assert ok is False
        assert "401" in msg

    def test_check_quota_timeout_optimistic(self):
        import subprocess as sp
        from app.provider.cline import ClineProvider
        with patch("app.provider.cline.subprocess.run",
                   side_effect=sp.TimeoutExpired("cline", 1)):
            ok, _ = ClineProvider().check_quota_available("/tmp")
        assert ok is True

    def test_check_quota_generic_error_optimistic(self):
        from app.provider.cline import ClineProvider
        with patch("app.provider.cline.subprocess.run",
                   side_effect=OSError("no binary")):
            ok, _ = ClineProvider().check_quota_available("/tmp")
        assert ok is True

    def test_detect_quota_exhaustion_stderr(self):
        from app.provider.cline import ClineProvider
        p = ClineProvider()
        assert p.detect_quota_exhaustion(stderr_text="rate limit exceeded") is True
        assert p.detect_quota_exhaustion(stderr_text="quota exceeded") is True
        assert p.detect_quota_exhaustion(stderr_text="HTTP 429") is True

    def test_detect_quota_exhaustion_stdout_with_exit_code(self):
        from app.provider.cline import ClineProvider
        p = ClineProvider()
        assert p.detect_quota_exhaustion(
            stdout_text="error: rate limit", exit_code=1
        ) is True
        # Should NOT detect quota on exit_code=0 (normal output)
        assert p.detect_quota_exhaustion(
            stdout_text="rate limit discussion", exit_code=0
        ) is False

    def test_detect_auth_failure(self):
        from app.provider.cline import ClineProvider
        p = ClineProvider()
        assert p.detect_auth_failure(stderr_text="401 Unauthorized", exit_code=1) is True
        assert p.detect_auth_failure(
            stdout_text="invalid api key", exit_code=1
        ) is True
        assert p.detect_auth_failure(
            stderr_text="authentication failed", exit_code=1
        ) is True
        # Should NOT detect auth on exit_code=0
        assert p.detect_auth_failure(
            stdout_text="401 Unauthorized", exit_code=0
        ) is False


# ---------------------------------------------------------------------------
# _format_cli_error
# ---------------------------------------------------------------------------


class TestFormatCliError:
    def test_stderr_present(self):
        from app.provider import _format_cli_error
        msg = _format_cli_error(1, "", "something broke")
        assert "exit=1" in msg
        assert "stderr=something broke" in msg
        assert "stdout=" not in msg

    def test_stdout_fallback_when_stderr_empty(self):
        from app.provider import _format_cli_error
        msg = _format_cli_error(2, "token expired", "")
        assert "exit=2" in msg
        assert "stdout=token expired" in msg
        assert "stderr=" not in msg

    def test_both_empty(self):
        from app.provider import _format_cli_error
        msg = _format_cli_error(42, "", "")
        assert "exit=42" in msg
        assert "stdout=" not in msg
        assert "stderr=" not in msg

    def test_stderr_truncated_at_300(self):
        from app.provider import _format_cli_error
        long_err = "x" * 500
        msg = _format_cli_error(1, "", long_err)
        assert f"stderr={'x' * 300}" in msg

    def test_stdout_truncated_at_300(self):
        from app.provider import _format_cli_error
        long_out = "y" * 500
        msg = _format_cli_error(1, long_out, "")
        assert f"stdout={'y' * 300}" in msg

    def test_jsonl_error_preview_prefers_last_provider_error(self):
        from app.provider import _format_cli_error
        stdout = "\n".join([
            json.dumps({"type": "thread.started"}),
            json.dumps({
                "type": "error",
                "message": "Reconnecting... 2/5",
            }),
            json.dumps({
                "type": "error",
                "message": (
                    'unexpected status 401 Unauthorized: {"detail":"Unauthorized"}'
                ),
            }),
            json.dumps({"type": "turn.failed"}),
        ])
        msg = _format_cli_error(1, stdout, "")
        assert "unexpected status 401 Unauthorized" in msg
        assert "thread.started" not in msg


# ---------------------------------------------------------------------------
# _write_system_prompt_file / cleanup_managed_paths
# ---------------------------------------------------------------------------


class TestWriteSystemPromptFile:
    def test_creates_readable_file(self, tmp_path):
        from app.provider import _write_system_prompt_file
        host_path, cmd_path = _write_system_prompt_file("test content")
        try:
            assert host_path == cmd_path
            assert os.path.isfile(host_path)
            with open(host_path) as f:
                assert f.read() == "test content"
        finally:
            os.unlink(host_path)

    def test_file_permissions_are_restrictive(self, tmp_path):
        from app.provider import _write_system_prompt_file
        host_path, _ = _write_system_prompt_file("secret prompt")
        try:
            import stat
            mode = os.stat(host_path).st_mode
            # Owner read+write should be set; group/other should not have write
            assert mode & stat.S_IRUSR
            assert mode & stat.S_IWUSR
            assert not (mode & stat.S_IWOTH)
        finally:
            os.unlink(host_path)

    def test_file_prefix_and_suffix(self):
        from app.provider import _write_system_prompt_file
        host_path, cmd_path = _write_system_prompt_file("x")
        try:
            basename = os.path.basename(host_path)
            assert basename.startswith("koan-sysprompt-")
            assert basename.endswith(".txt")
            assert host_path == cmd_path
        finally:
            os.unlink(host_path)


class TestCleanupManagedPaths:
    def test_removes_existing_files(self, tmp_path):
        from app.provider import cleanup_managed_paths
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a")
        f2.write_text("b")
        cleanup_managed_paths([str(f1), str(f2)])
        assert not f1.exists()
        assert not f2.exists()

    def test_ignores_missing_files(self, tmp_path):
        from app.provider import cleanup_managed_paths
        # Should not raise
        cleanup_managed_paths([str(tmp_path / "nonexistent.txt")])

    def test_empty_list_is_noop(self):
        from app.provider import cleanup_managed_paths
        cleanup_managed_paths([])


# ---------------------------------------------------------------------------
# build_full_command_managed
# ---------------------------------------------------------------------------


class TestBuildFullCommandManaged:
    def setup_method(self):
        from app.provider import reset_provider
        reset_provider()

    def teardown_method(self):
        from app.provider import reset_provider
        reset_provider()

    def test_without_system_prompt_no_temp_file(self):
        from app.provider import build_full_command_managed
        fake_prov = MagicMock()
        fake_prov.supports_system_prompt_file.return_value = True
        fake_prov.build_command.return_value = ["fake"]
        with patch("app.provider.get_provider", return_value=fake_prov), \
             patch("app.config.get_skip_permissions", return_value=False):
            cmd, cleanup_paths = build_full_command_managed(prompt="hi")
        assert cleanup_paths == []
        assert cmd == ["fake"]

    def test_with_system_prompt_creates_temp_file(self):
        from app.provider import build_full_command_managed, cleanup_managed_paths
        fake_prov = MagicMock()
        fake_prov.supports_system_prompt_file.return_value = True
        captured_kwargs = {}

        def capture_build(**kwargs):
            captured_kwargs.update(kwargs)
            return ["fake"]

        fake_prov.build_command.side_effect = capture_build
        with patch("app.provider.get_provider", return_value=fake_prov), \
             patch("app.config.get_skip_permissions", return_value=False):
            cmd, cleanup_paths = build_full_command_managed(
                prompt="hi", system_prompt="Be helpful.",
            )
        try:
            assert len(cleanup_paths) == 1
            path = cleanup_paths[0]
            assert os.path.isfile(path)
            with open(path) as f:
                assert f.read() == "Be helpful."
            # system_prompt should be cleared, system_prompt_file should be set
            assert captured_kwargs["system_prompt"] == ""
            assert captured_kwargs["system_prompt_file"] == path
        finally:
            cleanup_managed_paths(cleanup_paths)

    def test_provider_without_file_support_passes_inline(self):
        from app.provider import build_full_command_managed
        fake_prov = MagicMock()
        fake_prov.supports_system_prompt_file.return_value = False
        captured_kwargs = {}

        def capture_build(**kwargs):
            captured_kwargs.update(kwargs)
            return ["fake"]

        fake_prov.build_command.side_effect = capture_build
        with patch("app.provider.get_provider", return_value=fake_prov), \
             patch("app.config.get_skip_permissions", return_value=False):
            cmd, cleanup_paths = build_full_command_managed(
                prompt="hi", system_prompt="Be helpful.",
            )
        assert cleanup_paths == []
        assert captured_kwargs["system_prompt"] == "Be helpful."


# ---------------------------------------------------------------------------
# _extract_assistant_text_chunks
# ---------------------------------------------------------------------------


class TestExtractAssistantTextChunks:
    def test_extracts_text_blocks(self):
        from app.provider import _extract_assistant_text_chunks
        event = {
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "world"},
            ]},
        }
        assert _extract_assistant_text_chunks(event) == ["hello", "world"]

    def test_ignores_non_assistant_events(self):
        from app.provider import _extract_assistant_text_chunks
        assert _extract_assistant_text_chunks({"type": "user"}) == []
        assert _extract_assistant_text_chunks({"type": "result"}) == []

    def test_skips_empty_text(self):
        from app.provider import _extract_assistant_text_chunks
        event = {
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": ""},
                {"type": "text", "text": "real"},
            ]},
        }
        assert _extract_assistant_text_chunks(event) == ["real"]

    def test_handles_missing_message(self):
        from app.provider import _extract_assistant_text_chunks
        assert _extract_assistant_text_chunks({"type": "assistant"}) == []

    def test_handles_non_dict_blocks(self):
        from app.provider import _extract_assistant_text_chunks
        event = {
            "type": "assistant",
            "message": {"content": ["not a dict", {"type": "text", "text": "ok"}]},
        }
        assert _extract_assistant_text_chunks(event) == ["ok"]


# ---------------------------------------------------------------------------
# _extract_result_text
# ---------------------------------------------------------------------------


class TestExtractResultText:
    def test_extracts_result_string(self):
        from app.provider import _extract_result_text
        event = {"type": "result", "result": "the answer"}
        assert _extract_result_text(event) == "the answer"

    def test_returns_none_for_non_result_event(self):
        from app.provider import _extract_result_text
        assert _extract_result_text({"type": "assistant"}) is None

    def test_returns_none_for_empty_result(self):
        from app.provider import _extract_result_text
        assert _extract_result_text({"type": "result", "result": ""}) is None

    def test_returns_none_for_missing_result_field(self):
        from app.provider import _extract_result_text
        assert _extract_result_text({"type": "result"}) is None

    def test_returns_none_for_non_string_result(self):
        from app.provider import _extract_result_text
        assert _extract_result_text({"type": "result", "result": 42}) is None


# ---------------------------------------------------------------------------
# _is_stream_json_max_turns
# ---------------------------------------------------------------------------


class TestIsStreamJsonMaxTurns:
    def test_detects_error_max_turns(self):
        from app.provider import _is_stream_json_max_turns
        event = {"type": "result", "subtype": "error_max_turns"}
        assert _is_stream_json_max_turns(event) is True

    def test_detects_max_turns(self):
        from app.provider import _is_stream_json_max_turns
        event = {"type": "result", "subtype": "max_turns"}
        assert _is_stream_json_max_turns(event) is True

    def test_rejects_success(self):
        from app.provider import _is_stream_json_max_turns
        event = {"type": "result", "subtype": "success"}
        assert _is_stream_json_max_turns(event) is False

    def test_rejects_non_result_event(self):
        from app.provider import _is_stream_json_max_turns
        event = {"type": "assistant", "subtype": "error_max_turns"}
        assert _is_stream_json_max_turns(event) is False

    def test_handles_none_subtype(self):
        from app.provider import _is_stream_json_max_turns
        event = {"type": "result", "subtype": None}
        assert _is_stream_json_max_turns(event) is False

    def test_case_insensitive(self):
        from app.provider import _is_stream_json_max_turns
        event = {"type": "result", "subtype": "Error_Max_Turns"}
        assert _is_stream_json_max_turns(event) is True


# ---------------------------------------------------------------------------
# _summarize_stream_event (additional edge cases)
# ---------------------------------------------------------------------------


class TestSummarizeStreamEventEdgeCases:
    def test_system_init_with_model(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "system", "subtype": "init", "model": "sonnet-4",
        })
        assert "session init" in line
        assert "sonnet-4" in line

    def test_system_without_init(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({"type": "system", "subtype": "other"})
        assert "other" in line

    def test_assistant_thinking_block(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [{"type": "thinking"}]},
        })
        assert "thinking" in line

    def test_assistant_empty_content(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": []},
        })
        assert "(empty)" in line

    def test_user_tool_result_with_error(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "user",
            "message": {"content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_abc123xyz",
                    "is_error": True,
                    "content": "permission_denied\nadditional detail",
                },
            ]},
        })
        assert "tool_result" in line
        assert "(error)" in line
        assert line.endswith(": permission_denied")

    def test_user_turn_without_tool_result(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "user",
            "message": {"content": [{"type": "text", "text": "ok"}]},
        })
        assert "user turn" in line

    def test_result_without_duration(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "result", "subtype": "success",
        })
        assert "success" in line
        assert "s)" not in line

    def test_empty_type(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({"type": ""})
        assert "?" in line

    def test_text_block_multiline_takes_first_line(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "first line\nsecond line\nthird"},
            ]},
        })
        assert "first line" in line
        assert "second line" not in line

    def test_text_block_empty_string(self):
        from app.provider import _summarize_stream_event
        line = _summarize_stream_event({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": ""},
            ]},
        })
        # Empty text should render as just "text" without preview
        assert "text" in line


# ---------------------------------------------------------------------------
# Read-only role enforcement
# ---------------------------------------------------------------------------


class TestReadOnlyRoleEnforcement:
    """A read-only role must be enforced by the CLI, not by the prompt.

    The premise these tests defend, verified against Claude CLI 2.1.234:
    ``--allowedTools`` only *pre-approves* invocations, it does not restrict
    which tools exist. Under ``--allowedTools Read,Glob,Grep`` a ``Bash`` call
    still succeeds; under ``--disallowedTools Bash`` it is blocked. So passing a
    read-only ``allowed_tools`` list is NOT sufficient — the side-effecting
    tools have to be denied by name.
    """

    def test_read_only_denies_side_effect_tools(self):
        from app.provider import build_full_command
        from app.provider.base import READ_ONLY_TOOLS, SIDE_EFFECT_TOOLS
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p",
                allowed_tools=["Read", "Glob", "Grep"],
                provider=ClaudeProvider(),
                read_only=True,
            )
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
        # Bash is excluded from the denial ON PURPOSE: --disallowedTools removes
        # a tool from the model's context, so denying it here would cancel the
        # gated shell that --tools grants. Everything else still gets denied.
        for tool in SIDE_EFFECT_TOOLS:
            if tool in READ_ONLY_TOOLS:
                assert tool not in denied, (
                    f"{tool} is granted by READ_ONLY_TOOLS; denying it would "
                    "cancel the grant"
                )
                continue
            assert tool in denied, f"{tool} must be denied for a read-only role"

    def test_non_read_only_adds_no_denial(self):
        """The default path must stay byte-identical to before this change."""
        from app.provider import build_full_command
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p",
                allowed_tools=["Read", "Glob", "Grep"],
                provider=ClaudeProvider(),
            )
        assert "--disallowedTools" not in cmd
        assert "--tools" not in cmd
        assert "--strict-mcp-config" not in cmd

    def test_read_only_restricts_tools_to_the_allowlist(self):
        """The positive allowlist is the primary mechanism."""
        from app.provider import build_full_command
        from app.provider.base import READ_ONLY_TOOLS
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p",
                allowed_tools=["Read", "Glob", "Grep"],
                provider=ClaudeProvider(),
                read_only=True,
            )
        assert cmd[cmd.index("--tools") + 1].split(",") == list(READ_ONLY_TOOLS)

    def test_read_only_excludes_subagent_spawning_tools(self):
        """Task/Skill/MultiEdit must be withheld WITHOUT being enumerated.

        This is the whole reason the allowlist replaced the denial as primary:
        `SIDE_EFFECT_TOOLS` never named these, so a "read-only" review could
        spawn a write-capable subagent via Task. Nothing enumerates them now
        either -- they are absent because they were never named as allowed.
        """
        from app.provider import build_full_command
        from app.provider.base import SIDE_EFFECT_TOOLS
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p", provider=ClaudeProvider(), read_only=True,
            )
        granted = cmd[cmd.index("--tools") + 1].split(",")
        for tool in ("Task", "Skill", "MultiEdit", "Write", "Edit"):
            assert tool not in granted, f"{tool} must not be in the allowlist"
        # ...and none of them had to be listed as a denial to achieve that.
        for tool in ("Task", "Skill", "MultiEdit"):
            assert tool not in SIDE_EFFECT_TOOLS

    def test_bash_is_granted_but_never_pre_approved(self):
        """The whole fail-closed property of the review shell.

        `Bash` is in READ_ONLY_TOOLS so the tool exists, but nothing
        pre-approves it -- the PreToolUse hook returning "allow" is the only
        thing that lets a command run. If the hook fails to load, Bash is
        un-pre-approved and headless mode denies it, so the gate fails to
        "no shell" rather than to an unguarded one. Putting Bash into
        --allowedTools here would silently destroy that.
        """
        from app.provider import build_full_command
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p",
                allowed_tools=["Read", "Glob", "Grep"],
                provider=ClaudeProvider(),
                read_only=True,
            )
        assert "Bash" in cmd[cmd.index("--tools") + 1].split(",")
        pre_approved = cmd[cmd.index("--allowedTools") + 1].split(",")
        assert not any(t == "Bash" or t.startswith("Bash(") for t in pre_approved), (
            f"Bash must never be pre-approved; got {pre_approved}"
        )

    def test_read_only_installs_the_shell_gate(self):
        import json

        from app.provider import build_full_command
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p", provider=ClaudeProvider(), read_only=True,
            )
        settings_path = cmd[cmd.index("--settings") + 1]
        with open(settings_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        entry = cfg["hooks"]["PreToolUse"][0]
        assert entry["matcher"] == "Bash"
        assert "review_bash_guard.py" in entry["hooks"][0]["command"]

    def test_non_read_only_installs_no_gate(self):
        from app.provider import build_full_command
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(prompt="p", provider=ClaudeProvider())
        assert "--settings" not in cmd

    def test_read_only_isolates_ambient_mcp_servers(self):
        """--tools covers built-ins only; MCP needs its own isolation.

        Measured on CLI 2.1.235: under `--tools "Read,Glob,Grep"` every
        user-level MCP tool was still present, including write-capable ones.
        """
        from app.provider import build_full_command
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p", provider=ClaudeProvider(), read_only=True,
            )
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" not in cmd

    def test_empty_restriction_is_distinct_from_no_restriction(self):
        """`None` means unrestricted; `[]` means no built-in tools at all.

        Collapsing the two is how a positive allowlist quietly becomes a
        fail-open, so both must stay expressible.
        """
        from app.provider.claude import ClaudeProvider

        p = ClaudeProvider()
        assert p.build_tool_args(restrict_tools=None) == []
        assert p.build_tool_args(restrict_tools=[]) == ["--tools", ""]
        assert p.build_tool_args(restrict_tools=["Read"]) == ["--tools", "Read"]

    def test_claude_and_ollama_launch_declare_restriction(self):
        from app.provider.claude import ClaudeProvider
        from app.provider.ollama_launch import OllamaLaunchProvider

        for cls in (ClaudeProvider, OllamaLaunchProvider):
            assert cls().supports_tool_restriction() is True, cls.__name__

    def test_ollama_launch_forwards_restriction_and_isolation(self):
        """It re-implements build_command, so it can silently fail open."""
        from app.provider.base import READ_ONLY_TOOLS
        from app.provider.ollama_launch import OllamaLaunchProvider

        cmd = OllamaLaunchProvider().build_command(prompt="p", read_only=True)
        assert cmd[cmd.index("--tools") + 1].split(",") == list(READ_ONLY_TOOLS)
        assert "--strict-mcp-config" in cmd

    def test_read_only_preserves_caller_supplied_denials(self):
        from app.provider import build_full_command
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(
                prompt="p",
                allowed_tools=["Read"],
                disallowed_tools=["WebFetch"],
                provider=ClaudeProvider(),
                read_only=True,
            )
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
        assert "WebFetch" in denied, "caller-supplied denials must survive"
        assert "Write" in denied

    def test_read_only_suppresses_permission_bypass_end_to_end(self):
        """skip_permissions must not survive into a read-only command."""
        from app.provider import build_full_command
        from app.provider.claude import ClaudeProvider

        with patch("app.config.get_skip_permissions", return_value=True):
            cmd = build_full_command(
                prompt="p",
                allowed_tools=["Read", "Glob", "Grep"],
                provider=ClaudeProvider(),
                read_only=True,
            )
        assert "--dangerously-skip-permissions" not in cmd

    def test_every_provider_fails_closed_for_read_only(self):
        """Fail-closed across the whole registry.

        Each provider either enforces the boundary -- denying the side-effecting
        tools by name, or expressing a read-only posture some other way (Codex's
        sandbox) -- or the invocation is REFUSED. There is no third outcome: a
        provider that can do neither must never run a "read-only" review with
        full write access to the live project clone, so adding a new provider
        cannot silently produce an unenforced review either way.
        """
        from app.provider import (
            ReadOnlyUnenforceable,
            build_full_command,
            known_providers,
            _PROVIDERS,
        )

        # Providers that can express NEITHER a per-tool denial nor a read-only
        # sandbox. Documented in specs/skills/review.md: read-only invocations on
        # these are rejected. "fake" is test-only and refuses instantiation
        # without KOAN_ALLOW_FAKE_PROVIDER=1.
        unenforceable = {"cline", "haze", "copilot", "grok", "gemini", "fake"}
        assert unenforceable <= set(known_providers())

        enforced_count = 0
        refused_count = 0
        for name in known_providers():
            if name == "fake":
                continue
            provider = _PROVIDERS[name]()
            assert provider.enforces_read_only() is (name not in unenforceable), (
                f"provider {name!r}: enforces_read_only() disagrees with the "
                "documented unenforceable set"
            )
            if provider.supports_tool_restriction():
                assert provider.enforces_read_only(), (
                    f"provider {name!r}: a positive tool allowlist must imply "
                    "enforces_read_only()"
                )
            with patch("app.config.get_skip_permissions", return_value=False):
                if name in unenforceable:
                    with pytest.raises(ReadOnlyUnenforceable):
                        build_full_command(
                            prompt="p",
                            allowed_tools=["Read", "Glob", "Grep"],
                            provider=provider,
                            read_only=True,
                        )
                    refused_count += 1
                    continue
                cmd = build_full_command(
                    prompt="p",
                    allowed_tools=["Read", "Glob", "Grep"],
                    provider=provider,
                    read_only=True,
                )
            # Inspect each flag's OWN value, never the joined string. Once
            # `--tools Read,Glob,Grep` is also present, a substring check for
            # "Bash" would pass for the wrong reason and the assertion would
            # silently stop testing what it claims.
            def _flag_value(flag):
                return cmd[cmd.index(flag) + 1] if flag in cmd else ""

            # A positive allowlist enforces when it exists at all: whatever it
            # omits is withheld. Bash may legitimately be IN it (gated by the
            # PreToolUse hook), so its presence says nothing either way.
            restricted = "--tools" in cmd
            denied = "Write" in _flag_value("--disallowedTools").split(",")
            sandboxed = "read-only" in cmd
            assert restricted or denied or sandboxed, (
                f"provider {name!r} claims enforces_read_only() but produced no "
                "read-only enforcement in the built command"
            )
            enforced_count += 1
        assert enforced_count, "no enforceable provider was exercised"
        assert refused_count, "no unenforceable provider was exercised"

    def test_unenforceable_provider_is_refused_with_actionable_error(self):
        """The refusal must name the provider and point at the fix."""
        from app.provider import ReadOnlyUnenforceable, build_full_command
        from app.provider.haze import HazeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            with pytest.raises(ReadOnlyUnenforceable) as exc:
                build_full_command(
                    prompt="p",
                    allowed_tools=["Read", "Glob", "Grep"],
                    provider=HazeProvider(),
                    read_only=True,
                )
        msg = str(exc.value)
        assert "haze" in msg
        assert "cli.review_mode" in msg

    def test_unenforceable_provider_still_runs_when_not_read_only(self):
        """The refusal is scoped to read-only roles, not the provider itself."""
        from app.provider import build_full_command
        from app.provider.haze import HazeProvider

        with patch("app.config.get_skip_permissions", return_value=False):
            cmd = build_full_command(prompt="p", provider=HazeProvider())
        assert cmd, "a non-read-only haze invocation must still build"

    def test_fallback_swap_into_unenforceable_provider_is_refused(self):
        """The check must run AFTER provider resolution, including cli.fallback.

        `resolve_role_provider` swaps to `cli.fallback` when the role's binary is
        missing, so a fallback into an unenforceable provider must be refused
        exactly like a directly-configured one. Nothing else pins this ordering:
        moving the predicate up into `resolve_role_provider` or
        `get_provider_for_role` would silently break the documented contract in
        specs/components/providers.md.
        """
        import app.provider as provider_mod
        from app.provider import ReadOnlyUnenforceable, run_command_streaming
        from app.provider.haze import HazeProvider

        with patch.object(
            provider_mod, "resolve_role_provider", return_value=HazeProvider(),
        ), patch("app.config.get_skip_permissions", return_value=False), \
                patch("app.config.get_model_config", return_value={}):
            with pytest.raises(ReadOnlyUnenforceable):
                run_command_streaming(
                    prompt="p",
                    project_path=".",
                    allowed_tools=["Read", "Glob", "Grep"],
                    model_key="review_mode",
                )

    def test_grok_emits_tool_flags_but_still_declares_no_capability(self):
        """Grok is refused for a measured reason, not for lacking flags.

        `GrokProvider.build_tool_args` DOES emit `--tools` / `--disallowed-tools`,
        so "grok has no deny flag" is false and must not be the justification.
        It stays unenforceable because (1) nobody has measured that those flags
        withhold rather than pre-approve -- the exact assumption that made
        `--allowedTools` look like a restriction when it was not -- and (2)
        `build_permission_args` returns `--always-approve` unconditionally,
        ignoring read_only. This test fails if someone flips the predicate
        without first making the permission args conditional.
        """
        from app.provider.base import SIDE_EFFECT_TOOLS
        from app.provider.grok import GrokProvider

        p = GrokProvider()
        flags = p.build_tool_args(["Read", "Glob", "Grep"], list(SIDE_EFFECT_TOOLS))
        assert "--disallowed-tools" in flags, (
            "premise changed: grok no longer emits a denial flag"
        )
        assert p.enforces_read_only() is False

        # Blocker (2): the blanket auto-approve is emitted even for read_only.
        assert p.build_permission_args(False, read_only=True) == ["--always-approve"]

    def test_codex_declares_read_only_sandbox_support(self):
        from app.provider.codex import CodexProvider

        p = CodexProvider()
        assert p.supports_tool_denial() is False
        assert p.supports_read_only_sandbox() is True
        assert p.enforces_read_only() is True

    def test_managed_build_unlinks_prompt_file_when_build_raises(self):
        """A failed build must not leave the 0600 system-prompt file behind.

        ``cleanup_paths`` only reaches the caller on success, so a raise out of
        ``build_full_command`` would otherwise leak a file containing the system
        prompt with nobody holding its path.
        """
        import os

        import app.provider as provider_mod
        from app.provider import build_full_command_managed
        from app.provider.claude import ClaudeProvider

        captured = {}
        real = provider_mod._write_system_prompt_file

        def spy(content, host_dir=None, container_dir=None):
            host_path, cmd_path = real(content, host_dir, container_dir)
            captured["path"] = host_path
            return host_path, cmd_path

        with patch.object(provider_mod, "_write_system_prompt_file", spy), \
                patch("app.config.get_skip_permissions", return_value=False), \
                patch.object(
                    ClaudeProvider, "build_command", side_effect=RuntimeError("boom"),
                ):
            with pytest.raises(RuntimeError, match="boom"):
                build_full_command_managed(
                    prompt="p",
                    system_prompt="a system prompt",
                    provider=ClaudeProvider(),
                )

        assert captured.get("path"), "the spy never saw a temp file"
        assert not os.path.exists(captured["path"]), (
            "system-prompt temp file leaked when the build raised"
        )


class TestStreamingReadLoopIsInactivityBounded:
    """A provider that opens stdout then goes silent must not hang the caller.

    Production symptom (2026-09-10): a review pass printed its `session init`
    banner and then emitted nothing for 600s. The ``timeout=`` argument did not
    bound it — ``timeout`` is applied by the ``proc.wait()`` that runs *after*
    stdout EOF, and a blocking ``for line in proc.stdout`` never reaches that
    line. The only thing that ended the run was run.py's outer skill-runner
    watchdog, which SIGKILLs the whole runner: the mission died as a generic
    "Skill runner timed out", no partial result was written, and the stall was
    not attributable to the pass that caused it.

    A wall-clock cap is the wrong instrument — a healthy long pass streams
    progress for many minutes — so the bound is on inactivity.
    """

    STALL_SECONDS = 30

    def _spawn(self, script):
        import subprocess as _sp
        import sys as _sys
        return _sp.Popen(
            [_sys.executable, "-c", script],
            stdout=_sp.PIPE, stderr=_sp.PIPE,
            encoding="utf-8", errors="replace",
            start_new_session=True,
        )

    def _run(self, proc, **kwargs):
        from app.provider import run_command_streaming
        cleanup = MagicMock()
        with patch("app.config.get_model_config",
                   return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli", return_value=(proc, cleanup)), \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            return run_command_streaming("hi", "/tmp", [], **kwargs)

    def test_a_silent_provider_fails_the_pass_instead_of_blocking(self):
        import time as _time
        proc = self._spawn(
            "import sys,time\n"
            "sys.stdout.write('session init\\n')\n"
            "sys.stdout.flush()\n"
            f"time.sleep({self.STALL_SECONDS})\n"
        )
        started = _time.monotonic()
        try:
            with pytest.raises(RuntimeError, match="no output for 2s"):
                self._run(proc, idle_timeout=2)
        finally:
            proc.kill()
            proc.wait()
        elapsed = _time.monotonic() - started
        assert elapsed < self.STALL_SECONDS / 2, (
            f"returned in {elapsed:.1f}s — the bound did not reach the read loop"
        )

    def test_a_streaming_provider_is_not_killed_by_the_idle_bound(self):
        """Every consumed line heartbeats, so steady progress survives."""
        proc = self._spawn(
            "import sys,time\n"
            "for i in range(10):\n"
            "    sys.stdout.write('tick %d\\n' % i)\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.2)\n"
        )
        try:
            out = self._run(proc, idle_timeout=2)
        finally:
            proc.kill()
            proc.wait()
        # Ran 2s wall-clock with a 2s idle bound and still completed.
        assert "tick 0" in out and "tick 9" in out

    def _spawn_kwargs(self, **kwargs):
        """popen_cli kwargs for a trivially-successful streaming call."""
        from app.provider import run_command_streaming
        proc = MagicMock()
        stdout = MagicMock()
        stdout.__iter__ = lambda self: iter(["ok\n"])
        proc.stdout = stdout
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = ""
        proc.returncode = 0
        proc.wait.return_value = None
        with patch("app.config.get_model_config",
                   return_value={"chat": "m", "fallback": "f"}), \
             patch("app.provider.build_full_command", return_value=["fake"]), \
             patch("app.cli_exec.popen_cli",
                   return_value=(proc, MagicMock())) as mock_popen, \
             patch("app.claude_step.strip_cli_noise", side_effect=lambda s: s):
            run_command_streaming("hi", "/tmp", [], **kwargs)
        return mock_popen.call_args[1]

    def test_an_opted_in_child_gets_its_own_session(self):
        """An armed watchdog group-kills; a shared group would kill koan."""
        assert self._spawn_kwargs(idle_timeout=30).get(
            "start_new_session") is True

    def test_a_non_opted_in_child_stays_in_the_caller_s_group(self):
        """Session isolation must not outrun the watchdog that needs it.

        run.py's skill-runner teardown and ``mission_scope``'s fallback both
        reap by process group. A child in its own session is outside both, so
        isolating one with no watchdog to protect buys nothing and lets a
        stuck provider outlive the runner that spawned it — surviving a skill
        timeout, an abort, or the outer liveness kill while still burning
        quota.
        """
        assert "start_new_session" not in self._spawn_kwargs()
        assert "start_new_session" not in self._spawn_kwargs(idle_timeout=0)
        assert "start_new_session" not in self._spawn_kwargs(idle_timeout=None)

    def test_a_sigterm_ignoring_descendant_cannot_hold_the_pipe_open(self):
        """The watchdog kill must be SIGKILL-to-the-group, not SIGTERM-first.

        ``kill_process_group`` stops escalating the moment the *leader* exits.
        A descendant that ignores SIGTERM therefore survives it — and if it
        inherited the stdout write end, the reader never sees EOF, never
        reaches the ``fired`` check, and hangs exactly as it did before the
        watchdog existed. Only a group SIGKILL is guaranteed to release the
        pipe.
        """
        import time as _time
        proc = self._spawn(
            "import signal,subprocess,sys,time\n"
            # Inherits stdout, ignores SIGTERM: survives the graceful path
            # while still holding the pipe's write end open.
            "subprocess.Popen([sys.executable, '-c',\n"
            "  'import signal,time\\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
            f"time.sleep({self.STALL_SECONDS})'])\n"
            "sys.stdout.write('session init\\n')\n"
            "sys.stdout.flush()\n"
            f"time.sleep({self.STALL_SECONDS})\n"
        )
        started = _time.monotonic()
        try:
            with pytest.raises(RuntimeError, match="no output for 2s"):
                self._run(proc, idle_timeout=2)
        finally:
            import os as _os
            import signal as _signal
            with contextlib.suppress(OSError, ProcessLookupError):
                _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
        elapsed = _time.monotonic() - started
        assert elapsed < self.STALL_SECONDS / 2, (
            f"returned in {elapsed:.1f}s — a SIGTERM survivor held the pipe"
        )

    def test_the_watchdog_is_disarmed_once_stdout_reaches_eof(self):
        """A completed run must not be killed during the post-EOF wait.

        ``proc.stderr.read()`` and ``proc.wait()`` emit no heartbeats. A
        watchdog still armed across them SIGKILLs a run that already streamed
        everything, and because the ``fired`` check has already been
        evaluated the kill surfaces as an opaque ``exit -9`` instead of a
        stall — a successful review reported as a provider crash.
        """
        proc = self._spawn(
            "import os,sys,time\n"
            "sys.stdout.write('done\\n')\n"
            "sys.stdout.flush()\n"
            # os.close, not sys.stdout.close(): CPython builds the std streams
            # with closefd=False, so closing the wrapper leaves fd 1 (and the
            # pipe) open and the reader would never see EOF.
            "os.close(1)\n"
            # Still alive well past the idle bound, holding stderr open — the
            # exact window `proc.stderr.read()` blocks in with no heartbeats.
            "time.sleep(4)\n"
        )
        try:
            out = self._run(proc, idle_timeout=2)
        finally:
            proc.kill()
            proc.wait()
        assert "done" in out


class TestReviewStallTimeoutStaysUnderTheOuterWatchdog:
    """An inner bound at or above the outer one is decorative."""

    @pytest.mark.parametrize("outer,expected", [
        (600, 540),  # the default: a flat 60s of reporting margin
        (120, 60),   # exactly the floor, still strictly below the outer
        (119, 0),    # margin leaves less than the 60s floor -> outer governs
        (60, 0),     # outer already tighter than the floor
        (0, 0),      # operator disabled stall killing -- honour it
    ])
    def test_derived_from_first_output_timeout(self, outer, expected):
        from app.review_runner import _review_stall_timeout
        with patch("app.config.get_first_output_timeout", return_value=outer):
            assert _review_stall_timeout() == expected

    @pytest.mark.parametrize("outer", [0, 1, 59, 60, 119, 120, 300, 600, 3600])
    def test_result_is_zero_or_strictly_below_the_outer_budget(self, outer):
        """The postcondition, over the whole input range.

        A single off-by-one here silently reinstates the bug: the outer
        watchdog fires first and SIGKILLs the runner, so the pass never gets
        to report its own stall.
        """
        from app.review_runner import _review_stall_timeout
        with patch("app.config.get_first_output_timeout", return_value=outer):
            inner = _review_stall_timeout()
        assert inner == 0 or inner < outer
