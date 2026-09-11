from copy import deepcopy
from pathlib import Path

import pytest

from app.apiclient.spec import load_operations, load_spec
from app.mcp import catalog as catalog_module
from app.mcp.catalog import (
    DENIED_NAMED_OPERATIONS,
    CuratedTool,
    ToolAnnotations,
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


def test_deny_list_pins_every_operation_that_must_never_be_a_tool():
    """Pin the deny-list membership itself.

    The intersection assertion above holds trivially while no curated entry
    names a denied operation, so on its own it would still pass if an entry were
    dropped from ``DENIED_NAMED_OPERATIONS``. Assert the set directly, so
    weakening the deny-list fails here rather than in review.
    """
    assert DENIED_NAMED_OPERATIONS == frozenset(
        {
            ("POST", "/v1/shutdown"),
            ("POST", "/v1/restart"),
            ("POST", "/v1/update"),
            ("POST", "/v1/update_release"),
            ("POST", "/v1/projects"),
            ("PATCH", "/v1/projects/{name}"),
            ("DELETE", "/v1/projects/{name}"),
        }
    )


def test_deny_list_overrides_a_curated_entry(monkeypatch, api_spec_path):
    """The deny-list is an enforced gate, not documentation.

    A future edit that both marks a denied route ``x-koan-mcp`` and adds a
    curated entry for it — the exact accident the deny-list exists to stop —
    must still produce no named tool, even with the destructive gate open.
    Marking the route is what makes the deny-list the *only* gate left, so this
    fails if that check is removed.
    """
    spec = deepcopy(load_spec(api_spec_path))
    spec["paths"]["/v1/shutdown"]["post"]["x-koan-mcp"] = True
    smuggled = CuratedTool(
        "koan_shutdown",
        "Shut Kōan down",
        "POST",
        "/v1/shutdown",
        ToolAnnotations(),
    )
    monkeypatch.setattr(
        catalog_module,
        "CURATED_TOOLS",
        (*catalog_module.CURATED_TOOLS, smuggled),
    )

    tools = build_tool_definitions(load_operations(spec), allow_destructive=True)

    assert "koan_shutdown" not in {tool.name for tool in tools}
    assert ("POST", "/v1/shutdown") not in {tool.operation_key for tool in tools}


def test_curated_tools_have_titles_and_explicit_hints(operations):
    tools = build_tool_definitions(operations, allow_destructive=True)
    by_name = {tool.name: tool for tool in tools}

    assert len(by_name) == 15
    assert all(tool.title for tool in tools)
    assert all(tool.annotations.open_world is False for tool in tools)

    for name in {
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
        "koan_resume",
        "koan_missions_delete",
    }:
        assert by_name[name].annotations.idempotent is True

    for name in {
        "koan_missions_create",
        "koan_missions_reorder",
        "koan_pause",
    }:
        assert by_name[name].annotations.idempotent is False


def test_curated_descriptions_compose_all_openapi_layers(operations):
    tools = build_tool_definitions(operations, allow_destructive=True)

    for tool in tools:
        assert tool.operation.summary in tool.description
        assert tool.operation.description in tool.description
        assert tool.operation.mcp_description in tool.description


def test_new_marked_operation_stays_hidden_without_curated_entry(operations):
    extra = deepcopy(operations[0])
    object.__setattr__(extra, "operation_id", "future_operation")
    object.__setattr__(extra, "path", "/v1/future")

    tools = build_tool_definitions([*operations, extra], allow_destructive=True)

    assert all(tool.operation.operation_id != "future_operation" for tool in tools)
