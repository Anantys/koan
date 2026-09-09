import stat
import subprocess
from pathlib import Path

import requests

from app.cli import EXIT_LOCAL, EXIT_OK
from app.cli.main import main


def test_health_uses_spec_default_without_config(
    api_spec_path, tmp_path, session_factory, response_factory
):
    session = session_factory([response_factory(200, {"status": "ok"})])
    code = main(
        ["health", "--compact"],
        spec_path=api_spec_path,
        config_path=tmp_path / "missing.cfg",
        session=session,
    )
    assert code == EXIT_OK
    assert session.calls[0][1] == "http://127.0.0.1:8420/v1/health"
    assert "Authorization" not in session.calls[0][2]["headers"]


def test_raw_dispatch_honors_profile(
    api_spec_path, tmp_path, session_factory, response_factory
):
    path = tmp_path / "koan-cli.cfg"
    path.write_text("[prod]\nbase_url = https://prod.example\ntoken = prod-token\n")
    path.chmod(0o600)
    session = session_factory([response_factory(200, {"ok": True})])

    code = main(
        ["--profile", "prod", "raw", "GET", "/v1/status", "--compact"],
        spec_path=api_spec_path,
        config_path=path,
        session=session,
    )
    assert code == EXIT_OK
    assert session.calls[0][1] == "https://prod.example/v1/status"
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer prod-token"


def test_configure_saves_even_when_server_is_down(
    api_spec_path, tmp_path, monkeypatch, session_factory
):
    path = tmp_path / "koan-cli.cfg"
    monkeypatch.setattr("builtins.input", lambda prompt: "https://down.example")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "token")
    session = session_factory([requests.ConnectionError("down")])

    assert main(
        ["configure"],
        spec_path=api_spec_path,
        config_path=path,
        session=session,
    ) == EXIT_LOCAL
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "https://down.example" in path.read_text()


def test_destructive_non_tty_stops_before_request(
    api_spec_path, tmp_path, session_factory, monkeypatch
):
    session = session_factory([])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = main(
        ["admin", "shutdown"],
        spec_path=api_spec_path,
        config_path=tmp_path / "missing.cfg",
        session=session,
    )
    assert code == EXIT_LOCAL
    assert session.calls == []


def test_executable_help():
    executable = Path(__file__).resolve().parents[3] / "bin" / "koan-cli"
    result = subprocess.run(
        [executable, "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "configure" in result.stdout
    assert "raw" in result.stdout
