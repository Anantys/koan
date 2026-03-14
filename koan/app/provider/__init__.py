"""
CLI provider abstraction for Kōan.

Allows switching between Claude Code CLI, GitHub Copilot CLI,
or a local LLM server as the underlying AI agent binary. Each
provider knows how to translate Kōan's generic command spec into
provider-specific flags.

Configuration:
    config.yaml:  cli_provider: "claude"   (default)
    env var:      KOAN_CLI_PROVIDER=copilot (overrides config.yaml)

Package structure:
    provider/base.py         — CLIProvider base class + tool constants
    provider/claude.py       — ClaudeProvider implementation
    provider/copilot.py      — CopilotProvider implementation
    provider/local.py        — LocalLLMProvider implementation
    provider/ollama_launch.py — OllamaLaunchProvider (ollama launch claude)
    provider/__init__.py     — Registry, resolution, convenience functions
"""

import os
import subprocess
import sys
from typing import List, Optional

# Re-export base class and constants for convenience
from app.provider.base import (  # noqa: F401
    CLIProvider,
    CLAUDE_TOOLS,
    TOOL_NAME_MAP,
)

# Import concrete providers
from app.provider.claude import ClaudeProvider  # noqa: F401
from app.provider.copilot import CopilotProvider  # noqa: F401
from app.provider.local import LocalLLMProvider  # noqa: F401
from app.provider.ollama_launch import OllamaLaunchProvider  # noqa: F401


# ---------------------------------------------------------------------------
# Provider registry & resolution
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "claude": ClaudeProvider,
    "copilot": CopilotProvider,
    "local": LocalLLMProvider,
    "ollama-launch": OllamaLaunchProvider,
}

# Cached provider instance (reset with reset_provider() in tests)
_cached_provider: Optional[CLIProvider] = None
_cached_provider_name: str = ""


def reset_provider():
    """Reset the cached provider (for testing)."""
    global _cached_provider, _cached_provider_name
    _cached_provider = None
    _cached_provider_name = ""


def get_provider_name() -> str:
    """Determine which CLI provider to use.

    Resolution order:
    1. KOAN_CLI_PROVIDER env var (with CLI_PROVIDER fallback; highest priority)
    2. config.yaml cli_provider key
    3. Default: "claude"
    """
    # Lazy import to avoid circular dependency
    from app.utils import get_cli_provider_env, load_config

    env_val = get_cli_provider_env()
    if env_val and env_val in _PROVIDERS:
        return env_val

    try:
        config = load_config()
        config_val = str(config.get("cli_provider", "")).strip().lower()
        if config_val and config_val in _PROVIDERS:
            return config_val
    except Exception as e:
        print(f"[provider] Config loading failed: {e}", file=sys.stderr)

    return "claude"


def get_provider() -> CLIProvider:
    """Get the configured CLI provider instance (cached singleton)."""
    global _cached_provider, _cached_provider_name
    name = get_provider_name()
    if _cached_provider is None or name != _cached_provider_name:
        _cached_provider = _PROVIDERS[name]()
        _cached_provider_name = name
    return _cached_provider


def get_cli_binary() -> str:
    """Get the CLI binary command for the configured provider.

    For shell scripts: returns the full command prefix needed to invoke
    the provider (e.g., "claude" or "copilot" or "gh copilot").
    """
    return get_provider().shell_command()


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def build_cli_flags(
    model: str = "",
    fallback: str = "",
    disallowed_tools: Optional[List[str]] = None,
) -> List[str]:
    """Build extra CLI flags for the configured provider.

    Drop-in replacement for utils.build_claude_flags() that respects
    the configured CLI provider.
    """
    return get_provider().build_extra_flags(model, fallback, disallowed_tools)


def build_tool_flags(
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
) -> List[str]:
    """Build tool access flags for the configured provider.

    Translates Claude-style tool names (Bash, Read, Write, etc.) into
    provider-specific flags.
    """
    return get_provider().build_tool_args(allowed_tools, disallowed_tools)


def build_prompt_flags(prompt: str) -> List[str]:
    """Build prompt flags for the configured provider.

    Returns ["-p", prompt] for Claude, or ["copilot", "-p", prompt] for gh mode.
    """
    return get_provider().build_prompt_args(prompt)


def build_output_flags(fmt: str = "") -> List[str]:
    """Build output format flags for the configured provider."""
    return get_provider().build_output_args(fmt)


def build_max_turns_flags(max_turns: int = 0) -> List[str]:
    """Build max-turns flags for the configured provider."""
    return get_provider().build_max_turns_args(max_turns)


def build_full_command(
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
    """Build a complete CLI command for the configured provider.

    This is the high-level API: pass generic parameters, get back a
    provider-specific command list ready for subprocess.run().

    Automatically reads ``skip_permissions`` from config.yaml so all
    callers get the flag without needing changes.
    """
    from app.config import get_skip_permissions

    return get_provider().build_command(
        prompt=prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=model,
        fallback=fallback,
        output_format=output_format,
        max_turns=max_turns,
        mcp_configs=mcp_configs,
        plugin_dirs=plugin_dirs,
        skip_permissions=get_skip_permissions(),
    )


def run_command(
    prompt: str,
    project_path: str,
    allowed_tools: List[str],
    model_key: str = "chat",
    max_turns: int = 10,
    timeout: int = 300,
) -> str:
    """Build and run a CLI command, returning stripped stdout.

    Higher-level helper for runner modules that need to invoke the
    configured CLI provider with a prompt and get back text output.
    Combines build_full_command + subprocess execution + error handling.

    Raises:
        RuntimeError: If the command exits with non-zero code.
    """
    from app.config import get_model_config

    models = get_model_config()
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=allowed_tools,
        model=models.get(model_key, ""),
        fallback=models.get("fallback", ""),
        max_turns=max_turns,
    )

    from app.cli_exec import run_cli_with_retry

    result = run_cli_with_retry(
        cmd,
        capture_output=True, text=True, timeout=timeout,
        cwd=project_path,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"CLI invocation failed: {result.stderr[:300]}"
        )

    from app.claude_step import strip_cli_noise
    return strip_cli_noise(result.stdout.strip())
