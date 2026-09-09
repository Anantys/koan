import argparse
import json

import pytest

from app.cli import CliError
from app.cli.commands import (
    build_operation_request,
    build_parser,
    build_raw_request,
    expand_alias,
    is_destructive,
)
from app.cli.spec import load_operations, load_spec


def _sample_value(schema):
    return {
        "boolean": True,
        "integer": 1,
        "number": 1.0,
        "object": {},
    }.get(schema.get("type"), "sample-value")


def test_data_is_available_on_get(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    parser = build_parser(operations)
    args = parser.parse_args(["health", "--data", '{"probe":true}'])
    request = build_operation_request(args._operation, args, "http://localhost:8420")
    assert request.has_body is True
    assert request.body == {"probe": True}


def test_enriched_required_query_and_body_flags(enriched_operations):
    parser = build_parser(enriched_operations)

    logs = parser.parse_args(["observability", "logs", "--cursor", "next"])
    logs_request = build_operation_request(
        logs._operation, logs, "http://localhost:8420"
    )
    assert logs_request.query == {"cursor": "next"}

    create = parser.parse_args(
        ["missions", "create", "--command", "/review", "--urgent", "true"]
    )
    create_request = build_operation_request(
        create._operation, create, "http://localhost:8420"
    )
    assert create_request.body == {"command": "/review", "urgent": True}


def test_required_values_can_come_from_generic_flags(enriched_operations):
    parser = build_parser(enriched_operations)
    logs = parser.parse_args(["observability", "logs", "-q", "cursor=generic"])
    assert build_operation_request(
        logs._operation, logs, "http://localhost"
    ).query == {"cursor": "generic"}

    create = parser.parse_args(
        ["missions", "create", "--data", '{"command":"/review"}']
    )
    assert build_operation_request(
        create._operation, create, "http://localhost"
    ).body == {"command": "/review"}


def test_hidden_alias_expands_to_public_command(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    argv = expand_alias(
        ["missions_list_missions_route_get", "-q", "status=pending"],
        operations,
    )
    assert argv == ["missions", "list", "-q", "status=pending"]


def test_path_values_are_percent_encoded(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    parser = build_parser(operations)
    args = parser.parse_args(["missions", "get", "value/with spaces"])
    request = build_operation_request(args._operation, args, "http://localhost/")
    assert request.url == "http://localhost/v1/missions/value%2Fwith%20spaces"


def test_raw_request_is_fully_constructed():
    request = build_raw_request(
        "POST",
        "/v1/missions",
        "https://koan.example.com",
        data='{"command":"/review"}',
        query=["urgent=true"],
    )
    assert request.method == "POST"
    assert request.url == "https://koan.example.com/v1/missions"
    assert request.body == {"command": "/review"}
    assert request.query == {"urgent": "true"}
    assert request.requires_auth is True


def test_raw_health_auth_exception_is_exact():
    public = build_raw_request(
        "get", "/v1/health", "http://localhost", data=None, query=[]
    )
    queried = build_raw_request(
        "GET", "/v1/health?verbose=1", "http://localhost", data=None, query=[]
    )
    post = build_raw_request(
        "POST", "/v1/health", "http://localhost", data=None, query=[]
    )
    assert public.requires_auth is False
    assert queried.requires_auth is False
    assert post.requires_auth is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/v1/missions/1"),
        ("POST", "/v1/restart"),
        ("POST", "/v1/shutdown"),
        ("POST", "/v1/update"),
        ("POST", "/v1/update_release?now=true"),
    ],
)
def test_destructive_requests(method, path):
    assert is_destructive(method, path)


@pytest.mark.parametrize(
    ("data", "query", "message"),
    [
        ("not-json", [], "invalid JSON"),
        (None, ["missing"], "KEY=VALUE"),
        (None, ["=value"], "key cannot be empty"),
    ],
)
def test_invalid_generic_input_fails_locally(data, query, message):
    with pytest.raises(CliError, match=message):
        build_raw_request("GET", "/v1/status", "http://localhost", data=data, query=query)


def test_typed_body_flags_cannot_merge_into_array(enriched_operations):
    operation = next(op for op in enriched_operations if op.command == ("missions", "create"))
    args = argparse.Namespace(
        data="[]",
        query=[],
        _body_command="/review",
        _body_urgent=None,
    )
    with pytest.raises(CliError, match="non-object JSON"):
        build_operation_request(operation, args, "http://localhost")


def test_missing_enriched_required_values_fail(enriched_operations):
    parser = build_parser(enriched_operations)
    logs = parser.parse_args(["observability", "logs"])
    with pytest.raises(CliError, match="required query parameter: cursor"):
        build_operation_request(logs._operation, logs, "http://localhost")

    create = parser.parse_args(["missions", "create"])
    with pytest.raises(CliError, match="requires a JSON request body"):
        build_operation_request(create._operation, create, "http://localhost")


def test_parser_usage_errors_exit_one(api_spec_path):
    parser = build_parser(load_operations(load_spec(api_spec_path)))
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["unknown"])
    assert exc.value.code == 1


def test_every_spec_operation_is_parser_reachable(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    parser = build_parser(operations)

    for operation in operations:
        argv = list(operation.command)
        for parameter in operation.parameters:
            if parameter.location == "path":
                argv.append("sample-value")
            elif parameter.location == "query" and parameter.required:
                argv.extend(
                    [f"--{parameter.name.replace('_', '-')}", "sample-value"]
                )

        namespace = parser.parse_args(argv)
        assert namespace._operation == operation


def test_every_required_parameter_is_expressible(enriched_operations):
    parser = build_parser(enriched_operations)

    for operation in enriched_operations:
        path_names = {
            parameter.name
            for parameter in operation.parameters
            if parameter.location == "path" and parameter.required
        }
        query_names = {
            parameter.name
            for parameter in operation.parameters
            if parameter.location == "query" and parameter.required
        }
        body_names = set((operation.body_schema or {}).get("required", []))

        argv = list(operation.command)
        argv.extend("path-value" for _ in path_names)
        for name in query_names:
            parameter = next(
                item for item in operation.parameters if item.name == name
            )
            argv.extend(
                [f"--{name.replace('_', '-')}", str(_sample_value(parameter.schema))]
            )
        for name in body_names:
            schema = operation.body_schema["properties"][name]
            argv.extend([f"--{name.replace('_', '-')}", str(_sample_value(schema))])

        namespace = parser.parse_args(argv)
        request = build_operation_request(
            operation, namespace, "http://127.0.0.1:8420"
        )
        assert all(f"{{{name}}}" not in request.url for name in path_names)
        assert query_names <= set(request.query)
        assert body_names <= set(request.body)


def test_every_operation_accepts_generic_data_and_query(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    parser = build_parser(operations)

    for operation in operations:
        argv = list(operation.command)
        argv.extend(
            "path-value"
            for parameter in operation.parameters
            if parameter.location == "path"
        )
        body = {"generic": True}
        schema = operation.body_schema or {}
        for name in schema.get("required", []):
            body[name] = _sample_value(schema["properties"][name])
        argv.extend(["--data", json.dumps(body), "-q", "trace=test"])
        namespace = parser.parse_args(argv)
        request = build_operation_request(
            operation, namespace, "http://127.0.0.1:8420"
        )
        assert request.body == body
        assert request.query["trace"] == "test"
