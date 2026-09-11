---
type: component-spec
title: "Component Spec — CLI Provider Abstraction"
description: "Design contract for the CLI provider abstraction that decouples the agent loop from any single AI coding CLI (Claude, Cline, Codex, Copilot, Haze, Grok, Gemini) behind one `CLIProvider` contract."
tags: [providers]
created: 2026-06-27
updated: 2026-09-10
---

# Component Spec — CLI Provider Abstraction

**Package:** `koan/app/provider/` (`base.py`, `claude.py`, `cline.py`, `codex.py`,
`copilot.py`, `fake.py`, `haze.py`, `grok.py`, `gemini.py`, `__init__.py`) + `cli_provider.py` (legacy re-export facade)

## Purpose

Decouple the agent loop from any single AI CLI. Kōan invokes an external coding CLI as
a subprocess; this layer abstracts *which* CLI, its flags, its tool-name vocabulary, and
its usage-tracking quirks behind one `CLIProvider` contract.

## Architecture

```
provider/__init__.py  → registry + resolution (env → config → default) + cached singleton
       │                 convenience: run_command(), run_command_streaming(), build_full_command()
       ├─ base.py      → CLIProvider ABC + tool-name constants + usage hooks
       ├─ claude.py    → ClaudeProvider (Claude Code CLI)
       ├─ cline.py     → ClineProvider
       ├─ codex.py     → CodexProvider (quota via stream-json summary only)
       ├─ copilot.py   → CopilotProvider (with tool-name mapping)
       ├─ fake.py      → FakeProvider (fail-closed test/dev stub; never a real LLM)
       ├─ haze.py      → HazeProvider (haze ≥0.7.0 headless stream-json)
       ├─ grok.py      → GrokProvider (xAI Grok Build headless streaming-json)
       └─ gemini.py    → GeminiProvider (Google Gemini CLI headless stream-json)
```

## Key types & functions

| Symbol | Contract |
|---|---|
| `base.CLIProvider` | The contract: build command, run, stream, tool-name vocabulary. |
| `base.supports_usage_tracking()` / `record_usage()` | Per-provider usage hooks. Not all CLIs surface usage the same way. |
| `__init__.run_command()` / `run_command_streaming()` | The single invocation entry points. Both accept an optional `mcp_configs` list; callers pass it only for roles opted into MCP via `config.mcp_roles` (resolved through `config.mcp_configs_for_role(role, project_name)`). Omitted/`None` → no `--mcp-config` is emitted. Callers should not spawn provider subprocesses directly. |
| `__init__.build_full_command()` | Assembles the provider-specific argv. |
| `__init__.get_provider_display()` / `get_cli_binary_name()` | Display helpers. `get_provider_display()` returns `"<name>"` or `"<name> (<binary>)"` when `KOAN_CLAUDE_CLI_PATH` points at a different binary. Single source of truth for the global provider line shown by the startup banner and `/status`. Per-role provider overrides are summarized separately by `describe_cli_roles()`. |
| `base.custom_binary_name()` / `__init__.provider_cli_display(provider)` | Per-instance attribution helpers. `custom_binary_name()` returns the basename of a pinned custom binary (per-role `_binary_override` from `cli.<role>: flavor:path`; Claude also surfaces the global `KOAN_CLAUDE_CLI_PATH`), or `''` when no override is configured. `provider_cli_display(provider)` returns that basename or, failing that, the provider flavor name — used by `review_runner._review_attribution()` so the review footer shows the CLI that actually ran (e.g. `claude-deep`), not just the flavor. Only real overrides count: a provider's natural fallback (Copilot's `gh`) is never surfaced as "custom". |
| `__init__.get_provider_for_role(role, project_name)` / `get_fallback_provider(project_name)` / `resolve_role_provider(role, project_name)` | Per-role provider selection (the `cli:` config section). `get_provider_for_role` returns the **global cached singleton** when the role is unset (parity) or a **fresh** `_PROVIDERS[flavor](binary_path=path)` otherwise — never written to `_cached_provider`. `get_fallback_provider` returns the single section-wide `cli.fallback` instance (or `None`). `resolve_role_provider` is the stateless-helper entry point: it pre-flight-swaps to the fallback when the role binary is unavailable. |
| `cli:` config / `config.get_cli_config()` / `get_cli_fallback()` | New config section parallel to `models:`. `cli.default.<role>` (+ per-project flat `cli.<role>`) maps a mission role (`mission`/`chat`/`lightweight`/`review_mode`/`reflect`) to a `flavor` or `flavor:path`; a single `cli.fallback` provider is used on launch/auth failure. The role's MODEL resolves against that provider's `models.<provider>.<role>` block (`get_model_config(role_providers=…)`). Replaces the removed `KOAN_CLAUDE_CLI_FOR_REVIEW_PATH`. |
| `effort:` config / `config.get_effort(mode, mission_type)` / `CLIProvider.build_effort_args()` | Reasoning-effort control for the Claude `--effort` flag (low/medium/high/max). `effort:` mapping keys are **mission types** (the `session_tracker.classify_mission_type` taxonomy: plan/review/implement/audit/…), not budget modes. Resolution in `get_effort()`: `effort.<mission_type>` → `effort.<autonomous_mode>` (legacy) → `_DEFAULT_EFFORT_MAP[mode]` (the dynamic default). The dynamic default — review→low, deep→high, else none — is preserved verbatim when `effort:` is absent; a per-type pin only layers on top. `build_mission_command()` classifies the mission type and passes it through, so a pin only reaches `get_effort()` for missions that run through the main agent loop — **not** for skill-dispatched commands (`/review`, `/plan`, …), which bypass `build_mission_command()` (see reach caveat below); `get_effort_for_mode()` is the type-unaware wrapper for callers outside the mission build path. `extended thinking` short-circuits effort to `max`. |
| Provider resolution | Order: `KOAN_CLI_PROVIDER` env (fallback `CLI_PROVIDER`) → `projects.yaml`/`config.yaml` → default. Centralized in `utils.get_cli_provider_env()`. This resolves the GLOBAL provider; `cli.<role>` layers per-role selection on top via `get_provider_for_role`. |
| `CLIProvider(binary_path="")` / `ClaudeProvider.binary()` | The base class takes an optional per-instance `binary_path` override (the replacement for the removed review ContextVar); `_resolve_binary_path()` is the shared resolver (absolute → as-is / relative → `normpath(join(KOAN_ROOT, …))` / bare name → PATH lookup). `ClaudeProvider.binary()`: `_binary_override` if set → else `KOAN_CLAUDE_CLI_PATH` → else `"claude"`. Every provider's `binary()` honors the override so `flavor:path` works uniformly. Relative paths root at `KOAN_ROOT` (not CWD — the agent runs from `KOAN_ROOT/koan`); bare names are never re-rooted. |
| `build_command(..., project_context=True)` / `build_project_context_args` / `build_full_command(..., project_context=…)` | When `project_context=False`, the provider must suppress **project-scope** tooling loaded from cwd (Claude: `--setting-sources user`). Default `True` preserves mission/project CLAUDE.md / skills. Other providers may no-op. Callers that run with `cwd=KOAN_ROOT` (Telegram chat, **dashboard web chat**, contemplative, rituals, outbox formatting) **must** pass `False`. Do not implement isolation by mutating the worktree (`skip-worktree` / quarantine) on this path. |

