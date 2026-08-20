"""English `description_en` field on `ToolSpec` — added for the MCP surface (Phase 1 of
`docs/MCP_ROADMAP.md`) without touching the Ukrainian `description` used by the backend
(prompt-cached in `backend/app/llm/providers.py`)."""
from __future__ import annotations

import hashlib

from mcbp_core.tools import TOOLS, tool_specs

# Snapshot of the Ukrainian `description` values as of the commit that introduced
# `description_en` — guards against an accidental reword invalidating the Anthropic prompt cache.
_UA_DESCRIPTION_SHA256 = {
    "search_catalog": "5e8e37365a2fd52f7d84d9e78168d2b9cf3c90a60401c6436f72c8fd82302ede",
    "get_documents": "c5d67095d419fca314c4a938144a01b2663d3a5d9880dfe3ed39f01aa8a43950",
    "get_schema": "d3be181efe3a5b719deefee74d22116ebfbba11700ecd5e2aa7792b4a258faa7",
    "get_register_balance": "d41a2b2a71945bb4c9634af252bee01dd36c20304b351043cca381a26460691e",
    "list_metadata": "c2e653bb50f82be685233312443eb1ececb3d9212651e07ae6abcde91c7d01e6",
    "describe_metadata": "2fc553a566d0891a078fa88e6c31c390f3845c2e3d2f965c548b5d8596aee5aa",
    "filter_catalog": "00d49eda5480210ad18d0011064d90572fb55f318faf16bbc3ff04df6ac7b8d8",
    "get_object": "f00ce22527682f57f50f8a6ac352b92d6350c126008e9165541ce4fcee3390fe",
    "health": "2b472b33a1af9c2bbfbe89bbb2434dd9a85ad05c91208a7e24d856e8edc9f365",
    "write_object": "a321af95fec111f84201bd925f03b71c4b4f719d6a91a5f9d2274da091095a0f",
    "save_context": "3a87ceeb0b5daa3077c67d66ee5f3be38bb4241df2ec06c7c70bc00712d970ff",
    "patch_object": "b7a55be8ce5c1e11044144f0a2de4b7e2b693f8670bb2289a21655725d65e236",
    "get_register_records": "f387cbc16f077b5656fb06ca919cb4a4a5de3e4cbdefcbaf0aaa736d2c8b0795",
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
