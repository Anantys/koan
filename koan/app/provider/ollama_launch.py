"""Ollama Launch provider — delegates to 'ollama launch claude'.

Uses Ollama v0.16.0+ ``ollama launch claude`` integration to run Claude
Code CLI through a local Ollama server.  This is simpler than manual
env-var configuration: Ollama handles ``ANTHROPIC_BASE_URL`` setup and
server lifecycle internally.

Command structure::

    ollama launch claude --model <model> -- -p <prompt> --allowedTools ...

Everything before ``--`` is Ollama's responsibility (model selection,
server management).  Everything after ``--`` is passed through to the
Claude Code CLI verbatim.
"""

import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

from app.provider.base import CLIProvider


class OllamaLaunchProvider(CLIProvider):
    """Provider that uses ``ollama launch claude`` to run Claude Code.

    Advantages over manual OllamaClaudeProvider:
    - No manual env-var setup (ANTHROPIC_BASE_URL, etc.)
    - Ollama auto-starts the server if needed
    - Native integration maintained by Ollama upstream
    - Model validated by Ollama before launch

    Configuration (config.yaml)::

        cli_provider: "ollama-launch"
        ollama_launch:
            model: "qwen2.5-coder:14b"

    Or via environment::

        KOAN_CLI_PROVIDER=ollama-launch
        KOAN_OLLAMA_LAUNCH_MODEL=qwen2.5-coder:14b
    """

    name = "ollama-launch"

    def _get_config(self) -> dict:
        """Get ollama_launch config section from config.yaml."""
        try:
            from app.utils import load_config
            config = load_config()
            return config.get("ollama_launch", {})
        except Exception as e:
            print(f"[ollama-launch] config loading failed: {e}", file=sys.stderr)
            return {}

    def _get_setting(self, env_key: str, config_key: str, default: str = "") -> str:
        """Resolve a setting: env var > config.yaml > default."""
        env_val = os.environ.get(env_key, "")
        if env_val:
            return env_val
        return self._get_config().get(config_key, default)

    def _get_default_model(self) -> str:
        return self._get_setting(
            "KOAN_OLLAMA_LAUNCH_MODEL", "model", ""
        )

    def binary(self) -> str:
        return "ollama"

    def shell_command(self) -> str:
        return "ollama launch claude"

    def is_available(self) -> bool:
        """Check that ollama binary exists and is v0.16.0+."""
        return shutil.which("ollama") is not None

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
        # Model is handled by ollama --model flag, not Claude --model
        # So we don't add anything here — it's injected in build_command()
        return []

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
        flags = ["--mcp-config"]
        flags.extend(configs)
        return flags

    def build_plugin_args(self, plugin_dirs: Optional[List[str]] = None) -> List[str]:
        if not plugin_dirs:
            return []
        flags: List[str] = []
        for d in plugin_dirs:
            flags.extend(["--plugin-dir", d])
        return flags

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
    ) -> List[str]:
        """Build: ollama launch claude --model X -- <claude-flags>.

        The ``--`` separator divides Ollama args from Claude Code args.
        """
        # Ollama part: binary + launch subcommand + model
        cmd = ["ollama", "launch", "claude"]
        effective_model = model or self._get_default_model()
        if effective_model:
            cmd.extend(["--model", effective_model])

        # Separator between ollama args and Claude Code args
        cmd.append("--")

        # Claude Code part: all flags passed through verbatim
        cmd.extend(self.build_prompt_args(prompt))
        cmd.extend(self.build_tool_args(allowed_tools, disallowed_tools))
        cmd.extend(self.build_output_args(output_format))
        cmd.extend(self.build_max_turns_args(max_turns))
        cmd.extend(self.build_mcp_args(mcp_configs))
        cmd.extend(self.build_plugin_args(plugin_dirs))
        return cmd

    def get_env(self) -> Dict[str, str]:
        """No extra env vars needed — ollama handles everything."""
        return {}

    def check_quota_available(self, project_path: str, timeout: int = 15) -> Tuple[bool, str]:
        """Local models have no API quota — always available."""
        return True, ""
