"""Secure named profile storage and REST CLI setting resolution."""

import configparser
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from app.apiclient import DEFAULT_TIMEOUT
from app.cli import CliError


CONFIG_PATH = Path.home() / ".config" / "koan-cli.cfg"
ALLOWED_MODES = {0o600, 0o400}


@dataclass(frozen=True)
class Settings:
    profile: str
    base_url: str
    token: str
    timeout: float = DEFAULT_TIMEOUT


def _nonempty(mapping: Mapping[str, str], name: str) -> str | None:
    value = mapping.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_timeout(value: str, source: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise CliError(f"invalid {source}: {value!r} is not a number") from exc
    if timeout <= 0:
        raise CliError(f"invalid {source}: {value!r} must be greater than zero")
    return timeout


def resolve_profile(
    *,
    cli_profile: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the profile name: flag, then environment, then ``default``."""
    env = os.environ if environ is None else environ
    return (cli_profile or "").strip() or _nonempty(env, "KOAN_PROFILE") or "default"


def resolve_base_url(
    *,
    cli_base_url: str | None = None,
    section: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    fallback: str = "",
) -> str:
    """Resolve the base URL: flag, environment, stored profile, then fallback."""
    env = os.environ if environ is None else environ
    stored = str(section.get("base_url", "")).strip() if section is not None else ""
    value = (
        (cli_base_url or "").strip()
        or _nonempty(env, "KOAN_BASE_URL")
        or stored
        or fallback
    )
    return value.rstrip("/")


def resolve_timeout(
    *,
    cli_timeout: float | None = None,
    section: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Resolve the response timeout: flag, then environment, then profile."""
    env = os.environ if environ is None else environ
    if cli_timeout is not None:
        return _parse_timeout(str(cli_timeout), "--timeout")
    if (raw := _nonempty(env, "KOAN_TIMEOUT")) is not None:
        return _parse_timeout(raw, "KOAN_TIMEOUT")
    if section is not None and (raw := _nonempty(section, "timeout")) is not None:
        return _parse_timeout(raw, "profile timeout")
    return DEFAULT_TIMEOUT


def _read_config(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        return parser
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode not in ALLOWED_MODES:
        raise CliError(
            f"refusing insecure config {path} (mode {mode:04o}); "
            f"run: chmod 600 {path}"
        )
    try:
        with path.open() as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        raise CliError(f"cannot read config {path}: {exc}") from exc
    return parser


def load_settings(
    path: Path,
    server_default: str,
    *,
    cli_profile: str | None = None,
    cli_base_url: str | None = None,
    cli_timeout: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    profile = resolve_profile(cli_profile=cli_profile, environ=env)
    parser = _read_config(path)
    if parser.sections() and profile not in parser:
        raise CliError(f"unknown profile {profile!r} in {path}")
    section = parser[profile] if profile in parser else {}
    base_url = resolve_base_url(
        cli_base_url=cli_base_url,
        section=section,
        environ=env,
        fallback=server_default,
    )
    token = _nonempty(env, "KOAN_API_TOKEN") or str(section.get("token", "")).strip()
    timeout = resolve_timeout(cli_timeout=cli_timeout, section=section, environ=env)
    return Settings(profile, base_url, token, timeout)


def write_profile(path: Path, profile: str, base_url: str, token: str) -> None:
    profile = profile.strip()
    if not profile:
        raise CliError("profile name cannot be empty")
    parser = _read_config(path)
    if profile not in parser:
        try:
            parser.add_section(profile)
        except ValueError as exc:
            raise CliError(f"invalid profile name: {profile!r}") from exc
    parser[profile]["base_url"] = base_url.strip().rstrip("/")
    parser[profile]["token"] = token.strip()

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
            text=True,
        )
    except OSError as exc:
        raise CliError(f"cannot create config {path}: {exc}") from exc

    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            parser.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise CliError(f"cannot write config {path}: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
