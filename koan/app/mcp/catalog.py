"""SDK-free named-tool curation and metadata policy."""

from dataclasses import dataclass

from app.apiclient.spec import Operation


OperationKey = tuple[str, str]


@dataclass(frozen=True)
class ToolAnnotations:
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False


@dataclass(frozen=True)
class CuratedTool:
    name: str
    title: str
    method: str
    path: str
    annotations: ToolAnnotations


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    title: str
    operation: Operation
    annotations: ToolAnnotations

    @property
    def description(self) -> str:
        return "\n\n".join(
            part
            for part in (
                self.operation.summary,
                self.operation.description,
                self.operation.mcp_description,
            )
            if part
        )

    @property
    def operation_key(self) -> OperationKey:
        return (self.operation.method, self.operation.path)


def _read(name: str, title: str, method: str, path: str) -> CuratedTool:
    return CuratedTool(
        name,
        title,
        method,
        path,
        ToolAnnotations(read_only=True, idempotent=True),
    )


def _write(
    name: str,
    title: str,
    method: str,
    path: str,
    *,
    destructive: bool = False,
    idempotent: bool = False,
) -> CuratedTool:
    return CuratedTool(
        name,
        title,
        method,
        path,
        ToolAnnotations(
            destructive=destructive,
            idempotent=idempotent,
        ),
    )


CURATED_TOOLS = (
    _read("koan_health", "Check API health", "GET", "/v1/health"),
    _read("koan_status", "Get Kōan status", "GET", "/v1/status"),
    _read("koan_missions_list", "List missions", "GET", "/v1/missions"),
    _read(
        "koan_missions_get",
        "Get a mission",
        "GET",
        "/v1/missions/{mission_id}",
    ),
    _read(
        "koan_missions_result",
        "Get mission result",
        "GET",
        "/v1/missions/{mission_id}/result",
    ),
    _read("koan_projects_list", "List projects", "GET", "/v1/projects"),
    _read("koan_usage", "Get usage", "GET", "/v1/usage"),
    _read("koan_metrics", "Get mission metrics", "GET", "/v1/metrics"),
    _read("koan_logs", "Read recent logs", "GET", "/v1/logs"),
    _read("koan_config", "Get effective config", "GET", "/v1/config"),
    _write(
        "koan_missions_create",
        "Queue a mission",
        "POST",
        "/v1/missions",
    ),
    _write(
        "koan_missions_reorder",
        "Reorder a mission",
        "POST",
        "/v1/missions/reorder",
    ),
    _write("koan_pause", "Pause Kōan", "POST", "/v1/pause"),
    _write(
        "koan_resume",
        "Resume Kōan",
        "POST",
        "/v1/resume",
        idempotent=True,
    ),
    _write(
        "koan_missions_delete",
        "Delete a mission",
        "DELETE",
        "/v1/missions/{mission_id}",
        destructive=True,
        idempotent=True,
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
                title=curated.title,
                operation=operation,
                annotations=curated.annotations,
            )
        )
    return result
