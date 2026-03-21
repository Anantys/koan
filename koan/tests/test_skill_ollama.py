"""Tests for the /ollama skill handler."""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from skills.core.ollama.handler import (
    handle,
    _handle_status,
    _handle_list,
    _handle_pull,
    _handle_remove,
    _handle_show,
    HELP_TEXT,
    OLLAMA_PROVIDERS,
)


def _ctx(args="", provider="ollama-launch"):
    """Build a minimal SkillContext-like object."""
    return SimpleNamespace(
        command_name="ollama",
        args=args,
        koan_root="/tmp/test",
        instance_dir="/tmp/test/instance",
    )


# ---------------------------------------------------------------------------
# Provider gate
# ---------------------------------------------------------------------------


class TestProviderGate:
    def test_rejects_claude_provider(self):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="claude"):
            result = handle(_ctx())
        assert "require an Ollama-based provider" in result
        assert "claude" in result

    def test_rejects_copilot_provider(self):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="copilot"):
            result = handle(_ctx())
        assert "require an Ollama-based provider" in result

    def test_accepts_local_provider(self):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="local"):
            with patch("skills.core.ollama.handler._handle_status", return_value="ok"):
                result = handle(_ctx())
        assert result == "ok"

    def test_accepts_ollama_provider(self):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="ollama"):
            with patch("skills.core.ollama.handler._handle_status", return_value="ok"):
                result = handle(_ctx())
        assert result == "ok"

    def test_accepts_ollama_launch_provider(self):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="ollama-launch"):
            with patch("skills.core.ollama.handler._handle_status", return_value="ok"):
                result = handle(_ctx())
        assert result == "ok"


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------


class TestSubcommandDispatch:
    def _run(self, args, expected_handler):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="local"):
            with patch(f"skills.core.ollama.handler.{expected_handler}", return_value="dispatched") as mock_fn:
                result = handle(_ctx(args))
        assert result == "dispatched"
        return mock_fn

    def test_empty_args_dispatches_to_status(self):
        self._run("", "_handle_status")

    def test_status_subcommand(self):
        self._run("status", "_handle_status")

    def test_list_subcommand(self):
        self._run("list", "_handle_list")

    def test_ls_alias(self):
        self._run("ls", "_handle_list")

    def test_models_alias(self):
        self._run("models", "_handle_list")

    def test_pull_subcommand(self):
        mock = self._run("pull qwen3-coder", "_handle_pull")
        mock.assert_called_once_with("qwen3-coder")

    def test_remove_subcommand(self):
        mock = self._run("remove old-model", "_handle_remove")
        mock.assert_called_once_with("old-model")

    def test_rm_alias(self):
        mock = self._run("rm old-model", "_handle_remove")
        mock.assert_called_once_with("old-model")

    def test_delete_alias(self):
        mock = self._run("delete old-model", "_handle_remove")
        mock.assert_called_once_with("old-model")

    def test_show_subcommand(self):
        mock = self._run("show qwen3-coder", "_handle_show")
        mock.assert_called_once_with("qwen3-coder")

    def test_info_alias(self):
        mock = self._run("info qwen3-coder", "_handle_show")
        mock.assert_called_once_with("qwen3-coder")

    def test_help_subcommand(self):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="local"):
            result = handle(_ctx("help"))
        assert result == HELP_TEXT

    def test_unknown_subcommand(self):
        with patch("skills.core.ollama.handler._get_provider_name", return_value="local"):
            result = handle(_ctx("foobar"))
        assert "Unknown subcommand: foobar" in result
        assert "Ollama commands:" in result


# ---------------------------------------------------------------------------
# _handle_status
# ---------------------------------------------------------------------------


