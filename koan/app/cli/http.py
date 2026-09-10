"""HTTP transport, confirmation, output, and configuration probes."""

import errno
import json
import sys
from dataclasses import dataclass
from typing import TextIO

import requests

from app.cli import (
    EXIT_AUTH,
    EXIT_LOCAL,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_SERVER,
    CliError,
)
from app.cli.commands import RequestPlan
from app.cli.config import DEFAULT_TIMEOUT, Settings


@dataclass(frozen=True)
class VerificationResult:
    message: str
    exit_code: int


def exit_for_status(status: int) -> int:
    if 200 <= status < 300:
        return EXIT_OK
    if status in {401, 403}:
        return EXIT_AUTH
    if status == 404:
        return EXIT_NOT_FOUND
    if status >= 500:
        return EXIT_SERVER
    return EXIT_LOCAL


def confirm_destructive(
    plan: RequestPlan,
    *,
    yes: bool,
    stdin: TextIO = sys.stdin,
    stderr: TextIO = sys.stderr,
) -> None:
    if not plan.destructive or yes:
        return
    if not getattr(stdin, "isatty", lambda: False)():
        raise CliError("destructive request requires --yes when stdin is not a TTY")
    print(f"Confirm {plan.method} {plan.url}? [y/N] ", end="", file=stderr)
    if stdin.readline().strip().lower() not in {"y", "yes"}:
        raise CliError("aborted")


def _json_payload(response):
    try:
        return response.json()
    except ValueError:
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


def execute(
    plan: RequestPlan,
    settings: Settings,
    session=requests,
    *,
    compact: bool = False,
    pretty: bool = False,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    if plan.requires_auth and not settings.token:
        print(
            "authentication required; run `koan-cli configure` or set "
            "KOAN_API_TOKEN",
            file=stderr,
        )
        return EXIT_LOCAL

    headers = {"Accept": "application/json"}
    if settings.token:
        headers["Authorization"] = f"Bearer {settings.token}"
    kwargs = {"params": plan.query, "headers": headers, "timeout": settings.timeout}
    if plan.has_body:
        kwargs["json"] = plan.body

    try:
        response = session.request(plan.method, plan.url, **kwargs)
    except requests.ConnectionError as exc:
        if _is_connection_refused(exc):
            print(f"connection refused: {plan.url}", file=stderr)
            print("1. Set api.enabled: true in instance/config.yaml", file=stderr)
            print("2. Run make api-token and configure the token", file=stderr)
            print("3. Start the server with make api", file=stderr)
        else:
            print(f"request failed: {exc}", file=stderr)
        return EXIT_LOCAL
    except requests.Timeout as exc:
        # The request was delivered; only the response is missing. Say so, or a
        # script keying off the exit code re-issues a non-idempotent operation.
        print(
            f"no response within {settings.timeout:g}s: {exc}",
            file=stderr,
        )
        print(
            f"{plan.method} {plan.url} may still have been applied; "
            "verify before retrying, or raise --timeout.",
            file=stderr,
        )
        return EXIT_LOCAL
    except requests.RequestException as exc:
        print(f"request failed: {exc}", file=stderr)
        return EXIT_LOCAL

    payload = _json_payload(response)
    code = exit_for_status(response.status_code)
    target = stdout if code == EXIT_OK else stderr
    indent = (
        2
        if pretty
        or (not compact and getattr(stdout, "isatty", lambda: False)())
        else None
    )
    print(json.dumps(payload, ensure_ascii=False, indent=indent), file=target)
    return code


def verify_configuration(
    base_url: str,
    token: str,
    session=requests,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> VerificationResult:
    try:
        health = session.request(
            "GET",
            f"{base_url.rstrip('/')}/v1/health",
            timeout=timeout,
        )
    except requests.RequestException as exc:
        # A malformed URL and a TLS failure are RequestExceptions too; reporting
        # every one as "unreachable" sends the operator to start a server that
        # was never the problem.
        return VerificationResult(f"saved; cannot reach {base_url}: {exc}", EXIT_LOCAL)

    if not health.ok:
        return VerificationResult(
            f"saved; health probe returned HTTP {health.status_code}",
            exit_for_status(health.status_code),
        )
    if not token:
        return VerificationResult("reachable; no token configured", EXIT_OK)

    try:
        status = session.request(
            "GET",
            f"{base_url.rstrip('/')}/v1/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return VerificationResult(
            f"reachable; authentication probe failed: {exc}",
            EXIT_LOCAL,
        )

    if status.status_code in {401, 403}:
        return VerificationResult("reachable; token was rejected", EXIT_AUTH)
    if status.ok:
        return VerificationResult("reachable; token authenticated", EXIT_OK)
    return VerificationResult(
        f"reachable; authentication probe returned HTTP {status.status_code}",
        exit_for_status(status.status_code),
    )
