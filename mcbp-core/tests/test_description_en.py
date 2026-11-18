"""English `description_en` field on `ToolSpec` — added for the MCP surface (Phase 1 of
`docs/MCP_ROADMAP.md`) without touching the Ukrainian `description` used by the backend
(prompt-cached in `backend/app/llm/providers.py`)."""
from __future__ import annotations

import hashlib

from mcbp_core.tools import TOOLS, tool_specs

# Snapshot of the Ukrainian `description` values as of the commit that introduced
# `description_en` (search_catalog/filter_catalog hashes updated in 0.1.9 for the count rule; patch_object for tabular sections, create mode and DeletionMark; write_object for the nested reference form; get_object/get_register_records in 0.1.30 for selectors and register aggregation; get_register_balance/get_register_records/list_metadata/describe_metadata in 0.1.31 for accounting registers; get_register_balance again for the drcr_turnovers ExtDimensionDr/CrN keys; get_documents/get_object for following references by Data/Metadata; in 0.1.37 the six busiest backend tools got one short English text shared with description_en) — guards against an accidental reword invalidating the Anthropic prompt cache.
_UA_DESCRIPTION_SHA256 = {
    "search_catalog": "f5b1ca73880a36473b640da1de946a941688dd9ff4eb129e0acfab133dec3c3e",
    "get_documents": "eb42afff5a726d593ee72c3aa134d7df50c5c2d2ea1b75db203dcfda9ae1141e",
    "get_schema": "d3be181efe3a5b719deefee74d22116ebfbba11700ecd5e2aa7792b4a258faa7",
    "get_register_balance": "f462931253c71c3526cf4f607d7ad768e18726bd51f4d8482e0983c2a4e147fe",
    "list_metadata": "935700ebfeaa3aa4bdffffb13bf0baf4f8d5788ec75ffb4468a69811c8aa433d",
    "describe_metadata": "b6f8a9b8458d7dc1be7b1c039960816a8482fd9499fd04e29dcbcbf3e338a920",
    "filter_catalog": "5080b83d6548bc43df1c1aa93763131236201e05c91884767e4d341091375558",
    "get_object": "e9b09466eea837b0ee40e9503688ef4515e49be27644e205e52db9b6e68f2be0",
    "health": "2b472b33a1af9c2bbfbe89bbb2434dd9a85ad05c91208a7e24d856e8edc9f365",
    "write_object": "af53d4d8b7f051cb853d3e311cfbdb8a62e4418b2111a533ff51cba665c78ff4",
    "save_context": "3a87ceeb0b5daa3077c67d66ee5f3be38bb4241df2ec06c7c70bc00712d970ff",
    "patch_object": "d6b341cfb0fc2ab4f65aa37e26fb063279122ab2a932cda410583a8ad6d8e46a",
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
    assert len(TOOLS) == 30
    assert len(tool_specs(surface="mcp")) == 17
    assert len(tool_specs(surface="mcp", include_write=True)) == 30


def test_aggregation_rule_in_register_records_descriptions():
    for text in (TOOLS["get_register_records"].description,
                 TOOLS["get_register_records"].description_en):
        assert "agg" in text and "groupby" in text and "sum:*" in text and "Period:month" in text
    for text in (TOOLS["get_object"].description, TOOLS["get_object"].description_en):
        assert "tabular_sections" in text and "rows_limit" in text
        assert "tabular_section_counts" in text and "offset" in text
