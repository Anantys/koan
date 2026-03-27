# Local LLM Provider

The local provider connects Koan to any OpenAI-compatible LLM server
running on your machine. This gives you full offline operation with no
API costs — ideal for experimentation, privacy-sensitive work, or when
you've exhausted your cloud quota.

## Quick Setup

### 1. Install a Local LLM Server

Pick one:

**Ollama (recommended for beginners)**

```bash
# macOS
brew install ollama

# Start the server
ollama serve

# Pull a model
ollama pull qwen2.5-coder:14b
```

Ollama exposes an OpenAI-compatible API at `http://localhost:11434/v1`.

**llama.cpp**

```bash
# Build from source
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp && make

# Download a GGUF model and start the server
./llama-server -m /path/to/model.gguf --port 8080
```

API at `http://localhost:8080/v1`.

**LM Studio**

Download from https://lmstudio.ai, load a model, and start the local
server from the UI. API at `http://localhost:1234/v1`.

**vLLM**

```bash
pip install vllm
vllm serve "Qwen/Qwen2.5-Coder-14B" --port 8000
```

API at `http://localhost:8000/v1`.

### 2. Configure Koan

In `config.yaml`:

```yaml
cli_provider: "local"

local_llm:
  base_url: "http://localhost:11434/v1"   # Adjust for your server
  model: "qwen2.5-coder:14b"             # Model name on your server
  api_key: ""                              # Usually empty for local servers
```

Or via environment variables (in `.env`):

```bash
KOAN_CLI_PROVIDER=local
KOAN_LOCAL_LLM_BASE_URL=http://localhost:11434/v1
KOAN_LOCAL_LLM_MODEL=qwen2.5-coder:14b
KOAN_LOCAL_LLM_API_KEY=
```

Environment variables override `config.yaml` values.

### 3. Start Everything

If you're using Ollama, the easiest way to start Koan is:

```bash
make ollama
```

This single command starts all three components in the background:
1. `ollama serve` — the LLM server
2. The Telegram bridge (awake)
3. The agent loop (run)

To stop everything:

```bash
make stop
```

This stops all Koan processes including `ollama serve`.

To check what's running:

```bash
make status
```

Environment variables from `.env` (like `OLLAMA_HOST`) are automatically
loaded and passed to all components.

### 4. Verify (Optional)

If you want to manually verify the LLM server:

```bash
# Quick test with curl
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen2.5-coder:14b", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Ollama Launch Provider (Recommended)

If you have Ollama v0.15.0+, the `ollama-launch` provider is the simplest
way to run Koan with local models. Ollama handles server lifecycle and
environment setup automatically:

```yaml
# config.yaml
cli_provider: "ollama-launch"

ollama_launch:
  model: "qwen3-coder"
```

Or via environment:

```bash
KOAN_CLI_PROVIDER=ollama-launch
KOAN_OLLAMA_LAUNCH_MODEL=qwen3-coder
```

Then start Koan normally with `make start` — no need for `make ollama`.

**Advantages over `local` provider:**
- No manual `ollama serve` — Ollama auto-starts the server
- No env-var setup (ANTHROPIC_BASE_URL etc.)
- `OLLAMA_NO_CLOUD=1` set by default for privacy
- Version validation on startup

**Model management via Telegram:**

```
/ollama list       — List locally available models
/ollama pull NAME  — Download a new model
/ollama remove NAME — Delete a local model
/ollama status     — Server health check
```

## Per-Project Configuration

Use local LLM for specific projects (e.g., small libraries) while
keeping Claude for complex work:

```yaml
# projects.yaml
defaults:
  cli_provider: "claude"

projects:
  critical-app:
    path: "/path/to/app"
    # Uses Claude (default)

  side-project:
    path: "/path/to/side"
    cli_provider: "local"   # Use local LLM for this project
