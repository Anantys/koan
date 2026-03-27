"""Tests for ollama_client — Ollama REST API client."""

import json
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

from app.ollama_client import (
    _api_request,
    is_server_running,
    get_version,
    list_models,
    show_model,
    pull_model,
    delete_model,
    list_running,
    format_model_size,
    DEFAULT_HOST,
)


# ---------------------------------------------------------------------------
# format_model_size
# ---------------------------------------------------------------------------


class TestFormatModelSize:
    def test_gigabytes(self):
        assert format_model_size(4_700_000_000) == "4.7 GB"

    def test_megabytes(self):
        assert format_model_size(500_000_000) == "500 MB"

    def test_bytes(self):
        assert format_model_size(999) == "999 B"

    def test_exact_gb_boundary(self):
        assert format_model_size(1_000_000_000) == "1.0 GB"

    def test_exact_mb_boundary(self):
        assert format_model_size(1_000_000) == "1 MB"

    def test_zero(self):
        assert format_model_size(0) == "0 B"


# ---------------------------------------------------------------------------
# _api_request
# ---------------------------------------------------------------------------


class TestApiRequest:
    def test_get_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"version": "0.16.0"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, data = _api_request("/api/version")
        assert ok is True
        assert data == {"version": "0.16.0"}

    def test_post_with_body(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            ok, data = _api_request("/api/show", method="POST", body={"name": "test"})
        assert ok is True
        # Verify request was made with JSON body
        req = mock_open.call_args[0][0]
        assert req.data == json.dumps({"name": "test"}).encode()
        assert req.get_header("Content-type") == "application/json"

    def test_empty_response_body(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, data = _api_request("/api/delete", method="DELETE")
        assert ok is True
        assert data == {}

    def test_http_error_with_json_detail(self):
        error_body = json.dumps({"error": "model not found"}).encode()
        http_error = urllib.error.HTTPError(
            "http://localhost:11434/api/show", 404, "Not Found", {}, None
        )
        http_error.read = MagicMock(return_value=error_body)

        with patch("app.ollama_client.urllib.request.urlopen", side_effect=http_error):
            ok, data = _api_request("/api/show", method="POST", body={"name": "x"})
        assert ok is False
        assert "model not found" in data

    def test_http_error_without_json(self):
        http_error = urllib.error.HTTPError(
            "http://localhost:11434/api/show", 500, "Server Error", {}, None
        )
        http_error.read = MagicMock(return_value=b"not json")

        with patch("app.ollama_client.urllib.request.urlopen", side_effect=http_error):
            ok, data = _api_request("/api/show", method="POST", body={"name": "x"})
        assert ok is False

    def test_url_error(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("Connection refused")):
            ok, data = _api_request("/api/tags")
        assert ok is False
        assert "Connection failed" in data

    def test_generic_exception(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=OSError("timeout")):
            ok, data = _api_request("/api/tags")
        assert ok is False
        assert "timeout" in data

    def test_custom_host(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            _api_request("/api/tags", host="http://myhost:1234")
        req = mock_open.call_args[0][0]
        assert req.full_url == "http://myhost:1234/api/tags"

    def test_trailing_slash_stripped_from_host(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            _api_request("/api/tags", host="http://localhost:11434/")
        req = mock_open.call_args[0][0]
        assert req.full_url == "http://localhost:11434/api/tags"


# ---------------------------------------------------------------------------
# is_server_running
# ---------------------------------------------------------------------------


class TestIsServerRunning:
    def test_running(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            assert is_server_running() is True

    def test_not_running(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("Connection refused")):
            assert is_server_running() is False

    def test_timeout(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=OSError("timeout")):
            assert is_server_running() is False


# ---------------------------------------------------------------------------
# get_version
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_returns_version_string(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"version": "0.16.3"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            assert get_version() == "0.16.3"

    def test_returns_none_on_failure(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("refused")):
            assert get_version() is None

    def test_returns_none_on_missing_key(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            assert get_version() is None


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


class TestListModels:
    def test_returns_model_list(self):
        models_data = {
            "models": [
                {"name": "qwen3-coder", "size": 4700000000},
                {"name": "llama3:8b", "size": 4100000000},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(models_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, models = list_models()
        assert ok is True
        assert len(models) == 2
        assert models[0]["name"] == "qwen3-coder"

    def test_empty_model_list(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"models": []}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, models = list_models()
        assert ok is True
        assert models == []

    def test_failure(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("refused")):
            ok, data = list_models()
        assert ok is False


# ---------------------------------------------------------------------------
# show_model
# ---------------------------------------------------------------------------


class TestShowModel:
    def test_returns_model_details(self):
        detail = {
            "details": {
                "family": "qwen2",
                "parameter_size": "14.8B",
                "quantization_level": "Q4_K_M",
            }
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(detail).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, data = show_model("qwen3-coder")
        assert ok is True
        assert data["details"]["family"] == "qwen2"

    def test_model_not_found(self):
        http_error = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None
        )
        http_error.read = MagicMock(return_value=json.dumps({"error": "not found"}).encode())

        with patch("app.ollama_client.urllib.request.urlopen", side_effect=http_error):
            ok, data = show_model("nonexistent")
        assert ok is False


# ---------------------------------------------------------------------------
# pull_model
# ---------------------------------------------------------------------------


class TestPullModel:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "success"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, msg = pull_model("qwen3-coder")
        assert ok is True
        assert msg == "success"

    def test_failure(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("refused")):
            ok, msg = pull_model("bad-model")
        assert ok is False


# ---------------------------------------------------------------------------
# delete_model
# ---------------------------------------------------------------------------


class TestDeleteModel:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, msg = delete_model("old-model")
        assert ok is True
        assert msg == "deleted"

    def test_model_not_found(self):
        http_error = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None
        )
        http_error.read = MagicMock(return_value=json.dumps({"error": "not found"}).encode())

        with patch("app.ollama_client.urllib.request.urlopen", side_effect=http_error):
            ok, msg = delete_model("nonexistent")
        assert ok is False


# ---------------------------------------------------------------------------
# list_running
# ---------------------------------------------------------------------------


class TestListRunning:
    def test_returns_running_models(self):
        data = {"models": [{"name": "qwen3-coder", "size": 4700000000}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, models = list_running()
        assert ok is True
        assert len(models) == 1

    def test_no_running_models(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"models": []}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("app.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            ok, models = list_running()
        assert ok is True
        assert models == []

    def test_failure(self):
        with patch("app.ollama_client.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("refused")):
            ok, data = list_running()
        assert ok is False
