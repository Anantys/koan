"""OpenAPI operation executor shared by non-interactive front-ends."""

from pathlib import Path
from typing import Any

import requests

from app.apiclient import ApiClientError
from app.apiclient.http import send_request
from app.apiclient.request import render_operation_request
from app.apiclient.spec import load_operations, load_spec


class RestApiClient:
    def __init__(
        self,
        spec_path: Path,
        base_url: str,
        token: str,
        *,
        session=requests,
        timeout: float = 10,
    ):
        operations = load_operations(load_spec(spec_path))
        self._operations = {operation.operation_id: operation for operation in operations}
        self.base_url = base_url
        self.token = token
        self.session = session
        self.timeout = timeout

    def execute_operation(
        self,
        operation_id: str,
        *,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        try:
            operation = self._operations[operation_id]
        except KeyError as exc:
            raise ApiClientError(f"unknown OpenAPI operation_id: {operation_id}") from exc
        plan = render_operation_request(
            operation,
            self.base_url,
            path=path,
            query=query,
            body=body,
            has_body=body is not None,
        )
        response = send_request(
            plan,
            self.token,
            self.session,
            timeout=self.timeout,
        )
        if not 200 <= response.status_code < 300:
            raise ApiClientError(
                f"REST API returned HTTP {response.status_code} for {plan.url}: "
                f"{response.payload}"
            )
        return response.payload
