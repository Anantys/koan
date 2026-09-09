"""Secure named profile storage and REST CLI setting resolution."""

import configparser
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from app.cli import CliError


CONFIG_PATH = Path.home() / ".config" / "koan-cli.cfg"
ALLOWED_MODES = {0o600, 0o400}


@dataclass(frozen=True)
class Settings:
    profile: str
    base_url: str
    token: str


def _nonempty(mapping: Mapping[str, str], name: str) -> str | None:
    value = mapping.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


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
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    profile = (cli_profile or "").strip() or _nonempty(env, "KOAN_PROFILE") or "default"
    parser = _read_config(path)
    if parser.sections() and profile not in parser:
        raise CliError(f"unknown profile {profile!r} in {path}")
    section = parser[profile] if profile in parser else {}
    base_url = (
        (cli_base_url or "").strip()
        or _nonempty(env, "KOAN_BASE_URL")
        or str(section.get("base_url", "")).strip()
        or server_default
    )
    token = _nonempty(env, "KOAN_API_TOKEN") or str(section.get("token", "")).strip()
    return Settings(profile, base_url.rstrip("/"), token)


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
