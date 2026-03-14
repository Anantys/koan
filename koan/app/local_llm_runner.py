"""
Local LLM agentic runner for Kōan.

Provides a simple agentic loop that calls a local LLM via OpenAI-compatible
API and executes tool calls (read, write, edit, grep, glob, shell).

Supports any server exposing /v1/chat/completions:
- Ollama (http://localhost:11434/v1)
- llama.cpp server (http://localhost:8080/v1)
- LM Studio (http://localhost:1234/v1)
- vLLM (http://localhost:8000/v1)

Usage as CLI:
    python3 -m app.local_llm_runner --prompt "..." --model "..." --base-url "..."

The runner handles:
1. Sending the prompt + system context to the LLM
2. Parsing tool_calls from the response (OpenAI function calling format)
3. Executing tools locally and feeding results back
4. Repeating until the LLM produces a final text response or max_turns is hit
5. Outputting the result to stdout (plain text or JSON)
"""

import argparse
import glob as glob_module
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a string in a file with new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "old_string": {"type": "string", "description": "Text to find"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.py')"},
                    "path": {"type": "string", "description": "Base directory (default: cwd)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regex pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (default: cwd)"},
                    "file_glob": {"type": "string", "description": "File pattern filter (e.g., '*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": "Execute a user-installed skill by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "The skill name to invoke"},
                    "args": {"type": "string", "description": "Optional arguments for the skill"},
                },
                "required": ["skill"],
            },
        },
    },
]

# Re-use the canonical tool name mapping from the provider package
from app.provider.base import TOOL_NAME_MAP


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _resolve_path(path: str, cwd: str) -> Optional[str]:
    """Resolve a path relative to cwd and verify it stays within the sandbox.

    Returns None if the resolved path escapes the cwd subtree.
    """
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(cwd, path))
    real_cwd = os.path.realpath(cwd)
    if not (resolved == real_cwd or resolved.startswith(real_cwd + os.sep)):
        return None
    return resolved


def _tool_read_file(arguments: Dict[str, Any], cwd: str) -> str:
    path = _resolve_path(arguments["path"], cwd)
    if path is None:
        return f"Error: path escapes working directory: {arguments['path']}"
    if not os.path.isfile(path):
        return f"Error: file not found: {path}"
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    if len(content) > 50000:
        content = content[:50000] + "\n... (truncated)"
    return content


def _tool_write_file(arguments: Dict[str, Any], cwd: str) -> str:
    path = _resolve_path(arguments["path"], cwd)
    if path is None:
        return f"Error: path escapes working directory: {arguments['path']}"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    Path(path).write_text(arguments["content"], encoding="utf-8")
    return f"Written {len(arguments['content'])} chars to {path}"


def _tool_edit_file(arguments: Dict[str, Any], cwd: str) -> str:
    path = _resolve_path(arguments["path"], cwd)
    if path is None:
        return f"Error: path escapes working directory: {arguments['path']}"
    if not os.path.isfile(path):
        return f"Error: file not found: {path}"
    content = Path(path).read_text(encoding="utf-8")
    old = arguments["old_string"]
    new = arguments["new_string"]
    if old not in content:
        return f"Error: old_string not found in {path}"
    count = content.count(old)
    if count > 1:
        return f"Error: old_string matches {count} locations in {path}. Be more specific."
    content = content.replace(old, new, 1)
    Path(path).write_text(content, encoding="utf-8")
    return f"Edited {path}: replaced 1 occurrence"


def _tool_glob(arguments: Dict[str, Any], cwd: str) -> str:
    base = _resolve_path(arguments.get("path", cwd), cwd)
    if base is None:
        return f"Error: path escapes working directory: {arguments.get('path', '')}"
    pattern = arguments["pattern"]
    matches = sorted(glob_module.glob(os.path.join(base, pattern), recursive=True))
    if len(matches) > 200:
        total = len(matches)
        matches = matches[:200]
        return "\n".join(matches) + f"\n... ({total} matches, showing first 200)"
    return "\n".join(matches) if matches else "No matches found"


