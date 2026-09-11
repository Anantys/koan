"""
CLI provider abstraction for Kōan.

Allows switching between Claude Code CLI, GitHub Copilot CLI,
OpenAI Codex CLI, Cline CLI, or Ollama Launch as the underlying AI agent
binary. Each provider knows how to translate Kōan's generic command
spec into provider-specific flags.

Configuration:
    config.yaml:  cli_provider: "claude"   (default)
    env var:      KOAN_CLI_PROVIDER=codex  (overrides config.yaml)

Package structure:
    provider/base.py         — CLIProvider base class + tool constants
    provider/claude.py       — ClaudeProvider implementation
    provider/cline.py        — ClineProvider implementation
    provider/codex.py        — CodexProvider implementation
    provider/copilot.py      — CopilotProvider implementation
    provider/gemini.py       — GeminiProvider (Google Gemini CLI)
    provider/ollama_launch.py — OllamaLaunchProvider (ollama launch claude)
    provider/__init__.py     — Registry, resolution, convenience functions
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Re-export base class and constants for convenience
from app.provider.base import (  # noqa: F401
    CLIProvider,
    CLAUDE_TOOLS,
    PROVIDER_ERROR_EVENT_TYPES,
    ReadOnlyUnenforceable,
    READ_ONLY_TOOLS,
    SIDE_EFFECT_TOOLS,
    TOOL_NAME_MAP,
)

# Import concrete providers
from app.provider.claude import ClaudeProvider  # noqa: F401
from app.provider.cline import ClineProvider  # noqa: F401
from app.provider.codex import CodexProvider  # noqa: F401
from app.provider.copilot import CopilotProvider  # noqa: F401
from app.provider.fake import FakeProvider, FakeProviderNotAllowed  # noqa: F401
from app.provider.gemini import GeminiProvider  # noqa: F401
from app.provider.haze import HazeProvider  # noqa: F401
from app.provider.grok import GrokProvider  # noqa: F401
from app.provider.ollama_launch import OllamaLaunchProvider  # noqa: F401
from app.token_parser import clamp_cached_input, dominant_stats_model


def _extract_provider_error_preview(stdout: str) -> str:
    """Return the most useful direct provider error from JSONL stdout."""
    previews: List[str] = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        # Haze-style failures: the terminal result envelope carries the error
        # text in ``result`` with status "failed"/"aborted", and a fatal
        # context overflow reports its error inline. Shape-keyed — no
        # provider-name checks.
        if etype == "result":
            status = str(event.get("status") or "").lower()
            result = event.get("result")
            if status in {"failed", "aborted"} and isinstance(result, str) and result.strip():
                previews.append(result.strip())
            continue
        if etype == "context_overflow" and event.get("recovered") is False:
            error = event.get("error")
            if isinstance(error, str) and error.strip():
                previews.append(error.strip())
            continue
        if etype not in PROVIDER_ERROR_EVENT_TYPES:
            continue
        message = event.get("message")
        if isinstance(message, str) and message.strip():
            previews.append(message.strip())
            continue
        error = event.get("error")
        if isinstance(error, dict):
            err_message = error.get("message")
            if isinstance(err_message, str) and err_message.strip():
                previews.append(err_message.strip())
    return previews[-1] if previews else ""


def _format_cli_error(returncode: int, stdout: str, stderr: str) -> str:
    """Build a diagnostic message for non-zero CLI exits.

    Includes exit code, stderr (truncated), and stdout (truncated) when
    stderr is empty — Claude CLI sometimes prints fatal errors to stdout.
    """
    parts = [f"exit={returncode}"]
    err = (stderr or "").strip()
    out = (stdout or "").strip()
    if err:
        parts.append(f"stderr={err[:300]}")
    if out and not err:
        preview = _extract_provider_error_preview(out) or out
        parts.append(f"stdout={preview[:300]}")
    return "CLI invocation failed: " + " | ".join(parts)


# ---------------------------------------------------------------------------
# Provider registry & resolution
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "claude": ClaudeProvider,
    "cline": ClineProvider,
    "codex": CodexProvider,
    "copilot": CopilotProvider,
    "fake": FakeProvider,
    "gemini": GeminiProvider,
    "haze": HazeProvider,
    "grok": GrokProvider,
    "ollama-launch": OllamaLaunchProvider,
}

# Execution roles that must never be able to write. Keyed by ``model_key``, the
# role name callers already pass to run_command/run_command_streaming, so the
# posture is derived rather than opt-in and no call site can forget it.
#
# ``review_mode`` only. Deliberately NOT "chat": scripts/wiki_sync_ci.py drives a
# *write* job (Edit/Write plus a git-log Bash rule) on the chat role, and adding
# it here would break that CI fixer.
READ_ONLY_ROLES = frozenset({"review_mode"})

# Cached provider instance (reset with reset_provider() in tests). The path is
# part of the key: `cli_provider: claude:/a` and `claude:/b` are the same flavor
# but must not share an instance.
_cached_provider: Optional[CLIProvider] = None
_cached_provider_name: str = ""
_cached_provider_path: str = ""

# True once an unparseable global cli_provider has been reported. get_provider()
# runs on every command build, so without this a stale value (e.g. the removed
# "local" flavor, which config_validator still special-cases) would log once per
# CLI invocation for the life of the process.
_warned_global_cli: bool = False


def reset_provider():
    """Reset the cached provider and warn-once state (for testing)."""
    global _cached_provider, _cached_provider_name, _cached_provider_path
    global _warned_global_cli
    _cached_provider = None
    _cached_provider_name = ""
    _cached_provider_path = ""
    _warned_global_cli = False


def is_known_provider(name: str) -> bool:
    """True if *name* is a registered provider flavor (claude, codex, ...)."""
    return str(name or "").strip().lower() in _PROVIDERS


def _global_cli_spec() -> Tuple[str, str]:
    """Resolve the global provider as ``(flavor, binary_path)``.

    Resolution order:
    1. KOAN_CLI_PROVIDER env var (with CLI_PROVIDER fallback; highest priority).
       Flavor only — ``KOAN_CLAUDE_CLI_PATH`` is the env channel for a custom
       binary, so this one deliberately carries no path.
    2. config.yaml ``cli_provider`` key — ``flavor`` or ``flavor:path``, the same
       grammar the per-role ``cli:`` entries use.
    3. Default: ``("claude", "")``

    An unparseable value warns once per distinct value rather than once per
    call: this runs on every command build via :func:`get_provider`.
    """
    # Lazy import to avoid circular dependency
    from app.utils import get_cli_provider_env

    env_val = get_cli_provider_env()
    if env_val and env_val in _PROVIDERS:
        return (env_val, "")

    try:
        from app.config import get_global_cli_spec

        global _warned_global_cli
        flavor, path = get_global_cli_spec(warn=not _warned_global_cli)
        if not flavor:
            _warned_global_cli = True
        if flavor:
            return (flavor, path)
    except Exception as e:
        print(f"[provider] Config loading failed: {e}", file=sys.stderr)

    return ("claude", "")


def get_provider_name() -> str:
    """Determine which CLI provider to use.

    Returns the bare flavor name; a ``cli_provider: flavor:path`` value resolves
    to its flavor here and the path is applied by :func:`get_provider`.
    """
    return _global_cli_spec()[0]


def get_provider() -> CLIProvider:
    """Get the configured CLI provider instance (cached singleton)."""
    global _cached_provider, _cached_provider_name, _cached_provider_path
    # The flavor still comes from get_provider_name() so tests that patch it keep
    # steering resolution. The configured path applies only when the resolved
    # flavor is still the one it was configured for — an override must not
    # inherit another flavor's binary.
    spec_flavor, spec_path = _global_cli_spec()
    name = get_provider_name()
    path = spec_path if name == spec_flavor else ""
    if (
        _cached_provider is None
        or name != _cached_provider_name
        or path != _cached_provider_path
    ):
        _cached_provider = _PROVIDERS[name](binary_path=path)
        _cached_provider_name = name
        _cached_provider_path = path
    return _cached_provider


def known_providers() -> list:
    """Return the sorted names of all registered CLI providers.

    Single source of truth so dashboard forms stay in sync with the registry
    instead of hardcoding a provider list that drifts as providers are added.
    Includes test/dev-only flavors (e.g. ``fake``) so name-based lookup and
    config validation resolve them; use :func:`selectable_providers` for
    UI-facing pickers that should hide them.
    """
    return sorted(_PROVIDERS)


def selectable_providers() -> list:
    """Sorted registered providers minus test/dev-only ones (``test_only``).

    UI-facing surfaces (the dashboard provider dropdown) use this so a
    fail-closed test stub like ``fake`` never appears as a selectable option on
    a production instance, while it stays in :func:`known_providers` for
    name-based lookup and config validation.
    """
    return sorted(name for name, cls in _PROVIDERS.items() if not cls.test_only)


def get_provider_by_name(name: str) -> CLIProvider:
    """Return a fresh provider instance by name.

    Used by provider-aware code paths that need to classify historical output
    with the provider that produced it, without mutating the configured cached
    provider for the current process.
    """
    provider_name = str(name or "").strip().lower()
    if provider_name not in _PROVIDERS:
        raise KeyError(f"Unknown CLI provider: {name}")
    return _PROVIDERS[provider_name]()


def get_provider_for_role(role: str, project_name: str = "") -> CLIProvider:
    """Return the provider instance for a mission role (the ``cli:`` section).

    When the role resolves to the global provider with no custom binary path
    (the parity case — including when no ``cli:`` section is configured), the
    GLOBAL cached singleton is returned, so role-less behavior is byte-for-byte
    unchanged. When the role names a different flavor and/or a ``flavor:path``,
    a FRESH instance of that flavor is constructed carrying the path as a
    per-instance binary override.

    Role-bearing instances are NEVER written to ``_cached_provider`` — the
    global singleton's identity must not be poisoned by a path-bearing instance.
    """
    from app.config import get_cli_config

    try:
        flavor, path = get_cli_config(project_name).get(role, ("", ""))
    except Exception as e:  # never let config resolution break execution
        print(f"[provider] cli role resolution failed for {role!r}: {e}", file=sys.stderr)
        return get_provider()

    if not flavor or flavor not in _PROVIDERS:
        return get_provider()
    # Same flavor as the global default and no custom path → reuse the singleton.
    if flavor == get_provider_name() and not path:
        return get_provider()
    return _PROVIDERS[flavor](binary_path=path)


def get_fallback_provider(project_name: str = "") -> Optional[CLIProvider]:
    """Return the configured ``cli.fallback`` provider instance, or ``None``.

    Used only for launch/auth-failure recovery (see ``mission_executor`` and the
    ``run_command*`` helpers). Returns ``None`` when no fallback is configured.
    Like :func:`get_provider_for_role`, never writes the global cache.
    """
    from app.config import get_cli_fallback

    try:
        flavor, path = get_cli_fallback(project_name)
    except Exception as e:
        print(f"[provider] cli fallback resolution failed: {e}", file=sys.stderr)
        return None
    if not flavor or flavor not in _PROVIDERS:
        return None
    try:
        return _PROVIDERS[flavor](binary_path=path)
    except FakeProviderNotAllowed:
        # A fail-closed provider (``fake``) configured as the section-wide
        # fallback must not crash the recovery path — ``get_fallback_provider``
        # is contractually Optional and is called on *any* non-zero mission
        # exit (see mission_executor._maybe_fallback_provider_rerun), including
        # real-provider failures unrelated to ``fake``. Decline it so the
        # original result stands. The PRIMARY selection paths
        # (get_provider/get_provider_for_role) still error loudly, so this is
        # not a silent swap: no work is ever routed to ``fake`` here.
        print(
            f"[provider] cli.fallback {flavor!r} is fail-closed and not "
            "enabled (KOAN_ALLOW_FAKE_PROVIDER unset); ignoring fallback",
            file=sys.stderr,
        )
        return None


def resolve_role_provider(model_key: str, project_name: str = "") -> CLIProvider:
    """Provider for a role, pre-flight-swapped to ``cli.fallback`` if unavailable.

    Used by the stateless ``run_command*`` helpers: when the role's CLI binary
    is not installed/resolvable (the dominant "cli not working" case — e.g. a
    wrong ``flavor:path``), and a different, available ``cli.fallback`` is
    configured, return the fallback up front. When no fallback applies, returns
    the role provider unchanged so the call fails normally. (The stateful
    mission path additionally recovers from auth failures post-run via
    ``mission_executor._maybe_fallback_provider_rerun``.)
    """
    provider = get_provider_for_role(model_key, project_name)
    if provider.is_available():
        return provider
    fb = get_fallback_provider(project_name)
    if fb is not None and fb.binary() != provider.binary() and fb.is_available():
        print(
            f"[provider] role {model_key!r} CLI {provider.name!r} "
            f"({provider.binary()}) unavailable — using fallback {fb.name!r}",
            file=sys.stderr,
        )
        return fb
    return provider


def _resolve_role_provider_and_models(model_key: str, project_name: str):
    """Resolve a role's provider (with launch-fallback) plus its model dict.

    Shared by the stateless ``run_command`` / ``run_command_streaming`` helpers:
    returns ``(provider, models)`` where ``models`` is resolved against that
    provider's section for ``model_key`` and the fallback model.
    """
    from app.config import get_model_config

    provider = resolve_role_provider(model_key, project_name)
    models = get_model_config(
        project_name,
        role_providers={model_key: provider.name, "fallback": provider.name},
    )
    return provider, models


def get_cli_binary() -> str:
    """Get the CLI binary command for the configured provider.

    For shell scripts: returns the full command prefix needed to invoke
    the provider (e.g., "claude" or "copilot" or "gh copilot").
    """
    return get_provider().shell_command()


def get_cli_binary_name() -> str:
    """Return the binary basename from ``KOAN_CLAUDE_CLI_PATH``, or '' if unset.

    The Claude provider honors ``KOAN_CLAUDE_CLI_PATH`` to point at an
    alternate CLI binary (e.g. an ollama-wrapping shim). Surfacing its
    basename lets banners and ``/status`` advertise which flavor is in use.
    """
    path = os.environ.get("KOAN_CLAUDE_CLI_PATH", "").strip()
    return path.rstrip("/").rsplit("/", 1)[-1] if path else ""


def provider_cli_display(provider: CLIProvider) -> str:
    """Footer/attribution label for a resolved provider instance.

    Returns the basename of the CLI binary actually in use when the provider
    pins a custom path (per-role ``cli.<role>: flavor:path`` or, for Claude,
    the global ``KOAN_CLAUDE_CLI_PATH``) — e.g. ``claude-deep`` from
    ``cli.review_mode: claude:/root/.local/bin/claude-deep``. Falls back to
    the provider flavor name (``claude``) when no custom binary is configured,
    so the default setup is unchanged. Unlike a naive ``binary()`` basename,
    this never surfaces a provider's natural fallback (e.g. Copilot's ``gh``)
    as a "custom" binary — only explicit overrides count.
    """
    return provider.custom_binary_name() or provider.name


def get_provider_display(name: str = "") -> str:
    """Provider name for display, with the custom CLI binary flavor appended.

    Returns ``"<name>"`` or ``"<name> (<binary>)"`` when ``KOAN_CLAUDE_CLI_PATH``
    points at a binary whose basename differs from the provider name (e.g.
    ``claude (ollama-claude)``). Suppressed when unset or identical, so this is
    a no-op for non-Claude providers. When *name* is empty the configured
    provider is resolved via :func:`get_provider_name`. Single source of truth
    for the global provider line shown by the startup banner and ``/status``.

    Per-role provider overrides (the ``cli:`` config section) are summarized
    separately by :func:`describe_cli_roles`.
    """
    if not name:
        name = get_provider_name()
    parts: List[str] = []
    binary = get_cli_binary_name()
    if binary and binary != name:
        parts.append(binary)
    if parts:
        return f"{name} ({', '.join(parts)})"
    return name


def describe_cli_roles(project_name: str = "") -> str:
    """Compact summary of per-role provider overrides for ``/status`` and the banner.

    Returns e.g. ``"mission→codex, review_mode→deep-claude, fallback→claude"`` listing
    only roles whose resolved provider/binary differs from the global default, plus
    the ``cli.fallback`` provider if set. Empty string when no ``cli:`` section is
    configured, so it stays a no-op for the default setup.
    """
    try:
        from app.config import get_cli_config, get_cli_fallback

        resolved = get_cli_config(project_name)
    except Exception:
        return ""

    def _label(flavor: str, path: str) -> str:
        if not path:
            return flavor
        return f"{flavor}({path.rstrip('/').rsplit('/', 1)[-1]})"

    global_name = get_provider_name()
    parts: List[str] = []
    for role in ("mission", "chat", "lightweight", "review_mode", "reflect"):
        flavor, path = resolved.get(role, (global_name, ""))
        if flavor == global_name and not path:
            continue
        parts.append(f"{role}→{_label(flavor, path)}")
    try:
        fb_flavor, fb_path = get_cli_fallback(project_name)
    except Exception:
        fb_flavor, fb_path = "", ""
    if fb_flavor:
        parts.append(f"fallback→{_label(fb_flavor, fb_path)}")
    return ", ".join(parts)


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
    system_prompt: str = "",
    system_prompt_file: str = "",
    effort: str = "",
    resume_session_id: str = "",
    project_context: bool = True,
    provider: Optional[CLIProvider] = None,
    read_only: bool = False,
) -> List[str]:
    """Build a complete CLI command for the configured provider.

    This is the high-level API: pass generic parameters, get back a
    provider-specific command list ready for subprocess.run().

    Args:
        system_prompt: Optional system prompt text. When the provider
            supports it (e.g., Claude ``--append-system-prompt``), sent
            as a dedicated system prompt for better prompt caching.
            Otherwise prepended to the user prompt transparently.
        effort: Reasoning effort level (e.g. "low", "medium", "high", "max").
            Empty string means no override.
        resume_session_id: When set and the provider supports session
            resumption, continues the given session instead of starting
            fresh.
        project_context: When False, ask the provider to suppress
            project-scope tooling (Claude: ``--setting-sources user``).
            Use for KOAN_ROOT runtime sessions (chat, contemplative,
            rituals, outbox). Mission sessions leave the default True.
        provider: Explicit provider instance to build for. ``None`` (default)
            uses the global :func:`get_provider`. Pass a per-role instance
            (from :func:`get_provider_for_role`) to build a command for a
            specific mission role's CLI / custom binary.
        read_only: When True this invocation must not be able to write. The
            side-effecting tools are added to *disallowed_tools* for providers
            that can deny tools, and the flag is forwarded so providers with a
            sandbox (Codex) can express the posture directly. Derived from the
            role by :func:`run_command` / :func:`run_command_streaming` — see
            :data:`READ_ONLY_ROLES`. Fails closed: a provider that can express
            neither mechanism raises
            :class:`~app.provider.base.ReadOnlyUnenforceable` instead of running
            with full write access.

    Raises:
        ReadOnlyUnenforceable: *read_only* is True and the resolved provider
            (including a ``cli.fallback`` swap) reports
            ``enforces_read_only() is False``.

    Automatically reads ``skip_permissions`` from config.yaml so all
    callers get the flag without needing changes.
    """
    from app.config import get_skip_permissions

    resolved = provider or get_provider()
    if read_only:
        if not resolved.enforces_read_only():
            # Fail closed. A provider that can neither deny tools by name nor
            # run in a read-only sandbox would execute this invocation with full
            # write access to the live project clone, which is exactly what the
            # role forbids. Refuse rather than downgrade the boundary to advice.
            raise ReadOnlyUnenforceable(
                f"CLI provider {resolved.name!r} cannot enforce a read-only "
                "invocation: it supports neither per-tool denial nor a read-only "
                "sandbox, so the session could write to the live project clone. "
                "Pin a provider that can (claude, codex, ollama-launch) on this "
                "role's `cli:` entry in instance/config.yaml (for reviews: "
                "`cli.review_mode`), or clear the `cli.fallback` that resolved "
                f"to {resolved.name!r}."
            )
        if resolved.supports_tool_denial():
            # The secondary denial layer. An allowlist does not withhold
            # anything -- ``--allowedTools`` only pre-approves -- so denying the
            # side-effecting tools by name is what holds the boundary on a CLI
            # that ignores an unknown ``--tools``. Merge rather than replace so
            # an explicit caller-supplied denial is preserved.
            #
            # Subtract whatever READ_ONLY_TOOLS deliberately grants, but ONLY
            # when the provider can actually restrict: ``--disallowedTools``
            # removes a tool from the model's context, so denying ``Bash`` here
            # would cancel the gated shell that ``--tools`` just granted. When
            # the provider cannot restrict it also cannot install the PreToolUse
            # gate, so it gets no shell at all and the full denial applies --
            # still fail-closed, just without the capability.
            granted = (
                set(READ_ONLY_TOOLS) if resolved.supports_tool_restriction()
                else set()
            )
            merged = list(disallowed_tools or [])
            merged += [
                t for t in SIDE_EFFECT_TOOLS
                if t not in merged and t not in granted
            ]
            disallowed_tools = merged

    return resolved.build_command(
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
        system_prompt=system_prompt,
        system_prompt_file=system_prompt_file,
        effort=effort,
        resume_session_id=resume_session_id,
        project_context=project_context,
        read_only=read_only,
    )


def _write_system_prompt_file(
    content: str,
    host_dir: Optional[str] = None,
    container_dir: Optional[str] = None,
) -> Tuple[str, str]:
    """Write a system prompt to a 0600 temp file and return ``(host_path, cmd_path)``.

    ``host_path`` is always the real filesystem path used for cleanup.
    ``cmd_path`` is the path embedded in the CLI command — equal to
    ``host_path`` normally, or ``container_dir/<filename>`` in devcontainer
    mode so the container can open the bind-mounted file.

    The file is intentionally not auto-deleted — the caller is responsible
    for unlinking ``host_path`` after the subprocess has finished. Use
    :func:`build_full_command_managed`, which pairs this with cleanup.

    Args:
        host_dir: Directory on the host where the file is written. In
            devcontainer mode, pass the host side of the koan-tmp bind-mount.
        container_dir: When set, ``cmd_path`` is ``container_dir/<filename>``
            so the CLI command embeds the container-accessible path.
    """
    from app.utils import koan_tmp_dir

    # NamedTemporaryFile creates with 0600 on POSIX (same as mkstemp).
    # delete=False so the subprocess can open the path after we close it.
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="koan-sysprompt-",
            suffix=".txt",
            delete=False,
            dir=host_dir or koan_tmp_dir(),
            encoding="utf-8",
        ) as f:
            host_path = f.name
            f.write(content)
    except Exception:
        # If NamedTemporaryFile raised after creating the file, unlink it.
        with contextlib.suppress(OSError, NameError):
            os.unlink(host_path)  # type: ignore[possibly-undefined]
        raise
    cmd_path = str(Path(container_dir) / Path(host_path).name) if container_dir else host_path
    return host_path, cmd_path


def build_full_command_managed(
    prompt: str,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
    model: str = "",
    fallback: str = "",
    output_format: str = "",
    max_turns: int = 0,
    mcp_configs: Optional[List[str]] = None,
    plugin_dirs: Optional[List[str]] = None,
    system_prompt: str = "",
    effort: str = "",
    resume_session_id: str = "",
    project_context: bool = True,
    system_prompt_dir: Optional[str] = None,
    system_prompt_container_dir: Optional[str] = None,
    provider: Optional[CLIProvider] = None,
) -> Tuple[List[str], List[str]]:
    """Build a CLI command, routing large system prompts through a temp file.

    Same parameters as :func:`build_full_command` — minus ``read_only``, which
    this path deliberately does not accept (see
    ``specs/components/providers.md``: the fail-closed enforcement covers
    ``read_only`` invocations, and the mission path through here is explicitly
    out of scope). But when ``system_prompt``
    is non-empty AND the configured provider supports
    ``--append-system-prompt-file`` (or its equivalent), the prompt is
    written to a 0600 temp file and the file path is passed instead of the
    content.  This keeps the prompt out of ``argv`` so it doesn't show up
    in ``ps`` listings or process supervisors.

    Returns:
        ``(cmd, cleanup_paths)`` — the caller MUST unlink each path in
        ``cleanup_paths`` after the subprocess exits, typically from a
        ``finally`` block alongside its other temp-file cleanup. If the build
        itself raises, the temp file is unlinked here before the exception
        propagates, so the caller never has to clean up a path it never got.
    """
    cleanup_paths: List[str] = []

    kwargs = dict(
        prompt=prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=model,
        fallback=fallback,
        output_format=output_format,
        max_turns=max_turns,
        mcp_configs=mcp_configs,
        plugin_dirs=plugin_dirs,
        effort=effort,
        resume_session_id=resume_session_id,
        project_context=project_context,
        provider=provider,
    )
    if system_prompt and (provider or get_provider()).supports_system_prompt_file():
        host_path, cmd_path = _write_system_prompt_file(
            system_prompt,
            host_dir=system_prompt_dir,
            container_dir=system_prompt_container_dir,
        )
        cleanup_paths.append(host_path)
        kwargs.update(system_prompt="", system_prompt_file=cmd_path)
    else:
        kwargs["system_prompt"] = system_prompt
    try:
        return build_full_command(**kwargs), cleanup_paths
    except BaseException:
        # The temp file is already on disk but ``cleanup_paths`` only reaches the
        # caller on success, so anything raised out of the build would leak a
        # 0600 file containing the system prompt. Unlink here and re-raise.
        cleanup_managed_paths(cleanup_paths)
        raise


def cleanup_managed_paths(paths: List[str]) -> None:
    """Unlink each path in *paths*, ignoring missing files.

    Companion to :func:`build_full_command_managed`. Safe to call from
    a ``finally`` block; never raises.
    """
    for p in paths:
        with contextlib.suppress(OSError):
            os.unlink(p)


_MAX_TURNS_RE = re.compile(r"Reached max turns", re.IGNORECASE)


def _is_max_turns_error(stdout: str) -> bool:
    """Return True if the CLI output indicates a max-turns limit was hit."""
    return bool(_MAX_TURNS_RE.search(stdout))


def _warn_max_turns(max_turns: int, config_key: Optional[str] = "skill_max_turns") -> None:
    """Print a user-visible warning about max turns being hit.

    ``config_key`` names the ``instance/config.yaml`` setting that controls
    this call site's max_turns, when one exists. Pass ``None`` for callers
    that hardcode max_turns (chat replies, intent classification, spec
    review subagents) so the user is not pointed at an unrelated config key.
    """
    hint = (
        f"   To increase: set {config_key} in instance/config.yaml "
        f"(current: {max_turns}).\n"
        if config_key
        else "   This call uses a hardcoded limit and is not configurable.\n"
    )
    print(
        f"\n⚠️  Claude hit the max turns limit ({max_turns}). "
        f"The output may be incomplete.\n{hint}",
        file=sys.stderr,
        flush=True,
    )


def run_command(
    prompt: str,
    project_path: str,
    allowed_tools: List[str],
    model_key: str = "chat",
    max_turns: int = 10,
    timeout: int = 300,
    max_turns_source: Optional[str] = "skill_max_turns",
    project_name: str = "",
    mcp_configs: Optional[List[str]] = None,
) -> str:
    """Build and run a CLI command, returning stripped stdout.

    Higher-level helper for runner modules that need to invoke the
    configured CLI provider with a prompt and get back text output.
    Combines build_full_command + subprocess execution + error handling.

    The provider is resolved per role: ``model_key`` selects both the model and
    the CLI provider (the ``cli:`` section). Pass ``project_name`` to honor
    per-project ``cli:`` overrides; omitting it uses the section/global
    resolution (which matches the historical behavior for these helpers).

    ``mcp_configs`` is an optional list of MCP server config paths (or
    ``None`` to omit ``--mcp-config``). Callers should resolve it through
    ``config.mcp_configs_for_role(role, project_name)`` so the per-role
    allowlist and kill switch apply.

    When the CLI hits its max-turns limit, the partial output is returned
    instead of raising — the caller can still extract useful results from
    an incomplete session.

    Raises:
        RuntimeError: If the command exits with non-zero code (except
            max-turns, which returns partial output).
    """
    provider, models = _resolve_role_provider_and_models(model_key, project_name)
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=allowed_tools,
        model=models.get(model_key, ""),
        fallback=models.get("fallback", ""),
        max_turns=max_turns,
        mcp_configs=mcp_configs,
        provider=provider,
        read_only=model_key in READ_ONLY_ROLES,
    )

    from app.cli_exec import run_cli_with_retry

    result = run_cli_with_retry(
        cmd,
        provider=provider,
        capture_output=True, text=True, timeout=timeout,
        cwd=project_path,
    )

    if result.returncode != 0:
        # Max-turns is a graceful limit, not a hard error — return
        # whatever Claude produced so callers can extract partial results.
        if _is_max_turns_error(result.stdout or ""):
            _warn_max_turns(max_turns, max_turns_source)
            from app.claude_step import strip_cli_noise
            return strip_cli_noise(result.stdout.strip())
        raise RuntimeError(
            _format_cli_error(result.returncode, result.stdout, result.stderr)
        )

    from app.claude_step import strip_cli_noise
    return strip_cli_noise(result.stdout.strip())


def _content_text(content: Any) -> str:
    """Extract text from common provider content shapes."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(text, (list, dict)):
                    nested = _content_text(text)
                    if nested:
                        parts.append(nested)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if isinstance(text, str):
            return text
    return ""


