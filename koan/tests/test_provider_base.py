"""Tests for the CLIProvider base class (app.provider.base)."""

from unittest.mock import patch

import pytest

from app.provider.base import CLIProvider, CLAUDE_TOOLS, TOOL_NAME_MAP


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    """Verify canonical tool constants are correct."""

    def test_claude_tools_is_a_set(self):
        assert isinstance(CLAUDE_TOOLS, set)

    def test_claude_tools_contains_core_tools(self):
        expected = {"Bash", "Read", "Write", "Glob", "Grep", "Edit", "Skill"}
        assert CLAUDE_TOOLS == expected

    def test_tool_name_map_is_a_dict(self):
        assert isinstance(TOOL_NAME_MAP, dict)

    def test_tool_name_map_covers_all_claude_tools(self):
        """Every canonical tool should have a mapping."""
        assert set(TOOL_NAME_MAP.keys()) == CLAUDE_TOOLS

    def test_tool_name_map_values_are_lowercase(self):
        for key, val in TOOL_NAME_MAP.items():
            assert val == val.lower(), f"{key} maps to non-lowercase '{val}'"

    def test_tool_name_map_bash_maps_to_shell(self):
        assert TOOL_NAME_MAP["Bash"] == "shell"

    def test_tool_name_map_read_maps_to_read_file(self):
        assert TOOL_NAME_MAP["Read"] == "read_file"

    def test_tool_name_map_write_maps_to_write_file(self):
        assert TOOL_NAME_MAP["Write"] == "write_file"

    def test_tool_name_map_edit_maps_to_edit_file(self):
        assert TOOL_NAME_MAP["Edit"] == "edit_file"

    def test_tool_name_map_glob_maps_to_glob(self):
        assert TOOL_NAME_MAP["Glob"] == "glob"

    def test_tool_name_map_grep_maps_to_grep(self):
        assert TOOL_NAME_MAP["Grep"] == "grep"


# ---------------------------------------------------------------------------
# CLIProvider base class — abstract methods raise NotImplementedError
# ---------------------------------------------------------------------------

class TestAbstractMethods:
    """Abstract methods on the base class must raise NotImplementedError."""

    def setup_method(self):
        self.base = CLIProvider()

    def test_binary_raises(self):
        with pytest.raises(NotImplementedError):
            self.base.binary()

    def test_build_prompt_args_raises(self):
        with pytest.raises(NotImplementedError):
            self.base.build_prompt_args("hello")

    def test_build_tool_args_raises(self):
        with pytest.raises(NotImplementedError):
            self.base.build_tool_args()

    def test_build_model_args_raises(self):
        with pytest.raises(NotImplementedError):
            self.base.build_model_args()

    def test_build_output_args_raises(self):
        with pytest.raises(NotImplementedError):
            self.base.build_output_args()

    def test_build_max_turns_args_raises(self):
        with pytest.raises(NotImplementedError):
            self.base.build_max_turns_args()

    def test_build_mcp_args_raises(self):
        with pytest.raises(NotImplementedError):
            self.base.build_mcp_args()


# ---------------------------------------------------------------------------
# CLIProvider base class — default implementations
# ---------------------------------------------------------------------------

class TestDefaults:
    """Default implementations that subclasses may inherit."""

    def setup_method(self):
        self.base = CLIProvider()

    def test_name_is_empty_string(self):
        assert self.base.name == ""

    def test_build_plugin_args_empty_by_default(self):
        assert self.base.build_plugin_args() == []

    def test_build_plugin_args_with_none(self):
        assert self.base.build_plugin_args(None) == []

    def test_build_plugin_args_with_empty_list(self):
        assert self.base.build_plugin_args([]) == []

    def test_check_quota_available_returns_true(self):
        available, detail = self.base.check_quota_available("/tmp/test")
        assert available is True
        assert detail == ""

    def test_check_quota_available_with_timeout(self):
        available, detail = self.base.check_quota_available("/tmp/test", timeout=30)
        assert available is True
        assert detail == ""

    def test_shell_command_delegates_to_binary(self):
        """shell_command() calls binary() — which raises on the bare base class."""
        with pytest.raises(NotImplementedError):
            self.base.shell_command()

    def test_is_available_calls_which_on_binary(self):
        """is_available() calls shutil.which(binary()) — binary() raises."""
        with pytest.raises(NotImplementedError):
            self.base.is_available()


# ---------------------------------------------------------------------------
# Concrete stub for testing composition methods
# ---------------------------------------------------------------------------

class StubProvider(CLIProvider):
    """Minimal concrete provider for testing build_command/build_extra_flags."""

    name = "stub"

    def binary(self):
        return "stub-cli"

    def build_prompt_args(self, prompt):
        return ["-p", prompt]

    def build_tool_args(self, allowed_tools=None, disallowed_tools=None):
        flags = []
        if allowed_tools:
            flags.extend(["--allow", ",".join(allowed_tools)])
        if disallowed_tools:
            flags.extend(["--deny", ",".join(disallowed_tools)])
        return flags

    def build_model_args(self, model="", fallback=""):
        flags = []
        if model:
            flags.extend(["--model", model])
        if fallback:
            flags.extend(["--fallback", fallback])
        return flags

    def build_output_args(self, fmt=""):
        if fmt:
            return ["--output", fmt]
        return []

    def build_max_turns_args(self, max_turns=0):
        if max_turns > 0:
            return ["--turns", str(max_turns)]
        return []

    def build_mcp_args(self, configs=None):
        if not configs:
            return []
        return ["--mcp"] + configs


