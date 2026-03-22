"""Tests for CLI provider abstraction (app.provider package)."""

import os
from unittest.mock import patch, MagicMock

import pytest

from app.cli_provider import (
    ClaudeProvider,
    CopilotProvider,
    LocalLLMProvider,
    get_provider,
    get_provider_name,
    get_cli_binary,
    reset_provider,
    build_cli_flags,
    build_tool_flags,
    build_prompt_flags,
    build_output_flags,
    build_max_turns_flags,
    build_full_command,
    run_command,
    CLAUDE_TOOLS,
    TOOL_NAME_MAP,
)


# ---------------------------------------------------------------------------
# Package structure
# ---------------------------------------------------------------------------

class TestPackageStructure:
    """Verify the provider package is properly split and re-exports work."""

    def test_import_from_provider_package(self):
        from app.provider import ClaudeProvider, CopilotProvider, CLIProvider
        assert ClaudeProvider.name == "claude"
        assert CopilotProvider.name == "copilot"

    def test_import_from_base(self):
        from app.provider.base import CLIProvider, CLAUDE_TOOLS
        assert "Bash" in CLAUDE_TOOLS
        assert hasattr(CLIProvider, "build_command")

    def test_import_from_claude_module(self):
        from app.provider.claude import ClaudeProvider
        assert ClaudeProvider().binary() == "claude"

    def test_import_from_copilot_module(self):
        from app.provider.copilot import CopilotProvider
        assert CopilotProvider.name == "copilot"

    def test_import_from_local_module(self):
        from app.provider.local import LocalLLMProvider
        assert LocalLLMProvider.name == "local"

    def test_facade_reexports_local(self):
        from app.cli_provider import LocalLLMProvider as Facade
        from app.provider import LocalLLMProvider as Package
        assert Facade is Package

    def test_facade_reexports_same_objects(self):
        """cli_provider.py re-exports are identical to provider package objects."""
        from app.cli_provider import ClaudeProvider as Facade
        from app.provider import ClaudeProvider as Package
        assert Facade is Package

    def test_base_class_is_same(self):
        from app.cli_provider import CLIProvider as Facade
        from app.provider.base import CLIProvider as Base
        assert Facade is Base


# ---------------------------------------------------------------------------
# ClaudeProvider
# ---------------------------------------------------------------------------

