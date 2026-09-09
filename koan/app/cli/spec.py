"""Discover stable CLI commands from Kōan's committed OpenAPI document."""

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from app.cli import CliError


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
RESERVED_ROOTS = {"configure", "raw"}
ITEM_VERBS = {
    "get": "get",
    "patch": "update",
    "put": "update",
    "delete": "delete",
}


class SpecError(CliError):
    """An invalid or ambiguous OpenAPI document."""


@dataclass(frozen=True)
class Parameter:
    name: str
    location: str
    required: bool
    schema: dict[str, Any]


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    operation_id: str
    command: tuple[str, ...]
    parameters: tuple[Parameter, ...]
    body_schema: dict[str, Any] | None
    body_required: bool
    requires_auth: bool


def load_spec(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"cannot load OpenAPI document {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        raise SpecError(f"invalid OpenAPI document: {path}")
    return data


def resolve_local_ref(spec: dict[str, Any], value: Any) -> Any:
    if not isinstance(value, dict) or "$ref" not in value:
        return value
    ref = value["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise SpecError(f"unsupported OpenAPI reference: {ref}")
    current: Any = spec
    try:
        for token in ref[2:].split("/"):
            key = token.replace("~1", "/").replace("~0", "~")
            current = current[key]
    except (KeyError, TypeError) as exc:
        raise SpecError(f"unresolved OpenAPI reference: {ref}") from exc
    return resolve_local_ref(spec, current)


def command_name(method: str, path: str, tag: str, tag_count: int) -> tuple[str, ...]:
    segments = path.removeprefix("/v1/").strip("/").split("/")
    first = segments[0].replace("_", "-")
    if len(segments) == 1:
        if tag_count == 1 and tag == first:
            return (tag,)
        if tag == first and method == "get":
            return (tag, "list")
        if tag == first and method == "post":
            return (tag, "create")
        return (tag, first)
    if segments[1].startswith("{"):
        if len(segments) > 2:
            return (tag, segments[-1].replace("_", "-"))
        try:
            return (tag, ITEM_VERBS[method])
        except KeyError as exc:
            raise SpecError(f"unsupported item operation: {method.upper()} {path}") from exc
    return (tag, segments[-1].replace("_", "-"))


def load_operations(spec: dict[str, Any]) -> list[Operation]:
    raw = []
    tag_counts: dict[str, int] = {}
    for path, path_item in spec["paths"].items():
        if not isinstance(path_item, dict):
            raise SpecError(f"invalid path item: {path}")
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                raise SpecError(f"invalid operation: {method.upper()} {path}")
            tags = operation.get("tags", [])
            if not tags or not isinstance(tags[0], str):
                raise SpecError(f"operation has no tag: {method.upper()} {path}")
            tag = tags[0]
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            raw.append((path, path_item, method, operation, tag))

    operations = []
    for path, path_item, method, operation, tag in raw:
        params = []
        for item in [*path_item.get("parameters", []), *operation.get("parameters", [])]:
            item = resolve_local_ref(spec, item)
            params.append(
                Parameter(
                    name=item["name"],
                    location=item["in"],
                    required=bool(item.get("required")),
                    schema=resolve_local_ref(spec, item.get("schema", {})),
                )
            )
        request_body = resolve_local_ref(spec, operation.get("requestBody", {}))
        media = request_body.get("content", {}).get("application/json", {})
        body_schema = resolve_local_ref(spec, media.get("schema"))
        security = operation.get("security", spec.get("security", []))
        try:
            operation_id = operation["operationId"]
        except KeyError as exc:
            raise SpecError(f"operation has no operationId: {method.upper()} {path}") from exc
        operations.append(
            Operation(
                method=method.upper(),
                path=path,
                operation_id=operation_id,
                command=command_name(method, path, tag, tag_counts[tag]),
                parameters=tuple(params),
                body_schema=body_schema,
                body_required=bool(request_body.get("required")),
                requires_auth=bool(security),
            )
        )
    assert_unique(operations)
    return operations


def assert_unique(operations: list[Operation]) -> None:
    commands: dict[tuple[str, ...], Operation] = {}
    aliases: dict[str, Operation] = {}

    for operation in operations:
        if operation.command in commands:
            raise SpecError(f"duplicate command: {' '.join(operation.command)}")
        commands[operation.command] = operation

        if operation.operation_id in aliases:
            raise SpecError(f"duplicate operationId alias: {operation.operation_id}")
        aliases[operation.operation_id] = operation

    for left, right in combinations(commands, 2):
        shorter, longer = sorted((left, right), key=len)
        if longer[: len(shorter)] == shorter:
            raise SpecError(
                f"command prefix collision: {' '.join(shorter)} / {' '.join(longer)}"
            )

    public_roots = {command[0] for command in commands}
    reserved_collision = public_roots & RESERVED_ROOTS
    if reserved_collision:
        raise SpecError(f"reserved root command: {sorted(reserved_collision)[0]}")

    for alias in aliases:
        if alias in RESERVED_ROOTS:
            raise SpecError(f"operationId alias collides with built-in: {alias}")
        if alias in public_roots:
            raise SpecError(f"operationId alias collides with root command: {alias}")


def load_server_default(spec: dict[str, Any]) -> str:
    servers = spec.get("servers")
    if not isinstance(servers, list) or not servers:
        raise SpecError("OpenAPI document has no servers entry")
    first = servers[0]
    if not isinstance(first, dict):
        raise SpecError("OpenAPI servers[0] is invalid")
    url = first.get("url", "")
    if not isinstance(url, str) or not url.strip():
        raise SpecError("OpenAPI servers[0].url is empty")
    return url.strip().rstrip("/")
