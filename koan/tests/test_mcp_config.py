"""Tests for koan/app/mcp_config.py — MCP config file generation."""

import json
import pytest
from pathlib import Path

from app.mcp_config import generate_mcp_config


# ---------------------------------------------------------------------------
# generate_mcp_config
# ---------------------------------------------------------------------------


class TestGenerateMcpConfig:
    """Tests for generate_mcp_config()."""

    def test_generates_valid_json(self, tmp_path):
        entries = [
            {
                "name": "playwright",
                "command": "npx",
                "args": ["-y", "@anthropic/playwright-mcp@latest", "--headless"],
            }
        ]
        result = generate_mcp_config(entries, str(tmp_path), "myapp")
        assert result is not None

        data = json.loads(Path(result).read_text())
        assert "mcpServers" in data
        assert "playwright" in data["mcpServers"]
        server = data["mcpServers"]["playwright"]
        assert server["command"] == "npx"
        assert "--headless" in server["args"]

    def test_creates_config_in_mcp_configs_dir(self, tmp_path):
        entries = [{"name": "test", "command": "test-cmd"}]
        result = generate_mcp_config(entries, str(tmp_path), "myproject")
        assert result is not None
        assert ".mcp-configs" in result
        assert "myproject.json" in result
        assert Path(result).exists()

    def test_multiple_servers(self, tmp_path):
        entries = [
            {"name": "playwright", "command": "npx", "args": ["--headless"]},
            {"name": "custom", "command": "my-server"},
        ]
        result = generate_mcp_config(entries, str(tmp_path))
        data = json.loads(Path(result).read_text())
        assert len(data["mcpServers"]) == 2
        assert "playwright" in data["mcpServers"]
        assert "custom" in data["mcpServers"]

    def test_returns_none_for_empty_entries(self, tmp_path):
        assert generate_mcp_config([], str(tmp_path)) is None

    def test_returns_none_for_invalid_entries(self, tmp_path):
        entries = [{"name": "no-cmd"}, {"command": "no-name"}]
        assert generate_mcp_config(entries, str(tmp_path)) is None

    def test_includes_env_when_present(self, tmp_path):
        entries = [
            {
                "name": "server",
                "command": "cmd",
                "env": {"FOO": "bar"},
            }
        ]
        result = generate_mcp_config(entries, str(tmp_path))
        data = json.loads(Path(result).read_text())
        assert data["mcpServers"]["server"]["env"] == {"FOO": "bar"}

    def test_omits_args_when_empty(self, tmp_path):
        entries = [{"name": "server", "command": "cmd"}]
        result = generate_mcp_config(entries, str(tmp_path))
        data = json.loads(Path(result).read_text())
        assert "args" not in data["mcpServers"]["server"]

    def test_idempotent_regeneration(self, tmp_path):
        entries = [{"name": "test", "command": "cmd"}]
        path1 = generate_mcp_config(entries, str(tmp_path), "proj")
        path2 = generate_mcp_config(entries, str(tmp_path), "proj")
        assert path1 == path2
        assert Path(path1).read_text() == Path(path2).read_text()
