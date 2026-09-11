# Checkout-local command-line tools (`bin/`)

## REST client

`koan-cli` is a spec-driven client for Kōan's REST API. It reads the committed
OpenAPI document, manages secure named profiles, and exposes every documented
operation. It is independent from the provider wrappers below; do not set it as
`KOAN_CLAUDE_CLI_PATH`. See the [Kōan REST CLI guide](../docs/users/koan-cli.md).

## Community provider wrappers

These are small, self-contained wrapper scripts that make Koan's **Claude
provider** drive an alternative model backend, without any Koan code changes.

## How they plug in

Each wrapper is a `claude`-compatible executable. Point Koan at one with:

```bash
# .env
KOAN_CLAUDE_CLI_PATH=bin/<wrapper>
```

The path may be **relative to `KOAN_ROOT`** (recommended — keeps the config
portable across installs/copies) or absolute. Koan's `ClaudeProvider` reads
`KOAN_CLAUDE_CLI_PATH` (see `koan/app/provider/claude.py`), resolves a relative
value against `KOAN_ROOT`, and invokes it exactly like the real `claude` CLI,
e.g. `-p "<prompt>" --model <name> --output-format json --verbose …`.

## The contract every wrapper must honor

1. Accept the full Claude CLI argument vector unchanged.
2. If the backend selects models differently, pull `--model` (both
   `--model X` and `--model=X`) out of the vector and re-emit it in the
   backend's form; forward all other args verbatim (the prompt may contain
   spaces/newlines — use a bash array, never re-split). Drop Claude-only
   flags the backend can't honor (e.g. `--fallback-model`, an Anthropic
   tier name) so they never reach it.
3. `exec` the backend so exit codes, stdio, and signals pass through
   (Koan relies on these for JSON parsing, quota detection, and its
   stagnation/timeout killer).
4. Fail fast with a non-zero exit and a clear stderr message if a required
   dependency is missing, so Koan classifies it as a real error.
5. Re-apply the exec bit after clone if your umask strips it:
   `chmod +x bin/<wrapper>`.

## Available wrappers

| Wrapper | Backend | Setup guide |
|---------|---------|-------------|
| `oc-claude` | OpenCode Go (via `ocgo` proxy) | [docs/providers/opencode.md](../docs/providers/opencode.md) |
| `ollama-claude` | Local Ollama (via `ollama launch claude`) | [docs/providers/ollama-wrapper.md](../docs/providers/ollama-wrapper.md) |
| `zai-claude` | Z.ai / GLM (real `claude` + Z.ai endpoint) | [docs/providers/zai.md](../docs/providers/zai.md) |

## Adding a new flavor

Copy `oc-claude`, adjust the dependency check and the backend exec line, and
add a matching `docs/providers/<backend>.md` setup guide plus a row in the
table above. A planned future flavor is an OpenAI variant.
