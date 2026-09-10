"""SDK-free named-tool curation and schema logic."""

from copy import deepcopy
from dataclasses import dataclass

from app.apiclient.spec import Operation


OperationKey = tuple[str, str]


@dataclass(frozen=True)
class ToolAnnotations:
    read_only: bool = False
    destructive: bool = False


@dataclass(frozen=True)
class CuratedTool:
    name: str
    method: str
    path: str
    annotations: ToolAnnotations
    input_schema: dict


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    operation: Operation
    annotations: ToolAnnotations
    input_schema: dict

    @property
    def operation_key(self) -> OperationKey:
        return (self.operation.method, self.operation.path)


_EMPTY_SCHEMA = {"type": "object", "properties": {}}

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Slash-command mission; takes precedence over text.",
        },
        "text": {"type": "string", "description": "Free-form mission text."},
        "project": {"type": "string", "description": "Optional project name."},
        "urgent": {
            "type": "boolean",
            "default": False,
            "description": "Insert at front of pending queue.",
        },
    },
    "anyOf": [{"required": ["command"]}, {"required": ["text"]}],
}

_REORDER_SCHEMA = {
    "type": "object",
    "required": ["mission_id", "target_position"],
    "properties": {
        "mission_id": {"type": "string"},
        "target_position": {"type": "integer", "minimum": 1},
    },
}

_PAUSE_SCHEMA = {
    "type": "object",
    "properties": {
        "duration": {
            "type": "string",
            "description": "Duration such as 2h or 30m; omit for indefinite.",
        }
    },
}


def _read(name: str, method: str, path: str) -> CuratedTool:
    return CuratedTool(
        name,
        method,
        path,
        ToolAnnotations(read_only=True),
        _EMPTY_SCHEMA,
    )


def _write(
    name: str,
    method: str,
    path: str,
    schema: dict = _EMPTY_SCHEMA,
    *,
    destructive: bool = False,
) -> CuratedTool:
    return CuratedTool(
        name,
        method,
        path,
        ToolAnnotations(destructive=destructive),
        schema,
    )


CURATED_TOOLS = (
    _read("koan_health", "GET", "/v1/health"),
    _read("koan_status", "GET", "/v1/status"),
    _read("koan_missions_list", "GET", "/v1/missions"),
    _read("koan_missions_get", "GET", "/v1/missions/{mission_id}"),
    _read("koan_missions_result", "GET", "/v1/missions/{mission_id}/result"),
    _read("koan_projects_list", "GET", "/v1/projects"),
    _read("koan_usage", "GET", "/v1/usage"),
    _read("koan_metrics", "GET", "/v1/metrics"),
    _read("koan_logs", "GET", "/v1/logs"),
    _read("koan_config", "GET", "/v1/config"),
    _write("koan_missions_create", "POST", "/v1/missions", _CREATE_SCHEMA),
    _write(
        "koan_missions_reorder",
        "POST",
        "/v1/missions/reorder",
        _REORDER_SCHEMA,
    ),
    _write("koan_pause", "POST", "/v1/pause", _PAUSE_SCHEMA),
    _write("koan_resume", "POST", "/v1/resume"),
    _write(
        "koan_missions_delete",
        "DELETE",
        "/v1/missions/{mission_id}",
        destructive=True,
    ),
)

DENIED_NAMED_OPERATIONS: frozenset[OperationKey] = frozenset(
    {
        ("POST", "/v1/shutdown"),
        ("POST", "/v1/restart"),
        ("POST", "/v1/update"),
        ("POST", "/v1/update_release"),
        ("POST", "/v1/projects"),
        ("PATCH", "/v1/projects/{name}"),
        ("DELETE", "/v1/projects/{name}"),
    }
)


def _operation_schema(operation: Operation, curated: CuratedTool) -> dict:
    if curated.input_schema is not _EMPTY_SCHEMA:
        return deepcopy(curated.input_schema)
    properties = {}
    required = []
    for parameter in operation.parameters:
        if parameter.location not in {"path", "query"}:
            continue
        schema = deepcopy(parameter.schema)
        if parameter.description:
            schema["description"] = parameter.description
        properties[parameter.name] = schema
        if parameter.required:
            required.append(parameter.name)
    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def build_tool_definitions(
    operations: list[Operation],
    *,
    allow_destructive: bool,
) -> list[ToolDefinition]:
    """Return fail-closed named tools without importing MCP SDK."""
    by_key = {(operation.method, operation.path): operation for operation in operations}
    result = []
    for curated in CURATED_TOOLS:
        key = (curated.method, curated.path)
        if key in DENIED_NAMED_OPERATIONS:
            continue
        if curated.annotations.destructive and not allow_destructive:
            continue
        operation = by_key.get(key)
        if operation is None or not operation.mcp_enabled:
            continue
        result.append(
            ToolDefinition(
                name=curated.name,
                operation=operation,
                annotations=curated.annotations,
                input_schema=_operation_schema(operation, curated),
            )
        )
    return result
