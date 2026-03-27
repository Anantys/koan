"""
CLI provider abstraction — backward-compatible re-export facade.

All implementation lives in the app.provider package:
    app/provider/base.py    — CLIProvider base class + tool constants
    app/provider/claude.py  — ClaudeProvider
    app/provider/codex.py   — CodexProvider
    app/provider/copilot.py — CopilotProvider
    app/provider/local.py   — LocalLLMProvider
    app/provider/ollama_launch.py — OllamaLaunchProvider
    app/provider/__init__.py — Registry, resolution, convenience functions

This module re-exports everything so existing imports from
``app.cli_provider`` continue to work unchanged.
"""

from app.provider import (  # noqa: F401
    # Base class and constants
    CLIProvider,
    CLAUDE_TOOLS,
    TOOL_NAME_MAP,
    # Concrete providers
    ClaudeProvider,
    CodexProvider,
    CopilotProvider,
    LocalLLMProvider,
    OllamaLaunchProvider,
    # Registry & resolution
    get_provider_name,
    get_provider,
    get_cli_binary,
    reset_provider,
    # Convenience functions
    build_cli_flags,
    build_tool_flags,
    build_prompt_flags,
    build_output_flags,
    build_max_turns_flags,
    build_full_command,
    run_command,
    run_command_streaming,
)
