"""Generate CLI arguments and convert them into transport-neutral requests."""

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.cli import CliError
from app.cli.config import DEFAULT_TIMEOUT
from app.cli.spec import Operation, load_tag_descriptions

# One executable example per group, shown in that group's --help epilog.
GROUP_EXAMPLES = {
    "missions": [
        "koan-cli missions list --status pending",
        'koan-cli missions create --text "fix the flaky test" --project koan',
        'koan-cli missions create --command "/fix 1234"',
        "koan-cli missions get 42",
        "koan-cli missions delete 42 --yes",
    ],
    "projects": [
        "koan-cli projects list --pretty",
        "koan-cli projects create --github-url https://github.com/acme/my-toolkit",
        "koan-cli projects update my-toolkit --patch '{\"focus\": true}'",
    ],
    "observability": [
        "koan-cli observability usage --days 30",
        "koan-cli observability metrics --pretty",
        "koan-cli observability logs --limit 200",
    ],
    "admin": [
        "koan-cli admin config",
        "koan-cli admin pause",
        "koan-cli admin resume",
        "koan-cli admin restart --yes",
        "koan-cli admin shutdown --yes",
    ],
    "health": ["koan-cli health"],
    "status": ["koan-cli status"],
}


def _example_epilog(examples: list[str]) -> str | None:
    if not examples:
        return None
    return "examples:\n  " + "\n  ".join(examples)


def _first_examples() -> list[str]:
    examples = []
    examples.append("koan-cli status")
    examples.append('koan-cli missions create --text "review PR 42" --project koan')
    examples.append("koan-cli projects list --pretty")
    examples.append("koan-cli raw GET /v1/metrics")
    return examples


def _root_epilog() -> str:
    lines = ["examples:"] + [f"  {line}" for line in _first_examples()]
    lines.append("")
    lines.append(
        "The bearer token comes from KOAN_API_TOKEN or the profile. Run "
        "`koan-cli configure` to write one."
    )
    return "\n".join(lines)


DESTRUCTIVE = {
    ("POST", "/v1/restart"),
    ("POST", "/v1/shutdown"),
    ("POST", "/v1/update"),
    ("POST", "/v1/update_release"),
}


class _RawHelp(argparse.RawDescriptionHelpFormatter):
    """Help formatter that preserves newlines in help/description/epilog.

    The group and root epilogs are hand-formatted, multi-line example blocks
    (and the ``anyOf`` note appends a line to a generated description); the
    default formatter would collapse them onto one line.
    """