class TestClaudeProvider:
    """Tests for ClaudeProvider flag generation."""

    def setup_method(self):
        self.provider = ClaudeProvider()

    def test_binary(self):
        assert self.provider.binary() == "claude"

    def test_name(self):
        assert self.provider.name == "claude"

    def test_prompt_args(self):
        assert self.provider.build_prompt_args("hello world") == ["-p", "hello world"]

    def test_tool_args_allowed(self):
        result = self.provider.build_tool_args(allowed_tools=["Bash", "Read"])
        assert result == ["--allowedTools", "Bash,Read"]

    def test_tool_args_disallowed(self):
        result = self.provider.build_tool_args(disallowed_tools=["Bash", "Edit", "Write"])
        assert result == ["--disallowedTools", "Bash", "Edit", "Write"]

    def test_tool_args_empty(self):
        assert self.provider.build_tool_args() == []

    def test_model_args(self):
        result = self.provider.build_model_args(model="opus", fallback="sonnet")
        assert result == ["--model", "opus", "--fallback-model", "sonnet"]

    def test_model_args_empty(self):
        assert self.provider.build_model_args() == []

    def test_model_args_partial(self):
        assert self.provider.build_model_args(model="haiku") == ["--model", "haiku"]
        assert self.provider.build_model_args(fallback="sonnet") == ["--fallback-model", "sonnet"]

    def test_model_args_same_skips_fallback(self):
        """When fallback equals model, skip --fallback-model to avoid CLI rejection."""
        result = self.provider.build_model_args(model="sonnet", fallback="sonnet")
        assert result == ["--model", "sonnet"]

    def test_output_args_json(self):
        assert self.provider.build_output_args("json") == ["--output-format", "json"]

    def test_output_args_empty(self):
        assert self.provider.build_output_args() == []

    def test_max_turns_args(self):
        assert self.provider.build_max_turns_args(3) == ["--max-turns", "3"]

    def test_max_turns_args_zero(self):
        assert self.provider.build_max_turns_args(0) == []

    def test_mcp_args(self):
        result = self.provider.build_mcp_args(["config1.json", "config2.json"])
        assert result == ["--mcp-config", "config1.json", "config2.json"]

    def test_mcp_args_empty(self):
        assert self.provider.build_mcp_args() == []
        assert self.provider.build_mcp_args([]) == []

    def test_build_command_full(self):
        cmd = self.provider.build_command(
            prompt="do the thing",
            allowed_tools=["Bash", "Read"],
            model="opus",
            fallback="sonnet",
            output_format="json",
            max_turns=5,
            mcp_configs=["mcp.json"],
        )
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "do the thing" in cmd
        assert "--allowedTools" in cmd
        assert "--model" in cmd
        assert "opus" in cmd
        assert "--fallback-model" in cmd
        assert "--output-format" in cmd
        assert "--max-turns" in cmd
        assert "--mcp-config" in cmd

    def test_build_command_minimal(self):
        cmd = self.provider.build_command(prompt="hello")
        assert cmd == ["claude", "-p", "hello"]

    def test_extra_flags(self):
        result = self.provider.build_extra_flags(
            model="opus", fallback="sonnet", disallowed_tools=["Bash"]
        )
        assert "--model" in result
        assert "--fallback-model" in result
        assert "--disallowedTools" in result

    def test_build_permission_args_true(self):
        assert self.provider.build_permission_args(True) == ["--dangerously-skip-permissions"]

    def test_build_permission_args_false(self):
        assert self.provider.build_permission_args(False) == []

    def test_build_command_with_skip_permissions(self):
        cmd = self.provider.build_command(prompt="hello", skip_permissions=True)
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert cmd.index("--dangerously-skip-permissions") < cmd.index("-p")

    def test_build_command_without_skip_permissions(self):
        cmd = self.provider.build_command(prompt="hello", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd


# ---------------------------------------------------------------------------
# System prompt support
# ---------------------------------------------------------------------------

class TestSystemPromptSupport:
    """Tests for --append-system-prompt support across providers."""

    def test_claude_build_system_prompt_args(self):
        """ClaudeProvider returns --append-system-prompt flag."""
        p = ClaudeProvider()
        args = p.build_system_prompt_args("You are helpful.")
        assert args == ["--append-system-prompt", "You are helpful."]

    def test_claude_build_system_prompt_args_empty(self):
        """Empty system prompt returns empty list."""
        p = ClaudeProvider()
        assert p.build_system_prompt_args("") == []

    def test_copilot_build_system_prompt_args_empty(self):
        """CopilotProvider returns empty (no native support)."""
        with patch("shutil.which", return_value="/usr/bin/gh"):
            p = CopilotProvider()
        assert p.build_system_prompt_args("content") == []

    def test_local_build_system_prompt_args_empty(self):
        """LocalLLMProvider returns empty (no native support)."""
        p = LocalLLMProvider()
        assert p.build_system_prompt_args("content") == []

    def test_claude_build_command_with_system_prompt(self):
        """ClaudeProvider.build_command includes --append-system-prompt."""
        p = ClaudeProvider()
        cmd = p.build_command(
            prompt="do something",
            system_prompt="you are koan",
        )
        assert "--append-system-prompt" in cmd
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == "you are koan"
        assert "-p" in cmd
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "do something"

    def test_copilot_build_command_prepends_system_prompt(self):
        """Non-supporting providers prepend system prompt to user prompt."""
        with patch("shutil.which", return_value="/usr/bin/gh"):
            p = CopilotProvider()
        cmd = p.build_command(
            prompt="do something",
            system_prompt="you are koan",
        )
        # System prompt should be prepended to user prompt
        assert "--append-system-prompt" not in cmd
        # Find the prompt value
        for i, arg in enumerate(cmd):
            if arg == "-m":  # copilot uses -m
                assert "you are koan" in cmd[i + 1]
                assert "do something" in cmd[i + 1]
                break

    @patch("app.config.get_skip_permissions", return_value=False)
    @patch("app.provider.get_provider")
    def test_build_full_command_passes_system_prompt(self, mock_get, mock_perm):
        """build_full_command passes system_prompt to provider."""
        mock_provider = MagicMock()
        mock_provider.build_command.return_value = ["claude", "-p", "test"]
        mock_get.return_value = mock_provider

        build_full_command(
            prompt="test",
            system_prompt="stable content",
        )
        mock_provider.build_command.assert_called_once()
        call_kwargs = mock_provider.build_command.call_args
        assert call_kwargs.kwargs.get("system_prompt") == "stable content"


# ---------------------------------------------------------------------------
# CopilotProvider
# ---------------------------------------------------------------------------

class TestCopilotProvider:
    """Tests for CopilotProvider flag translation.

    CopilotProvider caches shutil.which() at __init__, so tests that need
    specific which() results must create the provider inside the patch.
    """

    def _make(self, which_side_effect=None):
        """Create a CopilotProvider with mocked shutil.which."""
        if which_side_effect is None:
            return CopilotProvider()
        with patch("app.provider.copilot.shutil.which", side_effect=which_side_effect):
            return CopilotProvider()

    @staticmethod
    def _standalone(x):
        return "/usr/local/bin/copilot" if x == "copilot" else None

    @staticmethod
    def _gh_only(x):
        return "/usr/bin/gh" if x == "gh" else None

    def test_name(self):
        assert CopilotProvider.name == "copilot"

    def test_binary_standalone(self):
        """Uses 'copilot' when standalone binary is available."""
        p = self._make(self._standalone)
        assert p.binary() == "copilot"

    def test_binary_gh_fallback(self):
        """Falls back to 'gh' when copilot binary not found."""
        p = self._make(self._gh_only)
        assert p.binary() == "gh"

    def test_shell_command_standalone(self):
        p = self._make(self._standalone)
        assert p.shell_command() == "copilot"

    def test_shell_command_gh_mode(self):
        p = self._make(self._gh_only)
        assert p.shell_command() == "gh copilot"

    def test_is_available_copilot(self):
        p = self._make(self._standalone)
        assert p.is_available()

    def test_is_available_gh(self):
        p = self._make(self._gh_only)
        assert p.is_available()

    def test_not_available(self):
        p = self._make(lambda x: None)
        assert not p.is_available()

    def test_prompt_args_standalone(self):
        p = self._make(self._standalone)
        assert p.build_prompt_args("test") == ["-p", "test"]

    def test_prompt_args_gh_mode(self):
        p = self._make(self._gh_only)
        result = p.build_prompt_args("test")
        assert result == ["copilot", "-p", "test"]

    def test_tool_args_individual(self):
        """Maps Claude tool names to Copilot equivalents."""
        p = self._make()
        result = p.build_tool_args(allowed_tools=["Read", "Grep"])
        assert "--allow-tool" in result
        assert "read_file" in result
        assert "grep" in result

    def test_tool_args_all_tools(self):
        """Uses --allow-all-tools when all canonical tools are requested."""
        p = self._make()
        all_tools = list(CLAUDE_TOOLS)
        result = p.build_tool_args(allowed_tools=all_tools)
        assert "--allow-all-tools" in result

    def test_tool_args_disallowed_inverse(self):
        """Computes inverse set when using disallowed_tools."""
        p = self._make()
        result = p.build_tool_args(disallowed_tools=["Bash", "Edit", "Write"])
        # Should allow the remaining tools: Read, Glob, Grep
        assert "--allow-tool" in result
        tool_names = [result[i + 1] for i in range(len(result)) if result[i] == "--allow-tool"]
        assert set(tool_names) == {"read_file", "glob", "grep", "skill"}

    def test_model_args(self):
        p = self._make()
        result = p.build_model_args(model="opus", fallback="sonnet")
        assert result == ["--model", "opus"]
        # No --fallback-model for copilot

    def test_model_args_empty(self):
        p = self._make()
        assert p.build_model_args() == []

    def test_output_args_json(self):
        p = self._make()
        # Copilot doesn't support --json, should return empty
        assert p.build_output_args("json") == []

    def test_output_args_empty(self):
        p = self._make()
        assert p.build_output_args() == []

    def test_max_turns_args(self):
        p = self._make()
        # Copilot doesn't support --max-turns, should return empty
        assert p.build_max_turns_args(3) == []
        assert p.build_max_turns_args(0) == []

    def test_mcp_args(self):
        p = self._make()
        result = p.build_mcp_args(["config.json"])
        assert result == ["--mcp-config", "config.json"]

    def test_build_command_gh_mode(self):
        """Full command in gh mode includes 'copilot' subcommand."""
        p = self._make(self._gh_only)
        cmd = p.build_command(
            prompt="hello",
            allowed_tools=["Read"],
            max_turns=1,
        )
        assert cmd[0] == "gh"
        assert "copilot" in cmd
        assert "-p" in cmd
        assert "--allow-tool" in cmd
        assert "read_file" in cmd

    def test_build_command_standalone(self):
        p = self._make(self._standalone)
        cmd = p.build_command(prompt="hello")
        assert cmd[0] == "copilot"
        assert "copilot" not in cmd[1:]  # No redundant 'copilot' subcommand


# ---------------------------------------------------------------------------
# Tool name mapping
# ---------------------------------------------------------------------------

class TestToolMapping:
    """Verify tool name mapping between providers."""

    def test_all_claude_tools_mapped(self):
        for tool in CLAUDE_TOOLS:
            assert tool in TOOL_NAME_MAP

    def test_mapping_values(self):
        assert TOOL_NAME_MAP["Bash"] == "shell"
        assert TOOL_NAME_MAP["Read"] == "read_file"
        assert TOOL_NAME_MAP["Write"] == "write_file"
        assert TOOL_NAME_MAP["Edit"] == "edit_file"
        assert TOOL_NAME_MAP["Glob"] == "glob"
        assert TOOL_NAME_MAP["Grep"] == "grep"


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

class TestProviderResolution:
    """Tests for get_provider_name() and get_provider()."""

    def setup_method(self):
        reset_provider()

    def teardown_method(self):
        reset_provider()

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    def test_env_var_override(self):
        assert get_provider_name() == "copilot"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_env_var_claude(self):
        assert get_provider_name() == "claude"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "invalid"})
    @patch("app.utils.load_config", return_value={})
    def test_env_var_invalid_falls_to_default(self, mock_config):
        assert get_provider_name() == "claude"

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.utils.load_config", return_value={"cli_provider": "copilot"})
    def test_config_yaml(self, mock_config):
        # Remove env var if present
        import os
        os.environ.pop("KOAN_CLI_PROVIDER", None)
        assert get_provider_name() == "copilot"

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.utils.load_config", return_value={})
    def test_default_claude(self, mock_config):
        import os
        os.environ.pop("KOAN_CLI_PROVIDER", None)
        assert get_provider_name() == "claude"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_get_provider_returns_claude(self):
        provider = get_provider()
        assert isinstance(provider, ClaudeProvider)

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    def test_get_provider_returns_copilot(self):
        provider = get_provider()
        assert isinstance(provider, CopilotProvider)

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_get_provider_caches_instance(self):
        """Same provider name returns the same cached instance."""
        p1 = get_provider()
        p2 = get_provider()
        assert p1 is p2

    def test_get_provider_invalidates_on_name_change(self, monkeypatch):
        """Changing provider name returns a new instance."""
        monkeypatch.setenv("KOAN_CLI_PROVIDER", "claude")
        p1 = get_provider()
        reset_provider()
        monkeypatch.setenv("KOAN_CLI_PROVIDER", "copilot")
        p2 = get_provider()
        assert type(p1) is not type(p2)

    @patch.dict("os.environ", {"CLI_PROVIDER": "copilot"}, clear=True)
    @patch("app.utils.load_config", return_value={})
    def test_fallback_to_cli_provider(self, mock_config, capsys):
        """CLI_PROVIDER fallback works when KOAN_CLI_PROVIDER is not set."""
        # Import after patching env to reset the warning flag
        import app.utils
        app.utils._cli_provider_warned = False

        assert get_provider_name() == "copilot"
        captured = capsys.readouterr()
        assert "CLI_PROVIDER is deprecated" in captured.out

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude", "CLI_PROVIDER": "copilot"})
    def test_koan_cli_provider_takes_priority(self, capsys):
        """KOAN_CLI_PROVIDER takes priority over CLI_PROVIDER."""
        import app.utils
        app.utils._cli_provider_warned = False

        assert get_provider_name() == "claude"
        captured = capsys.readouterr()
        # Should not warn since KOAN_CLI_PROVIDER is set
        assert "deprecated" not in captured.out

    @patch.dict("os.environ", {"CLI_PROVIDER": "claude"}, clear=True)
    @patch("app.utils.load_config", return_value={})
    def test_fallback_warning_only_once(self, mock_config, capsys):
        """Deprecation warning is shown only once per process."""
        import app.utils
        app.utils._cli_provider_warned = False

        # First call should warn
        get_provider_name()
        captured1 = capsys.readouterr()
        assert "CLI_PROVIDER is deprecated" in captured1.out

        # Second call should not warn
        reset_provider()
        get_provider_name()
        captured2 = capsys.readouterr()
        assert "deprecated" not in captured2.out

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.utils.load_config", return_value={})
    def test_empty_when_neither_set(self, mock_config):
        """Falls back to default when neither env var is set."""
        import os
        os.environ.pop("KOAN_CLI_PROVIDER", None)
        os.environ.pop("CLI_PROVIDER", None)
        assert get_provider_name() == "claude"


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

