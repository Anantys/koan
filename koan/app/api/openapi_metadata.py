"""Route-adjacent metadata consumed by the OpenAPI generator."""

from collections.abc import Callable
from typing import Any

REQUEST_SCHEMA_ATTR = "_koan_openapi_request_schema"
REQUEST_REQUIRED_ATTR = "_koan_openapi_request_required"
QUERY_PARAMETERS_ATTR = "_koan_openapi_query_parameters"
MCP_ENABLED_ATTR = "_koan_openapi_mcp_enabled"


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
    mcp: bool = False,
) -> Callable:
    """Attach request-side OpenAPI metadata to a Flask view.

    ``mcp=True`` marks a route as eligible for MCP named-tool exposure: the
    generator emits ``x-koan-mcp: true``. Missing markers stay absent (fail
    closed); the MCP server applies a fixed curation table on top.
    """
    if request_schema is None and not request_required:
        raise ValueError("request_required has no effect without request_schema")

    def decorate(view):
        if request_schema is not None:
            setattr(view, REQUEST_SCHEMA_ATTR, request_schema)
            setattr(view, REQUEST_REQUIRED_ATTR, request_required)
        if query_parameters:
            setattr(view, QUERY_PARAMETERS_ATTR, query_parameters)
        if mcp:
            setattr(view, MCP_ENABLED_ATTR, True)
        return view

    return decorate
