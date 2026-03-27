"""Local LLM provider implementation via OpenAI-compatible API."""

import os
import sys
from typing import List, Optional

from app.provider.base import CLIProvider


class LocalLLMProvider(CLIProvider):
    """Local LLM provider via OpenAI-compatible API.

    Uses the local_llm_runner.py agentic loop to provide tool-using
    agent capabilities with any local LLM server (Ollama, llama.cpp,
    LM Studio, vLLM, or any OpenAI-compatible endpoint).

    Configuration (config.yaml):
        cli_provider: "local"
        local_llm:
            base_url: "http://localhost:11434/v1"  # Ollama default
            model: "glm4:latest"
            api_key: ""  # Usually empty for local servers

    Key differences from Claude/Copilot:
        - No external binary: runs local_llm_runner.py via Python
        - Tool use via OpenAI function calling protocol
        - --fallback-model not supported (local server serves one model)
        - MCP configs not supported (tools are built-in)
    """

    name = "local"

    def _get_config(self) -> dict:
        """Get local_llm config section from config.yaml."""
        try:
            from app.utils import load_config
            config = load_config()
            return config.get("local_llm", {})
        except (OSError, ValueError, ImportError):
            return {}

    def _get_setting(self, env_key: str, config_key: str, default: str = "") -> str:
        """Resolve a setting: env var > config.yaml > default."""
        env_val = os.environ.get(env_key, "")
        if env_val:
            return env_val
        return self._get_config().get(config_key, default)

    def _get_base_url(self) -> str:
        return self._get_setting("KOAN_LOCAL_LLM_BASE_URL", "base_url", "http://localhost:11434/v1")

    def _get_default_model(self) -> str:
        return self._get_setting("KOAN_LOCAL_LLM_MODEL", "model")

    def _get_api_key(self) -> str:
        return self._get_setting("KOAN_LOCAL_LLM_API_KEY", "api_key")

    def binary(self) -> str:
        return sys.executable

    def shell_command(self) -> str:
        return f"{self.binary()} -m app.local_llm_runner"

    def is_available(self) -> bool:
        """Check if local LLM is configured (model name set)."""
        return bool(self._get_default_model())

    def build_prompt_args(self, prompt: str) -> List[str]:
        return ["-m", "app.local_llm_runner", "-p", prompt]

    def build_tool_args(
        self,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
    ) -> List[str]:
        flags: List[str] = []
        if allowed_tools:
            flags.extend(["--allowed-tools", ",".join(allowed_tools)])
        if disallowed_tools:
            flags.extend(["--disallowed-tools", ",".join(disallowed_tools)])
        return flags

    def build_model_args(self, model: str = "", fallback: str = "") -> List[str]:
        flags: List[str] = []
        effective_model = model or self._get_default_model()
        if effective_model:
            flags.extend(["--model", effective_model])
        # Fallback not supported by local LLM — silently ignored
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
        # MCP not supported by local LLM runner — tools are built-in
        return []

    def build_command(self, prompt: str, **kwargs) -> List[str]:
        """Build a complete command to run the local LLM agent."""
        cmd = super().build_command(prompt, **kwargs)
        cmd.extend(["--base-url", self._get_base_url()])
        api_key = self._get_api_key()
        if api_key:
            cmd.extend(["--api-key", api_key])
        return cmd
