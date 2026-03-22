"""Tests for local_llm_runner.py — local LLM agentic loop."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.local_llm_runner import (
    _call_api,
    _default_system_prompt,
    _execute_tool,
    _filter_tools,
    _resolve_path,
    _tool_glob,
    _tool_grep,
    _tool_read_file,
    _tool_shell,
    _tool_write_file,
    _tool_edit_file,
    run_agent,
    TOOL_DEFINITIONS,
    TOOL_NAME_MAP,
)


# ---------------------------------------------------------------------------
# Tool execution tests
# ---------------------------------------------------------------------------

class TestExecuteTool:
    """Tests for _execute_tool()."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create a test file
        self.test_file = os.path.join(self.tmpdir, "test.txt")
        Path(self.test_file).write_text("line 1\nline 2\nline 3\n")

    def test_read_file(self):
        result = _execute_tool("read_file", {"path": self.test_file}, self.tmpdir)
        assert "line 1" in result
        assert "line 2" in result

    def test_read_file_not_found(self):
        result = _execute_tool("read_file", {"path": "/nonexistent/file.txt"}, self.tmpdir)
        assert "Error" in result

    def test_read_file_relative(self):
        result = _execute_tool("read_file", {"path": "test.txt"}, self.tmpdir)
        assert "line 1" in result

    def test_write_file(self):
        out_path = os.path.join(self.tmpdir, "output.txt")
        result = _execute_tool("write_file", {"path": out_path, "content": "hello"}, self.tmpdir)
        assert "Written" in result
        assert Path(out_path).read_text() == "hello"

    def test_write_file_creates_dirs(self):
        out_path = os.path.join(self.tmpdir, "sub", "dir", "file.txt")
        result = _execute_tool("write_file", {"path": out_path, "content": "deep"}, self.tmpdir)
        assert "Written" in result
        assert Path(out_path).read_text() == "deep"

    def test_edit_file(self):
        result = _execute_tool(
            "edit_file",
            {"path": self.test_file, "old_string": "line 2", "new_string": "LINE TWO"},
            self.tmpdir,
        )
        assert "replaced" in result.lower() or "Edited" in result
        content = Path(self.test_file).read_text()
        assert "LINE TWO" in content
        assert "line 2" not in content

    def test_edit_file_not_found(self):
        result = _execute_tool(
            "edit_file",
            {"path": "/nonexistent.txt", "old_string": "x", "new_string": "y"},
            self.tmpdir,
        )
        assert "Error" in result

    def test_edit_file_string_not_found(self):
        result = _execute_tool(
            "edit_file",
            {"path": self.test_file, "old_string": "nonexistent", "new_string": "y"},
            self.tmpdir,
        )
        assert "not found" in result.lower()

    def test_edit_file_ambiguous(self):
        # Write a file with duplicate content
        dup_file = os.path.join(self.tmpdir, "dup.txt")
        Path(dup_file).write_text("aaa\naaa\n")
        result = _execute_tool(
            "edit_file",
            {"path": dup_file, "old_string": "aaa", "new_string": "bbb"},
            self.tmpdir,
        )
        assert "2 locations" in result

    def test_glob(self):
        # Create some files
        Path(os.path.join(self.tmpdir, "a.py")).write_text("")
        Path(os.path.join(self.tmpdir, "b.py")).write_text("")
        Path(os.path.join(self.tmpdir, "c.txt")).write_text("")
        result = _execute_tool("glob", {"pattern": "*.py"}, self.tmpdir)
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_glob_no_matches(self):
        result = _execute_tool("glob", {"pattern": "*.xyz"}, self.tmpdir)
        assert "No matches" in result

    def test_glob_with_path(self):
        result = _execute_tool("glob", {"pattern": "*.txt", "path": self.tmpdir}, self.tmpdir)
        assert "test.txt" in result

    def test_grep(self):
        result = _execute_tool("grep", {"pattern": "line 2", "path": self.tmpdir}, self.tmpdir)
        assert "line 2" in result

    def test_grep_no_matches(self):
        result = _execute_tool("grep", {"pattern": "nonexistent_xyz", "path": self.tmpdir}, self.tmpdir)
        assert "No matches" in result

    def test_shell(self):
        result = _execute_tool("shell", {"command": "echo hello world"}, self.tmpdir)
        assert "hello world" in result

    def test_shell_stderr(self):
        result = _execute_tool("shell", {"command": "echo err >&2"}, self.tmpdir)
        assert "err" in result

    def test_shell_exit_code(self):
        result = _execute_tool("shell", {"command": "true"}, self.tmpdir)
        assert "exit code 0" in result

    def test_unknown_tool(self):
        result = _execute_tool("unknown_tool", {}, self.tmpdir)
        assert "unknown tool" in result.lower()