class TestConvenienceFunctions:
    """Tests for module-level helper functions."""

    def setup_method(self):
        reset_provider()

    def teardown_method(self):
        reset_provider()

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_get_cli_binary_claude(self):
        assert get_cli_binary() == "claude"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    @patch("app.provider.copilot.shutil.which")
    def test_get_cli_binary_copilot_standalone(self, mock_which):
        mock_which.side_effect = lambda x: "/usr/local/bin/copilot" if x == "copilot" else None
        assert get_cli_binary() == "copilot"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    @patch("app.provider.copilot.shutil.which")
    def test_get_cli_binary_copilot_gh_mode(self, mock_which):
        mock_which.side_effect = lambda x: "/usr/bin/gh" if x == "gh" else None
        assert get_cli_binary() == "gh copilot"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_build_cli_flags_claude(self):
        flags = build_cli_flags(model="opus", fallback="sonnet")
        assert flags == ["--model", "opus", "--fallback-model", "sonnet"]

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    def test_build_cli_flags_copilot(self):
        flags = build_cli_flags(model="opus", fallback="sonnet")
        assert flags == ["--model", "opus"]
        # Copilot ignores fallback

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_build_tool_flags_claude(self):
        flags = build_tool_flags(allowed_tools=["Bash", "Read"])
        assert flags == ["--allowedTools", "Bash,Read"]

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    def test_build_tool_flags_copilot(self):
        flags = build_tool_flags(allowed_tools=["Bash", "Read"])
        assert "--allow-tool" in flags
        assert "shell" in flags
        assert "read_file" in flags

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_build_output_flags_claude(self):
        assert build_output_flags("json") == ["--output-format", "json"]

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    def test_build_output_flags_copilot(self):
        # Copilot doesn't support --json, returns empty
        assert build_output_flags("json") == []

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_build_full_command_claude(self):
        cmd = build_full_command(
            prompt="hello",
            allowed_tools=["Bash", "Read"],
            model="opus",
            max_turns=3,
            output_format="json",
        )
        assert cmd[0] == "claude"
        assert "--allowedTools" in cmd
        assert "--output-format" in cmd

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    @patch("app.provider.copilot.shutil.which")
    def test_build_full_command_copilot(self, mock_which):
        mock_which.side_effect = lambda x: "/usr/local/bin/copilot" if x == "copilot" else None
        cmd = build_full_command(
            prompt="hello",
            allowed_tools=["Bash", "Read"],
            model="opus",
            max_turns=3,
            output_format="json",
        )
        assert cmd[0] == "copilot"
        assert "--allow-tool" in cmd
        # Copilot doesn't support --json or --max-turns
        assert "--json" not in cmd
        assert "--max-turns" not in cmd

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.config._load_config", return_value={"skip_permissions": True})
    def test_build_full_command_skip_permissions_enabled(self, _mock_config):
        cmd = build_full_command(prompt="hello")
        assert "--dangerously-skip-permissions" in cmd

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.config._load_config", return_value={"skip_permissions": False})
    def test_build_full_command_skip_permissions_disabled(self, _mock_config):
        cmd = build_full_command(prompt="hello")
        assert "--dangerously-skip-permissions" not in cmd

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.config._load_config", return_value={})
    def test_build_full_command_skip_permissions_default(self, _mock_config):
        cmd = build_full_command(prompt="hello")
        assert "--dangerously-skip-permissions" not in cmd


