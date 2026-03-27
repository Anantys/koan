"""OpenAI Codex CLI provider implementation."""

import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

from app.provider.base import CLIProvider


class CodexProvider(CLIProvider):
    """OpenAI Codex CLI provider.

    Translates Kōan's generic command spec into Codex CLI equivalents.
    Uses ``codex exec`` for non-interactive (scripted/autonomous) execution.

    Key differences from Claude CLI:
    - Binary: 'codex'
    - Non-interactive: 'codex exec "prompt"' (prompt is positional)
    - Tool control: No per-tool allow/disallow flags; uses sandbox policies
    - Model: --model flag (same as Claude)
    - No --fallback-model equivalent
    - No --append-system-prompt (falls back to prepend via base class)
    - No --max-turns (runs to completion)
    - Output: --json flag for JSONL events (not --output-format)
    - Permissions: --yolo (equivalent to Claude's --dangerously-skip-permissions)
    - MCP: configured via config.toml, not CLI flags

    Configuration (config.yaml):
        cli_provider: "codex"

    Environment:
        KOAN_CLI_PROVIDER=codex
    """

    name = "codex"

    def binary(self) -> str:
        return "codex"

    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def build_permission_args(self, skip_permissions: bool = False) -> List[str]:
        # Codex equivalent: --yolo bypasses approvals and sandbox entirely.
        #
        # When skip_permissions=False we use --full-auto rather than Codex's
        # interactive default, because Kōan runs headless (codex exec) where
        # interactive approval prompts would block forever.  --full-auto
        # grants workspace-write sandbox + on-request approval, which is the
        # least-privilege mode that still works unattended.
        #
        # TODO: for read-only contexts (chat, review mode) a future
        # enhancement could pass --sandbox read-only instead.
        if skip_permissions:
            return ["--yolo"]
        return ["--full-auto"]

    def build_prompt_args(self, prompt: str) -> List[str]:
        # Codex non-interactive mode: codex exec "prompt"
        return ["exec", prompt]

    def build_tool_args(
        self,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
    ) -> List[str]:
        # Codex CLI does not support per-tool allow/disallow flags.
        # Tool access is controlled via sandbox policies (--sandbox flag)
        # and approval modes (--ask-for-approval).
        # We silently ignore tool specifications — the sandbox policy
        # set via build_permission_args controls what Codex can do.
        return []

    def build_model_args(self, model: str = "", fallback: str = "") -> List[str]:
        flags: List[str] = []
        if model:
            flags.extend(["--model", model])
        # Codex has no --fallback-model; ignored silently
        return flags

    def build_output_args(self, fmt: str = "") -> List[str]:
        # Codex uses --json for machine-readable JSONL output.
        # Without it, codex exec prints formatted text to stdout
        # (which is what Kōan expects for most use cases).
        # We do NOT pass --json by default because Kōan's output
        # parsing expects plain text, not JSONL events.
        return []

    def build_max_turns_args(self, max_turns: int = 0) -> List[str]:
        # Codex CLI does not support --max-turns.
        # codex exec runs to completion.
        return []

    def build_mcp_args(self, configs: Optional[List[str]] = None) -> List[str]:
        # Codex configures MCP servers via config.toml, not CLI flags.
        # Users should configure MCP in ~/.codex/config.toml [mcp_servers].
        return []

    def build_plugin_args(self, plugin_dirs: Optional[List[str]] = None) -> List[str]:
        # Codex uses skills (stored in ~/.codex/skills/ or .codex/skills/),
        # not plugin directories. Silently ignored.
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
        """Build a complete Codex CLI command.

        Codex exec command structure:
            codex [global-flags] exec [exec-flags] "prompt"

        Global flags (--model, --yolo, etc.) must come before 'exec'.
        The prompt is a positional argument to exec.
        """
        # Handle system prompt: Codex has no --append-system-prompt,
        # so prepend to user prompt (base class fallback behavior).
        if system_prompt:
            prompt = system_prompt + "\n\n" + prompt

        cmd = [self.binary()]

        # Global flags go before 'exec'
        cmd.extend(self.build_permission_args(skip_permissions))
        cmd.extend(self.build_model_args(model, fallback))

        # 'exec' subcommand + prompt (positional) — delegate to
        # build_prompt_args() so standalone callers get the same shape.
        cmd.extend(self.build_prompt_args(prompt))

        # Exec-specific flags go after prompt if needed in future

        return cmd

    def check_quota_available(self, project_path: str, timeout: int = 15) -> Tuple[bool, str]:
        """Check Codex API quota via a minimal exec probe.

        Sends a tiny prompt ("ok") to surface rate-limit or subscription
        errors before a full mission is attempted.

        NOTE: Unlike Claude's zero-cost ``claude usage``, this probe
        consumes a small number of tokens on each call.  Kōan's main
        loop calls this before every mission, so the cost is real but
        negligible compared to the mission itself.
        """
        cmd = [self.binary(), "--full-auto", "exec", "ok"]

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
            return True, ""
        except Exception as e:
            print(f"[codex] quota probe error: {e}", file=sys.stderr)
            return True, ""