# Leading lines that carry no readable preview on their own: code-fence
# markers (```/```json), bare structural punctuation ({, }, [], ...), and
# horizontal rules. Skipped when picking a preview so a text block that opens
# with ```json renders its actual content instead of "🧠 {".
_LOW_SIGNAL_PREVIEW = re.compile(r"^(?:`{3,}[\w+-]*|[-{}\[\]()]+|-{3,})$")


def _first_line(value: Any, limit: int = 80) -> str:
    """First meaningful line of *value* as a bounded preview ('' when empty).

    Skips leading blank lines and low-signal markers (code fences, bare
    brackets, rules) so a block that opens with ```json or ``{`` surfaces its
    real content instead of the marker.
    """
    for line in str(value or "").splitlines():
        stripped = line.strip()
        if stripped and not _LOW_SIGNAL_PREVIEW.match(stripped):
            return stripped[:limit]
    return ""


# Keys, in priority order, whose value best identifies a tool call.
_TOOL_PREVIEW_KEYS = (
    "command",      # Bash
    "file_path",    # Read / Write / Edit / MultiEdit
    "pattern",      # Grep / Glob
    "path",         # generic path-taking tools
    "url",          # WebFetch
    "query",        # WebSearch
    "prompt",       # Task
    "description",  # Task / generic fallback
)