### MCP per-role boundary (safety contract)

MCP servers are loaded per **execution role**, not globally. `config.mcp_roles`
(default `["mission", "contemplative", "plan"]`; per-project override in
projects.yaml replaces the list) is the allowlist of roles that receive
`--mcp-config`. Conversational roles consuming untrusted input (`chat`,
`github_reply`) are excluded by default and opt-in only. `mcp_roles: []` is a
kill switch: no runner passes `--mcp-config`. Loading a server never grants its
tools — MCP tools must still be allowlisted via qualified names
(`mcp__<server>` / `mcp__<server>__<tool>`) in the role's `tools:` list unless
`skip_permissions` is set. Callers resolve configs through
`config.mcp_configs_for_role(role, project_name)` rather than
`get_mcp_configs()` directly so the gate and kill switch always apply.

## Invariants

- **KOAN_ROOT runtime sessions must not load contributor project tooling.**
  Telegram chat, dashboard web chat, contemplative, rituals, and outbox
  formatting run with `cwd=KOAN_ROOT` on a deployed clone (dashboard may
  also target a selected project path — only the KOAN_ROOT case requires
  `False`). They must pass `project_context=False` so Claude does not
  auto-load root `CLAUDE.md` / `AGENTS.md` / `.claude/skills` (e.g. `brain`,
  `speckit-*`) into operator-facing output. Mission sessions keep the default
  (`True`) so `workspace/` project guidance still loads. Isolation is at the
  **CLI flag boundary**, not by relocating tracked files on disk.
- **One invocation lock per uid.** Provider auth state is per-user, so the subprocess
  lock lives under `koan_tmp_dir()` (per-uid), not a fixed `/tmp` path.
- **Provider resolution has a fixed precedence** (env → config → default) for the
  GLOBAL provider. Per-role selection (`cli.<role>`) layers on top via
  `get_provider_for_role`; it does not introduce a second GLOBAL resolution path.
- **`KOAN_CLAUDE_CLI_PATH` and `cli: flavor:path` relative paths root at `KOAN_ROOT`, not CWD.** The
  agent runs from `KOAN_ROOT/koan` (the Makefile does `cd koan`), so a naive
  relative path would resolve to the wrong place. The shared `_resolve_binary_path()`
  joins against `KOAN_ROOT`; a future simplification that re-targets the join at
  CWD silently breaks every such setup. Bare command names stay PATH lookups and
  are never re-rooted.
- **Per-role provider instances must never poison the global singleton.**
  `get_provider_for_role`/`get_fallback_provider` construct a fresh
  `_PROVIDERS[flavor](binary_path=path)` and return it directly; they must never
  assign `_cached_provider`. `get_provider()` (role-less) stays the cached
  singleton. A path-bearing instance leaking into the cache would silently
  rebind every role-less caller to a custom binary.
- **The `cli:` absence contract is exact parity.** With no `cli:` section, every
  role resolves to `(get_provider_name(), "")` and `get_model_config(role_providers=None)`
  is byte-for-byte the historical behavior. Changes here must preserve that.
- **The `effort:` absence contract is the dynamic default.** With no `effort:`
  section, `get_effort()` returns `_DEFAULT_EFFORT_MAP[mode]` (review→low,
  deep→high, else `""`) — the historical budget-mode-driven behavior, untouched.
  Per-mission-type pins only layer on top: `effort.<mission_type>` (a
  `classify_mission_type` category) wins over `effort.<mode>`, which wins over
  the dynamic default. **Reach caveat:** this path is wired only into
  `build_mission_command()`, which the main agent loop calls for missions that
  are *not* dispatched to a dedicated skill runner. Skill-dispatched commands
  (`/review`, `/plan`, `/rebase`, `/recreate`, `/implement`, `/fix`, `/audit`,
  `/check`, …) are routed to their own runners before this path and are not
  governed by `effort:` — so a `review: low` pin has no effect on `/review`,
  which runs in `review_runner`. Slash commands *without* a dedicated runner
  don't reach it either: `/refactor`/`/pr` are handled by their bridge-side
  handler or failed as an unknown skill in `_handle_skill_dispatch` before
  `build_mission_command`. In practice the only pins that fire are
  **`autonomous`** and **`freetext`** (non-slash missions). A partial dict
  leaves unlisted modes on the dynamic default (not disabled), preserving the
  absence contract per-mode.
  `get_effort_for_mode()` is the type-unaware wrapper and must stay equivalent
  to `get_effort(mode, "")`. `config_validator` accepts any `effort.*` key (the
  mission-type set is open) but validates every value — dict entries *and* the
  scalar shorthand (`effort: "high"`) — against `_VALID_EFFORT_LEVELS`, so a
  typo'd level warns rather than silently dropping the flag.