class TestHandleStatus:
    def test_server_not_running(self):
        with patch("app.ollama_client.is_server_running", return_value=False):
            result = _handle_status()
        assert "not running" in result
        assert "ollama serve" in result

    def test_server_running_basic(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.get_version", return_value="0.16.3"), \
             patch("app.ollama_client.list_models", return_value=(True, [{"name": "m1"}, {"name": "m2"}])), \
             patch("app.ollama_client.list_running", return_value=(True, [])), \
             patch("skills.core.ollama.handler._get_provider_name", return_value="local"), \
             patch("skills.core.ollama.handler._append_configured_model"):
            result = _handle_status()
        assert "Ollama Server" in result
        assert "0.16.3" in result
        assert "2 available" in result

    def test_server_running_with_loaded_models(self):
        running = [{"name": "qwen3-coder"}]
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.get_version", return_value="0.16.0"), \
             patch("app.ollama_client.list_models", return_value=(True, [{"name": "qwen3-coder"}])), \
             patch("app.ollama_client.list_running", return_value=(True, running)), \
             patch("skills.core.ollama.handler._get_provider_name", return_value="local"), \
             patch("skills.core.ollama.handler._append_configured_model"):
            result = _handle_status()
        assert "1 in memory" in result
        assert "qwen3-coder" in result

    def test_no_version_available(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.get_version", return_value=None), \
             patch("app.ollama_client.list_models", return_value=(True, [])), \
             patch("app.ollama_client.list_running", return_value=(True, [])), \
             patch("skills.core.ollama.handler._get_provider_name", return_value="local"), \
             patch("skills.core.ollama.handler._append_configured_model"):
            result = _handle_status()
        assert "Version" not in result
        assert "running" in result


# ---------------------------------------------------------------------------
# _handle_list
# ---------------------------------------------------------------------------


class TestHandleList:
    def test_server_not_running(self):
        with patch("app.ollama_client.is_server_running", return_value=False):
            result = _handle_list()
        assert "not running" in result

    def test_no_models(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.list_models", return_value=(True, [])):
            result = _handle_list()
        assert "No models installed" in result

    def test_lists_models_with_details(self):
        models = [
            {
                "name": "qwen3-coder",
                "size": 4_700_000_000,
                "details": {"family": "qwen2", "quantization_level": "Q4_K_M"},
            },
            {
                "name": "llama3:8b",
                "size": 4_100_000_000,
                "details": {"family": "llama"},
            },
        ]
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.list_models", return_value=(True, models)):
            result = _handle_list()
        assert "Models (2)" in result
        assert "qwen3-coder" in result
        assert "Q4_K_M" in result
        assert "4.7 GB" in result
        assert "llama3:8b" in result

    def test_list_failure(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.list_models", return_value=(False, "error")):
            result = _handle_list()
        assert "Failed" in result

    def test_model_without_details(self):
        models = [{"name": "minimal", "size": 0}]
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.list_models", return_value=(True, models)):
            result = _handle_list()
        assert "minimal" in result


# ---------------------------------------------------------------------------
# _handle_pull
# ---------------------------------------------------------------------------


class TestHandlePull:
    def test_missing_name(self):
        result = _handle_pull("")
        assert "Usage" in result

    def test_server_not_running(self):
        with patch("app.ollama_client.is_server_running", return_value=False):
            result = _handle_pull("qwen3-coder")
        assert "not running" in result

    def test_success(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.pull_model", return_value=(True, "success")):
            result = _handle_pull("qwen3-coder")
        assert "pulled successfully" in result

    def test_failure(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.pull_model", return_value=(False, "not found")):
            result = _handle_pull("bad-model")
        assert "Failed" in result
        assert "not found" in result


# ---------------------------------------------------------------------------
# _handle_remove
# ---------------------------------------------------------------------------


class TestHandleRemove:
    def test_missing_name(self):
        result = _handle_remove("")
        assert "Usage" in result

    def test_server_not_running(self):
        with patch("app.ollama_client.is_server_running", return_value=False):
            result = _handle_remove("old-model")
        assert "not running" in result

    def test_success(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.delete_model", return_value=(True, "deleted")):
            result = _handle_remove("old-model")
        assert "removed" in result

    def test_failure(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.delete_model", return_value=(False, "not found")):
            result = _handle_remove("nonexistent")
        assert "Failed" in result


# ---------------------------------------------------------------------------
# _handle_show
# ---------------------------------------------------------------------------


class TestHandleShow:
    def test_missing_name(self):
        result = _handle_show("")
        assert "Usage" in result

    def test_server_not_running(self):
        with patch("app.ollama_client.is_server_running", return_value=False):
            result = _handle_show("qwen3-coder")
        assert "not running" in result

    def test_shows_model_details(self):
        data = {
            "details": {
                "family": "qwen2",
                "parameter_size": "14.8B",
                "quantization_level": "Q4_K_M",
            },
            "model_info": {
                "general.context_length": 32768,
            },
        }
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.show_model", return_value=(True, data)):
            result = _handle_show("qwen3-coder")
        assert "qwen3-coder" in result
        assert "qwen2" in result
        assert "14.8B" in result
        assert "Q4_K_M" in result
        assert "32768" in result

    def test_model_not_found(self):
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.show_model", return_value=(False, "not found")):
            result = _handle_show("nonexistent")
        assert "Failed" in result

    def test_partial_details(self):
        data = {"details": {"family": "llama"}}
        with patch("app.ollama_client.is_server_running", return_value=True), \
             patch("app.ollama_client.show_model", return_value=(True, data)):
            result = _handle_show("llama3")
        assert "llama" in result
        assert "Parameters" not in result  # Not present in data