def _tool_input_preview(inp: Any, limit: int = 60) -> str:
    """Short, single-line preview of a tool_use ``input`` ('' when none).

    Picks the first present, non-empty string among the known identifying
    keys, reduces it to its first meaningful line (skipping code fences /
    bare brackets via ``_first_line``), and truncates to *limit* chars with
    a trailing ellipsis when it had to cut.
    """
    if not isinstance(inp, dict):
        return ""
    for key in _TOOL_PREVIEW_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            line = _first_line(val, limit=10_000)  # first real line, unbounded
            if not line:
                continue
            # More content follows when the value spans multiple non-empty
            # lines beyond the chosen one.
            multiline = len([s for s in val.splitlines() if s.strip()]) > 1
            if len(line) > limit:
                return line[:limit].rstrip() + "…"
            return line + "…" if multiline else line
    return ""


def _tool_result_preview(content: Any, limit: int = 120) -> str:
    """Return a bounded, single-line diagnostic from a failed tool result."""
    values: List[str] = []
    if isinstance(content, str):
        values.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                values.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    values.append(text)

    for value in values:
        preview = _first_line(value, limit=limit)
        if preview:
            return _drop_part_sep(preview)
    return ""


def _error_message(err: Any) -> str:
    """Pull the human-readable message out of an error field.

    Accepts the bare string some providers emit and the
    ``{"type": …, "message": …}`` object others do; anything else yields ''.
    """
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        message = err.get("message")
        if isinstance(message, str) and message.strip():
            return message
        # A machine-readable-only error ({"type": "TOOL_CONFIRMATION_REQUIRED"})
        # still carries the sole clue about why the session failed — never drop
        # it on the floor.
        err_type = err.get("type")
        if isinstance(err_type, str) and err_type.strip():
            return err_type.strip()
        if err:
            return repr(err)[:200]
    return ""