# ---------------------------------------------------------------------------
# LocalLLMProvider
# ---------------------------------------------------------------------------

class TestLocalLLMProvider:
    """Tests for LocalLLMProvider flag generation."""

    def setup_method(self):
        self.provider = LocalLLMProvider()

    def test_name(self):
        assert self.provider.name == "local"

    def test_binary_is_python(self):
        import sys
        assert self.provider.binary() == sys.executable

    def test_prompt_args(self):
        result = self.provider.build_prompt_args("do the thing")
        assert "-m" in result
        assert "app.local_llm_runner" in result
        assert "-p" in result
        assert "do the thing" in result

    def test_tool_args_allowed(self):
        result = self.provider.build_tool_args(allowed_tools=["Bash", "Read"])
        assert result == ["--allowed-tools", "Bash,Read"]

    def test_tool_args_disallowed(self):
        result = self.provider.build_tool_args(disallowed_tools=["Bash", "Edit"])
        assert result == ["--disallowed-tools", "Bash,Edit"]

    def test_tool_args_empty(self):
        assert self.provider.build_tool_args() == []

    @patch("app.utils.load_config", return_value={"local_llm": {"model": "glm4"}})
    def test_model_args_from_config(self, mock_config):
        """Uses model from config when no explicit model given."""
        result = self.provider.build_model_args()
        assert result == ["--model", "glm4"]

    def test_model_args_explicit(self):
        result = self.provider.build_model_args(model="nemotron-nano")
        assert result == ["--model", "nemotron-nano"]

    def test_model_args_fallback_ignored(self):
        result = self.provider.build_model_args(model="glm4", fallback="sonnet")
        assert result == ["--model", "glm4"]
        assert "--fallback" not in " ".join(result)

    def test_output_args_json(self):
        assert self.provider.build_output_args("json") == ["--output-format", "json"]

    def test_output_args_empty(self):
        assert self.provider.build_output_args() == []

    def test_max_turns_args(self):
        assert self.provider.build_max_turns_args(5) == ["--max-turns", "5"]

    def test_max_turns_args_zero(self):
        assert self.provider.build_max_turns_args(0) == []

    def test_mcp_args_ignored(self):
        """MCP not supported — always returns empty."""
        assert self.provider.build_mcp_args(["config.json"]) == []
        assert self.provider.build_mcp_args() == []

    @patch.dict("os.environ", {
        "KOAN_LOCAL_LLM_BASE_URL": "http://myserver:8080/v1",
        "KOAN_LOCAL_LLM_MODEL": "test-model",
    })
    def test_build_command_full(self):
        import sys
        cmd = self.provider.build_command(
            prompt="analyze code",
            allowed_tools=["Read", "Grep"],
            model="glm4",
            output_format="json",
            max_turns=3,
        )
        assert cmd[0] == sys.executable
        assert "-m" in cmd
        assert "app.local_llm_runner" in cmd
        assert "-p" in cmd
        assert "analyze code" in cmd
        assert "--allowed-tools" in cmd
        assert "Read,Grep" in cmd
        assert "--model" in cmd
        assert "glm4" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--max-turns" in cmd
        assert "3" in cmd
        assert "--base-url" in cmd
        assert "http://myserver:8080/v1" in cmd

    @patch("app.utils.load_config", return_value={"local_llm": {"model": "test"}})
    def test_build_command_minimal(self, mock_config):
        import sys
        cmd = self.provider.build_command(prompt="hello")
        assert cmd[0] == sys.executable
        assert "-p" in cmd
        assert "hello" in cmd
        assert "--base-url" in cmd

    @patch.dict("os.environ", {"KOAN_LOCAL_LLM_BASE_URL": "http://custom:1234/v1"})
    def test_base_url_from_env(self):
        assert self.provider._get_base_url() == "http://custom:1234/v1"

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.utils.load_config", return_value={"local_llm": {"base_url": "http://cfg:5555/v1"}})
    def test_base_url_from_config(self, mock_config):
        os.environ.pop("KOAN_LOCAL_LLM_BASE_URL", None)
        assert self.provider._get_base_url() == "http://cfg:5555/v1"

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.utils.load_config", return_value={})
    def test_base_url_default(self, mock_config):
        os.environ.pop("KOAN_LOCAL_LLM_BASE_URL", None)
        assert self.provider._get_base_url() == "http://localhost:11434/v1"

    @patch.dict("os.environ", {"KOAN_LOCAL_LLM_MODEL": "env-model"})
    def test_model_from_env(self):
        assert self.provider._get_default_model() == "env-model"

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.utils.load_config", return_value={"local_llm": {"model": "cfg-model"}})
    def test_model_from_config(self, mock_config):
        os.environ.pop("KOAN_LOCAL_LLM_MODEL", None)
        assert self.provider._get_default_model() == "cfg-model"

    @patch.dict("os.environ", {"KOAN_LOCAL_LLM_MODEL": "some-model"})
    def test_is_available_with_model(self):
        assert self.provider.is_available()

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.utils.load_config", return_value={})
    def test_not_available_without_model(self, mock_config):
        os.environ.pop("KOAN_LOCAL_LLM_MODEL", None)
        assert not self.provider.is_available()

    @patch.dict("os.environ", {"KOAN_LOCAL_LLM_API_KEY": "sk-test"})
    def test_api_key_from_env(self):
        assert self.provider._get_api_key() == "sk-test"

    @patch.dict("os.environ", {
        "KOAN_LOCAL_LLM_API_KEY": "sk-test",
        "KOAN_LOCAL_LLM_BASE_URL": "http://localhost:11434/v1",
        "KOAN_LOCAL_LLM_MODEL": "test",
    })
    def test_build_command_with_api_key(self):
        cmd = self.provider.build_command(prompt="test", model="test")
        assert "--api-key" in cmd
        assert "sk-test" in cmd

    def test_extra_flags(self):
        result = self.provider.build_extra_flags(
            model="glm4", disallowed_tools=["Bash"]
        )
        assert "--model" in result
        assert "glm4" in result
        assert "--disallowed-tools" in result