class TestResolvePath:
    """Tests for _resolve_path() with sandboxing."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_relative_path(self):
        result = _resolve_path("file.txt", self.tmpdir)
        assert result == os.path.join(os.path.realpath(self.tmpdir), "file.txt")

    def test_relative_nested(self):
        result = _resolve_path("sub/file.txt", self.tmpdir)
        assert result == os.path.join(os.path.realpath(self.tmpdir), "sub", "file.txt")

    def test_absolute_inside_cwd(self):
        abs_path = os.path.join(self.tmpdir, "inside.txt")
        result = _resolve_path(abs_path, self.tmpdir)
        assert result == os.path.realpath(abs_path)

    def test_absolute_outside_cwd_returns_none(self):
        assert _resolve_path("/etc/passwd", self.tmpdir) is None

    def test_traversal_returns_none(self):
        assert _resolve_path("../../etc/passwd", self.tmpdir) is None

    def test_cwd_itself_is_allowed(self):
        result = _resolve_path(".", self.tmpdir)
        assert result == os.path.realpath(self.tmpdir)


# ---------------------------------------------------------------------------
# Tool filtering
# ---------------------------------------------------------------------------

class TestFilterTools:
    """Tests for _filter_tools()."""

    def test_no_filter_returns_all(self):
        result = _filter_tools()
        assert len(result) == len(TOOL_DEFINITIONS)

    def test_allowed_filters(self):
        result = _filter_tools(allowed=["Read", "Grep"])
        names = {t["function"]["name"] for t in result}
        assert names == {"read_file", "grep"}

    def test_disallowed_filters(self):
        result = _filter_tools(disallowed=["Bash", "Write", "Edit"])
        names = {t["function"]["name"] for t in result}
        assert "shell" not in names
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "read_file" in names

    def test_empty_allowed_returns_none(self):
        result = _filter_tools(allowed=[])
        # Empty allowed list means no tools are permitted
        assert len(result) == 0

    def test_koan_tool_mapping(self):
        assert TOOL_NAME_MAP["Read"] == "read_file"
        assert TOOL_NAME_MAP["Write"] == "write_file"
        assert TOOL_NAME_MAP["Edit"] == "edit_file"
        assert TOOL_NAME_MAP["Glob"] == "glob"
        assert TOOL_NAME_MAP["Grep"] == "grep"
        assert TOOL_NAME_MAP["Bash"] == "shell"


# ---------------------------------------------------------------------------
# API client mocking
# ---------------------------------------------------------------------------

def _make_api_response(content="", tool_calls=None, input_tokens=100, output_tokens=50):
    """Helper to build a mock API response."""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
    }


def _make_tool_call(name, arguments, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


# ---------------------------------------------------------------------------
# Agentic loop tests
# ---------------------------------------------------------------------------

class TestRunAgent:
    """Tests for run_agent() — the core agentic loop."""

    @patch("app.local_llm_runner._call_api")
    def test_simple_text_response(self, mock_api):
        """LLM returns text directly, no tool use."""
        mock_api.return_value = _make_api_response(content="Hello, this is my answer.")
        result = run_agent(
            prompt="Say hello",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert result["result"] == "Hello, this is my answer."
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    @patch("app.local_llm_runner._call_api")
    def test_tool_use_then_response(self, mock_api):
        """LLM calls a tool, then produces final answer."""
        tmpdir = tempfile.mkdtemp()
        test_file = os.path.join(tmpdir, "hello.txt")
        Path(test_file).write_text("Hello from file!")

        # First call: LLM requests to read the file
        # Second call: LLM gives final answer
        mock_api.side_effect = [
            _make_api_response(
                tool_calls=[_make_tool_call("read_file", {"path": test_file})],
                input_tokens=200,
                output_tokens=30,
            ),
            _make_api_response(
                content="The file says: Hello from file!",
                input_tokens=300,
                output_tokens=40,
            ),
        ]

        result = run_agent(
            prompt="Read hello.txt",
            base_url="http://localhost:11434/v1",
            model="test-model",
            cwd=tmpdir,
        )
        assert "Hello from file!" in result["result"]
        assert result["input_tokens"] == 500  # 200 + 300
        assert result["output_tokens"] == 70   # 30 + 40

    @patch("app.local_llm_runner._call_api")
    def test_max_turns_limit(self, mock_api):
        """Agent stops after max_turns."""
        # Every call returns a tool call, never a final answer
        mock_api.return_value = _make_api_response(
            tool_calls=[_make_tool_call("shell", {"command": "echo loop"})],
        )
        result = run_agent(
            prompt="Loop forever",
            base_url="http://localhost:11434/v1",
            model="test-model",
            max_turns=3,
        )
        assert "max turns" in result["result"].lower() or result["result"] == ""
        assert mock_api.call_count == 3

    @patch("app.local_llm_runner._call_api")
    def test_api_error_handling(self, mock_api):
        """Graceful handling of API errors."""
        mock_api.side_effect = RuntimeError("Connection refused")
        result = run_agent(
            prompt="Test error",
            base_url="http://localhost:99999/v1",
            model="test-model",
        )
        assert "Error" in result["result"]
        assert "Connection refused" in result["result"]

    @patch("app.local_llm_runner._call_api")
    def test_multi_tool_calls(self, mock_api):
        """LLM issues multiple tool calls in one turn."""
        tmpdir = tempfile.mkdtemp()
        Path(os.path.join(tmpdir, "a.txt")).write_text("file a")
        Path(os.path.join(tmpdir, "b.txt")).write_text("file b")

        mock_api.side_effect = [
            _make_api_response(
                tool_calls=[
                    _make_tool_call("read_file", {"path": os.path.join(tmpdir, "a.txt")}, "call_a"),
                    _make_tool_call("read_file", {"path": os.path.join(tmpdir, "b.txt")}, "call_b"),
                ],
            ),
            _make_api_response(content="Both files read successfully."),
        ]

        result = run_agent(
            prompt="Read both files",
            base_url="http://localhost:11434/v1",
            model="test-model",
            cwd=tmpdir,
        )
        assert "Both files read" in result["result"]

    @patch("app.local_llm_runner._call_api")
    def test_tool_filtering_applied(self, mock_api):
        """Allowed tools filter is respected in API calls."""
        mock_api.return_value = _make_api_response(content="Done")
        run_agent(
            prompt="Test",
            base_url="http://localhost:11434/v1",
            model="test-model",
            allowed_tools=["Read", "Grep"],
        )
        # Check the tools passed to the API
        call_kwargs = mock_api.call_args
        tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
        if tools:
            names = {t["function"]["name"] for t in tools}
            assert names == {"read_file", "grep"}

    @patch("app.local_llm_runner._call_api")
    def test_no_tools_when_empty_filter(self, mock_api):
        """When all tools are disallowed, no tools are passed."""
        mock_api.return_value = _make_api_response(content="No tools needed")
        run_agent(
            prompt="Just answer",
            base_url="http://localhost:11434/v1",
            model="test-model",
            disallowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill"],
        )
        call_kwargs = mock_api.call_args
        tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
        assert tools is None  # Empty list -> use_tools is False -> None passed

    @patch("app.local_llm_runner._call_api")
    def test_write_tool_execution(self, mock_api):
        """LLM can write files via tool calls."""
        tmpdir = tempfile.mkdtemp()
        out_path = os.path.join(tmpdir, "output.txt")

        mock_api.side_effect = [
            _make_api_response(
                tool_calls=[_make_tool_call("write_file", {"path": out_path, "content": "created!"})],
            ),
            _make_api_response(content="File written."),
        ]

        result = run_agent(
            prompt="Create output.txt",
            base_url="http://localhost:11434/v1",
            model="test-model",
            cwd=tmpdir,
        )
        assert Path(out_path).read_text() == "created!"

    @patch("app.local_llm_runner._call_api")
    def test_empty_choices_returns_error(self, mock_api):
        """API returning empty choices list doesn't crash with IndexError."""
        mock_api.return_value = {
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        result = run_agent(
            prompt="Test empty",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert "Error" in result["result"]
        assert "empty choices" in result["result"].lower()

    @patch("app.local_llm_runner._call_api")
    def test_missing_choices_key_returns_error(self, mock_api):
        """API response with no 'choices' key doesn't crash."""
        mock_api.return_value = {
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        result = run_agent(
            prompt="Test missing",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert "Error" in result["result"]

    @patch("app.local_llm_runner._call_api")
    def test_null_choices_returns_error(self, mock_api):
        """API response with choices=null doesn't crash."""
        mock_api.return_value = {
            "choices": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        result = run_agent(
            prompt="Test null",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert "Error" in result["result"]

    @patch("app.local_llm_runner._call_api")
    def test_invalid_tool_args_json(self, mock_api):
        """Handles malformed JSON in tool arguments gracefully."""
        mock_api.side_effect = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "call_bad",
                            "function": {"name": "read_file", "arguments": "not json"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            },
            _make_api_response(content="Recovered."),
        ]
        result = run_agent(
            prompt="Test bad args",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        # Should not crash — the tool error is fed back to the LLM
        assert result["result"] == "Recovered."


# ---------------------------------------------------------------------------
# CLI entry point tests
# ---------------------------------------------------------------------------

class TestCLI:
    """Tests for the CLI entry point."""

    @patch("app.local_llm_runner.run_agent")
    def test_cli_json_output(self, mock_run):
        """--output-format json prints JSON to stdout."""
        mock_run.return_value = {
            "result": "test output",
            "input_tokens": 100,
            "output_tokens": 50,
        }
        import io
        from contextlib import redirect_stdout
        from app.local_llm_runner import main

        out = io.StringIO()
        with patch("sys.argv", [
            "local_llm_runner",
            "-p", "test prompt",
            "--model", "test-model",
            "--base-url", "http://localhost:11434/v1",
            "--output-format", "json",
        ]):
            with redirect_stdout(out):
                main()

        output = json.loads(out.getvalue())
        assert output["result"] == "test output"
        assert output["input_tokens"] == 100

    @patch("app.local_llm_runner.run_agent")
    def test_cli_text_output(self, mock_run):
        """Default output is plain text."""
        mock_run.return_value = {"result": "plain text response"}
        import io
        from contextlib import redirect_stdout
        from app.local_llm_runner import main

        out = io.StringIO()
        with patch("sys.argv", [
            "local_llm_runner",
            "-p", "test prompt",
            "--model", "test-model",
            "--base-url", "http://localhost:11434/v1",
        ]):
            with redirect_stdout(out):
                main()

        assert out.getvalue().strip() == "plain text response"

    def test_cli_missing_model_exits(self):
        """Missing model causes exit."""
        with patch("sys.argv", ["local_llm_runner", "-p", "test"]):
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("KOAN_LOCAL_LLM_MODEL", None)
                with pytest.raises(SystemExit):
                    from app.local_llm_runner import main
                    main()

    @patch("app.local_llm_runner.run_agent")
    def test_cli_env_var_defaults(self, mock_run):
        """Env vars provide defaults for base_url and model."""
        mock_run.return_value = {"result": "ok"}
        import io
        from contextlib import redirect_stdout
        from app.local_llm_runner import main

        out = io.StringIO()
        with patch.dict("os.environ", {
            "KOAN_LOCAL_LLM_BASE_URL": "http://custom:8080/v1",
            "KOAN_LOCAL_LLM_MODEL": "custom-model",
            "KOAN_LOCAL_LLM_API_KEY": "secret",
        }):
            with patch("sys.argv", ["local_llm_runner", "-p", "test"]):
                with redirect_stdout(out):
                    main()

        # Verify run_agent was called with env var values
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["base_url"] == "http://custom:8080/v1"
        assert call_kwargs.kwargs["model"] == "custom-model"
        assert call_kwargs.kwargs["api_key"] == "secret"


# ---------------------------------------------------------------------------
# Tool definition completeness
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    """Verify tool definitions are well-formed."""

    def test_all_tools_have_required_fields(self):
        for tool in TOOL_DEFINITIONS:
            assert tool["type"] == "function"
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            assert func["parameters"]["type"] == "object"

    def test_all_koan_tools_mapped(self):
        """Every Koan canonical tool has a mapping."""
        for tool in ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill"]:
            assert tool in TOOL_NAME_MAP

    def test_all_mapped_tools_exist(self):
        """Every mapped function name exists in tool definitions."""
        defined_names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
        for func_name in TOOL_NAME_MAP.values():
            assert func_name in defined_names


# ---------------------------------------------------------------------------
# Bug fix regression tests
# ---------------------------------------------------------------------------

class TestEmptyChoicesBug:
    """Regression tests for IndexError when API returns empty choices list."""

    @patch("app.local_llm_runner._call_api")
    def test_empty_choices_list_returns_error(self, mock_api):
        """API returning choices=[] should not crash with IndexError."""
        mock_api.return_value = {
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert "empty choices" in result["result"].lower()
        assert result["input_tokens"] == 10

    @patch("app.local_llm_runner._call_api")
    def test_missing_choices_key_returns_error(self, mock_api):
        """API returning no choices key should not crash."""
        mock_api.return_value = {
            "usage": {"prompt_tokens": 5, "completion_tokens": 0},
        }
        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert "empty choices" in result["result"].lower()

    @patch("app.local_llm_runner._call_api")
    def test_choices_none_returns_error(self, mock_api):
        """API returning choices=None should not crash."""
        mock_api.return_value = {
            "choices": None,
            "usage": {"prompt_tokens": 5, "completion_tokens": 0},
        }
        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert "empty choices" in result["result"].lower()

    @patch("app.local_llm_runner._call_api")
    def test_empty_choices_preserves_token_count(self, mock_api):
        """Token tracking survives across a normal turn + empty choices."""
        mock_api.side_effect = [
            _make_api_response(content="hello", input_tokens=100, output_tokens=20),
        ]
        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        # Normal response, tokens tracked correctly
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 20


class TestGlobTruncationBug:
    """Regression tests for glob truncation showing wrong match count."""

    def test_truncation_shows_original_count(self):
        """When >200 matches, message should show the real total."""
        tmpdir = tempfile.mkdtemp()
        # Create 205 files
        for i in range(205):
            Path(os.path.join(tmpdir, f"file_{i:03d}.txt")).touch()

        result = _tool_glob({"pattern": "*.txt"}, tmpdir)
        assert "205 matches" in result
        assert "showing first 200" in result

    def test_exact_200_matches_no_truncation(self):
        """Exactly 200 matches should not be truncated."""
        tmpdir = tempfile.mkdtemp()
        for i in range(200):
            Path(os.path.join(tmpdir, f"file_{i:03d}.txt")).touch()

        result = _tool_glob({"pattern": "*.txt"}, tmpdir)
        assert "truncated" not in result
        assert "showing first" not in result


class TestFilterToolsSemantic:
    """Tests for _filter_tools with edge cases around allowed/disallowed."""

    def test_none_allowed_returns_all(self):
        """allowed=None means 'no restriction' — all tools returned."""
        result = _filter_tools(allowed=None)
        assert len(result) == len(TOOL_DEFINITIONS)

    def test_empty_list_allowed_returns_none(self):
        """allowed=[] means 'nothing allowed' — zero tools returned."""
        result = _filter_tools(allowed=[])
        assert len(result) == 0

    def test_allowed_and_disallowed_combined(self):
        """Disallowed takes precedence over allowed."""
        result = _filter_tools(allowed=["Read", "Write", "Bash"], disallowed=["Bash"])
        names = {t["function"]["name"] for t in result}
        assert names == {"read_file", "write_file"}

    def test_disallowed_only(self):
        """Disallowed without allowed still filters correctly."""
        result = _filter_tools(disallowed=["Bash"])
        names = {t["function"]["name"] for t in result}
        assert "shell" not in names
        assert len(names) == len(TOOL_DEFINITIONS) - 1

    def test_unknown_tool_name_ignored(self):
        """Unknown tool names in allowed/disallowed are silently ignored."""
        result = _filter_tools(allowed=["Read", "NonExistent"])
        names = {t["function"]["name"] for t in result}
        assert names == {"read_file"}

    def test_lowercase_fallback(self):
        """Tool names not in TOOL_NAME_MAP get lowercased."""
        result = _filter_tools(allowed=["read_file"])
        names = {t["function"]["name"] for t in result}
        assert "read_file" in names


# ---------------------------------------------------------------------------
# Tool execution edge cases
# ---------------------------------------------------------------------------

class TestToolEdgeCases:
    """Edge case tests for tool execution functions."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_read_file_large_truncation(self):
        """Files >50K chars are truncated."""
        large_file = os.path.join(self.tmpdir, "large.txt")
        Path(large_file).write_text("x" * 60000)
        result = _tool_read_file({"path": large_file}, self.tmpdir)
        assert len(result) < 55000  # 50000 + truncation message
        assert "truncated" in result

    def test_write_file_sandbox_escape(self):
        """Writing outside cwd is blocked."""
        result = _tool_write_file(
            {"path": "/tmp/escape_test_koan.txt", "content": "bad"},
            self.tmpdir,
        )
        assert "Error" in result
        assert "escapes" in result

    def test_edit_file_sandbox_escape(self):
        """Editing outside cwd is blocked."""
        result = _tool_edit_file(
            {"path": "/etc/hosts", "old_string": "x", "new_string": "y"},
            self.tmpdir,
        )
        assert "Error" in result

    def test_glob_sandbox_escape(self):
        """Glob outside cwd is blocked."""
        result = _tool_glob({"pattern": "*.txt", "path": "/etc"}, self.tmpdir)
        assert "Error" in result

    def test_grep_sandbox_escape(self):
        """Grep outside cwd is blocked."""
        result = _tool_grep({"pattern": "root", "path": "/etc"}, self.tmpdir)
        assert "Error" in result

    def test_grep_with_file_glob_filter(self):
        """Grep respects file_glob filter."""
        Path(os.path.join(self.tmpdir, "code.py")).write_text("match_me\n")
        Path(os.path.join(self.tmpdir, "data.txt")).write_text("match_me\n")
        result = _tool_grep(
            {"pattern": "match_me", "file_glob": "*.py", "path": self.tmpdir},
            self.tmpdir,
        )
        assert "code.py" in result

    def test_grep_output_truncation(self):
        """Grep output >20K chars is truncated."""
        # Create a file with many matching lines
        big_file = os.path.join(self.tmpdir, "big.txt")
        Path(big_file).write_text("match\n" * 5000)
        result = _tool_grep({"pattern": "match", "path": self.tmpdir}, self.tmpdir)
        assert len(result) <= 21000  # 20000 + truncation message

    def test_shell_output_truncation(self):
        """Shell output >30K chars is truncated."""
        result = _tool_shell(
            {"command": f"python3 -c \"print('x' * 35000)\""},
            self.tmpdir,
        )
        assert len(result) <= 31000

    def test_shell_respects_cwd(self):
        """Shell command runs in the specified cwd."""
        result = _tool_shell({"command": "pwd"}, self.tmpdir)
        assert os.path.realpath(self.tmpdir) in result

    def test_execute_tool_timeout(self):
        """Tool timeout produces error message."""
        result = _execute_tool(
            "grep", {"pattern": "x", "path": self.tmpdir}, self.tmpdir
        )
        # Should work fine, but test the timeout path via mock
        with patch("app.local_llm_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("grep", 30)):
            result = _execute_tool("grep", {"pattern": "x"}, self.tmpdir)
            assert "timed out" in result.lower()

    def test_execute_tool_general_exception(self):
        """General exception in tool produces error message."""
        with patch("app.local_llm_runner.subprocess.run", side_effect=PermissionError("denied")):
            result = _execute_tool("grep", {"pattern": "x"}, self.tmpdir)
            assert "Error" in result


# ---------------------------------------------------------------------------
# API client tests
# ---------------------------------------------------------------------------

class TestCallApi:
    """Tests for _call_api() — the HTTP client layer."""

    @patch("urllib.request.urlopen")
    def test_successful_api_call(self, mock_urlopen):
        """Successful API call returns parsed JSON."""
        response_data = {"choices": [{"message": {"content": "hi"}}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _call_api(
            base_url="http://localhost:11434/v1",
            model="test",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert result == response_data

    @patch("urllib.request.urlopen")
    def test_api_includes_tools_when_provided(self, mock_urlopen):
        """Tools are included in the request payload."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices": []}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _call_api(
            base_url="http://localhost:11434/v1",
            model="test",
            messages=[],
            tools=[{"type": "function", "function": {"name": "test"}}],
        )
        # Verify the request body includes tools
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        assert "tools" in body
        assert body["tool_choice"] == "auto"

    @patch("urllib.request.urlopen")
    def test_api_key_in_header(self, mock_urlopen):
        """API key is sent in Authorization header."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices": []}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _call_api(
            base_url="http://localhost:11434/v1",
            model="test",
            messages=[],
            api_key="secret-key",
        )
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer secret-key"

    @patch("urllib.request.urlopen")
    def test_no_auth_header_without_key(self, mock_urlopen):
        """No Authorization header when api_key is empty."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices": []}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _call_api(
            base_url="http://localhost:11434/v1",
            model="test",
            messages=[],
        )
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None

    def test_http_error_raises_runtime(self):
        """HTTPError is wrapped in RuntimeError with body."""
        import urllib.error
        with patch("urllib.request.urlopen") as mock:
            error = urllib.error.HTTPError(
                "http://localhost/v1/chat/completions",
                500, "Server Error", {},
                MagicMock(read=lambda: b"internal error"),
            )
            mock.side_effect = error
            with pytest.raises(RuntimeError, match="API error 500"):
                _call_api("http://localhost/v1", "model", [])

    def test_url_error_raises_runtime(self):
        """URLError is wrapped in RuntimeError with connection hint."""
        import urllib.error
        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.URLError("Connection refused")
            with pytest.raises(RuntimeError, match="Cannot connect"):
                _call_api("http://localhost:99999/v1", "model", [])

    @patch("urllib.request.urlopen")
    def test_base_url_trailing_slash_stripped(self, mock_urlopen):
        """Trailing slash on base_url is handled."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices": []}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _call_api(
            base_url="http://localhost:11434/v1/",
            model="test",
            messages=[],
        )
        req = mock_urlopen.call_args[0][0]
        assert "/v1//chat" not in req.full_url
        assert "/v1/chat/completions" in req.full_url


# ---------------------------------------------------------------------------
# Default system prompt
# ---------------------------------------------------------------------------

class TestDefaultSystemPrompt:
    """Tests for _default_system_prompt()."""

    def test_fallback_when_prompt_file_missing(self):
        """Returns a sensible fallback if prompts module fails."""
        with patch("app.prompts.load_prompt", side_effect=OSError("missing")):
            prompt = _default_system_prompt()
            assert "coding assistant" in prompt.lower()
            assert "tool" in prompt.lower()

    def test_loads_from_prompts_module(self):
        """Loads prompt from the prompts system when available."""
        with patch("app.prompts.load_prompt", return_value="custom prompt"):
            prompt = _default_system_prompt()
            assert prompt == "custom prompt"


# ---------------------------------------------------------------------------
# Run agent — additional edge cases
# ---------------------------------------------------------------------------

class TestRunAgentEdgeCases:
    """Additional edge case tests for run_agent()."""

    @patch("app.local_llm_runner._call_api")
    def test_custom_system_prompt(self, mock_api):
        """Custom system prompt is used instead of default."""
        mock_api.return_value = _make_api_response(content="ok")
        run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
            system_prompt="You are a helpful assistant.",
        )
        call_args = mock_api.call_args
        messages = call_args.kwargs.get("messages") or call_args[1]["messages"]
        assert messages[0]["content"] == "You are a helpful assistant."

    @patch("app.local_llm_runner._call_api")
    def test_cwd_defaults_to_getcwd(self, mock_api):
        """When cwd is empty, uses os.getcwd()."""
        tmpdir = tempfile.mkdtemp()
        test_file = os.path.join(tmpdir, "test.txt")
        Path(test_file).write_text("hello")

        mock_api.side_effect = [
            _make_api_response(
                tool_calls=[_make_tool_call("read_file", {"path": test_file})],
            ),
            _make_api_response(content="done"),
        ]

        with patch("os.getcwd", return_value=tmpdir):
            result = run_agent(
                prompt="test",
                base_url="http://localhost:11434/v1",
                model="test-model",
                cwd="",
            )
        assert result["result"] == "done"

    @patch("app.local_llm_runner._call_api")
    def test_max_turns_extracts_last_assistant_content(self, mock_api):
        """When max turns hit, extracts last assistant content from history."""
        # Turn 1: tool call + assistant message with content
        mock_api.side_effect = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Let me check...",
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {"name": "shell", "arguments": '{"command": "echo hi"}'},
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            # Turn 2: another tool call (max_turns=2 reached)
            _make_api_response(
                tool_calls=[_make_tool_call("shell", {"command": "echo again"})],
            ),
        ]

        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
            max_turns=2,
        )
        # Should extract "Let me check..." from the assistant message
        assert result["result"] == "Let me check..."

    @patch("app.local_llm_runner._call_api")
    def test_missing_usage_in_response(self, mock_api):
        """Response without usage field doesn't crash."""
        mock_api.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert result["result"] == "ok"
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0

    @patch("app.local_llm_runner._call_api")
    def test_tool_call_with_missing_id_uses_fallback(self, mock_api):
        """Tool call without 'id' field gets a generated fallback."""
        tmpdir = tempfile.mkdtemp()
        Path(os.path.join(tmpdir, "f.txt")).write_text("content")
        mock_api.side_effect = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "function": {"name": "read_file", "arguments": '{"path": "f.txt"}'},
                        }],
                    },
                }],
                "usage": {},
            },
            _make_api_response(content="done"),
        ]
        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
            cwd=tmpdir,
        )
        assert result["result"] == "done"

    @patch("app.local_llm_runner._call_api")
    def test_empty_message_content_at_end(self, mock_api):
        """Final response with empty content returns empty string."""
        mock_api.return_value = {
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "usage": {},
        }
        result = run_agent(
            prompt="test",
            base_url="http://localhost:11434/v1",
            model="test-model",
        )
        assert result["result"] == ""