def _drop_part_sep(text: str) -> str:
    """Neutralize the ``", "`` part delimiter inside a free-text preview.

    ``_summarize_stream_event`` joins assistant parts with ``", "`` and the
    display formatter (``log_fmt._PART_SEP``) splits on that same token before
    a part keyword. A preview that itself contains ``", "`` (a Bash command or
    a text line with a comma) would be mis-split, so collapse the delimiter to
    a bare comma inside preview values. Cosmetic and display-side only — it
    keeps the emitted grammar unambiguous by construction.
    """
    return re.sub(r", +", ",", text)


def _summarize_stream_event(event: Dict[str, Any]) -> str:
    """Render a provider JSONL event as a single human-readable line.

    Returned strings are short and self-contained so the skill-runner's
    parent (run.py liveness watchdog) sees per-event activity instead of
    raw JSON. Unknown event shapes fall back to a generic type tag.
    """
    etype = event.get("type", "")

    if etype == "system":
        subtype = event.get("subtype", "")
        model = event.get("model", "")
        if subtype == "init" and model:
            return f"[cli] session init (model={model})"
        return f"[cli] system: {subtype or '?'}"

    if etype == "assistant":
        msg = event.get("message") or {}
        blocks = msg.get("content") or []
        parts: List[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "tool_use":
                name = block.get("name", "?")
                preview = _drop_part_sep(_tool_input_preview(block.get("input")))
                parts.append(
                    f"tool_use: {name}: {preview}" if preview
                    else f"tool_use: {name}"
                )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    preview = _drop_part_sep(
                        _first_line(text) or text.splitlines()[0][:80]
                    )
                    parts.append(f"text: {preview}")
                else:
                    parts.append("text")
            elif btype == "thinking":
                parts.append("thinking")
        return "[cli] assistant — " + (", ".join(parts) if parts else "(empty)")

    if etype == "user":
        msg = event.get("message") or {}
        blocks = msg.get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = str(block.get("tool_use_id") or "")[:12]
                err = " (error)" if block.get("is_error") else ""
                detail = _tool_result_preview(block.get("content")) if err else ""
                suffix = f": {detail}" if detail else ""
                return f"[cli] tool_result {tid}{err}{suffix}"
        return "[cli] user turn"

    # Gemini CLI-style NDJSON: init / message / tool_use / tool_result events,
    # shape-keyed on their own field names (``tool_name``, ``role`` + string
    # ``content``) — no provider-name checks. See tests/gemini_samples.py.
    if etype == "init" and isinstance(event.get("model"), str):
        return f"[cli] session init (model={event['model']})"

    if etype == "message" and isinstance(event.get("content"), str):
        if event.get("role") == "assistant":
            preview = _drop_part_sep(_first_line(event.get("content")))
            if preview:
                return f"[cli] assistant — text: {preview}"
            return "[cli] assistant — streaming"
        return "[cli] user turn"

    if etype == "tool_use" and isinstance(event.get("tool_name"), str):
        name = event.get("tool_name") or "?"
        preview = _drop_part_sep(_tool_input_preview(event.get("parameters")))
        return (
            f"[cli] assistant — tool_use: {name}: {preview}" if preview
            else f"[cli] assistant — tool_use: {name}"
        )

    if etype == "tool_result" and "tool_id" in event:
        tid = str(event.get("tool_id") or "")[:12]
        err = " (error)" if str(event.get("status") or "").lower() == "error" else ""
        detail = _tool_result_preview(_error_message(event.get("error"))) if err else ""
        suffix = f": {detail}" if detail else ""
        return f"[cli] tool_result {tid}{err}{suffix}"

    if etype == "result":
        # Claude labels results with ``subtype`` ("success"); haze-style
        # envelopes carry ``status`` ("complete"/"aborted"/"failed") instead.
        subtype = event.get("subtype", "") or event.get("status", "")
        duration_ms = event.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            return f"[cli] result: {subtype or '?'} ({int(duration_ms) // 1000}s)"
        return f"[cli] result: {subtype or '?'}"

    # Haze-style message lifecycle events ({type: message_*, id, text}).
    # ``message_update`` fires per streamed chunk with a cumulative text
    # snapshot — summarize it cheaply instead of reprinting the text.
    if etype == "message_start":
        return "[cli] assistant — message start"
    if etype == "message_update":
        return "[cli] assistant — streaming"
    if etype == "message_end":
        if event.get("hidden"):
            return "[cli] assistant — (hidden)"
        preview = _first_line(event.get("text"))
        if preview:
            return f"[cli] assistant — text: {preview}"
        return "[cli] assistant — message end"

    # Haze-style tool completion ({type: tool_end, name, success, durationMs}).
    # tool_start falls through to the generic name-keyed fallback below.
    if etype == "tool_end" and isinstance(event.get("name"), str):
        name = event.get("name") or "?"
        duration = event.get("durationMs")
        took = f", {int(duration)}ms" if isinstance(duration, (int, float)) else ""
        if event.get("success") is False:
            error = _first_line(event.get("error"))
            suffix = f": {error}" if error else ""
            return f"[cli] tool_end: {name} (FAILED{took}){suffix}"
        return f"[cli] tool_end: {name} (ok{took})"

    # Haze-style retry events ({type: retry, attempt, maxAttempts, error}).
    if etype == "retry" and "attempt" in event:
        error = _first_line(event.get("error"))
        suffix = f": {error}" if error else ""
        return f"[cli] retry {event.get('attempt')}/{event.get('maxAttempts')}{suffix}"

    # Haze-style context overflow ({type: context_overflow, recovered, error}).
    if etype == "context_overflow" and "recovered" in event:
        if event.get("recovered"):
            return "[cli] context_overflow (recovered)"
        error = _first_line(event.get("error"))
        suffix = f": {error}" if error else ""
        return f"[cli] context_overflow (fatal){suffix}"

    if etype == "rate_limit_event":
        # The new CLI emits these informationally (status "allowed") on every
        # session, plus on genuine exhaustion (status "rejected"). Only the
        # latter must pause Koan. Collapse to a status-aware summary line so the
        # quota detector — which sees only this summary, not the raw JSON — can
        # tell them apart. See quota_handler._rate_limit_exhausted.
        info = event.get("rate_limit_info") or {}
        status = str(info.get("status", "")).strip().lower()
        rtype = str(info.get("rateLimitType") or "").strip()
        label = f" ({rtype})" if rtype else ""
        if status in {"rejected", "exceeded", "blocked", "throttled"}:
            resets = info.get("resetsAt")
            suffix = f" resetsAt {resets}" if resets else ""
            return f"[cli] rate_limit_rejected{label}{suffix}"
        # NOTE: underscored ``rate_limit_ok`` (not "rate limit ok") — the
        # space-separated form collides with the loose ``rate limit`` quota
        # pattern, so a summary that leaks into a stderr-trusted buffer would
        # falsely pause Koan. Mirror the underscored ``rate_limit_rejected``
        # marker above. See quota_handler._rate_limit_exhausted.
        return f"[cli] rate_limit_ok: {status or 'unknown'}{label}"

    # Grok Build-style NDJSON (streaming-json): thought/text deltas + terminal
    # ``end`` envelope. Shape-keyed on type + ``data`` / stopReason — no
    # provider-name checks. See tests/grok_samples.py.
    if etype == "thought" and isinstance(event.get("data"), str):
        return "[cli] assistant — thinking"
    if etype == "text" and isinstance(event.get("data"), str):
        preview = _first_line(event.get("data"))
        if preview:
            return f"[cli] assistant — text: {_drop_part_sep(preview)}"
        return "[cli] assistant — streaming"
    if etype == "end":
        stop = str(event.get("stopReason") or event.get("stop_reason") or "").strip()
        turns = event.get("num_turns")
        parts = ["end"]
        if stop:
            parts.append(stop)
        if isinstance(turns, int):
            parts.append(f"{turns} turns")
        return f"[cli] result: {', '.join(parts)}"

    item = event.get("item")
    if isinstance(item, dict):
        item_type = item.get("type", "")
        status = event.get("status") or item.get("status") or ""
        if item_type == "message" or item.get("role") == "assistant":
            text = _content_text(item.get("content")).strip()
            if text:
                preview = _first_line(text) or text.splitlines()[0][:80]
                return f"[cli] assistant — text: {preview}"
            return "[cli] assistant — message"
        if item_type:
            suffix = f" ({status})" if status else ""
            return f"[cli] {item_type}{suffix}"

    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return f"[cli] {etype or 'message'}: {message.strip().splitlines()[0][:80]}"

    delta = event.get("delta")
    if isinstance(delta, str) and delta.strip():
        return f"[cli] {etype or 'delta'}: {delta.strip().splitlines()[0][:80]}"

    last_agent_message = event.get("last_agent_message")
    if isinstance(last_agent_message, str) and last_agent_message.strip():
        return f"[cli] {etype or 'result'}: {last_agent_message.strip().splitlines()[0][:80]}"

    for key in ("name", "status", "subtype"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return f"[cli] {etype or 'event'}: {value}"

    return f"[cli] event: {etype or '?'}"


def _extract_assistant_text_chunks(event: Dict[str, Any]) -> List[str]:
    """Pull raw assistant text out of common provider event shapes.

    Used as a partial-stream fallback: if the CLI dies before emitting a
    final ``result`` event, accumulated text chunks still surface to the
    caller instead of an empty string.
    """
    chunks: List[str] = []
    if event.get("type") == "assistant":
        msg = event.get("message") or {}
        blocks = msg.get("content") or []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)

    # Grok Build-style assistant text deltas: {"type":"text","data":"…"}.
    # These are token/word deltas that must be concatenated with "" (see
    # run_command_streaming delta flush). Not the same as Claude content
    # blocks or haze message_end segments.
    if event.get("type") == "text":
        data = event.get("data")
        if isinstance(data, str) and data:
            chunks.append(data)

    # Gemini CLI-style assistant chunks: {"type":"message","role":"assistant",
    # "content":"…","delta":true}. Deltas are concatenated with "" by the
    # caller (see _is_text_delta_event); user-role messages echo the prompt
    # back and must never be collected as assistant output.
    if event.get("type") == "message" and event.get("role") == "assistant":
        content = event.get("content")
        if isinstance(content, str) and content:
            chunks.append(content)

    # Haze-style segment completion: ``message_end`` carries the finalized
    # text for one assistant segment (one event per segment id). Cumulative
    # ``message_update`` snapshots are deliberately NOT collected — they would
    # duplicate the same text many times. Hidden segments are excluded, same
    # as haze's own result joining.
    if event.get("type") == "message_end" and not event.get("hidden"):
        text = event.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)

    item = event.get("item")
    if isinstance(item, dict) and (
        item.get("role") == "assistant" or item.get("type") == "message"
    ):
        text = _content_text(item.get("content"))
        if text:
            chunks.append(text)

    message = event.get("message")
    if isinstance(message, str) and event.get("type") in {
        "agent_message",
        "agent_message_content_delta",
        "assistant_message",
        "message",
    }:
        chunks.append(message)

    for key in ("output_text", "text", "delta"):
        text = event.get(key)
        if isinstance(text, str) and text and event.get("type") in {
            "agent_message",
            "agent_message_content_delta",
            "assistant_message",
            "message",
            "response.output_text.delta",
            "response.output_text.done",
        }:
            chunks.append(text)

    return chunks


