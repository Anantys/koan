import stat

import pytest

from app.cli import CliError
from app.cli.config import load_settings, write_profile


def test_empty_environment_values_are_unset(tmp_path):
    path = tmp_path / "koan-cli.cfg"
    path.write_text(
        "[default]\n"
        "base_url = https://file.example\n"
        "token = file-token\n"
    )
    path.chmod(0o600)

    settings = load_settings(
        path,
        "http://127.0.0.1:8420",
        environ={
            "KOAN_PROFILE": "",
            "KOAN_BASE_URL": "  ",
            "KOAN_API_TOKEN": "",
        },
    )

    assert settings.profile == "default"
    assert settings.base_url == "https://file.example"
    assert settings.token == "file-token"


def test_cli_and_environment_precedence(tmp_path):
    path = tmp_path / "koan-cli.cfg"
    path.write_text(
        "[prod]\n"
        "base_url = https://file.example/\n"
        "token = file-token\n"
    )
    path.chmod(0o400)

    settings = load_settings(
        path,
        "http://127.0.0.1:8420",
        cli_profile="prod",
        cli_base_url="https://cli.example/",
        environ={"KOAN_API_TOKEN": "env-token"},
    )

    assert settings.profile == "prod"
    assert settings.base_url == "https://cli.example"
    assert settings.token == "env-token"


def test_missing_config_uses_spec_default(tmp_path):
    settings = load_settings(
        tmp_path / "missing.cfg",
        "http://127.0.0.1:8420/",
        environ={},
    )
    assert settings.base_url == "http://127.0.0.1:8420"
    assert settings.token == ""


def test_unknown_selected_profile_names_path(tmp_path):
    path = tmp_path / "koan-cli.cfg"
    path.write_text("[default]\ntoken = local\n")
    path.chmod(0o600)
    with pytest.raises(CliError, match=rf"unknown profile 'prod' in {path}"):
        load_settings(path, "http://127.0.0.1:8420", cli_profile="prod")


def test_insecure_config_names_exact_fix(tmp_path):
    path = tmp_path / "koan-cli.cfg"
    path.write_text("[default]\ntoken = exposed\n")
    path.chmod(0o644)

    with pytest.raises(CliError, match=rf"chmod 600 {path}"):
        load_settings(path, "http://127.0.0.1:8420")


def test_write_profile_is_mode_600_and_preserves_profiles(tmp_path):
    path = tmp_path / "koan-cli.cfg"
    write_profile(path, "prod", "https://prod.example", "prod-token")
    write_profile(path, "default", "http://127.0.0.1:8420", "local-token")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "[prod]" in text
    assert "[default]" in text


def test_empty_profile_name_cannot_be_written(tmp_path):
    with pytest.raises(CliError, match="profile name cannot be empty"):
        write_profile(tmp_path / "koan-cli.cfg", "  ", "http://localhost", "token")
