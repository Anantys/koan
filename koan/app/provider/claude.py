"""Claude Code CLI provider implementation."""

import subprocess
from typing import List, Optional, Tuple

from app.provider.base import CLIProvider


class ClaudeProvider(CLIProvider):
    """Claude Code CLI provider."""

    name = "claude"

    def binary(self) -> str:
        return "claude"

    def build_permission_args(self, skip_permissions: bool = False) -> List[str]:
        if skip_permissions:
            return ["--dangerously-skip-permissions"]
        return []

    def build_system_prompt_args(self, system_prompt: str) -> List[str]:
        if system_prompt:
            return ["--append-system-prompt", system_prompt]
        return []

    def build_prompt_args(self, prompt: str) -> List[str]:
        return ["-p", prompt]

    def build_tool_args(
        self,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
    ) -> List[str]:
        flags: List[str] = []
        if allowed_tools:
            flags.extend(["--allowedTools", ",".join(allowed_tools)])
        if disallowed_tools:
            flags.extend(["--disallowedTools"] + disallowed_tools)
        return flags

    def build_model_args(self, model: str = "", fallback: str = "") -> List[str]:
        flags: List[str] = []
        if model:
            flags.extend(["--model", model])
        if fallback and fallback != model:
            flags.extend(["--fallback-model", fallback])
        return flags

    def build_output_args(self, fmt: str = "") -> List[str]:
        if fmt:
            return ["--output-format", fmt]
        return []

    def build_max_turns_args(self, max_turns: int = 0) -> List[str]:
        if max_turns > 0:
            return ["--max-turns", str(max_turns)]
        return []

    def build_mcp_args(self, configs: Optional[List[str]] = None) -> List[str]:
        if not configs:
            return []
        flags: List[str] = []
        for config_path in configs:
            flags.extend(["--mcp-config", config_path])
        return flags

    def build_plugin_args(self, plugin_dirs: Optional[List[str]] = None) -> List[str]:
        if not plugin_dirs:
            return []
        flags: List[str] = []
        for d in plugin_dirs:
            flags.extend(["--plugin-dir", d])
        return flags

    def check_quota_available(self, project_path: str, timeout: int = 15) -> Tuple[bool, str]:
        """Check Claude API quota via ``claude usage`` (no tokens consumed).

        Runs ``claude usage`` and checks the output for quota exhaustion
        signals. Unlike a prompt-based probe, this costs zero tokens.
        """
        cmd = [self.binary(), "usage"]
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
            return True, ""
        except subprocess.TimeoutExpired:
            # Timeout — proceed optimistically
            return True, ""
        except (subprocess.SubprocessError, OSError, ImportError):
            # Non-quota error — proceed optimistically
            return True, ""
