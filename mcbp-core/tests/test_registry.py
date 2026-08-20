"""Реєстр `mcbp_core.tools` — інваріанти, що раніше трималися лише на коментарі."""
from __future__ import annotations

import sys

from mcbp_core.tools import TOOLS, ToolSpec, tool_specs


def test_search_catalog_is_first():
    # MockProvider (backend/app/llm/providers.py) бере tools[0] — порядок реєстру священний.
    assert list(TOOLS)[0] == "search_catalog"


def test_names_are_unique_and_match_dict_key():
    names = [spec.name for spec in TOOLS.values()]
    assert len(names) == len(set(names)), "дублікати імен інструментів"
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
        assert spec.read_only is True, f"{spec.name} мав би бути прихований без include_write=True"


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
    # Порядок реєстру, не алфавітний/довільний.
    assert first[0] == "search_catalog"


def test_tool_specs_allowed_filters_by_name_and_drops_unknown():
    # Поведінка успадкована з оригінального tool_defs(): порядок — порядок `allowed`,
    # невідомі імена мовчки відкидаються (той самий контракт, що й раніше в backend).
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
        raise AssertionError("ToolSpec має бути frozen=True")


def test_core_does_not_import_mcp_or_app():
    # Ядро не тягне ні mcp.*, ні app.* — модуль уже імпортований (fixture-level), лише
    # перевіряємо, що це не сталося побічно.
    assert "mcp" not in sys.modules
    assert not any(name == "app" or name.startswith("app.") for name in sys.modules)


def test_get_documents_description_keeps_last_document_recipe():
    # Рецепт «останній документ» (orderby+desc+limit=1) має лишатись — інакше модель
    # не має дешевого способу відсортувати без describe_metadata.
    desc = TOOLS["get_documents"].description
    assert 'orderby="Дата", desc=true, limit=1' in desc


def test_get_documents_description_explains_bad_parameter_self_correction():
    # Хибний здогад "Дата" самокоригується за текстом помилки 422 BAD_PARAMETER — опис має
    # прямо казати брати ім'я звідти, а НЕ йти спершу за повним деревом метаданих.
    desc = TOOLS["get_documents"].description
    assert "BAD_PARAMETER" in desc
    assert "describe_metadata" in desc
    assert 'q="дата"' in desc


def test_get_documents_description_does_not_claim_data_is_universal():
    # "Дата" — типове ім'я в успадкованих російськомовних конфігураціях, НЕ гарантія на
    # будь-якій базі (onec-universality) — опис має явно позначити це як типове, не єдине.
    desc = TOOLS["get_documents"].description
    assert "типовим" in desc or "типове" in desc
    assert "не гарантією" in desc or "не гарантія" in desc
