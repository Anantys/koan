"""Ollama REST API client.

Lightweight wrapper around Ollama's HTTP API for health checks, model
listing, pulling, and removal.  Uses only ``urllib`` (no third-party
deps) to keep Kōan's dependency footprint minimal.

Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_HOST = "http://localhost:11434"


def _api_request(
    path: str,
    method: str = "GET",
    body: Optional[dict] = None,
    host: str = DEFAULT_HOST,
    timeout: int = 10,
) -> Tuple[bool, Any]:
    """Unified HTTP request to the Ollama API.

    Returns (success, data) where data is parsed JSON on success or an
    error message string on failure.
    """
    url = f"{host.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if body else {}

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return True, {}
            return True, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", str(e))
        except Exception:
            detail = str(e)
        return False, detail
    except urllib.error.URLError as e:
        return False, f"Connection failed: {e.reason}"
    except Exception as e:
        return False, str(e)


def is_server_running(host: str = DEFAULT_HOST, timeout: int = 3) -> bool:
    """Check if the Ollama server is responding."""
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_version(host: str = DEFAULT_HOST, timeout: int = 3) -> Optional[str]:
    """Get the Ollama server version string, or None if unavailable."""
    ok, data = _api_request("/api/version", host=host, timeout=timeout)
    if ok and isinstance(data, dict):
        return data.get("version")
    return None


def list_models(host: str = DEFAULT_HOST, timeout: int = 10) -> Tuple[bool, Any]:
    """List locally available models.

    Returns (success, list_of_models) where each model is a dict with
    keys like 'name', 'size', 'modified_at', 'details', etc.
    """
    ok, data = _api_request("/api/tags", host=host, timeout=timeout)
    if ok and isinstance(data, dict):
        return True, data.get("models", [])
    return ok, data


def show_model(name: str, host: str = DEFAULT_HOST, timeout: int = 10) -> Tuple[bool, Any]:
    """Get details about a specific model.

    Returns (success, model_info_dict) with keys like 'modelfile',
    'parameters', 'template', 'details' (family, parameter_size, etc.).
    """
    return _api_request("/api/show", method="POST", body={"name": name},
                        host=host, timeout=timeout)


def pull_model(name: str, host: str = DEFAULT_HOST, timeout: int = 600) -> Tuple[bool, str]:
    """Pull (download) a model.

    Uses the non-streaming API (stream=false) for simplicity.
    Returns (success, status_message).
    """
    ok, data = _api_request(
        "/api/pull", method="POST",
        body={"name": name, "stream": False},
        host=host, timeout=timeout,
    )
    if ok:
        status = data.get("status", "success") if isinstance(data, dict) else "success"
        return True, status
    return False, str(data)


def delete_model(name: str, host: str = DEFAULT_HOST, timeout: int = 30) -> Tuple[bool, str]:
    """Delete a locally stored model.

    Returns (success, message).
    """
    ok, data = _api_request(
        "/api/delete", method="DELETE",
        body={"name": name},
        host=host, timeout=timeout,
    )
    if ok:
        return True, "deleted"
    return False, str(data)


def list_running(host: str = DEFAULT_HOST, timeout: int = 5) -> Tuple[bool, Any]:
    """List models currently loaded in memory (running).

    Returns (success, list_of_running_models).
    """
    ok, data = _api_request("/api/ps", host=host, timeout=timeout)
    if ok and isinstance(data, dict):
        return True, data.get("models", [])
    return ok, data


def format_model_size(size_bytes: int) -> str:
    """Format byte count as human-readable size (e.g. '4.7 GB')."""
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.1f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.0f} MB"
    return f"{size_bytes} B"
