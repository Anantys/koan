"""Transport-neutral OpenAPI request planning shared by CLI and MCP front-ends."""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.apiclient.errors import ApiClientError
from app.apiclient.spec import Operation


@dataclass(frozen=True)
class RequestPlan:
    method: str
    url: str
    query: dict[str, Any]
    body: Any
    has_body: bool
    requires_auth: bool
    destructive: bool


DESTRUCTIVE = {
    ("POST", "/v1/restart"),
    ("POST", "/v1/shutdown"),
    ("POST", "/v1/update"),
    ("POST", "/v1/update_release"),
}


def is_destructive(method: str, path: str) -> bool:
    normalized = (method.upper(), path.split("?", 1)[0])
    return normalized[0] == "DELETE" or normalized in DESTRUCTIVE


def render_operation_request(
    operation: Operation,
    base_url: str,
    *,
    path: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    has_body: bool = False,
) -> RequestPlan:
    path_values = path or {}
    rendered = operation.path
    for parameter in operation.parameters:
        if parameter.location != "path":
            continue
        if parameter.required and parameter.name not in path_values:
            raise ApiClientError(f"missing required path parameter: {parameter.name}")
        if parameter.name in path_values:
            encoded = quote(str(path_values[parameter.name]), safe="")
            rendered = rendered.replace(f"{{{parameter.name}}}", encoded)
    if re.search(r"{[^{}]+}", rendered):
        raise ApiClientError(f"unresolved path parameter in {rendered}")
    query_values = dict(query or {})
    for parameter in operation.parameters:
        if (
            parameter.location == "query"
            and parameter.required
            and parameter.name not in query_values
        ):
            raise ApiClientError(f"missing required query parameter: {parameter.name}")
    return RequestPlan(
        method=operation.method,
        url=f"{base_url.rstrip('/')}/{rendered.lstrip('/')}",
        query=query_values,
        body=body,
        has_body=has_body,
        requires_auth=operation.requires_auth,
        destructive=is_destructive(operation.method, operation.path),
    )