class CliArgumentParser(argparse.ArgumentParser):
    """Argument parser whose usage errors follow the CLI exit contract."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _RawHelp)
        super().__init__(*args, **kwargs)

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


def _structured(schema: dict[str, Any]):
    """Build an argparse type that parses one JSON object/array flag value.

    Falling back to ``str`` here would transmit ``--patch '{"focus": true}'`` as
    a JSON *string*, which no object-typed server field can ever accept.
    """
    expected = dict if schema.get("type") == "object" else list

    def parse(value: str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"expected JSON: {exc.msg}") from exc
        if not isinstance(parsed, expected):
            raise argparse.ArgumentTypeError(f"expected a JSON {schema['type']}")
        return parsed

    parse.__name__ = schema.get("type", "json")
    return parse


def _schema_type(schema: dict[str, Any]):
    if schema.get("type") in {"object", "array"}:
        return _structured(schema)
    return {
        "integer": int,
        "number": float,
        "boolean": _boolean,
    }.get(schema.get("type"), str)


def _add_common_request_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", help="JSON text or @path, valid for every method")
    parser.add_argument(
        "-q",
        "--query",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Raw query parameter, repeated for each KEY=VALUE pair.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip the confirmation prompt on destructive requests. "
            "Required when stdin is not a TTY."
        ),
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--compact", action="store_true", help="Force single-line JSON (default when piped)."
    )
    output.add_argument(
        "--pretty", action="store_true", help="Force indented JSON (default on a TTY)."
    )


def _metavar(schema: dict[str, Any], fallback: str) -> str:
    if schema.get("type") == "boolean":
        return "true|false"
    if schema.get("type") in {"object", "array"}:
        return "JSON"
    return str(schema.get("title") or fallback).upper()


def _required_note(schema: dict | None) -> str | None:
    """Render an ``anyOf`` body schema as an argparse description note.

    The CLI renders each ``anyOf`` branch's properties as independent flags, so
    a schema like ``command XOR text`` cannot express its exclusivity in the
    parser itself. State it in prose on the leaf's description instead.
    """
    if not schema:
        return None
    branches = schema.get("anyOf")
    if not isinstance(branches, list) or not branches:
        return None
    labels = []
    for branch in branches:
        required = branch.get("required")
        if isinstance(required, list) and len(required) == 1:
            labels.append(required[0])
    if len(labels) == len(branches) and len(labels) > 1:
        flags = ", ".join("--" + label.replace("_", "-") for label in labels)
        return f"Provide exactly one of {flags}."
    return None


def _configure_operation_parser(
    parser: argparse.ArgumentParser,
    operation: Operation,
) -> None:
    for parameter in operation.parameters:
        if parameter.location == "path":
            parser.add_argument(
                parameter.name,
                metavar=parameter.name.replace("_", "-").upper(),
                help=parameter.description or None,
            )
        elif parameter.location == "query":
            parser.add_argument(
                f"--{parameter.name.replace('_', '-')}",
                dest=f"_query_{parameter.name}",
                type=_schema_type(parameter.schema),
                metavar=_metavar(parameter.schema, parameter.name),
                help=parameter.description or None,
            )
    properties = (operation.body_schema or {}).get("properties", {})
    for name, schema in properties.items():
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            dest=f"_body_{name}",
            type=_schema_type(schema),
            metavar=_metavar(schema, name),
            help=schema.get("description") or None,
        )

    note = _required_note(operation.body_schema)
    if note:
        base = parser.description.rstrip(".") if parser.description else ""
        parser.description = f"{base}. {note}" if base else note
    _add_common_request_flags(parser)
    parser.set_defaults(_operation=operation)


def build_parser(
    operations: list[Operation],
    spec: dict[str, Any] | None = None,
) -> CliArgumentParser:
    parser = CliArgumentParser(prog="koan-cli")
    parser.description = "Command-line client for the Kōan REST API."
    parser.add_argument("--profile")
    parser.add_argument("--base-url")
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help=(
            "Seconds to wait for a response. Overrides KOAN_TIMEOUT and the "
            f"profile's timeout key (default {DEFAULT_TIMEOUT:g})."
        ),
    )
    roots = parser.add_subparsers(dest="_root", required=True)

    configure = roots.add_parser(
        "configure",
        help="Write a named profile to the config file.",
        description=(
            "Write base URL and bearer token to a named profile in the config file."
        ),
    )
    configure.set_defaults(_builtin="configure")

    raw = roots.add_parser(
        "raw",
        help="Send an arbitrary METHOD/PATH request.",
        description="Send an arbitrary HTTP request to the Kōan REST API.",
    )
    raw.add_argument("raw_method", metavar="METHOD")
    raw.add_argument("raw_path", metavar="PATH")
    _add_common_request_flags(raw)
    raw.set_defaults(_builtin="raw")

    tag_descriptions = load_tag_descriptions(spec or {})

    def _tag_description(tagname: str) -> str | None:
        return tag_descriptions.get(tagname) or None

    groups = {}
    for operation in sorted(operations, key=lambda item: item.command):
        if len(operation.command) == 1:
            leaf = roots.add_parser(
                operation.command[0],
                help=operation.summary,
                description=operation.description or operation.summary,
                epilog=_example_epilog(GROUP_EXAMPLES.get(operation.command[0])),
            )
        else:
            group_name, leaf_name = operation.command
            if group_name not in groups:
                group = roots.add_parser(
                    group_name,
                    help=_tag_description(group_name) or operation.summary,
                    description=_tag_description(group_name) or operation.summary,
                    epilog=_example_epilog(GROUP_EXAMPLES.get(group_name)),
                )
                groups[group_name] = group.add_subparsers(
                    dest=f"_{group_name}_command",
                    required=True,
                )
            leaf = groups[group_name].add_parser(
                leaf_name,
                help=operation.summary,
                description=operation.description or operation.summary,
            )
        _configure_operation_parser(leaf, operation)

    parser.epilog = _root_epilog()
    return parser


def expand_alias(argv: list[str], operations: list[Operation]) -> list[str]:
    if not argv:
        return argv
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in {"--profile", "--base-url", "--timeout"}:
            index += 2
            continue
        if value.split("=", 1)[0] in {"--profile", "--base-url", "--timeout"}:
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

    if operation.body_required and not has_body:
        raise CliError("this operation requires a JSON request body")
    # schema.required lists what must be present *if* a body is sent; it does
    # not make an optional requestBody mandatory.
    if has_body:
        required = set((operation.body_schema or {}).get("required", []))
        missing = required - set(body) if isinstance(body, dict) else required
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
