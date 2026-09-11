"""MCP SDK adapter exposing curated REST operations over stdio."""

from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations as SdkToolAnnotations

from app.apiclient import DEFAULT_TIMEOUT, ApiClientError, RestApiClient
from app.apiclient.spec import load_operations, load_spec
from app.mcp.catalog import ToolDefinition, build_tool_definitions
from app.mcp.config import get_api_base_url


DEFAULT_SPEC = Path(__file__).resolve().parents[2] / "openapi.yaml"


def _present(**values) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def create_server(
    *,
    spec_path: Path | None = None,
    allow_destructive: bool = False,
    client=None,
) -> MCPServer:
    """Build an MCP server. No network probe occurs here."""
    resolved_spec = spec_path or DEFAULT_SPEC
    operations = load_operations(load_spec(resolved_spec))
    definitions = build_tool_definitions(
        operations,
        allow_destructive=allow_destructive,
    )
    if client is None:
        from app.config import get_api_token

        # exec_operation can reach the synchronous, non-idempotent handlers
        # (POST /v1/update, POST /v1/projects), so share the budget the CLI
        # settled on rather than reporting a completed action as a failure.
        client = RestApiClient(
            resolved_spec,
            get_api_base_url(),
            get_api_token(),
            timeout=DEFAULT_TIMEOUT,
        )
    server = MCPServer(
        "koan",
        description="Curated Kōan REST API tools",
    )
    by_name = {definition.name: definition for definition in definitions}

    def execute(name: str, *, path=None, query=None, body=None):
        definition = by_name[name]
        try:
            return client.execute_operation(
                definition.operation.operation_id,
                path=path,
                query=query,
                body=body,
            )
        except ApiClientError as exc:
            raise ToolError(str(exc)) from exc

    def register(name: str, function) -> None:
        definition: ToolDefinition = by_name[name]
        annotations = SdkToolAnnotations(
            readOnlyHint=definition.annotations.read_only,
            destructiveHint=definition.annotations.destructive,
        )
        server.tool(
            name=name,
            description=definition.operation.summary,
            annotations=annotations,
        )(function)

    if "koan_health" in by_name:
        def health() -> Any:
            return execute("koan_health")

        register("koan_health", health)

    if "koan_status" in by_name:
        def status() -> Any:
            return execute("koan_status")

        register("koan_status", status)

    if "koan_missions_list" in by_name:
        def missions_list(
            status: Literal["pending", "in_progress", "done", "failed", "removed"] | None = None,
            project: str | None = None,
        ) -> Any:
            return execute(
                "koan_missions_list",
                query=_present(status=status, project=project),
            )

        register("koan_missions_list", missions_list)

    if "koan_missions_get" in by_name:
        def missions_get(mission_id: str) -> Any:
            return execute("koan_missions_get", path={"mission_id": mission_id})

        register("koan_missions_get", missions_get)

    if "koan_missions_result" in by_name:
        def missions_result(mission_id: str) -> Any:
            return execute("koan_missions_result", path={"mission_id": mission_id})

        register("koan_missions_result", missions_result)

    if "koan_projects_list" in by_name:
        def projects_list() -> Any:
            return execute("koan_projects_list")

        register("koan_projects_list", projects_list)

    if "koan_usage" in by_name:
        def usage(
            days: int | None = None,
            offset: int | None = None,
            granularity: Literal["day", "week", "month"] | None = None,
            stacked: bool | None = None,
            project: str | None = None,
        ) -> Any:
            return execute(
                "koan_usage",
                query=_present(
                    days=days,
                    offset=offset,
                    granularity=granularity,
                    stacked=stacked,
                    project=project,
                ),
            )

        register("koan_usage", usage)

    if "koan_metrics" in by_name:
        def metrics(days: int | None = None, project: str | None = None) -> Any:
            return execute(
                "koan_metrics",
                query=_present(days=days, project=project),
            )

        register("koan_metrics", metrics)

    if "koan_logs" in by_name:
        def logs(
            source: Literal["run", "awake", "all"] | None = None,
            limit: int | None = None,
            q: str | None = None,
        ) -> Any:
            return execute(
                "koan_logs",
                query=_present(source=source, limit=limit, q=q),
            )

        register("koan_logs", logs)

    if "koan_config" in by_name:
        def config() -> Any:
            return execute("koan_config")

        register("koan_config", config)

    if "koan_missions_create" in by_name:
        def missions_create(
            command: str | None = None,
            text: str | None = None,
            project: str | None = None,
            urgent: bool = False,
        ) -> Any:
            return execute(
                "koan_missions_create",
                body=_present(
                    command=command,
                    text=text,
                    project=project,
                    urgent=urgent,
                ),
            )

        register("koan_missions_create", missions_create)

    if "koan_missions_reorder" in by_name:
        def missions_reorder(mission_id: str, target_position: int) -> Any:
            return execute(
                "koan_missions_reorder",
                body={
                    "mission_id": mission_id,
                    "target_position": target_position,
                },
            )

        register("koan_missions_reorder", missions_reorder)

    if "koan_pause" in by_name:
        def pause(duration: str | None = None) -> Any:
            return execute("koan_pause", body=_present(duration=duration))

        register("koan_pause", pause)

    if "koan_resume" in by_name:
        def resume() -> Any:
            return execute("koan_resume")

        register("koan_resume", resume)

    if "koan_missions_delete" in by_name:
        def missions_delete(mission_id: str) -> Any:
            return execute("koan_missions_delete", path={"mission_id": mission_id})

        register("koan_missions_delete", missions_delete)

    @server.tool(
        name="exec_operation",
        description=(
            "Execute any operationId from Kōan's committed OpenAPI document. "
            "Use only when no curated koan_* tool fits."
        ),
        annotations=SdkToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
        ),
    )
    def exec_operation(
        operation_id: str,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        try:
            return client.execute_operation(
                operation_id,
                path=path,
                query=query,
                body=body,
            )
        except ApiClientError as exc:
            raise ToolError(str(exc)) from exc

    return server
