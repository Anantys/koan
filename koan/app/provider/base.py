"""Base class and constants for CLI provider abstraction."""

import shutil
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Tool name mapping: Kōan canonical -> provider-specific
# ---------------------------------------------------------------------------

# Claude Code tool names (canonical, used throughout koan codebase)
CLAUDE_TOOLS = {"Bash", "Read", "Write", "Glob", "Grep", "Edit", "Skill"}

# Mapping from Kōan canonical tool names to OpenAI-style function names.
# Used by Copilot provider (--allow-tool) and local LLM runner (function calling).
TOOL_NAME_MAP = {
    "Bash": "shell",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Glob": "glob",
    "Grep": "grep",
    "Skill": "skill",
}


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class CLIProvider:
    """Base class for CLI provider abstraction.

    A provider knows:
    - What binary to invoke
    - How to translate generic flags into provider-specific CLI args
    """

    name: str = ""

    def binary(self) -> str:
        """Return the CLI binary name or path."""
        raise NotImplementedError

    def shell_command(self) -> str:
        """Return the full command prefix for shell scripts.

        Defaults to binary(), but providers that need a multi-word command
        (e.g., "gh copilot") should override this.
        """
        return self.binary()

    def is_available(self) -> bool:
        """Check if the binary is installed and accessible."""
        return shutil.which(self.binary()) is not None

    def build_prompt_args(self, prompt: str) -> List[str]:
        """Build args for passing a prompt to the CLI."""
        raise NotImplementedError

    def build_system_prompt_args(self, system_prompt: str) -> List[str]:
        """Build args for passing a system prompt to the CLI.

        Base implementation prepends system prompt to the user prompt by
        returning empty — callers must handle the fallback. Providers that
        support a dedicated system prompt flag should override this.
        """
        return []

    def build_tool_args(
        self,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
    ) -> List[str]:
        """Build args for tool access control.

        Args:
            allowed_tools: Explicit list of allowed tools (Claude names).
            disallowed_tools: Tools to block (Claude names).
        """
        raise NotImplementedError

    def build_model_args(
        self,
        model: str = "",
        fallback: str = "",
    ) -> List[str]:
        """Build args for model selection."""
        raise NotImplementedError

    def build_output_args(self, fmt: str = "") -> List[str]:
        """Build args for output format (e.g., 'json')."""
        raise NotImplementedError

    def build_max_turns_args(self, max_turns: int = 0) -> List[str]:
        """Build args for conversation turn limit."""
        raise NotImplementedError

    def build_mcp_args(self, configs: Optional[List[str]] = None) -> List[str]:
        """Build args for MCP server configuration."""
        raise NotImplementedError

    def build_plugin_args(self, plugin_dirs: Optional[List[str]] = None) -> List[str]:
        """Build args for plugin directory loading.

        Args:
            plugin_dirs: Paths to plugin directories to load.

        Returns:
            CLI flags list. Base implementation returns empty (not supported).
        """
        return []

    def build_permission_args(self, skip_permissions: bool = False) -> List[str]:
        """Build args for permission skipping.

        Base implementation returns empty — only Claude provider supports this.
        """
        return []

    def build_command(
        self,
        prompt: str,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        model: str = "",
        fallback: str = "",
        output_format: str = "",
        max_turns: int = 0,
        mcp_configs: Optional[List[str]] = None,
        plugin_dirs: Optional[List[str]] = None,
        skip_permissions: bool = False,
        system_prompt: str = "",
    ) -> List[str]:
        """Build a complete CLI command from generic parameters.

        Args:
            prompt: User prompt text.
            system_prompt: Optional system prompt text. When provided and the
                provider supports it, sent via a dedicated flag (e.g.,
                ``--append-system-prompt``). Otherwise prepended to *prompt*.

        Returns a list of strings suitable for subprocess.run().
        """
        # If system_prompt is set but provider doesn't support it natively,
        # prepend to user prompt as fallback.
        sys_args = self.build_system_prompt_args(system_prompt) if system_prompt else []
        if system_prompt and not sys_args:
            prompt = system_prompt + "\n\n" + prompt

        cmd = [self.binary()]
        cmd.extend(self.build_permission_args(skip_permissions))
        cmd.extend(sys_args)
        cmd.extend(self.build_prompt_args(prompt))
        cmd.extend(self.build_tool_args(allowed_tools, disallowed_tools))
        cmd.extend(self.build_model_args(model, fallback))
        cmd.extend(self.build_output_args(output_format))
        cmd.extend(self.build_max_turns_args(max_turns))
        cmd.extend(self.build_mcp_args(mcp_configs))
        cmd.extend(self.build_plugin_args(plugin_dirs))
        return cmd

    def check_quota_available(self, project_path: str, timeout: int = 15) -> Tuple[bool, str]:
        """Probe real API quota with a minimal CLI call.

        Returns (available: bool, error_detail: str).
        Base implementation returns (True, '') — no check needed
        (e.g. local/ollama providers have no quota).
        """
        return True, ""

    def build_extra_flags(
        self,
        model: str = "",
        fallback: str = "",
        disallowed_tools: Optional[List[str]] = None,
    ) -> List[str]:
        """Build extra flags (model + tool restrictions) for appending to a command.

        This is the provider-aware replacement for utils.build_claude_flags().
        """
        flags: List[str] = []
        flags.extend(self.build_model_args(model, fallback))
        flags.extend(self.build_tool_args(disallowed_tools=disallowed_tools))
        return flags
