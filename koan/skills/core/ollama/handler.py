"""Kōan ollama skill — manage Ollama models and server status.

Subcommands:
    /ollama           — Show server status + loaded models
    /ollama list      — List locally available models
    /ollama pull NAME — Download a model
    /ollama remove NAME — Delete a local model
    /ollama show NAME — Show model details
    /ollama help      — Show available subcommands

Only works when the configured CLI provider uses Ollama
(local, ollama, or ollama-launch).
"""

OLLAMA_PROVIDERS = ("local", "ollama", "ollama-launch")

HELP_TEXT = """Ollama commands:
/ollama — Server status
/ollama list — Available models
/ollama pull <name> — Download model
/ollama remove <name> — Delete model
/ollama show <name> — Model details
/ollama help — This message"""


def _get_provider_name() -> str:
    """Get the configured CLI provider name."""
    try:
        from app.provider import get_provider_name
        return get_provider_name()
    except Exception:
        return ""


def handle(ctx):
    """Dispatch to the appropriate subcommand."""
    provider = _get_provider_name()
    if provider not in OLLAMA_PROVIDERS:
        return f"Ollama commands require an Ollama-based provider (current: {provider})"

    args = (ctx.args or "").strip()
    parts = args.split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    subcmd_args = parts[1].strip() if len(parts) > 1 else ""

    if subcmd in ("", "status"):
        return _handle_status()
    elif subcmd in ("list", "ls", "models"):
        return _handle_list()
    elif subcmd == "pull":
        return _handle_pull(subcmd_args)
    elif subcmd in ("remove", "rm", "delete"):
        return _handle_remove(subcmd_args)
    elif subcmd in ("show", "info"):
        return _handle_show(subcmd_args)
    elif subcmd == "help":
        return HELP_TEXT
    else:
        return f"Unknown subcommand: {subcmd}\n\n{HELP_TEXT}"


def _handle_status() -> str:
    """Show Ollama server status."""
    from app.ollama_client import is_server_running, get_version, list_running, list_models

    if not is_server_running():
        return "Ollama server is not running.\nStart with: ollama serve"

    lines = ["Ollama Server"]

    version = get_version()
    if version:
        lines.append(f"  Version: {version}")

    lines.append(f"  Status: running")

    # Available models count
    ok, models = list_models()
    if ok:
        lines.append(f"  Models: {len(models)} available")

    # Running models
    ok, running = list_running()
    if ok and running:
        lines.append(f"  Loaded: {len(running)} in memory")
        for m in running[:5]:
            name = m.get("name", "?")
            lines.append(f"    {name}")

    # Configured model
    _append_configured_model(lines)

    return "\n".join(lines)


def _handle_list() -> str:
    """List locally available models."""
    from app.ollama_client import is_server_running, list_models, format_model_size

    if not is_server_running():
        return "Ollama server is not running.\nStart with: ollama serve"

    ok, models = list_models()
    if not ok:
        return f"Failed to list models: {models}"

    if not models:
        return "No models installed.\nPull one with: /ollama pull <name>"

    lines = [f"Models ({len(models)}):"]
    for m in models:
        name = m.get("name", "?")
        size = m.get("size", 0)
        size_str = format_model_size(size) if size else "?"
        details = m.get("details", {})
        family = details.get("family", "")
        quant = details.get("quantization_level", "")
        info_parts = [s for s in [family, quant, size_str] if s]
        info = f" ({', '.join(info_parts)})" if info_parts else ""
        lines.append(f"  {name}{info}")

    return "\n".join(lines)


def _handle_pull(name: str) -> str:
    """Pull (download) a model."""
    if not name:
        return "Usage: /ollama pull <model-name>\nExample: /ollama pull qwen3-coder"

    from app.ollama_client import is_server_running, pull_model

    if not is_server_running():
        return "Ollama server is not running.\nStart with: ollama serve"

    ok, msg = pull_model(name)
    if ok:
        return f"Model '{name}' pulled successfully."
    return f"Failed to pull '{name}': {msg}"


def _handle_remove(name: str) -> str:
    """Remove a locally stored model."""
    if not name:
        return "Usage: /ollama remove <model-name>\nExample: /ollama remove qwen2.5-coder:7b"

    from app.ollama_client import is_server_running, delete_model

    if not is_server_running():
        return "Ollama server is not running.\nStart with: ollama serve"

    ok, msg = delete_model(name)
    if ok:
        return f"Model '{name}' removed."
    return f"Failed to remove '{name}': {msg}"


def _handle_show(name: str) -> str:
    """Show details about a specific model."""
    if not name:
        return "Usage: /ollama show <model-name>\nExample: /ollama show qwen3-coder"

    from app.ollama_client import is_server_running, show_model, format_model_size

    if not is_server_running():
        return "Ollama server is not running.\nStart with: ollama serve"

    ok, data = show_model(name)
    if not ok:
        return f"Failed to get info for '{name}': {data}"

    lines = [f"Model: {name}"]

    details = data.get("details", {})
    if details.get("family"):
        lines.append(f"  Family: {details['family']}")
    if details.get("parameter_size"):
        lines.append(f"  Parameters: {details['parameter_size']}")
    if details.get("quantization_level"):
        lines.append(f"  Quantization: {details['quantization_level']}")

    model_info = data.get("model_info", {})
    # Context length from model_info
    for key in model_info:
        if "context_length" in key:
            lines.append(f"  Context: {model_info[key]}")
            break

    return "\n".join(lines)


def _append_configured_model(lines: list) -> None:
    """Append the configured model info to status lines."""
    provider = _get_provider_name()
    try:
        if provider == "ollama-launch":
            from app.provider.ollama_launch import OllamaLaunchProvider
            p = OllamaLaunchProvider()
            model = p._get_default_model()
            if model:
                lines.append(f"  Configured: {model}")
        elif provider == "local":
            from app.provider.local import LocalLLMProvider
            p = LocalLLMProvider()
            model = p._get_default_model()
            if model:
                lines.append(f"  Configured: {model}")
    except Exception:
        pass
