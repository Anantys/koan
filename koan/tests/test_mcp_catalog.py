from copy import deepcopy
from pathlib import Path

import pytest

from app.apiclient.spec import load_operations, load_spec
from app.mcp.catalog import (
    DENIED_NAMED_OPERATIONS,
    build_tool_definitions,
)


EXPECTED_DEFAULT_TOOLS = {
    "koan_health",
    "koan_status",
    "koan_missions_list",
    "koan_missions_get",
    "koan_missions_result",
    "koan_projects_list",
    "koan_usage",
    "koan_metrics",
    "koan_logs",
    "koan_config",
    "koan_missions_create",
    "koan_missions_reorder",
    "koan_pause",
    "koan_resume",
}


@pytest.fixture
def api_spec_path():
    return Path(__file__).resolve().parents[1] / "openapi.yaml"


@pytest.fixture
def operations(api_spec_path):
    return load_operations(load_spec(api_spec_path))


def test_curated_tools_and_annotations(operations):
    tools = build_tool_definitions(operations, allow_destructive=False)
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == EXPECTED_DEFAULT_TOOLS
    assert all(tool.annotations.read_only for tool in tools[:10])
    assert all(not tool.annotations.destructive for tool in tools)
    assert all(not by_name[name].annotations.read_only for name in {
        "koan_missions_create",
        "koan_missions_reorder",
        "koan_pause",
        "koan_resume",
    })


def test_destructive_tool_requires_separate_gate(operations):
    hidden = build_tool_definitions(operations, allow_destructive=False)
    visible = build_tool_definitions(operations, allow_destructive=True)

    assert "koan_missions_delete" not in {tool.name for tool in hidden}
    delete = next(tool for tool in visible if tool.name == "koan_missions_delete")
    assert delete.annotations.destructive is True
    assert delete.annotations.read_only is False


def test_named_tools_fail_closed_without_openapi_marker(api_spec_path):
    spec = deepcopy(load_spec(api_spec_path))
    del spec["paths"]["/v1/status"]["get"]["x-koan-mcp"]
    tools = build_tool_definitions(load_operations(spec), allow_destructive=True)

    assert "koan_status" not in {tool.name for tool in tools}


def test_deny_list_never_receives_named_tools(operations):
    tools = build_tool_definitions(operations, allow_destructive=True)

    assert not ({tool.operation_key for tool in tools} & DENIED_NAMED_OPERATIONS)


def test_write_schemas_describe_model_inputs(operations):
    tools = {
        tool.name: tool
        for tool in build_tool_definitions(operations, allow_destructive=True)
    }

    create = tools["koan_missions_create"].input_schema
    assert set(create["properties"]) == {"command", "text", "project", "urgent"}
    assert {tuple(branch["required"]) for branch in create["anyOf"]} == {
        ("command",),
        ("text",),
    }
    assert set(tools["koan_missions_reorder"].input_schema["required"]) == {
        "mission_id",
        "target_position",
    }
    assert tools["koan_pause"].input_schema["properties"]["duration"]["type"] == "string"


def test_new_marked_operation_stays_hidden_without_curated_entry(operations):
    extra = deepcopy(operations[0])
    object.__setattr__(extra, "operation_id", "future_operation")
    object.__setattr__(extra, "path", "/v1/future")

    tools = build_tool_definitions([*operations, extra], allow_destructive=True)

    assert all(tool.operation.operation_id != "future_operation" for tool in tools)