def _tool_grep(arguments: Dict[str, Any], cwd: str) -> str:
    pattern = arguments["pattern"]
    path = _resolve_path(arguments.get("path", cwd), cwd)
    if path is None:
        return f"Error: path escapes working directory: {arguments.get('path', '')}"
    file_glob = arguments.get("file_glob", "")
    cmd = ["grep", "-rn", "--include", file_glob, pattern, path] if file_glob else [
        "grep", "-rn", pattern, path
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL,
    )
    output = result.stdout
    if len(output) > 20000:
        output = output[:20000] + "\n... (truncated)"
    return output if output else "No matches found"


def _tool_shell(arguments: Dict[str, Any], cwd: str) -> str:
    command = arguments["command"]
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True,
        timeout=120, cwd=cwd, stdin=subprocess.DEVNULL,
    )
    output = result.stdout
    if result.stderr:
        output += "\nSTDERR:\n" + result.stderr
    if len(output) > 30000:
        output = output[:30000] + "\n... (truncated)"
    if not output.strip():
        output = f"(exit code {result.returncode})"
    return output


def _tool_skill(arguments: Dict[str, Any], cwd: str) -> str:
    skill_name = arguments["skill"]
    args = arguments.get("args", "")

    from app.skills import build_registry, execute_skill, SkillContext, SkillError

    koan_root = Path(os.environ.get("KOAN_ROOT", ""))
    instance_dir = koan_root / "instance"

    # Build registry including instance skills
    extra_dirs: list = []
    instance_skills = instance_dir / "skills"
    if instance_skills.is_dir():
        extra_dirs.append(instance_skills)
    registry = build_registry(extra_dirs=extra_dirs or None)

    skill = registry.find(skill_name)
    if skill is None:
        return f"Error: skill '{skill_name}' not found"

    ctx = SkillContext(
        koan_root=koan_root,
        instance_dir=instance_dir,
        command_name=skill_name,
        args=args,
    )
    result = execute_skill(skill, ctx)
    if isinstance(result, SkillError):
        return result.message
    return result or f"Skill '{skill_name}' executed (no output)"


_TOOL_HANDLERS = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "shell": _tool_shell,
    "skill": _tool_skill,
}


def _execute_tool(name: str, arguments: Dict[str, Any], cwd: str) -> str:
    """Execute a tool and return its output as a string."""
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return f"Error: unknown tool '{name}'"
    try:
        return handler(arguments, cwd)
    except subprocess.TimeoutExpired:
        return f"Error: tool '{name}' timed out"
    except Exception as e:
        return f"Error executing {name}: {e}"


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

def _call_api(
    base_url: str,
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    api_key: str = "",
    temperature: float = 0.0,
) -> Dict:
    """Call OpenAI-compatible chat completions API.

    Uses urllib to avoid requiring the openai package.
    """
    import urllib.request
    import urllib.error

    url = f"{base_url.rstrip('/')}/chat/completions"

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Cannot connect to {base_url}. Is the LLM server running? Error: {e.reason}"
        ) from e


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def _default_system_prompt() -> str:
    """Load the local LLM agent system prompt from the prompts directory."""
    try:
        from app.prompts import load_prompt
        return load_prompt("local-llm-agent")
    except (ImportError, OSError, ValueError):
        # Fallback if running outside the koan package context
        return (
            "You are an AI coding assistant. You have access to tools for "
            "reading, writing, and editing files, searching codebases, and "
            "running shell commands.\n\n"
            "When you need to perform an action, use the available tools. "
            "When you have completed the task or have a response ready, "
            "respond with your final answer as plain text (no tool calls).\n\n"
            "Be concise and direct. Focus on completing the task efficiently."
        )


def _filter_tools(
    allowed: Optional[List[str]] = None,
    disallowed: Optional[List[str]] = None,
) -> List[Dict]:
    """Filter tool definitions based on allowed/disallowed lists.

    Args use Koan canonical names (Read, Write, Edit, Glob, Grep, Bash).
    """
    allowed_funcs = None
    disallowed_funcs = set()

    if allowed is not None:
        allowed_funcs = {TOOL_NAME_MAP.get(t, t.lower()) for t in allowed}
    if disallowed:
        disallowed_funcs = {TOOL_NAME_MAP.get(t, t.lower()) for t in disallowed}

    result = []
    for tool in TOOL_DEFINITIONS:
        func_name = tool["function"]["name"]
        if allowed_funcs is not None and func_name not in allowed_funcs:
            continue
        if func_name in disallowed_funcs:
            continue
        result.append(tool)
    return result


