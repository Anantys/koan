"""GitHub Copilot CLI provider implementation."""

import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

from app.provider.base import CLIProvider, CLAUDE_TOOLS, TOOL_NAME_MAP


class CopilotProvider(CLIProvider):
    """GitHub Copilot CLI provider.

    Translates Claude Code flags into Copilot CLI equivalents.

    Key differences from Claude CLI:
    - Binary: 'copilot' (standalone) or 'gh copilot' (via gh)
    - Tool control: --allow-tool 'tool_name' (per tool) or --allow-all-tools
    - No --disallowedTools equivalent (use explicit allow-list instead)
    - Model: --model flag (same as Claude)
    - No --fallback-model equivalent
    - Output: --json flag instead of --output-format json
    - MCP: supported via config files
    """

    name = "copilot"

    def __init__(self):
        self._has_copilot = shutil.which("copilot") is not None
        self._has_gh = shutil.which("gh") is not None

    def binary(self) -> str:
        # Prefer standalone 'copilot' binary, fallback to 'gh'
        if self._has_copilot:
            return "copilot"
        return "gh"

    def shell_command(self) -> str:
        return "gh copilot" if self._is_gh_mode else "copilot"

    def is_available(self) -> bool:
        return self._has_copilot or self._has_gh

    @property
    def _is_gh_mode(self) -> bool:
        """True when using 'gh copilot' instead of standalone 'copilot'."""
        return not self._has_copilot and self._has_gh

    def build_prompt_args(self, prompt: str) -> List[str]:
        prefix = ["copilot"] if self._is_gh_mode else []
        return prefix + ["-p", prompt]

    def build_tool_args(
        self,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
    ) -> List[str]:
        flags: List[str] = []

        if allowed_tools:
            # Check if all canonical tools are allowed -> use --allow-all-tools
            if set(allowed_tools) >= CLAUDE_TOOLS:
                flags.append("--allow-all-tools")
            else:
                for tool in allowed_tools:
                    copilot_name = TOOL_NAME_MAP.get(tool, tool.lower())
                    flags.extend(["--allow-tool", copilot_name])

        # Copilot doesn't have --disallowedTools.
        # If we have disallowed tools, we compute the inverse:
        # allowed = ALL_TOOLS - disallowed
        if disallowed_tools and not allowed_tools:
            remaining = CLAUDE_TOOLS - set(disallowed_tools)
            for tool in sorted(remaining):
                copilot_name = TOOL_NAME_MAP.get(tool, tool.lower())
                flags.extend(["--allow-tool", copilot_name])

        return flags

    def build_model_args(self, model: str = "", fallback: str = "") -> List[str]:
        flags: List[str] = []
        if model:
            flags.extend(["--model", model])
        # Copilot has no --fallback-model; ignored silently
        return flags

    def build_output_args(self, fmt: str = "") -> List[str]:
        # Copilot CLI does not support --json or --output-format flags
        # Output is always plain text (we parse it in post-processing if needed)
        if fmt:
            import os
            if os.environ.get("KOAN_DEBUG") or os.environ.get("DEBUG"):
                import sys
                print(f"[DEBUG] Copilot provider: output format '{fmt}' requested but not supported",
                      file=sys.stderr)
        return []

    def build_max_turns_args(self, max_turns: int = 0) -> List[str]:
        # Copilot CLI does not support --max-turns flag
        # The conversation naturally ends when the model's response is complete
        if max_turns > 0:
            # Debug mode: warn that max-turns is requested but not supported
            import os
            if os.environ.get("KOAN_DEBUG") or os.environ.get("DEBUG"):
                import sys
                print(f"[DEBUG] Copilot provider: max-turns={max_turns} requested but not supported", 
                      file=sys.stderr)
        return []

    def build_mcp_args(self, configs: Optional[List[str]] = None) -> List[str]:
        if not configs:
            return []
        # Copilot supports MCP config files (same format)
        flags = ["--mcp-config"]
        flags.extend(configs)
        return flags

    def check_quota_available(self, project_path: str, timeout: int = 15) -> Tuple[bool, str]:
        """Check Copilot API quota via a minimal prompt probe.

        Unlike Claude's ``claude usage`` (zero-cost), Copilot CLI has no
        free usage endpoint.  We send a tiny prompt ("ok") which costs
        negligible tokens but reliably surfaces rate-limit / subscription
        errors before a full mission is attempted.
        """
        cmd = [self.binary()]
        cmd.extend(self.build_prompt_args("ok"))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=project_path,
            )
            combined = (result.stderr or "") + "\n" + (result.stdout or "")

            from app.quota_handler import detect_quota_exhaustion

            if detect_quota_exhaustion(combined):
                return False, combined

            # Non-zero exit with no detected pattern — could be auth failure
            # or other transient issue.  Proceed optimistically.
            return True, ""
        except subprocess.TimeoutExpired:
            return True, ""
        except Exception as e:
            print(f"[copilot] quota probe error: {e}", file=sys.stderr)
            return True, ""