def _is_text_delta_event(event: Dict[str, Any]) -> bool:
    """Return True when *event* carries an incremental assistant text chunk.

    Delta chunks are joined with ``""`` so ``"hel" + "lo"`` becomes ``"hello"``,
    whereas block-style segments are joined with newlines. Shape-keyed on two
    known delta shapes: Grok Build's ``{"type":"text","data":…}`` and Gemini
    CLI's ``{"type":"message","role":"assistant","delta":true,"content":…}``.
    """
    etype = event.get("type")
    if etype == "text" and isinstance(event.get("data"), str):
        return True
    return (
        etype == "message"
        and event.get("role") == "assistant"
        and bool(event.get("delta"))
        and isinstance(event.get("content"), str)
    )


def _extract_result_text(event: Dict[str, Any]) -> Optional[str]:
    """Pull the final assistant text out of a provider result event.

    Returns ``None`` when *event* is not a result event, when its
    ``result`` field is missing or not a string, or when it is an empty
    string — in any of these cases the caller falls back to accumulated
    assistant text blocks instead of pinning the return value to ``""``.
    The Claude CLI stuffs the same string a plain text-mode run would
    have printed into ``event["result"]``; we forward it verbatim so
    callers see the same return value they did before stream-json was on.
    """
    etype = str(event.get("type") or "")
    if etype != "result":
        if not (
            etype.endswith(".completed")
            or etype.endswith(".done")
            or etype in {
                "turn.completed",
                "response.completed",
                "task.completed",
                "turn_complete",
                "task_complete",
                # Grok Build terminal envelope carries usage/stopReason but not
                # the assistant text — return None so callers fall back to
                # accumulated text deltas.
                "end",
            }
        ):
            return None
        if etype == "end":
            return None
        for key in ("output_text", "last_agent_message", "text"):
            result = event.get(key)
            if isinstance(result, str) and result:
                return result
        return None
    for key in ("result", "output_text", "last_agent_message", "text"):
        result = event.get(key)
        if isinstance(result, str) and result:
            return result
    return None


