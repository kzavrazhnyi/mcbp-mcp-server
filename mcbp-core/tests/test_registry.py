"""The `mcbp_core.tools` registry — invariants that used to be held only by a comment."""
from __future__ import annotations

import sys

from mcbp_core.tools import TOOLS, ToolSpec, tool_specs


def test_search_catalog_is_first():
    # MockProvider (backend/app/llm/providers.py) takes tools[0] — registry order is sacred.
    assert list(TOOLS)[0] == "search_catalog"


def test_names_are_unique_and_match_dict_key():
    names = [spec.name for spec in TOOLS.values()]
    assert len(names) == len(set(names)), "duplicate tool names"
    for key, spec in TOOLS.items():
        assert key == spec.name


def test_every_parameters_is_a_valid_object_schema():
    for spec in TOOLS.values():
        params = spec.parameters
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)
        assert set(params["required"]) <= set(params["properties"]), spec.name


def test_tool_specs_default_excludes_write_tools():
    for spec in tool_specs():
        assert spec.read_only is True, f"{spec.name} should be hidden without include_write=True"


def test_tool_specs_include_write_exposes_write_tools():
    names = {spec.name for spec in tool_specs(include_write=True)}
    assert "write_object" in names
    assert "save_context" in names
    assert "patch_object" in names


def test_patch_object_is_write_only_and_on_both_surfaces():
    spec = TOOLS["patch_object"]
    assert spec.read_only is False
    assert {"backend", "mcp"} <= spec.surfaces
    assert "fields" in spec.parameters["properties"]
    assert set(spec.parameters["required"]) == {"metadata", "type", "id", "fields"}


def test_tool_specs_surface_filters_health_to_mcp_only():
    backend_names = {spec.name for spec in tool_specs(surface="backend", include_write=True)}
    mcp_names = {spec.name for spec in tool_specs(surface="mcp", include_write=True)}
    assert "health" not in backend_names
    assert "health" in mcp_names


def test_tool_specs_order_is_deterministic():
    first = [spec.name for spec in tool_specs()]
    second = [spec.name for spec in tool_specs()]
    assert first == second
    # Registry order, not alphabetical/arbitrary.
    assert first[0] == "search_catalog"


def test_tool_specs_allowed_filters_by_name_and_drops_unknown():
    # Behavior inherited from the original tool_defs(): order is the order of `allowed`,
    # unknown names are silently dropped (the same contract as before in the backend).
    result = tool_specs(allowed=["get_object", "search_catalog", "unknown_tool"])
    assert [spec.name for spec in result] == ["get_object", "search_catalog"]


def test_toolspec_is_frozen():
    spec = TOOLS["search_catalog"]
    assert isinstance(spec, ToolSpec)
    try:
        spec.name = "hacked"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("ToolSpec should be frozen=True")


def test_core_does_not_import_mcp_or_app():
    # Core doesn't pull in mcp.* or app.* — the module is already imported (fixture-level), this
    # just checks that it didn't happen as a side effect.
    assert "mcp" not in sys.modules
    assert not any(name == "app" or name.startswith("app.") for name in sys.modules)


def test_get_documents_description_keeps_last_document_recipe():
    # The "last document" recipe (orderby+desc+limit=1) must stay — otherwise the model has no
    # cheap way to sort without describe_metadata.
    desc = TOOLS["get_documents"].description
    assert 'orderby="Дата", desc=true, limit=1' in desc


def test_get_documents_description_explains_bad_parameter_self_correction():
    # A wrong "Дата" guess self-corrects from the 422 BAD_PARAMETER error text — the description
    # must explicitly say to take the name from there, NOT walk the full metadata tree first.
    desc = TOOLS["get_documents"].description
    assert "BAD_PARAMETER" in desc
    assert "describe_metadata" in desc
    assert 'q="дата"' in desc


def test_get_documents_description_does_not_claim_data_is_universal():
    # "Дата" is a TYPICAL name in inherited Russian-language configurations, NOT a guarantee on
    # an arbitrary base (onec-universality) — the description must explicitly flag it as typical,
    # not the only possibility.
    desc = TOOLS["get_documents"].description
    assert "типовим" in desc or "типове" in desc
    assert "не гарантією" in desc or "не гарантія" in desc
