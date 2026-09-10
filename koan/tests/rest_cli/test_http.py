import io

import pytest
import requests

from app.cli import EXIT_AUTH, EXIT_LOCAL, EXIT_NOT_FOUND, EXIT_OK, EXIT_SERVER, CliError
from app.cli.commands import RequestPlan
from app.cli.config import DEFAULT_TIMEOUT, Settings
from app.cli.http import confirm_destructive, execute, verify_configuration


def request_plan(*, destructive=False):
    return RequestPlan(
        method="GET",
        url="http://127.0.0.1:8420/v1/status",
        query={},
        body=None,
        has_body=False,
        requires_auth=True,
        destructive=destructive,
    )


def test_http_exit_mapping_and_stderr_only(session_factory, response_factory):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = execute(
        request_plan(),
        Settings("default", "http://127.0.0.1:8420", "bad"),
        session_factory([response_factory(403, {"error": {"code": "forbidden"}})]),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == EXIT_AUTH
    assert stdout.getvalue() == ""
    assert '"forbidden"' in stderr.getvalue()


@pytest.mark.parametrize(
    ("status", "code"),
    [(200, EXIT_OK), (400, EXIT_LOCAL), (401, EXIT_AUTH), (404, EXIT_NOT_FOUND), (503, EXIT_SERVER)],
)
def test_status_exit_codes(status, code, session_factory, response_factory):
    stdout, stderr = io.StringIO(), io.StringIO()
    result = execute(
        request_plan(),
        Settings("default", "http://127.0.0.1:8420", "token"),
        session_factory([response_factory(status, {"status": status})]),
        stdout=stdout,
        stderr=stderr,
    )
    assert result == code
    assert bool(stdout.getvalue()) is (code == EXIT_OK)
    assert bool(stderr.getvalue()) is (code != EXIT_OK)


def test_request_sends_auth_query_and_json(session_factory, response_factory):
    plan = RequestPlan(
        method="POST",
        url="https://koan.example/v1/missions",
        query={"trace": "test"},
        body={"command": "/review"},
        has_body=True,
        requires_auth=True,
        destructive=False,
    )
    session = session_factory([response_factory(202, {"id": "1"})])
    code = execute(
        plan,
        Settings("prod", "https://koan.example", "secret"),
        session,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == EXIT_OK
    assert session.calls == [(
        "POST",
        "https://koan.example/v1/missions",
        {
            "params": {"trace": "test"},
            "headers": {"Accept": "application/json", "Authorization": "Bearer secret"},
            "timeout": DEFAULT_TIMEOUT,
            "json": {"command": "/review"},
        },
    )]


def test_missing_token_fails_without_request(session_factory):
    stdout, stderr = io.StringIO(), io.StringIO()
    session = session_factory([])
    code = execute(
        request_plan(),
        Settings("default", "http://127.0.0.1:8420", ""),
        session,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == EXIT_LOCAL
    assert session.calls == []
    assert "authentication required" in stderr.getvalue()


def test_connection_refused_has_setup_steps(session_factory):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = execute(
        request_plan(),
        Settings("default", "http://127.0.0.1:8420", "token"),
        session_factory([requests.ConnectionError("refused")]),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == EXIT_LOCAL
    assert "http://127.0.0.1:8420/v1/status" in stderr.getvalue()
    assert "api.enabled" in stderr.getvalue()
    assert "make api-token" in stderr.getvalue()
    assert "make api" in stderr.getvalue()


def test_other_request_failure_is_local(session_factory):
    stderr = io.StringIO()
    code = execute(
        request_plan(),
        Settings("default", "http://localhost", "token"),
        session_factory([requests.TooManyRedirects("looping")]),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert code == EXIT_LOCAL
    assert "request failed" in stderr.getvalue()


def test_timeout_says_the_request_may_have_been_applied(session_factory):
    stderr = io.StringIO()
    code = execute(
        request_plan(),
        Settings("default", "http://localhost", "token", 45.0),
        session_factory([requests.Timeout("slow")]),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert code == EXIT_LOCAL
    message = stderr.getvalue()
    assert "no response within 45s" in message
    assert "may still have been applied" in message
    assert "request failed" not in message


def test_settings_timeout_reaches_the_transport(session_factory, response_factory):
    session = session_factory([response_factory(200, {})])
    execute(
        request_plan(),
        Settings("default", "http://127.0.0.1:8420", "token", 7.5),
        session,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert session.calls[0][2]["timeout"] == 7.5


def test_dns_connection_error_does_not_claim_refusal(session_factory):
    stderr = io.StringIO()
    code = execute(
        request_plan(),
        Settings("default", "http://missing.example", "token"),
        session_factory([requests.ConnectionError("name resolution failed")]),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert code == EXIT_LOCAL
    assert "request failed" in stderr.getvalue()
    assert "api.enabled" not in stderr.getvalue()


def test_non_json_response_remains_json(session_factory, response_factory):
    stdout = io.StringIO()
    execute(
        request_plan(),
        Settings("default", "http://localhost", "token"),
        session_factory([response_factory(200, ValueError(), text="plain")]),
        compact=True,
        stdout=stdout,
        stderr=io.StringIO(),
    )
    assert stdout.getvalue() == '{"body": "plain"}\n'


def test_destructive_confirmation_requires_yes_without_tty():
    with pytest.raises(CliError, match="requires --yes"):
        confirm_destructive(
            request_plan(destructive=True),
            yes=False,
            stdin=io.StringIO(),
            stderr=io.StringIO(),
        )


def test_destructive_confirmation_can_abort_or_continue():
    class TtyInput(io.StringIO):
        def isatty(self):
            return True

    with pytest.raises(CliError, match="aborted"):
        confirm_destructive(
            request_plan(destructive=True),
            yes=False,
            stdin=TtyInput("no\n"),
            stderr=io.StringIO(),
        )
    confirm_destructive(
        request_plan(destructive=True),
        yes=False,
        stdin=TtyInput("yes\n"),
        stderr=io.StringIO(),
    )


def test_configure_probes_health_then_authenticated_status(session_factory, response_factory):
    session = session_factory([
        response_factory(200, {"status": "ok"}),
        response_factory(200, {"agent": {"state": "idle"}}),
    ])
    result = verify_configuration("http://127.0.0.1:8420", "token", session)
    assert result.message == "reachable; token authenticated"
    assert result.exit_code == EXIT_OK
    assert [call[1] for call in session.calls] == [
        "http://127.0.0.1:8420/v1/health",
        "http://127.0.0.1:8420/v1/status",
    ]


def test_configuration_probe_reports_rejected_token(session_factory, response_factory):
    session = session_factory([
        response_factory(200, {"status": "ok"}),
        response_factory(401, {"error": {"code": "unauthorized"}}),
    ])
    result = verify_configuration("http://localhost", "bad", session)
    assert result.message == "reachable; token was rejected"
    assert result.exit_code == EXIT_AUTH


def test_unreachable_verification_names_the_cause(session_factory):
    session = session_factory([requests.exceptions.MissingSchema("bad url")])
    result = verify_configuration("htps://koan.example", "token", session)
    assert result.exit_code == EXIT_LOCAL
    assert "htps://koan.example" in result.message
    assert "bad url" in result.message