# Known stream-json ``result.subtype`` values that mean "max turns hit".
# Update when the Claude CLI ships new subtypes; the legacy regex
# fallback in ``_is_max_turns_error`` covers textual output.
_STREAM_JSON_MAX_TURNS_SUBTYPES = frozenset({
    "error_max_turns",
    "max_turns",
})


def _is_stream_json_max_turns(event: Dict[str, Any]) -> bool:
    """Detect the stream-json equivalent of the legacy 'Reached max turns' line."""
    if event.get("type") != "result":
        return False
    subtype = str(event.get("subtype", "") or "").lower()
    return subtype in _STREAM_JSON_MAX_TURNS_SUBTYPES


# Terminal ``end`` stopReasons that mean the session was aborted rather than
# completed productively. Shape-keyed (type=end + stopReason) — used by Grok
# Build headless when a tool permission would have prompted.
_CANCELLED_STOP_REASONS = frozenset({"cancelled", "canceled"})


def _is_cancelled_end_event(event: Dict[str, Any]) -> bool:
    """Return True when a stream terminal event reports a cancelled stop.

    Grok Build emits ``{"type":"end","stopReason":"Cancelled",...}`` when a
    headless permission prompt is auto-cancelled. Treating that as soft
    success left /implement with partial text and zero commits.
    """
    if str(event.get("type") or "") != "end":
        return False
    stop = str(
        event.get("stopReason") or event.get("stop_reason") or ""
    ).strip().lower()
    return stop in _CANCELLED_STOP_REASONS


# Terminal ``result`` statuses that mean the session DID complete. Anything
# else — ``timeout``, ``quota_exceeded``, ``interrupted``, a status this
# adapter has never seen — fails closed, matching the contract text in
# specs/components/providers.md. Only the text-less envelope shape is
# inspected (Gemini CLI: ``{"type":"result","status":…,"error"?,"stats"?}``) —
# envelopes that carry the assistant text alongside the status (haze
# ``result``+``result``/``usage``) keep their existing soft-return behaviour.
_RESULT_SUCCESS_STATUSES = frozenset(
    {"success", "succeeded", "complete", "completed", "ok"}
)


def _is_failed_result_event(event: Dict[str, Any]) -> bool:
    """Return True for a text-less terminal result reporting a failed status.

    Without this a headless run that could not answer a tool confirmation
    prompt exits 0 with partial prose, and the mission is reported complete
    with no branch and no commit — the same soft-success hole
    :func:`_is_cancelled_end_event` closes for Grok Build.

    ``stats`` is deliberately NOT required: a session that aborts before any
    model call emits ``{"type":"result","status":"error","error":{…}}`` with no
    stats block, which is exactly the shape this guard exists to catch.

    The status test is a SUCCESS allowlist, not a failure blocklist: an
    unrecognized terminal status (``timeout``, ``quota_exceeded``, a word a
    future build introduces) must fail rather than bank partial prose. A
    missing/empty status is left alone — that envelope reports no verdict at
    all, and other providers emit it.
    """
    if str(event.get("type") or "") != "result":
        return False
    if "result" in event or "usage" in event:
        return False
    status = str(event.get("status") or "").strip().lower()
    if not status:
        return False
    return status not in _RESULT_SUCCESS_STATUSES


