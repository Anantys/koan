"""Top-level orchestration for generated and built-in REST CLI commands."""

import getpass
import sys
from pathlib import Path

import requests

from app.cli import EXIT_LOCAL, EXIT_OK, CliError
from app.cli.commands import (
    build_operation_request,
    build_parser,
    build_raw_request,
    expand_alias,
)
from app.cli.config import (
    CONFIG_PATH,
    load_settings,
    resolve_timeout,
    write_profile,
)
from app.cli.http import confirm_destructive, execute, verify_configuration
from app.cli.spec import load_operations, load_server_default, load_spec


DEFAULT_SPEC = Path(__file__).resolve().parents[2] / "openapi.yaml"


def main(
    argv=None,
    *,
    spec_path: Path | None = None,
    config_path: Path | None = None,
    session=requests,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = load_spec(spec_path or DEFAULT_SPEC)
        operations = load_operations(spec)
        arguments = expand_alias(arguments, operations)
        args = build_parser(operations, spec).parse_args(arguments)
        server_default = load_server_default(spec)
        path = config_path or CONFIG_PATH

        if getattr(args, "_builtin", None) == "configure":
            profile = (args.profile or "default").strip()
            default_url = (args.base_url or server_default).rstrip("/")
            entered_url = input(f"Base URL [{default_url}]: ").strip()
            base_url = (entered_url or default_url).rstrip("/")
            token = getpass.getpass("Bearer token: ").strip()
            write_profile(path, profile, base_url, token)
            verification = verify_configuration(
                base_url,
                token,
                session,
                timeout=resolve_timeout(cli_timeout=args.timeout),
            )
            # stdout carries valid JSON only; a failed probe is a diagnostic.
            stream = sys.stdout if verification.exit_code == EXIT_OK else sys.stderr
            print(verification.message, file=stream)
            return verification.exit_code

        settings = load_settings(
            path,
            server_default,
            cli_profile=args.profile,
            cli_base_url=args.base_url,
            cli_timeout=args.timeout,
        )
        if getattr(args, "_builtin", None) == "raw":
            plan = build_raw_request(
                args.raw_method,
                args.raw_path,
                settings.base_url,
                data=args.data,
                query=args.query,
            )
        else:
            plan = build_operation_request(
                args._operation,
                args,
                settings.base_url,
            )

        confirm_destructive(plan, yes=args.yes)
        return execute(
            plan,
            settings,
            session,
            compact=args.compact,
            pretty=args.pretty,
        )
    except CliError as exc:
        print(f"koan-cli: {exc}", file=sys.stderr)
        return EXIT_LOCAL


if __name__ == "__main__":
    raise SystemExit(main())
