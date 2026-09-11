"""Route-adjacent metadata consumed by the OpenAPI generator."""

from collections.abc import Callable
from typing import Any

REQUEST_SCHEMA_ATTR = "_koan_openapi_request_schema"
REQUEST_REQUIRED_ATTR = "_koan_openapi_request_required"
QUERY_PARAMETERS_ATTR = "_koan_openapi_query_parameters"
MCP_ENABLED_ATTR = "_koan_openapi_mcp_enabled"
PATH_PARAMETER_DESCRIPTIONS_ATTR = (
    "_koan_openapi_path_parameter_descriptions"
)
MCP_DESCRIPTION_ATTR = "_koan_openapi_mcp_description"


def query_parameter(name: str, schema: dict[str, Any], description: str) -> dict:
    """Build one optional OpenAPI query-parameter declaration."""
    return {
        "name": name,
        "in": "query",
        "required": False,
        "description": description,
        "schema": schema,
    }


def openapi_operation(
    *,
    request_schema: dict[str, Any] | None = None,
    request_required: bool = True,
    query_parameters: tuple[dict, ...] = (),
    path_parameter_descriptions: dict[str, str] | None = None,
    mcp: bool = False,
    mcp_description: str | None = None,
) -> Callable:
    """Attach request-side OpenAPI metadata to a Flask view.

    ``mcp=True`` marks a route as eligible for MCP named-tool exposure: the
    generator emits ``x-koan-mcp: true``. Missing markers stay absent (fail
    closed); the MCP server applies a fixed curation table on top.
    """
    if request_schema is None and not request_required:
        raise ValueError("request_required has no effect without request_schema")

    if mcp_description is not None:
        mcp_description = mcp_description.strip()
        if not mcp:
            raise ValueError("mcp_description requires mcp=True")
        if not mcp_description:
            raise ValueError("mcp_description cannot be empty")

    def decorate(view):
        if request_schema is not None:
            setattr(view, REQUEST_SCHEMA_ATTR, request_schema)
            setattr(view, REQUEST_REQUIRED_ATTR, request_required)
        if query_parameters:
            setattr(view, QUERY_PARAMETERS_ATTR, query_parameters)
        if path_parameter_descriptions:
            setattr(
                view,
                PATH_PARAMETER_DESCRIPTIONS_ATTR,
                dict(path_parameter_descriptions),
            )
        if mcp:
            setattr(view, MCP_ENABLED_ATTR, True)
        if mcp_description is not None:
            setattr(view, MCP_DESCRIPTION_ATTR, mcp_description)
        return view

    return decorate