def _usage_snapshot_from_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract token usage snapshot from a stream event when present."""
    if not isinstance(event, dict):
        return None

    usage = event.get("usage")
    if isinstance(usage, dict):
        # camelCase usage shape (haze-style result envelopes):
        # {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens,
        #  reasoningTokens}. Shape-keyed on field presence — no provider names.
        if "inputTokens" in usage or "outputTokens" in usage:
            input_tokens = int(usage.get("inputTokens", 0) or 0)
            # reasoningTokens is a SUBSET of outputTokens in AI-SDK-based
            # reporting (OpenAI completion_tokens_details semantics) — it is
            # already accounted inside outputTokens; adding it would
            # double-count reasoning-model output.
            output_tokens = int(usage.get("outputTokens", 0) or 0)
            cache_read = int(usage.get("cacheReadTokens", 0) or 0)
            cache_write = int(usage.get("cacheWriteTokens", 0) or 0)
            if cache_read > 0:
                # Koan accounting: input_tokens excludes cache hits.
                input_tokens = max(0, input_tokens - cache_read)
            if input_tokens or output_tokens or cache_read or cache_write:
                return {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                    "model": str(event.get("model") or "unknown"),
                }
            return None

        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        # Claude/Codex: ``cached_input_tokens`` is a subset of input — subtract.
        # Grok Build: ``cache_read_input_tokens`` may exceed ``input_tokens`` on
        # multi-turn runs (cache spans prior turns). Only subtract when cache
        # is a subset of input so we never zero out a real input count.
        if "cached_input_tokens" in usage:
            cached_input = int(usage.get("cached_input_tokens", 0) or 0)
            if cached_input > 0:
                input_tokens = max(0, input_tokens - cached_input)
        else:
            cached_input = int(usage.get("cache_read_input_tokens", 0) or 0)
            if 0 < cached_input <= input_tokens:
                input_tokens = max(0, input_tokens - cached_input)
        if input_tokens or output_tokens or cached_input:
            model = str(event.get("model") or "")
            if not model:
                # Grok Build puts the model id in modelUsage keys, not event.model.
                model_usage = event.get("modelUsage")
                if isinstance(model_usage, dict) and model_usage:
                    model = str(next(iter(model_usage)))
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cached_input,
                "cache_creation_input_tokens": 0,
                "model": model or "unknown",
            }

    # Gemini CLI reports usage under ``stats`` on its terminal ``result``
    # event, not under ``usage``: {input_tokens, output_tokens, cached, input,
    # total_tokens, models:{<id>:…}}. Shape-keyed on the nested field names.
    stats = event.get("stats")
    if isinstance(stats, dict) and (
        "input_tokens" in stats or "output_tokens" in stats
    ):
        input_tokens = int(stats.get("input_tokens", 0) or 0)
        output_tokens = int(stats.get("output_tokens", 0) or 0)
        # ``cached`` is a SUBSET of input_tokens — subtract so input matches
        # Koan accounting (which excludes cache hits).
        cached_input = clamp_cached_input(
            int(stats.get("cached", 0) or 0), input_tokens
        )
        input_tokens -= cached_input
        if input_tokens or output_tokens or cached_input:
            model = str(event.get("model") or "")
            if not model:
                model = dominant_stats_model(stats.get("models"))
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cached_input,
                "cache_creation_input_tokens": 0,
                "model": model or "unknown",
            }

    payload = event.get("payload")
    if (
        isinstance(payload, dict)
        and event.get("type") == "event_msg"
        and payload.get("type") == "token_count"
    ):
        info = payload.get("info")
        if isinstance(info, dict):
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                input_tokens = int(total.get("input_tokens", 0) or 0)
                output_tokens = int(total.get("output_tokens", 0) or 0)
                cached_input = int(total.get("cached_input_tokens", 0) or 0)
                if cached_input > 0:
                    input_tokens = max(0, input_tokens - cached_input)
                if input_tokens or output_tokens or cached_input:
                    return {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": cached_input,
                        "cache_creation_input_tokens": 0,
                        "model": str(info.get("model") or event.get("model") or "unknown"),
                    }

    return None


_STREAM_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _persist_stream_usage_snapshot(snapshot: Optional[Dict[str, Any]]) -> None:
    """Accumulate a usage snapshot for skill-dispatch post-mission accounting.

    A single skill subprocess may make several provider calls (e.g. the main
    work plus a backend review/fix gate). The sidecar must hold the SUM of all
    of them so the mission's post-mission accounting reflects real consumption
    — overwriting would attribute only the last call (typically a small gate
    review) and silently drop the rest.
    """
    if not snapshot:
        return
    target = os.environ.get("KOAN_STREAM_USAGE_FILE", "").strip()
    if not target:
        return
    try:
        merged = dict(snapshot)
        existing_raw = ""
        try:
            existing_raw = Path(target).read_text().strip()
        except OSError:
            existing_raw = ""
        if existing_raw:
            try:
                prev = json.loads(existing_raw)
            except (json.JSONDecodeError, ValueError):
                prev = None
            if isinstance(prev, dict):
                for key in _STREAM_USAGE_TOKEN_KEYS:
                    merged[key] = (
                        int(prev.get(key, 0) or 0)
                        + int(snapshot.get(key, 0) or 0)
                    )
                if prev.get("model") and not merged.get("model"):
                    merged["model"] = prev["model"]
        Path(target).write_text(json.dumps(merged, separators=(",", ":")))
    except OSError as exc:
        print(f"[provider] WARNING: stream usage sidecar write failed: {exc}", file=sys.stderr)


def missing_binary_message(err: "FileNotFoundError", cmd, provider_name: str, model_key: str) -> str:
    """Actionable message for a provider launch that failed with FileNotFoundError.

    Only fires when the missing executable IS the provider binary (``cmd[0]``);
    a FileNotFoundError for some other file the CLI tried to open is re-raised
    unchanged so it is never masked. Shared by ``run_command_streaming`` (skill
    path, raises RuntimeError) and ``run.run_claude_task`` (mission path,
    returns exit 127 + writes this to stderr).
    """
    executable = err.filename or str(cmd[0])
    if executable != str(cmd[0]):
        raise err
    return (
        f"CLI executable not found: {executable!r} "
        f"(provider {provider_name!r}). Ensure it is on PATH or "
        f"configure cli.{model_key}: {provider_name}:/absolute/path."
    )


def run_command_streaming(
    prompt: str,
    project_path: str,
    allowed_tools: List[str],
    model_key: str = "chat",
    model: str = "",
    max_turns: int = 10,
    timeout: int = 300,
    max_turns_source: Optional[str] = "skill_max_turns",
    project_name: str = "",
    mcp_configs: Optional[List[str]] = None,
    project_context: bool = True,
    idle_timeout: Optional[int] = None,
) -> str:
    """Build and run a CLI command, streaming progress to stdout in real time.

    Some CLIs buffer rendered text until the session ends. For high-effort
    skills that can mean tens of minutes of silent tool use, which the
    skill-runner liveness watchdog in run.py reads as a hang and kills.

    Providers that support JSONL progress events opt in here: Claude uses
    ``--output-format stream-json --verbose`` and Codex uses ``--json``.
    Each event is rendered into a short human-readable line printed to the
    runner's stdout, so the parent watchdog sees real activity and
    ``/live`` shows what the provider is doing. The final assistant text is
    extracted from provider-specific result/message events so callers'
    return-value contract stays unchanged.

    Providers that don't support JSONL progress fall through to the
    original raw text path; lines that fail to parse as JSON are still
    printed and contribute to the return value.

    ``mcp_configs`` is an optional list of MCP server config paths (or
    ``None`` to omit ``--mcp-config``). Callers should resolve it through
    ``config.mcp_configs_for_role(role, project_name)`` so the per-role
    allowlist and kill switch apply.

    ``project_context=False`` suppresses project-scope settings, ``CLAUDE.md``
    and skills loaded from ``project_path``. Pass it whenever *project_path* is
    untrusted — a reviewed branch can carry a ``.claude/settings.json`` that
    defines hooks, which is code execution on this host.

    ``idle_timeout`` bounds **inactivity**, and is the only bound that reaches
    the read loop. ``timeout`` is applied by the ``proc.wait()`` *after* stdout
    EOF, so a provider that prints its session banner and then goes silent
    forever blocks in ``for line in proc.stdout`` and is never bounded by it;
    only run.py's outer skill-runner watchdog ends such a run, by SIGKILLing
    the whole runner. With ``idle_timeout`` set, every consumed line heartbeats
    a :class:`~app.subprocess_runner.LivenessWatchdog` and a stall raises
    ``RuntimeError`` the caller can attribute and degrade on. A wall-clock cap
    would be the wrong instrument — a healthy long pass streams progress for
    many minutes. Default ``None`` keeps the historical unbounded behavior;
    an opted-in caller must pick a value strictly below ``first_output_timeout``
    or the outer watchdog still wins.

    Raises:
        RuntimeError: If the command exits with non-zero code (except
            max-turns, which returns partial output).
    """
    # Resolve the CLI provider for this role (cli: section), swapping to the
    # cli.fallback up front if the role's binary is unavailable. An explicit
    # `model` arg still wins over the role-derived model.
    provider, models = _resolve_role_provider_and_models(model_key, project_name)
    use_stream_json = provider.supports_stream_json()
    cmd = build_full_command(
        prompt=prompt,
        allowed_tools=allowed_tools,
        model=model or models.get(model_key, ""),
        fallback=models.get("fallback", ""),
        max_turns=max_turns,
        output_format="stream-json" if use_stream_json else "",
        mcp_configs=mcp_configs,
        provider=provider,
        read_only=model_key in READ_ONLY_ROLES,
        project_context=project_context,
    )
    last_message_path: Optional[str] = None
    if provider.supports_last_message_file():
        from app.utils import koan_tmp_dir

        fd, last_message_path = tempfile.mkstemp(
            prefix="koan-last-message-",
            suffix=".txt",
            dir=koan_tmp_dir(),
        )
        os.close(fd)
        cmd = provider.add_last_message_file_args(cmd, last_message_path)

    print(f"[cli] Starting {provider.name or 'provider'} CLI session", flush=True)

    from app.cli_exec import popen_cli

    # raw_lines is scanned only for error/max-turns detection, never returned,
    # so a bounded tail is safe and caps RAM on long provider streams. 2000 is
    # deliberately generous so terminal _format_cli_error context and the
    # non-stream-json max-turns regex fallback keep working.
    raw_lines = deque(maxlen=2000)  # for error reporting (terminal lines)
    # text_lines IS the fallback return value when no result event arrives —
    # it must stay unbounded or long sessions would silently lose output.
    # Block-style providers (Claude, Haze segments) append full segments and
    # are joined with newlines. Delta-style providers (Grok Build text/data,
    # Gemini assistant message deltas — see _is_text_delta_event) buffer into
    # text_delta_parts and flush as one segment so "hel"+"lo" becomes "hello",
    # not "hel\\nlo".
    text_lines: List[str] = []  # fallback return value when no result event
    text_delta_parts: List[str] = []
    final_result: Optional[str] = None
    usage_snapshot: Optional[Dict[str, Any]] = None
    saw_max_turns_event = False
    saw_cancelled_end = False
    failed_result_status = ""
    failed_result_error = ""
    stderr_text = ""

    def _flush_text_deltas() -> None:
        if text_delta_parts:
            text_lines.append("".join(text_delta_parts))
            text_delta_parts.clear()

    try:
        bounded = bool(idle_timeout and idle_timeout > 0)
        spawn_kwargs = {}
        if bounded:
            # Only when the watchdog is armed. The watchdog group-kills, and a
            # child sharing Kōan's group would make that kill land on the
            # daemon itself — so an armed watchdog *requires* session
            # isolation. But isolation is not free: run.py's skill-runner
            # teardown and mission_scope's fallback both reap by process
            # group, and a child in its own session is outside both. Applying
            # it unconditionally would put every non-opted-in caller's provider
            # beyond that teardown, buying nothing (no watchdog to protect) and
            # letting a stuck provider outlive the runner that spawned it.
            spawn_kwargs["start_new_session"] = True
        try:
            proc, cleanup = popen_cli(
                cmd,
                provider=provider,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                cwd=project_path,
                **spawn_kwargs,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                missing_binary_message(e, cmd, provider.name, model_key)
            ) from e
        idle_watchdog = None
        if bounded:
            from app.subprocess_runner import LivenessWatchdog

            # graceful=False: SIGTERM-then-escalate stops escalating once the
            # leader exits, so a descendant that ignores SIGTERM survives while
            # still holding the inherited stdout write end. The read loop below
            # would then never see EOF, never reach the `fired` check, and hang
            # exactly as it did before this watchdog existed.
            idle_watchdog = LivenessWatchdog(
                proc, idle_timeout, graceful=False,
            ).start()
        # Every print() in this loop is the load-bearing watchdog signal —
        # run.py's skill-runner liveness watchdog (600s) resets on each line
        # emitted to stdout. Do not silence these prints; doing so reintroduces
        # the silent-CLI hang this PR fixes (see PR #1372).
        try:
            for line in proc.stdout:
                stripped = line.rstrip("\n")
                raw_lines.append(stripped)
                # Every consumed line is activity, blank ones included — the
                # `continue` below skips rendering, not liveness.
                if idle_watchdog is not None:
                    idle_watchdog.heartbeat()
                if not stripped:
                    continue
                event: Optional[Dict[str, Any]] = None
                if use_stream_json:
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            event = parsed
                    except (json.JSONDecodeError, ValueError):
                        event = None
                if event is not None:
                    print(_summarize_stream_event(event), flush=True)
                    event_usage = _usage_snapshot_from_event(event)
                    if event_usage is not None:
                        usage_snapshot = event_usage
                    # Accumulate assistant text blocks so a stream that dies
                    # before the final ``result`` event (timeout, watchdog
                    # kill, SIGPIPE) still returns whatever the provider managed
                    # to print, instead of silently returning "".
                    chunks = _extract_assistant_text_chunks(event)
                    if _is_text_delta_event(event):
                        text_delta_parts.extend(chunks)
                    else:
                        _flush_text_deltas()
                        text_lines.extend(chunks)
                    result_text = _extract_result_text(event)
                    if result_text is not None:
                        final_result = result_text
                    if _is_stream_json_max_turns(event):
                        saw_max_turns_event = True
                    if _is_cancelled_end_event(event):
                        saw_cancelled_end = True
                    if _is_failed_result_event(event):
                        failed_result_status = str(event.get("status") or "")
                        failed_result_error = _error_message(event.get("error"))
                else:
                    # Non-JSON: provider doesn't speak stream-json or a stray
                    # warning slipped in. Print and remember for the fallback.
                    _flush_text_deltas()
                    print(stripped, flush=True)
                    text_lines.append(stripped)
            _flush_text_deltas()
            if idle_watchdog is not None:
                # Disarm the moment the read loop ends. `stderr.read()` and
                # `proc.wait()` below emit no heartbeats, so a watchdog still
                # armed across them would SIGKILL a run that already completed
                # and surface as an opaque exit -9 — past the `fired` check,
                # so not even attributable. mark_completed() is what actually
                # closes it: Timer.cancel() is a no-op once `_fire` has begun,
                # and the graceful=False kill has no poll() guard.
                idle_watchdog.mark_completed()
                idle_watchdog.cancel()
            if idle_watchdog is not None and idle_watchdog.fired:
                # The watchdog already SIGKILLed the group, which is what ended
                # the read loop. Reap the corpse and report the stall rather
                # than letting it surface as an opaque exit -9 further down.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                # Carry whatever the pass streamed before going silent, same as
                # the cancelled-end path: a stall after most findings were
                # printed must stay diagnosable from the error alone.
                partial = (final_result or "\n".join(text_lines)).strip()
                suffix = f" Partial output: {partial[:200]}" if partial else ""
                raise RuntimeError(
                    f"CLI stalled — no output for {idle_timeout}s{suffix}"
                )
            stderr_text = proc.stderr.read() if proc.stderr else ""
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            proc.wait()
            raise RuntimeError(f"CLI invocation timed out after {timeout}s") from e
        finally:
            # The loop-exit path above already disarmed; this covers an
            # exception raised mid-loop. A live timer outliving this call could
            # group-kill a recycled PID.
            if idle_watchdog is not None:
                idle_watchdog.mark_completed()
                idle_watchdog.cancel()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            cleanup()

        raw_stdout = "\n".join(raw_lines)
        # The legacy regex still fires on non-stream-json output (codex,
        # warnings printed before the stream begins) and on stream-json
        # results whose subtype encodes the limit.
        hit_max_turns = saw_max_turns_event or _is_max_turns_error(raw_stdout)
        last_message_text = ""
        if last_message_path:
            with contextlib.suppress(OSError, UnicodeDecodeError):
                last_message_text = Path(last_message_path).read_text()
        if last_message_text.strip():
            return_text = last_message_text
        elif final_result is not None:
            return_text = final_result
        else:
            return_text = "\n".join(text_lines)

        if saw_cancelled_end:
            # Hard failure: cancelled sessions look like exit 0 with partial
            # text (Grok headless permission_cancelled). Callers must not
            # treat this as productive work.
            _persist_stream_usage_snapshot(usage_snapshot)
            detail = (return_text or "").strip()
            suffix = f" Partial output: {detail[:200]}" if detail else ""
            raise RuntimeError(
                "CLI session cancelled (stopReason=Cancelled) — often a "
                "headless permission denial. For Grok, ensure "
                "skip_permissions: true so tools use --always-approve."
                f"{suffix}"
            )

        if proc.returncode != 0:
            # Max-turns is a graceful limit — return partial output so callers
            # can extract useful results from an incomplete session.
            if hit_max_turns:
                _warn_max_turns(max_turns, max_turns_source)
                from app.claude_step import strip_cli_noise
                _persist_stream_usage_snapshot(usage_snapshot)
                return strip_cli_noise(return_text.strip())
            # Checked BEFORE the failed-result envelope: _format_cli_error is
            # the only path that carries stderr and the exit code to the
            # caller, and quota/auth classification is text-based on that
            # payload (RESOURCE_EXHAUSTED / 429 / 401 live on stderr).
            raise RuntimeError(
                _format_cli_error(proc.returncode, raw_stdout, stderr_text)
            )

        if failed_result_status:
            # Same hole as the cancelled-end case above, reported through a
            # terminal ``result`` envelope instead of an ``end`` event: exit 0
            # plus partial prose.
            _persist_stream_usage_snapshot(usage_snapshot)
            detail = (return_text or "").strip()
            suffix = f" Partial output: {detail[:200]}" if detail else ""
            reason = f" ({failed_result_error})" if failed_result_error else ""
            raise RuntimeError(
                f"CLI session ended with status={failed_result_status}{reason} — "
                "often a headless permission denial. Set skip_permissions: true "
                "so tool calls are not left waiting on a confirmation prompt."
                f"{suffix}"
            )

        if hit_max_turns:
            _warn_max_turns(max_turns, max_turns_source)

        from app.claude_step import strip_cli_noise
        _persist_stream_usage_snapshot(usage_snapshot)
        return strip_cli_noise(return_text.strip())
    finally:
        if last_message_path:
            with contextlib.suppress(OSError):
                os.unlink(last_message_path)
