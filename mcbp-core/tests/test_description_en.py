"""English `description_en` field on `ToolSpec` — added for the MCP surface (Phase 1 of
`docs/MCP_ROADMAP.md`) without touching the backend `description` (Ukrainian or English,
prompt-cached in `backend/app/llm/providers.py`)."""
from __future__ import annotations

import hashlib

from mcbp_core.tools import TOOLS, tool_specs

# Snapshot of the backend `description` values: the snapshot guards the prompt cache
# against an accidental reword.
_UA_DESCRIPTION_SHA256 = {
    "search_catalog": "f5b1ca73880a36473b640da1de946a941688dd9ff4eb129e0acfab133dec3c3e",
    "get_documents": "ad23f3413ea2c915e4d807d2ff2caa70e5183c674b26644ba007a77b1c152045",
    "get_schema": "d3be181efe3a5b719deefee74d22116ebfbba11700ecd5e2aa7792b4a258faa7",
    "get_register_balance": "f462931253c71c3526cf4f607d7ad768e18726bd51f4d8482e0983c2a4e147fe",
    "list_metadata": "935700ebfeaa3aa4bdffffb13bf0baf4f8d5788ec75ffb4468a69811c8aa433d",
    "describe_metadata": "b6f8a9b8458d7dc1be7b1c039960816a8482fd9499fd04e29dcbcbf3e338a920",
    "filter_catalog": "5080b83d6548bc43df1c1aa93763131236201e05c91884767e4d341091375558",
    "get_object": "6a7ac39616e2ea5e4a70732efecb444604eeea96dcf649e311bf5c53f0766876",
    "health": "2b472b33a1af9c2bbfbe89bbb2434dd9a85ad05c91208a7e24d856e8edc9f365",
    "write_object": "da62de7170e149ca4fc30e51113c8363cec71f68fef461b4e1913908f6308044",
    "save_context": "3a87ceeb0b5daa3077c67d66ee5f3be38bb4241df2ec06c7c70bc00712d970ff",
    "patch_object": "0faa4ee8afdbca15ad88c24d547891198ffd7d14c0e13fa64b1039c464b3f902",
    "get_register_records": "1bc3c869dd1d265c0cb3149a297ce1cda1fe3c01d93b1ca9a3b2115760f50b56",
}


def test_ukrainian_descriptions_unchanged():
    for name, expected_sha in _UA_DESCRIPTION_SHA256.items():
        actual_sha = hashlib.sha256(TOOLS[name].description.encode("utf-8")).hexdigest()
        assert actual_sha == expected_sha, f"{name}: description text changed — this invalidates the Anthropic prompt cache"


def test_every_mcp_surface_tool_has_description_en():
    for spec in tool_specs(surface="mcp", include_write=True):
        assert spec.description_en, f"{spec.name} is missing description_en"


def test_description_en_defaults_to_none():
    # Sanity check on the dataclass default — a spec built without description_en still works.
    from mcbp_core.tools import ToolSpec

    spec = ToolSpec("x", "опис", {"type": "object", "properties": {}, "required": []}, TOOLS["health"].executor)
    assert spec.description_en is None


def test_count_rule_in_descriptions():
    for text in (TOOLS["filter_catalog"].description, TOOLS["filter_catalog"].description_en):
        assert "agg" in text and "exclude_groups" in text and "count" in text
    for text in (TOOLS["search_catalog"].description, TOOLS["search_catalog"].description_en):
        assert "filter_catalog" in text and "agg" in text and "exclude_groups" in text


def test_tool_counts_unchanged():
    # 0.1.41: +3 additional-attribute tools (1 read, 2 write) on both surfaces.
    assert len(TOOLS) == 33
    assert len(tool_specs(surface="mcp")) == 18
    assert len(tool_specs(surface="mcp", include_write=True)) == 33
    assert len(tool_specs(surface="backend")) == 12
    assert len(tool_specs(surface="backend", include_write=True)) == 17


def test_aggregation_rule_in_register_records_descriptions():
    for text in (TOOLS["get_register_records"].description,
                 TOOLS["get_register_records"].description_en):
        assert "agg" in text and "groupby" in text and "sum:*" in text and "Period:month" in text
    for text in (TOOLS["get_object"].description, TOOLS["get_object"].description_en):
        assert "tabular_sections" in text and "rows_limit" in text
        assert "tabular_section_counts" in text and "offset" in text


def test_confirm_before_change_on_every_write_tool_except_save_context():
    en = ("Changes data in the base: first tell the user exactly what will change "
          "(object, field, old → new value) and call this only after their explicit "
          "confirmation of that change.")
    uk = ("Змінює дані в базі: спершу скажи користувачу, що саме зміниться (об'єкт, поле, "
          "старе → нове значення), і викликай лише після його явного підтвердження цієї зміни.")
    for spec in TOOLS.values():
        expected = not spec.read_only and spec.name != "save_context"
        assert (spec.description_en or "").endswith(en) is expected, spec.name
        assert spec.description.endswith(uk) is expected, spec.name
        assert (en in (spec.description_en or "")) is expected, spec.name
