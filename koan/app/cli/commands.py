"""Generate CLI arguments and convert them into transport-neutral requests."""

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.cli import CliError
from app.cli.spec import Operation


DESTRUCTIVE = {
    ("POST", "/v1/restart"),
    ("POST", "/v1/shutdown"),
    ("POST", "/v1/update"),
    ("POST", "/v1/update_release"),
}


class CliArgumentParser(argparse.ArgumentParser):
    """Argument parser whose usage errors follow the CLI exit contract."""

    def error(self, message):
        self.print_usage()
        self.exit(1, f"{self.prog}: error: {message}\n")


def _boolean(value: str) -> bool:
    normalized = value.lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _schema_type(schema: dict[str, Any]):
    return {
        "integer": int,
        "number": float,
        "boolean": _boolean,
    }.get(schema.get("type"), str)


def _add_common_request_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", help="JSON text or @path, valid for every method")
    parser.add_argument(
        "-q", "--query", action="append", default=[], metavar="KEY=VALUE"
    )
    parser.add_argument("--yes", action="store_true")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--compact", action="store_true")
    output.add_argument("--pretty", action="store_true")


def _configure_operation_parser(
    parser: argparse.ArgumentParser,
    operation: Operation,
) -> None:
    for parameter in operation.parameters:
        if parameter.location == "path":
            parser.add_argument(parameter.name)
        elif parameter.location == "query":
            parser.add_argument(
                f"--{parameter.name.replace('_', '-')}",
                dest=f"_query_{parameter.name}",
                type=_schema_type(parameter.schema),
            )
    properties = (operation.body_schema or {}).get("properties", {})
    for name, schema in properties.items():
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            dest=f"_body_{name}",
            type=_schema_type(schema),
        )
    _add_common_request_flags(parser)
    parser.set_defaults(_operation=operation)


def build_parser(operations: list[Operation]) -> CliArgumentParser:
    parser = CliArgumentParser(prog="koan-cli")
    parser.add_argument("--profile")
    parser.add_argument("--base-url")
    roots = parser.add_subparsers(dest="_root", required=True)

    configure = roots.add_parser("configure")
    configure.set_defaults(_builtin="configure")

    raw = roots.add_parser("raw")
    raw.add_argument("raw_method")
    raw.add_argument("raw_path")
    _add_common_request_flags(raw)
    raw.set_defaults(_builtin="raw")

    groups = {}
    for operation in sorted(operations, key=lambda item: item.command):
        if len(operation.command) == 1:
            leaf = roots.add_parser(operation.command[0])
        else:
            group_name, leaf_name = operation.command
            if group_name not in groups:
                group = roots.add_parser(group_name)
                groups[group_name] = group.add_subparsers(
                    dest=f"_{group_name}_command",
                    required=True,
                )
            leaf = groups[group_name].add_parser(leaf_name)
        _configure_operation_parser(leaf, operation)
    return parser


def expand_alias(argv: list[str], operations: list[Operation]) -> list[str]:
    if not argv:
        return argv
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in {"--profile", "--base-url"}:
            index += 2
            continue
        if value.startswith("--profile=") or value.startswith("--base-url="):
            index += 1
            continue
        break
    if index >= len(argv):
        return argv
    aliases = {operation.operation_id: operation.command for operation in operations}
    command = aliases.get(argv[index])
    if command is None:
        return argv
    return [*argv[:index], *command, *argv[index + 1 :]]


@dataclass(frozen=True)
class RequestPlan:
    method: str
    url: str
    query: dict[str, Any]
    body: Any
    has_body: bool
    requires_auth: bool
    destructive: bool


def _load_json(value: str) -> Any:
    if value.startswith("@"):
        try:
            text = Path(value[1:]).read_text()
        except OSError as exc:
            raise CliError(f"cannot read --data file {value[1:]}: {exc}") from exc
    else:
        text = value
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid JSON for --data: {exc.msg}") from exc


def _query_pairs(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise CliError(f"query value must be KEY=VALUE: {value}")
        key, item = value.split("=", 1)
        if not key:
            raise CliError("query key cannot be empty")
        result[key] = item
    return result


def _render_path(operation: Operation, args: argparse.Namespace) -> str:
    path = operation.path
    for parameter in operation.parameters:
        if parameter.location == "path":
            encoded = quote(str(getattr(args, parameter.name)), safe="")
            path = path.replace(f"{{{parameter.name}}}", encoded)
    if re.search(r"{[^{}]+}", path):
        raise CliError(f"unresolved path parameter in {path}")
    return path


def build_operation_request(
    operation: Operation,
    args: argparse.Namespace,
    base_url: str,
) -> RequestPlan:
    query = _query_pairs(args.query)
    for parameter in operation.parameters:
        if parameter.location != "query":
            continue
        value = vars(args).get(f"_query_{parameter.name}")
        if value is not None:
            query[parameter.name] = value
        if parameter.required and parameter.name not in query:
            raise CliError(f"missing required query parameter: {parameter.name}")

    has_body = args.data is not None
    body = _load_json(args.data) if has_body else {}
    body_values = {
        name: value
        for name in (operation.body_schema or {}).get("properties", {})
        if (value := vars(args).get(f"_body_{name}")) is not None
    }
    if body_values:
        if has_body and not isinstance(body, dict):
            raise CliError("typed body flags cannot be merged into non-object JSON")
        body.update(body_values)
        has_body = True

    required = set((operation.body_schema or {}).get("required", []))
    missing = required - set(body) if isinstance(body, dict) else required
    if operation.body_required and not has_body:
        raise CliError("this operation requires a JSON request body")
    if missing:
        raise CliError(f"missing required body field: {sorted(missing)[0]}")

    rendered = _render_path(operation, args)
    return RequestPlan(
        method=operation.method,
        url=f"{base_url.rstrip('/')}/{rendered.lstrip('/')}",
        query=query,
        body=body,
        has_body=has_body,
        requires_auth=operation.requires_auth,
        destructive=is_destructive(operation.method, operation.path),
    )


def is_destructive(method: str, path: str) -> bool:
    normalized = (method.upper(), path.split("?", 1)[0])
    return normalized[0] == "DELETE" or normalized in DESTRUCTIVE


def build_raw_request(
    method: str,
    path: str,
    base_url: str,
    *,
    data: str | None,
    query: list[str],
) -> RequestPlan:
    method = method.upper()
    if not method.isalpha():
        raise CliError(f"invalid HTTP method: {method}")
    if not path.startswith("/"):
        raise CliError("raw path must start with /")
    return RequestPlan(
        method=method,
        url=f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        query=_query_pairs(query),
        body=_load_json(data) if data is not None else None,
        has_body=data is not None,
        requires_auth=not (
            method == "GET" and path.split("?", 1)[0] == "/v1/health"
        ),
        destructive=is_destructive(method, path),
    )