class TestBuildCommand:
    """Test build_command() composition on a concrete provider."""

    def setup_method(self):
        self.provider = StubProvider()

    def test_minimal_command(self):
        cmd = self.provider.build_command(prompt="hello")
        assert cmd == ["stub-cli", "-p", "hello"]

    def test_full_command(self):
        cmd = self.provider.build_command(
            prompt="go",
            allowed_tools=["Bash"],
            model="gpt-4",
            fallback="gpt-3.5",
            output_format="json",
            max_turns=5,
            mcp_configs=["config.json"],
        )
        assert cmd == [
            "stub-cli",
            "-p", "go",
            "--allow", "Bash",
            "--model", "gpt-4",
            "--fallback", "gpt-3.5",
            "--output", "json",
            "--turns", "5",
            "--mcp", "config.json",
        ]

    def test_disallowed_tools(self):
        cmd = self.provider.build_command(
            prompt="test",
            disallowed_tools=["Edit", "Write"],
        )
        assert "--deny" in cmd
        assert "Edit,Write" in cmd

    def test_no_optional_flags_when_defaults(self):
        """Default parameters should produce no extra flags."""
        cmd = self.provider.build_command(prompt="x")
        assert cmd == ["stub-cli", "-p", "x"]

    def test_plugin_dirs_appended(self):
        # StubProvider inherits base build_plugin_args (returns [])
        cmd = self.provider.build_command(
            prompt="test",
            plugin_dirs=["/path/a", "/path/b"],
        )
        # Base build_plugin_args returns [] — no plugin flags
        assert cmd == ["stub-cli", "-p", "test"]

    def test_empty_mcp_configs(self):
        cmd = self.provider.build_command(prompt="x", mcp_configs=[])
        assert "--mcp" not in cmd

    def test_zero_max_turns(self):
        cmd = self.provider.build_command(prompt="x", max_turns=0)
        assert "--turns" not in cmd

    def test_empty_model(self):
        cmd = self.provider.build_command(prompt="x", model="")
        assert "--model" not in cmd


class TestBuildExtraFlags:
    """Test build_extra_flags() composition."""

    def setup_method(self):
        self.provider = StubProvider()

    def test_empty_when_no_args(self):
        flags = self.provider.build_extra_flags()
        assert flags == []

    def test_model_only(self):
        flags = self.provider.build_extra_flags(model="big-model")
        assert flags == ["--model", "big-model"]

    def test_model_and_fallback(self):
        flags = self.provider.build_extra_flags(model="big", fallback="small")
        assert flags == ["--model", "big", "--fallback", "small"]

    def test_disallowed_tools_only(self):
        flags = self.provider.build_extra_flags(
            disallowed_tools=["Edit"],
        )
        assert flags == ["--deny", "Edit"]

    def test_model_and_disallowed(self):
        flags = self.provider.build_extra_flags(
            model="fast",
            disallowed_tools=["Write", "Bash"],
        )
        assert flags == ["--model", "fast", "--deny", "Write,Bash"]


# ---------------------------------------------------------------------------
# shell_command / is_available with a concrete provider
# ---------------------------------------------------------------------------

class TestShellCommandAndAvailability:
    """Test shell_command and is_available on a concrete provider."""

    def test_shell_command_defaults_to_binary(self):
        p = StubProvider()
        assert p.shell_command() == "stub-cli"

    @patch("shutil.which", return_value="/usr/bin/stub-cli")
    def test_is_available_true_when_binary_found(self, mock_which):
        p = StubProvider()
        assert p.is_available() is True
        mock_which.assert_called_once_with("stub-cli")

    @patch("shutil.which", return_value=None)
    def test_is_available_false_when_binary_missing(self, mock_which):
        p = StubProvider()
        assert p.is_available() is False
        mock_which.assert_called_once_with("stub-cli")


# ---------------------------------------------------------------------------
# Provider with build_plugin_args override
# ---------------------------------------------------------------------------

class PluginProvider(StubProvider):
    """Provider that actually supports plugin dirs."""

    def build_plugin_args(self, plugin_dirs=None):
        if not plugin_dirs:
            return []
        flags = []
        for d in plugin_dirs:
            flags.extend(["--plugin-dir", d])
        return flags


class TestPluginArgsOverride:
    """Test that build_command respects overridden build_plugin_args."""

    def test_plugin_dirs_in_command(self):
        p = PluginProvider()
        cmd = p.build_command(
            prompt="go",
            plugin_dirs=["/a", "/b"],
        )
        assert "--plugin-dir" in cmd
        assert "/a" in cmd
        assert "/b" in cmd

    def test_no_plugin_dirs(self):
        p = PluginProvider()
        cmd = p.build_command(prompt="go")
        assert "--plugin-dir" not in cmd
