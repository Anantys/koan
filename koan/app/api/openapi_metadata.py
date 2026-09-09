"""Route-adjacent metadata consumed by the OpenAPI generator."""

from collections.abc import Callable
from typing import Any

REQUEST_SCHEMA_ATTR = "_koan_openapi_request_schema"
REQUEST_REQUIRED_ATTR = "_koan_openapi_request_required"
QUERY_PARAMETERS_ATTR = "_koan_openapi_query_parameters"


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
) -> Callable:
    """Attach request-side OpenAPI metadata to a Flask view."""

    def decorate(view):
        if request_schema is not None:
            setattr(view, REQUEST_SCHEMA_ATTR, request_schema)
            setattr(view, REQUEST_REQUIRED_ATTR, request_required)
        if query_parameters:
            setattr(view, QUERY_PARAMETERS_ATTR, query_parameters)
        return view

    return decorate
