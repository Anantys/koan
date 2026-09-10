from dataclasses import replace

import pytest
import yaml

from app.cli.spec import (
    SpecError,
    assert_unique,
    load_operations,
    load_server_default,
    load_spec,
    resolve_local_ref,
)


EXPECTED = {
    ("GET", "/v1/health"): ("health",),
    ("GET", "/v1/status"): ("status",),
    ("GET", "/v1/missions"): ("missions", "list"),
    ("POST", "/v1/missions"): ("missions", "create"),
    ("POST", "/v1/missions/reorder"): ("missions", "reorder"),
    ("GET", "/v1/missions/{mission_id}"): ("missions", "get"),
    ("PATCH", "/v1/missions/{mission_id}"): ("missions", "update"),
    ("DELETE", "/v1/missions/{mission_id}"): ("missions", "delete"),
    ("GET", "/v1/missions/{mission_id}/result"): ("missions", "result"),
    ("GET", "/v1/projects"): ("projects", "list"),
    ("POST", "/v1/projects"): ("projects", "create"),
    ("PATCH", "/v1/projects/{name}"): ("projects", "update"),
    ("DELETE", "/v1/projects/{name}"): ("projects", "delete"),
    ("GET", "/v1/config"): ("admin", "config"),
    ("POST", "/v1/pause"): ("admin", "pause"),
    ("POST", "/v1/resume"): ("admin", "resume"),
    ("POST", "/v1/restart"): ("admin", "restart"),
    ("POST", "/v1/shutdown"): ("admin", "shutdown"),
    ("POST", "/v1/update"): ("admin", "update"),
    ("POST", "/v1/update_release"): ("admin", "update-release"),
    ("GET", "/v1/usage"): ("observability", "usage"),
    ("GET", "/v1/metrics"): ("observability", "metrics"),
    ("GET", "/v1/logs"): ("observability", "logs"),
}


def test_current_spec_resolves_exact_command_table(api_spec_path):
    spec = load_spec(api_spec_path)
    operations = load_operations(spec)
    assert len(operations) == 23
    assert {(op.method, op.path): op.command for op in operations} == EXPECTED
    assert load_server_default(spec) == "http://127.0.0.1:8420"


def test_alias_cannot_collide_with_public_root(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    colliding = replace(operations[0], operation_id="missions")
    with pytest.raises(SpecError, match="root command"):
        assert_unique([colliding, *operations[1:]])


@pytest.mark.parametrize("reserved", ["configure", "raw"])
def test_alias_cannot_collide_with_builtin(api_spec_path, reserved):
    operations = load_operations(load_spec(api_spec_path))
    colliding = replace(operations[0], operation_id=reserved)
    with pytest.raises(SpecError, match="built-in"):
        assert_unique([colliding, *operations[1:]])


def test_public_root_cannot_use_builtin(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    colliding = replace(operations[0], command=("raw", "call"))
    with pytest.raises(SpecError, match="reserved root"):
        assert_unique([colliding, *operations[1:]])


def test_duplicate_and_prefix_commands_are_rejected(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    with pytest.raises(SpecError, match="duplicate command"):
        assert_unique([operations[0], replace(operations[1], command=operations[0].command)])
    with pytest.raises(SpecError, match="prefix collision"):
        assert_unique([
            replace(operations[0], command=("sample",)),
            replace(operations[1], command=("sample", "child")),
        ])


def test_duplicate_alias_is_rejected(api_spec_path):
    operations = load_operations(load_spec(api_spec_path))
    duplicate = replace(operations[1], operation_id=operations[0].operation_id)
    with pytest.raises(SpecError, match="duplicate operationId"):
        assert_unique([operations[0], duplicate])


def test_external_reference_is_rejected():
    with pytest.raises(SpecError, match="unsupported OpenAPI reference"):
        resolve_local_ref({}, {"$ref": "https://example.test/schema.yaml"})


@pytest.mark.parametrize("servers", [None, [], [{}], [{"url": "  "}]])
def test_invalid_server_default_is_rejected(servers):
    spec = {} if servers is None else {"servers": servers}
    with pytest.raises(SpecError, match="servers"):
        load_server_default(spec)


def test_same_path_methods_get_distinct_commands(api_spec_path):
    """A GET+POST pair on one path must not collapse onto a single command.

    The path-derived naming branches ignore the method, so without
    disambiguation ``assert_unique`` would reject the document and every
    invocation -- ``--help`` included -- would die at parse time.
    """
    spec = yaml.safe_load(api_spec_path.read_text())
    item = spec["paths"]["/v1/pause"]
    item["get"] = {
        "operationId": "admin_probe_pause",
        "tags": ["admin"],
        "summary": "Report pause state.",
        "responses": {"200": {"description": "ok"}},
    }

    commands = {op.path + op.method: op.command for op in load_operations(spec)}
    assert commands["/v1/pauseGET"] == ("admin", "pause-get")
    assert commands["/v1/pausePOST"] == ("admin", "pause-post")


def test_disambiguation_leaves_unique_commands_untouched(api_spec_path):
    spec = yaml.safe_load(api_spec_path.read_text())
    commands = {op.operation_id: op.command for op in load_operations(spec)}
    assert commands["missions_list_missions_route_get"] == ("missions", "list")
    assert commands["missions_create_mission_post"] == ("missions", "create")
    assert all("-get" not in leaf for command in commands.values() for leaf in command)