- **Footer attribution shows the binary that ran, then falls back to the flavor.**
  `review_runner._review_attribution()` is the single source of truth for the review
  footer's CLI label: `provider_cli_display()` surfaces the basename of a pinned
  review binary (`cli.review_mode: flavor:path`, or Claude's `KOAN_CLAUDE_CLI_PATH`)
  so the signature reads e.g. `claude-deep`, not `Claude`; with no override it falls
  back to the provider flavor. `pr_footer._provider_label()` title-cases known
  provider flavors (`claude` → `Claude`) but renders custom binary basenames
  verbatim — they are technical identifiers, not brand names.
- **Provider fallback is launch/auth only, never quota/transient.** The single
  `cli.fallback` provider is substituted only on binary-not-found (exit 127 /
  `is_available()` False) or `ErrorCategory.AUTH`, and (on the mission path) only
  when no commits were produced. Quota still pauses; transient errors still use
  the in-place retry. Do not widen this to quota — that would double-spend across
  subscriptions and change the pause contract.
- **Root handling for `skip_permissions` is Claude-specific.** The Claude CLI
  refuses `--dangerously-skip-permissions` under root/sudo, so
  `ClaudeProvider.build_permission_args()` (inherited by `OllamaLaunchProvider`)
  drops the flag under euid 0 with a once-per-process warning.
  `config.get_skip_permissions()` stays a pure config read — moving the root
  check there would silently strip Codex full access and Cline auto-approve
  for root deployments, whose CLIs accept the setting.
- **The `fake` provider is fail-closed by construction.** `FakeProvider` is a
  test/dev stub that never invokes a real LLM. Its `__init__` raises
  `FakeProviderNotAllowed` (a `RuntimeError`) unless `KOAN_ALLOW_FAKE_PROVIDER`
  is truthy (`1`/`true`/`yes`/`on`). The guard lives in the constructor — not at
  selection time — so **every** resolution path that instantiates a provider
  (`get_provider`, `get_provider_by_name`, `get_provider_for_role`,
  `get_fallback_provider`, all via `_PROVIDERS[name]()`) fails closed identically.
  Selecting `fake` without the flag must **error**, never silently fall back to a
  real provider. `binary()` returns the POSIX no-op `true` (never `claude`) so the
  built command runs harmlessly with empty output; `is_available()` is
  unconditionally `True` (no external binary) and `has_api_quota()` is `False`
  (no budget gating). The `RuntimeError` base means the broad `except Exception`
  guards in `quota_handler._detect_quota_for_provider` /
  `cli_errors._detect_auth_for_provider` degrade conservatively when a name-based
  lookup of `fake` is attempted without the flag. **`get_fallback_provider` is the
  one exception to "fail loud":** it is contractually `Optional` and is called on
  *any* non-zero mission exit (`mission_executor._maybe_fallback_provider_rerun`),
  so a `cli.fallback: fake` without the flag must return `None` (decline the
  fallback), not raise — otherwise an unrelated real-provider failure would crash
  during finalization. This is still not a silent swap: no work is routed to `fake`,
  and the primary selection paths (`get_provider`/`get_provider_for_role`) still
  error loudly. Response routing (canned/scripted output) is out of scope for this
  foundation — `build_command` is a no-op stub. `FakeProvider` sets the base-class
  `test_only = True` flag: it stays in `_PROVIDERS` (so `known_providers`,
  name-based lookup, and config validation resolve it), but UI-facing pickers use
  `selectable_providers()` — which filters `test_only` flavors — so `fake` never
  appears as a selectable option in the dashboard provider dropdown. The refusal
  message derives its real-provider hint from the registry (`known_providers()`
  minus `fake`) so it does not drift as providers are added.
- **Quota/usage extraction is provider-specific.** Claude exposes usage in
  `modelUsage` (no top-level `model` field); codex surfaces quota only via the
  stream-json summary (`rate_limit_rejected`, stdout JSONL — never stderr); haze
  reports usage only in its terminal result envelope with **camelCase** fields
  (`inputTokens`/`outputTokens`/`cacheReadTokens`/`cacheWriteTokens`/`reasoningTokens`);
  Grok Build reports **snake_case** `usage` on the terminal ``end`` event
  (`input_tokens`/`output_tokens`/`cache_read_input_tokens`/…) plus optional
  `modelUsage` map (camelCase per model id); Gemini CLI reports usage on the
  terminal `result` event under **`stats`** (not `usage`) as
  `input_tokens`/`output_tokens`/`cached`/`total_tokens` plus a per-model-id
  `models` map (json mode nests the counts as
  `stats.models.<id>.tokens.{prompt,candidates,thoughts,tool,cached}` instead).
  A `cached` count is a SUBSET of input and is clamped to it before
  subtraction — an inconsistent count is logged once, never double-counted, and
  a `stats.cached` that collides with an explicit `usage` cache field is logged
  once rather than dropped in silence. Shared extractors
  are shape-keyed
  on field names. Detectors read the summary stream, not assistant text.
- **`tool_use` summary grammar carries an optional input preview.**
  `_summarize_stream_event()` renders a `tool_use` block as
  `[cli] assistant — tool_use: <name>[: <input-preview>]`. The optional
  `: <preview>` suffix is a bounded first-line excerpt of the tool input
  (see `_tool_input_preview` / `_TOOL_PREVIEW_KEYS`) and is additive:
  consumers that key off `tool_use: <name>` (substring) or off the quota
  markers (`rate_limit_rejected`, session-limit phrasing) are unaffected.
  The display-side `log_fmt.py` splits name from preview on the first `": "`.
  Free-text preview values (tool-input and `text:` excerpts) never contain the
  `", "` part delimiter — `_summarize_stream_event()` collapses it to a bare
  comma (`_drop_part_sep`) so the display splitter (`log_fmt._PART_SEP`) can
  never mis-split a preview into a spurious part.
- **`tool_result` summary grammar carries an optional failure reason.**
  `_summarize_stream_event()` renders a failed tool result as
  `[cli] tool_result <id> (error)[: <reason>]`. The optional `: <reason>` suffix
  is a bounded first-line excerpt of the result content (`_tool_result_preview`,
  120 chars) emitted only when the provider supplies one, so consumers keying
  off `tool_result` or `(error)` (substring) are unaffected. Like the `tool_use`
  preview it is `_drop_part_sep`-cleaned, so it can never carry the `", "` part
  delimiter. Both display surfaces split the reason off the first `": "` through
  one shared helper (`log_fmt._tool_error_reason`) so `make logs` and the
  dashboard timeline cannot drift. Successful `tool_result` lines stay
  suppressed on the display side; error lines are never suppressed.
- **A read-only execution role is enforced by a POSITIVE tool allowlist, with
  denial as a second layer.** `allowed_tools` is a pre-approval list —
  `--allowedTools Read,Glob,Grep` leaves `Bash` fully usable (verified on Claude
  CLI 2.1.234; the CLI's own reference says *"To restrict which tools are
  available, use `--tools` instead"*). Roles in `READ_ONLY_ROLES` are therefore
  restricted to `READ_ONLY_TOOLS` via `--tools` when the provider declares
  `supports_tool_restriction()`, **and** have `SIDE_EFFECT_TOOLS` added to
  `disallowed_tools`. `read_only` is also forwarded to `build_permission_args`
  so a provider with a sandbox can express the posture directly. `read_only`
  **overrides** `skip_permissions`, because `--dangerously-skip-permissions` /
  `--dangerously-bypass-approvals-and-sandbox` would bypass both.

  The allowlist is the primary mechanism because it is **total by
  construction**: anything not named — `MultiEdit`, `Task` and `Skill` (either
  of which can spawn a write-capable subagent), and every built-in tool a future
  CLI release adds — is withheld without anyone having to enumerate it.
  Enumerated denial is a treadmill that fails open on each new tool.
  `SIDE_EFFECT_TOOLS` is retained anyway, deliberately: an operator may pin an
  older binary via `cli.review_mode: claude:/path` that ignores an unknown
  `--tools`, and two independent mechanisms that each fail closed are the right
  posture for a security boundary. **Do not "simplify" by removing either.**

  Measured on Claude CLI 2.1.235 — under `--tools "Read,Glob,Grep"` the model
  reports only those three, and both a `Bash` and a `Write` call produce no
  filesystem effect. Note the measurement was made against the *filesystem*: two
  separate runs asked the model to enumerate its own tools and disagreed with
  each other, one of them hallucinating `Write`. A self-report is a claim; only
  the side effect is evidence.

  Capability is declared per provider by three predicates —
  `supports_tool_restriction()` (positive allowlist), `supports_tool_denial()`
  (deny by name) and `supports_read_only_sandbox()` (OS-level sandbox) —
  combined by `enforces_read_only()`. Claude and ollama-launch restrict and
  deny; Codex ignores tool arguments entirely and is instead pinned to
  `--sandbox read-only`. Every other adapter declares none of the three
  and is therefore refused — **but the reason differs per provider, and the
  predicate is a claim about Kōan's adapter, not about the vendor CLI**:
  - `cline` and `haze` genuinely cannot: `build_tool_args` returns `[]` and
    haze's docstring states headless runs with full tool access.
  - `copilot`'s adapter emits no deny flag. The Copilot CLI itself does have
    `--deny-tool`; wiring it up is unclaimed work, not an upstream limitation.
  - `grok` **does** emit `--tools` / `--disallowed-tools`, so the flags exist —
    but two things block the claim. Kōan has never verified that they *withhold*
    rather than merely pre-approve, which is precisely the assumption this whole
    invariant exists to disprove (the Claude behaviour above is asserted only
    because it was measured). And `GrokProvider.build_permission_args` returns
    `--always-approve` **unconditionally**, ignoring `read_only` — a blanket
    auto-approve of exactly the kind a read-only role refuses to honour on
    Claude and Codex. Enabling grok requires measuring the flags AND making
    `--always-approve` conditional, in that order.
  - `gemini` has `--approval-mode plan` and `--sandbox`, and its `--allowed-tools`
    is *deprecated upstream* in favour of the Policy Engine — a pre-approval
    list, not a withholding one, which is exactly the shape this invariant
    exists to distrust. Kōan's adapter emits neither, so the predicate is
    False. Enabling it means wiring the Policy Engine (or a measured
    `--approval-mode plan`) and recording the measurement.
  - `fake` is test-only and refuses instantiation outright.

  Do not flip a `supports_*` predicate because a flag exists. Flip it because
  the behaviour was measured, and record the measurement.
- **A read-only role never inherits ambient MCP servers.** `--tools` restricts
  the *built-in* set only. Measured on Claude CLI 2.1.235: under
  `--tools "Read,Glob,Grep"` every MCP tool from the operator's user-level
  configuration was still present, including write-capable ones. A read-only
  invocation therefore also passes `--strict-mcp-config` and no `--mcp-config`,
  which reduces the exposed MCP tool count to zero. Without this the positive
  allowlist is not total and the boundary is only as narrow as whatever the
  operator happens to have installed.
- **A read-only role does not load the target repo's agent configuration — on
  providers with a real isolation mechanism.** The review path passes
  `project_context=False`, which suppresses `<cwd>/.claude/settings.json`,
  `<cwd>/CLAUDE.md` and `<cwd>/.claude/skills/`. This matters because a reviewed
  branch is untrusted input: a `.claude/settings.json` in a hostile PR can define
  `SessionStart` / `PreToolUse` hooks, which is arbitrary code execution on the
  review host triggered by opening a pull request. Measured on 2.1.235: with a
  planted `SessionStart` hook in the working directory, `--setting-sources user`
  suppressed it while a hook supplied via `--settings` still fired.

  **This suppression is Claude-specific and is NOT delivered on every provider
  the docs endorse for `/review`.** The mechanism is ``--setting-sources user``,
  emitted only by `ClaudeProvider.build_project_context_args` (and, by
  inheritance, `ollama-launch`). `CodexProvider.build_command` accepts
  `project_context` "for API parity" but emits nothing, so a review run on codex
  **still loads the reviewed branch's `AGENTS.md`** from the untrusted worktree —
  the "a pull request must not configure the agent that judges it" boundary is
  not held there until a first-class codex flag exists (see
  `docs/providers/codex.md` § Project-context isolation). Until then, either
  treat the codex review path as failing open for this one property, or scope
  codex reviews behind a provider that suppresses repo configs. Repo conventions
  still reach the model — `review_runner` injects them into the prompt
  explicitly, fenced as untrusted.
- **A read-only role that cannot be enforced FAILS CLOSED.** When `read_only` is
  True and the resolved provider reports `enforces_read_only() is False`,
  `build_full_command` raises `ReadOnlyUnenforceable` (a `RuntimeError`) naming
  the provider and the config key to change. It never builds the command. There
  is no advisory middle ground: a read-only role is a security boundary, and
  degrading it to a prompt-level suggestion would hand a review write access to
  the live project clone. The check runs **after** provider resolution, so a
  `cli.fallback` swap into an unenforceable provider is refused too. Providers
  that can enforce nothing remain fully usable for every non-read-only role —
  the refusal is scoped to the invocation, not the provider.
- **The enforcement reaches `read_only` invocations, NOT every `READ_ONLY_ROLES`
  consumer.** Both mechanisms above are driven by the `read_only` argument to
  `build_full_command`, which only the `run_command` / `run_command_streaming`
  helpers set (from `model_key in READ_ONLY_ROLES`). `build_full_command_managed`
  takes no `read_only` argument, so the mission path
  (`mission_runner.build_mission_command`, which resolves the `review_mode` role
  for `autonomous_mode == "review"`) is **not** covered: it passes a
  `Read`/`Glob`/`Grep` allowlist only, which does not withhold anything. Closing
  that gap is a separate decision about the autonomous mission path — it is a
  known, deliberate limitation of this contract, not an oversight, and must not
  be read as enforced.
- **A new `READ_ONLY_ROLES` caller inherits the refusal.** `/review` is the only
  `read_only` caller today. Any future call site that adopts a role listed in
  `READ_ONLY_ROLES` gets the fail-closed check for free — and must catch
  `ReadOnlyUnenforceable` explicitly and surface the configuration message,
  rather than letting it surface as a generic failure that sends the operator
  debugging the wrong thing.
- **Shared stream parsers extend by event SHAPE, never by provider name.** The
  central summarizer/text/usage extractors in `provider/__init__.py` (and the
  mission-stdout path in `token_parser.py`) branch on field presence
  (e.g. `inputTokens` ⇒ camelCase usage) so the agent loop never learns which
  provider is running (Provider Isolation). Adding a provider must not add
  `if provider == …` branches to shared code.
- **Haze headless contract (haze ≥ 0.7.0).** `HazeProvider` targets haze's
  documented harness mode: `--output stream-json` NDJSON progress events
  (`turn_start`/`message_*`/`tool_*`/`retry`/`context_overflow`/`turn_end`)
  terminated by a result envelope `{type:"result", status, result, usage}` that is
  byte-identical to `--output json`; exit code 0 ⇔ status `complete`
  (`failed`/`aborted` are failures, never success). Because haze streams,
  it uses the standard `supports_stream_json()` path — **no**
  incremental-progress capability flag and **no** agent-loop bypass may be
  (re)introduced for it. Prompt delivery: the *target* design is stdin via a
  flag-REMOVAL `rewrite_prompt_for_stdin()` (haze reads stdin only when `-p`
  is absent; the base marker substitution would send the marker as the literal
  prompt), but stdin passing is **disabled**
  (`supports_stdin_prompt_passing()` False) until upstream fixes its stdin
  gate — haze checks `process.stdin.isTTY === false` and Node reports
  `undefined` for pipes/files, so piped runs fall into the interactive UI
  (verified live 2026-07-10). Until then the prompt rides argv as `-p`
  (subject to OS per-argument limits); the dormant rewrite stays implemented
  and tested so the flip is one line. Headless haze is one-shot (no session
  resume) and exposes no
  per-tool/MCP/plugin/max-turns/fallback-model/effort controls — those inputs
  are skipped but never silently: a two-tier notice policy applies, deduped
  once per process. Static capabilities driven by Kōan's OWN defaults
  (per-tool allow/deny, max turns, fallback model — passed unconditionally by
  the loop; the operator cannot act) log at **info**; operator-actionable
  config (MCP, plugins, effort, resume, system-prompt file — removable) and
  the safety-relevant no-permission-gates notice log at **warning**.
  Quota/auth detection uses
  backend-agnostic patterns (haze fronts OpenAI/OpenRouter/local backends):
  stderr trusted fully, stdout only on non-zero exit with an error-marker gate.
  Pre-flight quota check is a minimal token-consuming `--output json` probe
  (cline precedent) run from a fresh EMPTY scratch directory — never the
  project dir, whose CLAUDE.md/AGENTS.md context haze would ingest (~12K
  tokens per probe); probe errors never block work. Invocation lock:
  `haze-cli` (shared `~/.haze/settings.json` state).
- **Grok Build headless contract (verified 0.2.101).** `GrokProvider` targets
  xAI Grok Build headless mode: `grok` with `--output-format streaming-json`
  (Koan internal name `stream-json` maps to CLI spelling `streaming-json`).
  NDJSON event vocabulary is shape-keyed: `thought`/`text` carry incremental
  `data` deltas; terminal `end` carries `stopReason`, `usage` (snake_case),
  `num_turns`, and optional `modelUsage` — **not** the final assistant body.
  Final text is the concatenation of `text.data` deltas (joined with `""`, not
  newlines). `--output-format json` returns a single object with top-level
  `text` + `usage` (probe mode).
  **Permissions (headless invariant):** Koan **always** passes
  `--always-approve` for Grok headless invokes. Grok’s CLI `--permission-mode`
  flag only effectively applies `bypassPermissions` and `default`; passing
  `acceptEdits` is a no-op on the flag. In headless mode, any tool call that
  would prompt is **cancelled immediately** (`stopReason: Cancelled`,
  `cancellation_category: permission_cancelled`) — shell tools such as
  `run_terminal_command` then fail, so `/implement` lands no commits. Do not
  reintroduce `acceptEdits` as a “safer” headless default. Operators who want
  to signal intent still set `skip_permissions: true`; when it is false, Grok
  still emits `--always-approve` and logs a once-per-process notice.
  **Tools:** Claude/Koan tool names are mapped to Grok internal IDs before
  `--tools` / `--disallowed-tools` (e.g. `Read`→`read_file`, `Edit`→
  `search_replace`, `Bash`→`run_terminal_cmd`, `Grep`→`grep`, `Glob`→
  `list_dir`, `Write`→`write`, `WebFetch`→`web_fetch`). Unknown names warn
  once; unmapped `Skill` is dropped. Max turns → `--max-turns`; system prompt
  append → `--rules` (file prompts are inlined); effort → `--reasoning-effort`;
  resume → `--resume`. **Models:** Claude tier aliases (`haiku`/`sonnet`/
  `opus`/…) are never passed as `-m` — warn once and omit so Grok’s default
  model is used. **Cancelled is hard failure:** a terminal `end` with
  `stopReason` Cancelled/canceled raises (never soft-success with partial
  text). **Prompt delivery:** large prompts use `--prompt-file` (temp file +
  cleanup); stdin prompt passing stays off. Unsupported inputs (MCP flags,
  plugin dirs, fallback model) warn once and are skipped — never silently
  accepted. Quota/auth: stderr trusted; stdout only on non-zero exit with an
  error-marker gate. Pre-flight probe uses `--output-format json -p ok` from an
  empty scratch dir. Invocation lock: `grok-cli` (shared `~/.grok/` state).
  Recorded samples: `koan/tests/grok_samples.py`. Operator docs:
  `docs/providers/grok.md`.
- **Gemini CLI headless contract (documented, not yet measured).**
  `GeminiProvider` targets Google Gemini CLI headless mode: `gemini -p <prompt>`
  with `--output-format stream-json`. NDJSON event vocabulary is shape-keyed:
  `init` (`session_id`/`model`), `message` (`role`/`content`, `delta: true` for
  assistant chunks), `tool_use` (`tool_name`/`tool_id`/`parameters`),
  `tool_result` (`tool_id`/`status`/`output`/`error`), `error`
  (`severity`/`message`), and a terminal `result` (`status` + `stats`). Final
  text is the concatenation of assistant `message.content` deltas (joined with
  `""`, not newlines) — the terminal `result` carries stats, **not** the
  assistant body. `--output-format json` — the shape the **mission path**
  builds — returns one object with top-level `response` + `stats`; both are
  consumed shape-keyed: `response` joins the `result`/`content`/`text` key list
  in `parse_claude_output`, and its `stats.models.<id>.tokens`
  (`prompt`/`candidates`/`thoughts`/`tool`/`cached`, the nested SessionMetrics
  shape) is summed across models by `token_parser`, so json-mode runs record
  usage rather than zero. Bucket mapping follows the Gemini API `usageMetadata`
  fields these counters mirror: `thoughts` is billed **output** (thinking
  models spend a large share there) and `tool` is prompt-side **input**.
  When a `models` map names more than one id, tokens are attributed to
  the **dominant** entry — ranked by those same counters, so attribution and
  reported totals cannot disagree — never the first key: a session that
  fell back pro→flash must not be mis-priced.
  **A terminal `result` whose `status` is not a success value is a hard
  failure** (`RuntimeError` naming `skip_permissions`), not a soft return of
  partial text: a headless run that could not answer a confirmation prompt
  exits 0 with prose and would otherwise be reported as a complete mission with
  no branch and no commit. `stats` is **optional** on that envelope — a session
  that aborts before any model call emits `status` + `error` only — so the
  guard is keyed on the *text-less* terminal shape, leaving text-bearing
  `result` envelopes (haze) with their existing behavior. The failure is
  reported **after** the exit-code branch: a non-zero exit still raises
  `_format_cli_error(...)`, which is the only payload carrying stderr and the
  exit code, and quota/auth classification is text-matched on it.
  "Success value" is an **allowlist** (`success`/`succeeded`/`complete`/
  `completed`/`ok`), never a list of known failure words: a status this adapter
  has not seen (`timeout`, `quota_exceeded`, a word a future build adds) must
  fail closed. A missing/empty `status` reports no verdict and is left alone.
  **Both** stream consumers enforce this — `run_command_streaming` and
  `claude_step.run_claude` (which drives the post-mission commit step) — so a
  failed session cannot become a commit subject on either path.
  The **json mission path** applies the same rule from the other end: a json
  object carrying a non-empty top-level `error` fails the mission
  (`mission_runner.json_output_reports_failure`), so partial `response` prose
  is never banked as completed work on an exit-0 abort. That synthetic failure
  is **finalization-only**: it must never be fed to the text classifiers as
  evidence that the CLI *process* failed. `mission_executor` snapshots the real
  exit code and passes it as `trust_stdout` / the quota handler's `exit_code`
  (including through `run_post_mission`'s `cli_exit_code`), preserving the
  invariant that stdout is scanned for quota/auth only after a genuine process
  failure — otherwise assistant prose about rate limits would pause the daemon
  for every project on a mission that merely hit an approval prompt.
  **`error` events are advisory.** `severity` is recorded in the vocabulary but
  deliberately not a failure trigger: upstream emits `error` events for
  recoverable conditions (a failed tool call the model then works around), and
  the authoritative session verdict is the terminal `result` status plus the
  exit code. Making a mid-stream `error` fatal would abort sessions that
  recover. Revisit only with a measured stream showing a fatal `severity:
  "error"` with no terminal `result`.
  **Permissions:** Kōan emits `--approval-mode yolo` **only** when
  `skip_permissions` is set. With permissions on, no approval flag is emitted
  and a once-per-process warning states that headless runs cannot answer a
  confirmation prompt — deliberately *not* Grok's unconditional auto-approve:
  a provider adapter must not escalate beyond what the operator configured.
  **Unsupported inputs** (per-tool allow/deny, max turns, MCP config paths,
  plugin dirs, reasoning effort, fallback model, system-prompt file) warn once
  and are skipped, never silently accepted. `--allowed-tools` is deprecated
  upstream (Policy Engine) and is deliberately **not** emitted.
  **Models:** Claude tier aliases (`haiku`/`sonnet`/`opus`/`claude-*`) are never
  passed as `-m` — warn once and omit, so Gemini's own default (`auto`) applies.
  **Prompt delivery** rides argv as `-p`; stdin passing stays off (Gemini
  *appends* `-p` to piped stdin rather than replacing it, so the base marker
  rewrite would corrupt the prompt) and there is no `--prompt-file` flag.
  Quota/auth: stderr trusted; stdout only on non-zero exit with an error-marker
  gate. Pre-flight probe uses `--output-format json -p ok` from an empty scratch
  dir. Invocation lock: `gemini-cli` (shared `~/.gemini/` state).
  **Not measured:** unlike the Grok and Haze contracts above, this one is
  derived from upstream documentation and source (`packages/core/src/output/
  types.ts`), not from a live run on a Kōan host. Exit codes `42` (input error)
  and `53` (turn limit) are documented but not mapped to Kōan's max-turns
  handling. Recorded samples: `koan/tests/gemini_samples.py`. Operator docs:
  `docs/providers/gemini.md`.
- **The streaming read loop must be inactivity-bounded, and the child must be
  session-isolated.** `run_command_streaming` consumes `proc.stdout` with a
  blocking `for line in ...`. Its `timeout` argument reaches only the
  `proc.wait(timeout=...)` that runs **after** stdout EOF, so a provider that
  opens the pipe, prints its session banner and then goes silent forever is not
  bounded by it at all — the loop simply blocks. The only thing that ever ended
  such a run was run.py's outer skill-runner liveness watchdog
  (`first_output_timeout`, default 600s), which SIGKILLs the whole runner
  mid-pipeline: the mission dies with a generic "Skill runner timed out", no
  partial result is written, and the stall is not attributable to the pass that
  caused it. A hard wall-clock bound is the wrong instrument here — a healthy
  long pass emits progress events for many minutes and must not be capped — so
  the bound is on **inactivity**: callers opt in with `idle_timeout`, a
  `LivenessWatchdog` heartbeats on every consumed line, and a stall raises
  `RuntimeError` that the caller can attribute and degrade on. `idle_timeout`
  defaults to `None`, which preserves the historical (unbounded) behavior for
  callers that have not opted in. An opted-in caller MUST pick a value strictly
  below `first_output_timeout`, otherwise the outer watchdog still wins and the
  inner bound is decorative. On the review path the two clocks are the *same*
  clock — run.py's watchdog resets on the read loop's per-event `print()`,
  which is what heartbeats the inner one — so the inner value MUST be derived
  as a small fixed margin below the outer budget (`outer - 60`), never a
  fraction of it: halving it would not add a bound where none existed, it
  would halve the silence the pass was always allowed and kill legitimate long
  turns.
- **Session isolation is scoped to the armed watchdog, and the watchdog's kill
  is SIGKILL-to-the-group.** These two follow from the bound above and are as
  load-bearing as it is.
  `start_new_session=True` is passed **only when `idle_timeout` is set**. An
  armed watchdog requires it — the kill is a group kill, and a child sharing
  Kōan's process group would make it SIGKILL the daemon itself. But isolation
  is not free in the other direction: `run.py`'s skill-runner teardown and
  `mission_scope`'s fallback path both reap by process group, and a child in
  its own session is outside both. Isolating unconditionally would put every
  non-opted-in caller's provider beyond that teardown with no watchdog to
  justify it, so a stuck provider could outlive a skill timeout, an abort, or
  the outer liveness kill while still burning quota.
  The kill must be `force_kill_process_group` (`graceful=False`), not the
  SIGTERM-then-escalate default: escalation stops as soon as the *leader*
  exits, so a descendant that handles or ignores SIGTERM survives it — and a
  survivor holding the inherited stdout write end keeps the reader blocked,
  never reaching the fired check. That is the same hang the watchdog exists to
  end, re-entered through the kill path.
  Residual, accepted: an opted-in child is outside the outer group teardown, so
  an abort or `skill_timeout` that fires while the provider is *actively
  streaming* leaves it running. It is bounded by its own strictly-tighter idle
  watchdog, which is why the trade is worth taking; closing it fully needs the
  outer teardown to track isolated provider sessions.
- **An inactivity watchdog must be disarmed when the read loop ends, not when
  the call returns.** `proc.stderr.read()` and `proc.wait()` run after stdout
  EOF and emit no heartbeats, so a watchdog still armed across them kills a run
  that already streamed everything — and past the `fired` check, so the kill
  surfaces as an opaque `exit -9` instead of an attributable stall. Disarming
  MUST use `mark_completed()` as well as `cancel()`: `threading.Timer.cancel()`
  is a no-op once `_fire` has begun, and the `graceful=False` kill path has no
  `poll()` guard, so a late fire would `killpg` a possibly recycled PID. The
  same applies to every `LivenessWatchdog` feeding a pipe read loop —
  `cli_exec.stream_with_timeout` (the `/rebase` review and CI phases) included,
  where a graceful kill would make `rebase_review_idle_timeout` ineffective
  against a SIGTERM-surviving descendant and misattribute the resulting hang to
  `rebase_review_max_duration`.

## Integration points

- **Startup availability gate.** `app.cli_health.check_primary_cli()` wraps
  `get_provider().is_available()` (`shutil.which(binary())`) as the single probe used by
  `startup_manager.check_cli_binary()` (enters an in-memory degraded/no-mission mode on a
  miss — see `specs/components/agent-loop.md`), the `/status` skill, and the `/doctor`
  diagnostics (`environment_check` / `connectivity_check`, which resolve the real
  `provider.binary()` rather than a hardcoded provider→binary map). `provider.missing_binary_message()`
  is the shared constructor for the actionable "CLI executable not found" error raised by
  `run_command_streaming` and (as an exit-127 failure) by `run.run_claude_task`.
- Invoked by `run.run_claude_task()` and skill runners.
- Usage flows to `usage_tracker.py` / `burn_rate.py` via the `record_usage()` hook.
  Structured per-call events are written to `instance/usage/*.jsonl` by
  `cost_tracker.record_usage()`, which now carries an optional `mission_id`
  (resolved best-effort from `.api-missions.json` in `mission_runner._record_cost_event`).
  `cost_tracker.aggregate_mission_usage(instance_dir, mission_id, mission_text=…)`
  is the per-mission read path used by `GET /v1/missions/{id}`.
- **Skill-dispatch token capture**: streaming skill runs persist per-call token
  totals to `KOAN_STREAM_USAGE_FILE` (summed across calls), appended to the stdout
  capture so `_ensure_tokens` parses real tokens. When that sidecar is empty,
  `_record_cost_event` backfills `input/output/cost` from the provider session tail
  (`get_session_data`) so command-missions do not record placeholder zeros.
- Per-role provider selection from the `cli:` section (`config.get_cli_config()`),
  threaded into `mission_runner.build_mission_command()` (mission/review roles),
  the `run_command*` helpers (their `model_key` role), and
  `contemplative_runner.build_contemplative_command()` (lightweight role). The
  launch/auth fallback re-run lives in `mission_executor._maybe_fallback_provider_rerun()`.
- `devcontainer.py` wraps the provider argv with `devcontainer exec` (claude-only
  credential steps); the fallback re-run re-applies this wrap.

## Known debt / watch-outs

- `cli_provider.py` is a legacy re-export — prefer importing from `provider` directly.
- `projects_config.get_project_cli_provider()` (the old per-project global-provider
  accessor) is still NOT wired into `get_provider()`; the `cli:` section (incl. its
  per-project flat form) is the supported per-project provider mechanism going forward.
- The stateless `run_command*` helpers fall back on *launch* failure (binary
  unavailable) via `resolve_role_provider`'s pre-flight `is_available()` swap;
  full AUTH-triggered fallback exists only on the stateful mission path.
- `ClaudeProvider` has no `detect_auth_failure()` override, so auth signals like
  "Please run /login" must be caught by the shared `_AUTH_RE` patterns against
  `[cli]`-prefixed runtime lines before delegating to the provider.
- Adding a provider means: subclass `CLIProvider`, register it, add tool-name mapping,
  and define usage extraction — partial implementations silently degrade usage tracking.

## Change protocol

A new provider or a change to the `CLIProvider` contract updates this spec, adds a
provider doc under `docs/providers/`, and verifies usage extraction against a recorded
sample of that CLI's output format.