def run_agent(
    prompt: str,
    base_url: str,
    model: str,
    api_key: str = "",
    max_turns: int = 10,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
    cwd: str = "",
    system_prompt: str = "",
) -> Dict[str, Any]:
    """Run the agentic loop.

    Returns a dict with:
        result: Final text response from the LLM
        input_tokens: Total input tokens used
        output_tokens: Total output tokens used
    """
    if not cwd:
        cwd = os.getcwd()

    tools = _filter_tools(allowed_tools, disallowed_tools)
    # Some local models don't support function calling well.
    # If no tools are available, skip tool definitions entirely.
    use_tools = len(tools) > 0

    sys_prompt = system_prompt or _default_system_prompt()
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": prompt},
    ]

    total_input_tokens = 0
    total_output_tokens = 0

    for turn in range(max_turns):
        try:
            response = _call_api(
                base_url=base_url,
                model=model,
                messages=messages,
                tools=tools if use_tools else None,
                api_key=api_key,
            )
        except RuntimeError as e:
            return {
                "result": f"Error: {e}",
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }

        # Track tokens
        usage = response.get("usage", {})
        total_input_tokens += usage.get("prompt_tokens", 0)
        total_output_tokens += usage.get("completion_tokens", 0)

        choices = response.get("choices") or []
        if not choices:
            return {
                "result": "Error: API returned empty choices",
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        choice = choices[0]
        message = choice.get("message", {})

        # If the model returned tool calls, execute them
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            # Add assistant message with tool calls to history
            messages.append(message)

            for tc in tool_calls:
                func = tc.get("function", {})
                func_name = func.get("name", "")
                try:
                    func_args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    func_args = {}

                tool_result = _execute_tool(func_name, func_args, cwd)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"call_{turn}_{func_name}"),
                    "content": tool_result,
                })

            continue  # Next turn — let LLM process tool results

        # No tool calls — this is the final response
        content = message.get("content", "")
        return {
            "result": content,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        }

    # Max turns reached — return whatever we have
    last_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            last_content = msg["content"]
            break

    return {
        "result": last_content or "(max turns reached without final response)",
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Local LLM agentic runner for Koan"
    )
    parser.add_argument("-p", "--prompt", required=True, help="Task prompt")
    parser.add_argument("--model", default="", help="Model name (e.g., glm4:latest)")
    parser.add_argument("--base-url", default="", help="API base URL")
    parser.add_argument("--api-key", default="", help="API key (if required)")
    parser.add_argument("--max-turns", type=int, default=10, help="Max agentic turns")
    parser.add_argument("--allowed-tools", default="", help="Comma-separated allowed tools")
    parser.add_argument("--disallowed-tools", default="", help="Comma-separated disallowed tools")
    parser.add_argument("--output-format", default="", help="Output format (json or empty for text)")
    parser.add_argument("--cwd", default="", help="Working directory")
    parser.add_argument("--system-prompt", default="", help="Custom system prompt")

    args = parser.parse_args()

    # Support stdin-based prompt passing (security: avoids ps leaking)
    if args.prompt == "@stdin":
        args.prompt = sys.stdin.read()

    # Resolve config from env vars if not provided via args
    base_url = args.base_url or os.environ.get("KOAN_LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
    model = args.model or os.environ.get("KOAN_LOCAL_LLM_MODEL", "")
    api_key = args.api_key or os.environ.get("KOAN_LOCAL_LLM_API_KEY", "")

    if not model:
        print("Error: --model is required (or set KOAN_LOCAL_LLM_MODEL)", file=sys.stderr)
        sys.exit(1)

    allowed = [t.strip() for t in args.allowed_tools.split(",") if t.strip()] if args.allowed_tools else None
    disallowed = [t.strip() for t in args.disallowed_tools.split(",") if t.strip()] if args.disallowed_tools else None

    result = run_agent(
        prompt=args.prompt,
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_turns=args.max_turns,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        cwd=args.cwd or os.getcwd(),
        system_prompt=args.system_prompt,
    )

    if args.output_format == "json":
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result.get("result", ""))


if __name__ == "__main__":
    main()