```

## How It Works

Unlike Claude and Copilot which call external CLI binaries, the local
provider runs Koan's own `local_llm_runner.py` — a Python-based agentic
loop that:

1. Sends your prompt + system context to the LLM via the OpenAI API
2. Parses `tool_calls` from the response (function calling format)
3. Executes tools locally (read, write, edit, grep, glob, shell)
4. Feeds results back to the LLM
5. Repeats until the LLM produces a final text response or max turns

This means any LLM server supporting the OpenAI function calling
protocol will work.

## Recommended Models

Not all local models handle tool use (function calling) well. Models
that work best with Koan's agentic loop:

| Model | Size | Tool Use | Notes |
|-------|------|----------|-------|
| `qwen3-coder` | 14B+ | Excellent | Best choice for agentic coding workflows |
| `qwen2.5-coder:14b` | 14B | Good | Solid balance of size and capability |
| `glm-4.7` | 30B | Good | Lightweight deployment, strong reasoning |
| `deepseek-coder-v2:16b` | 16B | Good | Strong coding, good function calling |
| `qwen2.5-coder:7b` | 7B | Fair | Lighter, faster, less reliable tool use |

**Hardware requirements vary by model size:**

- 7B models: 8GB RAM minimum, 16GB recommended
- 14B models: 16GB RAM minimum, 32GB recommended
- 34B+ models: 32GB+ RAM, consider GPU acceleration

## Provider Differences

| Feature | Claude Code | Local LLM |
|---------|------------|-----------|
| Binary | `claude` (external) | Python runner (built-in) |
| Tool protocol | Native Claude tools | OpenAI function calling |
| Fallback model | Supported | Not supported |
| MCP support | Yes | No (tools are built-in) |
| Output format | JSON supported | JSON supported |
| Max turns | Supported | Supported |
| Cost | API subscription | Free (hardware only) |
| Quality | State of the art | Varies by model |
| Speed | Network latency | Local inference speed |

## Configuration Reference

### config.yaml

```yaml
cli_provider: "local"

local_llm:
  # Server URL — must expose /v1/chat/completions
  base_url: "http://localhost:11434/v1"

  # Model name — as recognized by your server
  model: "qwen2.5-coder:14b"

  # API key — usually empty for local servers
  # Some servers (e.g., vLLM with auth) may require one
  api_key: ""
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KOAN_CLI_PROVIDER` | `claude` | Set to `local` to enable |
| `KOAN_LOCAL_LLM_BASE_URL` | `http://localhost:11434/v1` | Server URL |
| `KOAN_LOCAL_LLM_MODEL` | (none) | Model name (required) |
| `KOAN_LOCAL_LLM_API_KEY` | (none) | API key if needed |

### Default Server URLs by Platform

| Server | Default URL |
|--------|-------------|
| Ollama | `http://localhost:11434/v1` |
| llama.cpp | `http://localhost:8080/v1` |
| LM Studio | `http://localhost:1234/v1` |
| vLLM | `http://localhost:8000/v1` |

## Troubleshooting

### "Connection refused" or timeout

Your LLM server isn't running. Start it:

```bash
# Ollama
ollama serve

# llama.cpp
./llama-server -m /path/to/model.gguf

# Check if the server is up
curl http://localhost:11434/v1/models
```

### Model not found

The model name in your config doesn't match what's loaded on the
server.

```bash
# Ollama — list available models
ollama list

# Pull a model if needed
ollama pull qwen2.5-coder:14b
```

### Poor quality results / tool use failures

Local models have variable tool-use capability. If the agent produces
garbage or ignores tool results:

1. Try a larger model (14B+ recommended for tool use)
2. Try a different model family (Qwen2.5-Coder works well)
3. Consider using Claude for complex missions and local LLM for simpler
   tasks via per-project configuration

### Slow inference

Local inference speed depends on your hardware. Tips:

- Use quantized models (Q4_K_M is a good balance)
- Enable GPU acceleration in your server config
- Use a smaller model for chat/lightweight tasks
- Set `models.lightweight` to a smaller local model

### API key errors

Most local servers don't require an API key. If yours does, set it in
config:

```yaml
local_llm:
  api_key: "your-key-here"
```

Or via environment:

```bash
KOAN_LOCAL_LLM_API_KEY=your-key-here
```