# ---------------------------------------------------------------------------
# Provider resolution with local provider
# ---------------------------------------------------------------------------

class TestLocalProviderResolution:
    """Tests for provider resolution with local LLM provider."""

    def setup_method(self):
        reset_provider()

    def teardown_method(self):
        reset_provider()

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "local"})
    def test_env_var_local(self):
        assert get_provider_name() == "local"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "local"})
    def test_get_provider_returns_local(self):
        provider = get_provider()
        assert isinstance(provider, LocalLLMProvider)

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "local"})
    def test_get_cli_binary_local(self):
        import sys
        binary = get_cli_binary()
        assert sys.executable in binary

    @patch.dict("os.environ", {}, clear=False)
    @patch("app.utils.load_config", return_value={"cli_provider": "local"})
    def test_config_yaml_local(self, mock_config):
        os.environ.pop("KOAN_CLI_PROVIDER", None)
        assert get_provider_name() == "local"

    @patch.dict("os.environ", {
        "KOAN_CLI_PROVIDER": "local",
        "KOAN_LOCAL_LLM_BASE_URL": "http://localhost:11434/v1",
        "KOAN_LOCAL_LLM_MODEL": "glm4",
    })
    def test_build_full_command_local(self):
        import sys
        cmd = build_full_command(
            prompt="hello",
            allowed_tools=["Read", "Grep"],
            model="glm4",
            max_turns=3,
            output_format="json",
        )
        assert cmd[0] == sys.executable
        assert "-m" in cmd
        assert "app.local_llm_runner" in cmd
        assert "--allowed-tools" in cmd
        assert "--output-format" in cmd