# ---------------------------------------------------------------------------
# CLI additional edge cases
# ---------------------------------------------------------------------------

class TestCLIEdgeCases:
    """Additional CLI entry point tests."""

    @patch("app.local_llm_runner.run_agent")
    def test_stdin_prompt(self, mock_run):
        """@stdin reads prompt from stdin."""
        mock_run.return_value = {"result": "ok"}
        import io
        from contextlib import redirect_stdout
        from app.local_llm_runner import main

        out = io.StringIO()
        with patch("sys.argv", [
            "local_llm_runner", "-p", "@stdin",
            "--model", "test-model",
        ]):
            with patch("sys.stdin", io.StringIO("prompt from stdin")):
                with redirect_stdout(out):
                    main()

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["prompt"] == "prompt from stdin"

    @patch("app.local_llm_runner.run_agent")
    def test_allowed_tools_parsing(self, mock_run):
        """--allowed-tools parses comma-separated list."""
        mock_run.return_value = {"result": "ok"}
        import io
        from contextlib import redirect_stdout
        from app.local_llm_runner import main

        out = io.StringIO()
        with patch("sys.argv", [
            "local_llm_runner", "-p", "test",
            "--model", "m", "--allowed-tools", "Read,Write,Bash",
        ]):
            with redirect_stdout(out):
                main()

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["allowed_tools"] == ["Read", "Write", "Bash"]

    @patch("app.local_llm_runner.run_agent")
    def test_disallowed_tools_parsing(self, mock_run):
        """--disallowed-tools parses comma-separated list."""
        mock_run.return_value = {"result": "ok"}
        import io
        from contextlib import redirect_stdout
        from app.local_llm_runner import main

        out = io.StringIO()
        with patch("sys.argv", [
            "local_llm_runner", "-p", "test",
            "--model", "m", "--disallowed-tools", "Bash,Write",
        ]):
            with redirect_stdout(out):
                main()

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["disallowed_tools"] == ["Bash", "Write"]
