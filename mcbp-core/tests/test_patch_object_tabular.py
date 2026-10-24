"""`patch_object` over the extended PATCH /object route: tabular sections, create mode (`id="new"`)
and the no-retry rule for the non-idempotent create/append calls."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import UpstreamError

_BASE = "http://test/ai/v1/object/Catalogs/MCBP_DataConversionSettings"
_UUID = "e3f1d9b6-0001-11ef-a850-cc52afc9fc6f"
_ROWS = [
    {"AttributeName": "Проведен", "DataName": "Posted", "DataSource": "ds-uuid"},
    {"AttributeName": "Номер", "DataName": "Number"},
]


@pytest.fixture
async def client(monkeypatch):
    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("mcbp_core.client.asyncio.sleep", _fake_sleep)
    c = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await c.startup()
    yield c
    await c.shutdown()


def _ok(status: int = 200, **extra) -> httpx.Response:
    body = {"metadata": "catalog", "type": "MCBP_DataConversionSettings", "id": _UUID,
            "created": status == 201, "changed": [], "posted": False}
    body.update(extra)
    return httpx.Response(status, json=body)


@respx.mock
async def test_create_returns_201_payload(client):
    route = respx.patch(f"{_BASE}/new").mock(return_value=_ok(
        201, changed=[{"field": "Attributes", "old": None, "new": 2}],
    ))
    result = await client.patch_object(
        "Catalogs", "MCBP_DataConversionSettings", "new",
        {"Description": "Orders", "Attributes": _ROWS},
    )
    assert route.call_count == 1
    assert route.calls[0].request.method == "PATCH"
    assert result["created"] is True
    assert result["id"] == _UUID
    assert result["changed"] == [{"field": "Attributes", "old": None, "new": 2}]


@respx.mock
async def test_tabular_body_passes_through_unchanged(client):
    body = {
        "Description": "Orders",
        "Attributes": _ROWS,
        "Registers": {"mode": "replace", "rows": [{"Register": "r-uuid", "Flag": None}]},
    }
    route = respx.patch(f"{_BASE}/{_UUID}").mock(return_value=_ok())
    await client.patch_object("Catalogs", "MCBP_DataConversionSettings", _UUID, body)
    assert json.loads(route.calls[0].request.content) == body


@pytest.mark.parametrize(("object_id", "fields"), [
    (_UUID, {"Attributes": {"mode": "append", "rows": _ROWS}}),
    (_UUID, {"Description": "x", "Attributes": {"mode": "Append", "rows": _ROWS}}),
    ("new", {"Description": "Orders"}),
    ("NEW", {"Attributes": _ROWS}),
])
@respx.mock
async def test_create_or_append_is_not_retried_on_503(client, object_id, fields):
    route = respx.patch(f"{_BASE}/{object_id}").mock(
        return_value=httpx.Response(503, text="Service Unavailable"),
    )
    with pytest.raises(UpstreamError):
        await client.patch_object("Catalogs", "MCBP_DataConversionSettings", object_id, fields)
    assert route.call_count == 1


@respx.mock
async def test_create_is_not_retried_on_connect_error(client):
    route = respx.patch(f"{_BASE}/new").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UpstreamError):
        await client.patch_object("Catalogs", "MCBP_DataConversionSettings", "new", {"X": 1})
    assert route.call_count == 1


@pytest.mark.parametrize("fields", [
    {"Attributes": _ROWS},
    {"Attributes": {"mode": "replace", "rows": _ROWS}},
    {"Description": "header only"},
])
@respx.mock
async def test_replace_and_header_patch_still_retried_on_503(client, fields):
    route = respx.patch(f"{_BASE}/{_UUID}").mock(side_effect=[
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(503, text="Service Unavailable"),
        _ok(),
    ])
    result = await client.patch_object("Catalogs", "MCBP_DataConversionSettings", _UUID, fields)
    assert route.call_count == 3
    assert result["created"] is False
