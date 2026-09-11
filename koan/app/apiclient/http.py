"""Synchronous HTTP transport shared by CLI and MCP front-ends."""

import errno
import logging
from dataclasses import dataclass
from typing import Any

import requests

from app.apiclient.errors import ApiClientError
from app.apiclient.request import RequestPlan

log = logging.getLogger("koan.apiclient.http")


@dataclass(frozen=True)
class ClientResponse:
    status_code: int
    payload: Any


def _json_payload(response):
    try:
        return response.json()
    except ValueError:
        request = getattr(response, "request", None)
        method = getattr(request, "method", "?")
        url = getattr(request, "url", "?")
        # A 2xx body that is not JSON hides a protocol failure (e.g. an HTML
        # proxy page served with status 200). Raise rather than mask it. Non-2xx
        # bodies keep the raw text so error diagnostics (redirect pages, gateway
        # messages) stay available to the caller.
        if 200 <= response.status_code < 300:
            log.warning(
                "REST API returned non-JSON body for %s %s (HTTP %s); "
                "raising instead of surfacing a masked body",
                method,
                url,
                response.status_code,
            )
            raise ApiClientError(
                f"REST API returned a non-JSON body (HTTP {response.status_code}); "
                f"expected application/json"
            )
        log.warning(
            "REST API returned non-JSON body for %s %s (HTTP %s); "
            "surfacing as raw body",
            method,
            url,
            response.status_code,
        )
        return {"body": response.text}


def _is_connection_refused(exc: BaseException) -> bool:
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno == errno.ECONNREFUSED:
            return True
        if "refused" in str(current).lower():
            return True
        pending.extend(
            item
            for item in getattr(current, "args", ())
            if isinstance(item, BaseException)
        )
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
    return False


def send_request(
    plan: RequestPlan,
    token: str,
    session=requests,
    *,
    timeout: float = 10,
) -> ClientResponse:
    if plan.requires_auth and not token:
        raise ApiClientError(
            "authentication required; run koan-cli configure or make api-token, "
            "or set KOAN_API_TOKEN"
        )
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs = {"params": plan.query, "headers": headers, "timeout": timeout}
    if plan.has_body:
        kwargs["json"] = plan.body
    try:
        response = session.request(plan.method, plan.url, **kwargs)
    except requests.ConnectionError as exc:
        if _is_connection_refused(exc):
            raise ApiClientError.connection(plan.url, "connection refused") from exc
        raise ApiClientError(f"request failed: {exc}") from exc
    except requests.Timeout as exc:
        # The request was delivered; only the response is missing. Say so, or a
        # caller keying off the failure re-issues a non-idempotent operation.
        raise ApiClientError(
            f"no response within {timeout:g}s: {exc}. "
            f"{plan.method} {plan.url} may still have been applied; "
            "verify before retrying, or raise the request timeout."
        ) from exc
    except requests.RequestException as exc:
        raise ApiClientError(f"request failed: {exc}") from exc
    return ClientResponse(response.status_code, _json_payload(response))