# ---------------------------------------------------------------------------
# ClaudeProvider.check_quota_available
# ---------------------------------------------------------------------------

class TestClaudeQuotaCheck:
    """Tests for ClaudeProvider.check_quota_available()."""

    def setup_method(self):
        self.provider = ClaudeProvider()

    @patch("app.provider.claude.subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_quota_available(self, mock_detect, mock_run):
        """Returns (True, '') when quota is available."""
        mock_run.return_value = MagicMock(stderr="", stdout="Usage: 50%")
        available, detail = self.provider.check_quota_available("/fake/path")
        assert available is True
        assert detail == ""
        mock_run.assert_called_once()
        mock_detect.assert_called_once()

    @patch("app.provider.claude.subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=True)
    def test_quota_exhausted(self, mock_detect, mock_run):
        """Returns (False, output) when quota is exhausted."""
        mock_run.return_value = MagicMock(
            stderr="Rate limit exceeded",
            stdout="Quota exhausted"
        )
        available, detail = self.provider.check_quota_available("/fake/path")
        assert available is False
        assert "Quota exhausted" in detail

    @patch("app.provider.claude.subprocess.run")
    def test_timeout_returns_available(self, mock_run):
        """Timeout is treated optimistically — proceed as if quota available."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["claude", "usage"], timeout=15)
        available, detail = self.provider.check_quota_available("/fake/path")
        assert available is True
        assert detail == ""

    @patch("app.provider.claude.subprocess.run")
    def test_other_exception_returns_available(self, mock_run):
        """Non-quota exceptions treated optimistically."""
        mock_run.side_effect = OSError("binary not found")
        available, detail = self.provider.check_quota_available("/fake/path")
        assert available is True
        assert detail == ""

    @patch("app.provider.claude.subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_custom_timeout(self, mock_detect, mock_run):
        """Custom timeout is passed to subprocess.run."""
        mock_run.return_value = MagicMock(stderr="", stdout="ok")
        self.provider.check_quota_available("/fake/path", timeout=30)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 30

    @patch("app.provider.claude.subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_uses_project_path_as_cwd(self, mock_detect, mock_run):
        """subprocess.run cwd is set to project_path."""
        mock_run.return_value = MagicMock(stderr="", stdout="ok")
        self.provider.check_quota_available("/my/project")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/my/project"

    @patch("app.provider.claude.subprocess.run")
    @patch("app.quota_handler.detect_quota_exhaustion", return_value=False)
    def test_combines_stderr_and_stdout(self, mock_detect, mock_run):
        """Both stderr and stdout are combined for quota detection."""
        mock_run.return_value = MagicMock(stderr="warning", stdout="usage data")
        self.provider.check_quota_available("/fake/path")
        combined = mock_detect.call_args[0][0]
        assert "warning" in combined
        assert "usage data" in combined


# ---------------------------------------------------------------------------
# Base CLIProvider defaults
# ---------------------------------------------------------------------------

class TestCLIProviderBase:
    """Tests for CLIProvider base class behavior."""

    def test_check_quota_available_default(self):
        """Base implementation always returns available (no quota concept)."""
        from app.provider.base import CLIProvider
        provider = CLIProvider()
        available, detail = provider.check_quota_available("/any/path")
        assert available is True
        assert detail == ""

    def test_shell_command_default(self):
        """shell_command() defaults to binary()."""
        from app.provider.base import CLIProvider

        class TestProvider(CLIProvider):
            def binary(self):
                return "test-bin"

        assert TestProvider().shell_command() == "test-bin"

    def test_is_available_returns_false_for_missing_binary(self):
        """is_available() returns False when binary not found."""
        from app.provider.base import CLIProvider

        class TestProvider(CLIProvider):
            def binary(self):
                return "nonexistent-binary-xyz"

        assert TestProvider().is_available() is False

    def test_build_command_raises_not_implemented(self):
        """Abstract methods raise NotImplementedError."""
        from app.provider.base import CLIProvider
        provider = CLIProvider()
        with pytest.raises(NotImplementedError):
            provider.build_prompt_args("hello")
        with pytest.raises(NotImplementedError):
            provider.build_tool_args()
        with pytest.raises(NotImplementedError):
            provider.build_model_args()
        with pytest.raises(NotImplementedError):
            provider.build_output_args()
        with pytest.raises(NotImplementedError):
            provider.build_max_turns_args()
        with pytest.raises(NotImplementedError):
            provider.build_mcp_args()

    def test_build_extra_flags_delegates(self):
        """build_extra_flags calls build_model_args + build_tool_args."""
        from app.provider.base import CLIProvider

        class TestProvider(CLIProvider):
            def build_model_args(self, model="", fallback=""):
                return ["--model", model] if model else []

            def build_tool_args(self, allowed_tools=None, disallowed_tools=None):
                return ["--no-bash"] if disallowed_tools else []

        result = TestProvider().build_extra_flags(model="opus", disallowed_tools=["Bash"])
        assert result == ["--model", "opus", "--no-bash"]


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------

class TestRunCommand:
    """Tests for the run_command() high-level helper."""

    def setup_method(self):
        reset_provider()

    def teardown_method(self):
        reset_provider()

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.cli_exec.run_cli")
    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": "haiku"})
    def test_success_returns_stripped_stdout(self, mock_models, mock_run):
        """Successful command returns stripped stdout."""
        mock_run.return_value = MagicMock(returncode=0, stdout="  result text  \n")
        result = run_command(
            prompt="analyze this",
            project_path="/fake/project",
            allowed_tools=["Read", "Grep"],
        )
        assert result == "result text"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.cli_exec.run_cli")
    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": "haiku"})
    def test_failure_raises_runtime_error(self, mock_models, mock_run):
        """Non-zero exit raises RuntimeError with stderr snippet."""
        mock_run.return_value = MagicMock(returncode=1, stderr="some error message")
        with pytest.raises(RuntimeError, match="CLI invocation failed"):
            run_command(
                prompt="analyze this",
                project_path="/fake/project",
                allowed_tools=["Read"],
            )

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.cli_exec.run_cli")
    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": "haiku"})
    def test_passes_model_from_config(self, mock_models, mock_run):
        """Uses model from config based on model_key."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_command(
            prompt="test",
            project_path="/fake",
            allowed_tools=["Read"],
            model_key="chat",
        )
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "sonnet"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.cli_exec.run_cli")
    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": "haiku"})
    def test_passes_max_turns(self, mock_models, mock_run):
        """max_turns parameter is forwarded to build_full_command."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_command(
            prompt="test",
            project_path="/fake",
            allowed_tools=["Read"],
            max_turns=5,
        )
        cmd = mock_run.call_args[0][0]
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "5"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.cli_exec.run_cli")
    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": "haiku"})
    def test_passes_cwd_to_run_cli(self, mock_models, mock_run):
        """project_path is passed as cwd to run_cli."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_command(
            prompt="test",
            project_path="/my/project",
            allowed_tools=["Read"],
        )
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/my/project"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.cli_exec.run_cli")
    @patch("app.config.get_model_config", return_value={"chat": "sonnet", "fallback": "haiku"})
    def test_passes_timeout(self, mock_models, mock_run):
        """Custom timeout is forwarded to run_cli."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_command(
            prompt="test",
            project_path="/fake",
            allowed_tools=["Read"],
            timeout=600,
        )
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 600

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    @patch("app.cli_exec.run_cli")
    @patch("app.config.get_model_config", return_value={})
    def test_missing_model_key_uses_empty(self, mock_models, mock_run):
        """Missing model key results in empty model (no --model flag)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        run_command(
            prompt="test",
            project_path="/fake",
            allowed_tools=["Read"],
            model_key="nonexistent",
        )
        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd


# ---------------------------------------------------------------------------
# Plugin directory support (--plugin-dir)
# ---------------------------------------------------------------------------

class TestPluginDirSupport:
    """Tests for --plugin-dir flag support across providers."""

    def setup_method(self):
        reset_provider()

    def teardown_method(self):
        reset_provider()

    def test_claude_build_plugin_args_single(self):
        provider = ClaudeProvider()
        result = provider.build_plugin_args(["/tmp/plugins"])
        assert result == ["--plugin-dir", "/tmp/plugins"]

    def test_claude_build_plugin_args_multiple(self):
        provider = ClaudeProvider()
        result = provider.build_plugin_args(["/tmp/a", "/tmp/b"])
        assert result == ["--plugin-dir", "/tmp/a", "--plugin-dir", "/tmp/b"]

    def test_claude_build_plugin_args_none(self):
        provider = ClaudeProvider()
        assert provider.build_plugin_args(None) == []
        assert provider.build_plugin_args([]) == []

    def test_copilot_build_plugin_args_returns_empty(self):
        """Copilot doesn't support --plugin-dir."""
        provider = CopilotProvider()
        assert provider.build_plugin_args(["/tmp/plugins"]) == []

    def test_local_build_plugin_args_returns_empty(self):
        """Local LLM doesn't support --plugin-dir."""
        provider = LocalLLMProvider()
        assert provider.build_plugin_args(["/tmp/plugins"]) == []

    def test_claude_build_command_includes_plugin_dir(self):
        provider = ClaudeProvider()
        cmd = provider.build_command(
            prompt="hello",
            plugin_dirs=["/tmp/koan-plugins"],
        )
        assert "--plugin-dir" in cmd
        idx = cmd.index("--plugin-dir")
        assert cmd[idx + 1] == "/tmp/koan-plugins"

    def test_claude_build_command_no_plugin_dir(self):
        provider = ClaudeProvider()
        cmd = provider.build_command(prompt="hello")
        assert "--plugin-dir" not in cmd

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_build_full_command_with_plugin_dirs(self):
        cmd = build_full_command(
            prompt="hello",
            plugin_dirs=["/tmp/koan-plugins"],
        )
        assert "--plugin-dir" in cmd
        idx = cmd.index("--plugin-dir")
        assert cmd[idx + 1] == "/tmp/koan-plugins"

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "claude"})
    def test_build_full_command_without_plugin_dirs(self):
        cmd = build_full_command(prompt="hello")
        assert "--plugin-dir" not in cmd

    @patch.dict("os.environ", {"KOAN_CLI_PROVIDER": "copilot"})
    @patch("app.provider.copilot.shutil.which")
    def test_build_full_command_copilot_ignores_plugin_dirs(self, mock_which):
        """Copilot silently ignores plugin_dirs."""
        mock_which.side_effect = lambda x: "/usr/local/bin/copilot" if x == "copilot" else None
        cmd = build_full_command(
            prompt="hello",
            plugin_dirs=["/tmp/koan-plugins"],
        )
        assert "--plugin-dir" not in cmd

    def test_base_provider_build_plugin_args_returns_empty(self):
        """Base CLIProvider returns empty for plugin dirs."""
        from app.provider.base import CLIProvider
        provider = CLIProvider()
        assert provider.build_plugin_args(["/tmp/plugins"]) == []
        assert provider.build_plugin_args(None) == []
