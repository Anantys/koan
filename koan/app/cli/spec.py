"""Compatibility exports for shared OpenAPI discovery."""

from app.apiclient.spec import (
    FIRST_LINE_FALLBACK,
    HTTP_METHODS,
    ITEM_VERBS,
    RESERVED_ROOTS,
    Operation,
    Parameter,
    SpecError,
    assert_unique,
    command_name,
    disambiguate_by_method,
    load_operations,
    load_server_default,
    load_spec,
    load_tag_descriptions,
    resolve_local_ref,
)


__all__ = [
    "FIRST_LINE_FALLBACK",
    "HTTP_METHODS",
    "ITEM_VERBS",
    "RESERVED_ROOTS",
    "Operation",
    "Parameter",
    "SpecError",
    "assert_unique",
    "command_name",
    "disambiguate_by_method",
    "load_operations",
    "load_server_default",
    "load_spec",
    "load_tag_descriptions",
    "resolve_local_ref",
]
