from unittest.mock import patch

from app.config import (
    get_mcp_configs,
    get_mcp_enabled,
    get_mcp_host,
    get_mcp_port,
    get_mcp_tools_allow_destructive,
    get_mcp_transport,
)
from app.mcp.config import get_api_base_url, get_mcp_http_url
from app.config_validator import validate_config


def test_mcp_server_defaults_off():
    with patch("app.config._load_config", return_value={}):
        assert get_mcp_enabled() is False
        assert get_mcp_tools_allow_destructive() is False


def test_mcp_server_flags_require_booleans():
    with patch(
        "app.config._load_config",
        return_value={
            "mcp": {"enabled": True, "tools_allow_destructive": True},
        },
    ):
        assert get_mcp_enabled() is True
        assert get_mcp_tools_allow_destructive() is True

    with patch(
        "app.config._load_config",
        return_value={"mcp": {"enabled": "yes", "tools_allow_destructive": 1}},
    ):
        assert get_mcp_enabled() is False
        assert get_mcp_tools_allow_destructive() is False


def test_legacy_mcp_config_list_does_not_enable_server():
    with patch("app.config._load_config", return_value={"mcp": ["server.json"]}):
        assert get_mcp_enabled() is False


def test_mapping_keeps_provider_config_paths():
    with patch(
        "app.config._load_config",
        return_value={"mcp": {"enabled": True, "configs": ["server.json"]}},
    ):
        assert get_mcp_configs() == ["server.json"]


def test_api_base_url_uses_api_host_and_port():
    with patch("app.mcp.config.get_api_host", return_value="127.0.0.2"), patch(
        "app.mcp.config.get_api_port", return_value=9000
    ):
        assert get_api_base_url() == "http://127.0.0.2:9000"


def test_mcp_mapping_and_legacy_list_validate():
    assert validate_config(
        {
            "mcp": {
                "enabled": False,
                "tools_allow_destructive": False,
                "configs": ["server.json"],
            }
        }
    ) == []
    assert validate_config({"mcp": ["server.json"]}) == []


def test_mcp_http_settings_default_to_stdio_loopback():
    with patch("app.config._load_config", return_value={}):
        assert get_mcp_transport() == "stdio"
        assert get_mcp_host() == "127.0.0.1"
        assert get_mcp_port() == 8421


def test_mcp_http_settings_read_mapping():
    config = {
        "mcp": {
            "enabled": True,
            "transport": "http",
            "host": "127.0.0.2",
            "port": 9123,
        }
    }
    with patch("app.config._load_config", return_value=config):
        assert get_mcp_transport() == "http"
        assert get_mcp_host() == "127.0.0.2"
        assert get_mcp_port() == 9123


def test_invalid_mcp_transport_falls_back_and_warns():
    with patch(
        "app.config._load_config",
        return_value={"mcp": {"transport": "sse"}},
    ):
        assert get_mcp_transport() == "stdio"

    warnings = validate_config({"mcp": {"transport": "sse"}})
    assert warnings == [
        ("mcp.transport", "'mcp.transport' must be one of http/stdio, got 'sse'")
    ]


def test_legacy_mcp_list_keeps_http_defaults():
    with patch("app.config._load_config", return_value={"mcp": ["server.json"]}):
        assert get_mcp_transport() == "stdio"
        assert get_mcp_host() == "127.0.0.1"
        assert get_mcp_port() == 8421


def test_mcp_http_url_brackets_ipv6_and_wildcard_to_loopback():
    with patch("app.mcp.config.get_mcp_host", return_value="::"), patch(
        "app.mcp.config.get_mcp_port", return_value=8421
    ):
        assert get_mcp_http_url() == "http://127.0.0.1:8421/mcp"

    with patch("app.mcp.config.get_mcp_host", return_value="2001:db8::1"), patch(
        "app.mcp.config.get_mcp_port", return_value=8421
    ):
        assert get_mcp_http_url() == "http://[2001:db8::1]:8421/mcp"
