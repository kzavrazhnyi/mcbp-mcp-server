"""Business tips methods of MCBPClient: respx for the wire format, the in-memory mock for state."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient, reset_mock_tips
from mcbp_core.errors import LicenseRequiredError, NotFoundError, ParameterError

TIP = {"id": "t1", "title": "T", "text": "x", "basis": "b", "topic": "sales",
       "date": "2026-10-14 10:00:00", "read": False, "pinned": False, "hidden": False, "chat": ""}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


@respx.mock
async def test_list_tips_params():
    route = respx.get("http://test/ai/v1/tips").mock(
        return_value=httpx.Response(200, json={"data": [TIP]})
    )
    client = await _live()
    assert (await client.list_tips())["data"] == [TIP]
    assert route.calls.last.request.url.query == b""
    await client.list_tips(hidden=True, client_key="k 1")
    assert dict(route.calls.last.request.url.params) == {"hidden": "true", "client": "k 1"}
    await client.shutdown()


@respx.mock
async def test_create_patch_delete_wire_format():
    post = respx.post("http://test/ai/v1/tips").mock(
        return_value=httpx.Response(201, json={"data": [TIP]}))
    patch = respx.patch("http://test/ai/v1/tips/t1").mock(
        return_value=httpx.Response(200, json={"data": TIP}))
    delete = respx.delete("http://test/ai/v1/tips/t1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "t1", "deleted": True}}))
    client = await _live()
    await client.create_tips([{"title": "T"}], client_key="k")
    assert json.loads(post.calls.last.request.content) == {"tips": [{"title": "T"}]}
    assert post.calls.last.request.url.query == b"client=k"
    await client.patch_tip("t1", read=True, chat="", client_key="k")
    assert json.loads(patch.calls.last.request.content) == {"read": True, "chat": ""}
    assert (await client.delete_tip("t1"))["data"]["deleted"] is True
    assert delete.calls.last.request.url.query == b""
    await client.shutdown()


@respx.mock
async def test_tips_errors_are_typed():
    respx.get("http://test/ai/v1/tips").mock(return_value=httpx.Response(
        403, json={"error": {"code": "LICENSE_REQUIRED", "message": "no license"}}))
    respx.patch("http://test/ai/v1/tips/zz").mock(return_value=httpx.Response(
        404, json={"error": {"code": "NOT_FOUND", "message": "tip"}}))
    client = await _live()
    with pytest.raises(LicenseRequiredError):
        await client.list_tips()
    with pytest.raises(NotFoundError):
        await client.patch_tip("zz", read=True)
    await client.shutdown()


@respx.mock
async def test_tips_log_does_not_leak_texts_or_client_key(caplog):
    respx.post("http://test/ai/v1/tips").mock(return_value=httpx.Response(201, json={"data": []}))
    client = await _live()
    with caplog.at_level(logging.INFO, logger="mcbp_core.client"):
        await client.create_tips([{"title": "SECRET-TITLE", "text": "SECRET-TEXT"}],
                                 client_key="abcdefgh-ijkl")
    log_text = caplog.text
    assert "SECRET" not in log_text and "ijkl" not in log_text
    await client.shutdown()


def _mock_client(user: str = "u") -> MCBPClient:
    return MCBPClient(ConnectionConfig(user=user, mock=True))


async def test_mock_full_cycle():
    reset_mock_tips()
    client = _mock_client()
    created = (await client.create_tips(
        [{"title": "A", "topic": "sales"}, {"title": "B", "text": "t"}], client_key="k"))["data"]
    assert len(created) == 2 and set(created[0]) == set(TIP)
    a = created[0]["id"]
    await client.patch_tip(a, pinned=True, client_key="k")
    listed = (await client.list_tips(client_key="k"))["data"]
    assert listed[0]["id"] == a
    await client.patch_tip(a, hidden=True, chat="c1", client_key="k")
    assert [t["id"] for t in (await client.list_tips(client_key="k"))["data"]] != [a]
    hidden = (await client.list_tips(hidden=True, client_key="k"))["data"]
    assert any(t["id"] == a and t["chat"] == "c1" for t in hidden)
    await client.delete_tip(a, client_key="k")
    with pytest.raises(NotFoundError):
        await client.patch_tip(a, read=True, client_key="k")


async def test_mock_owner_isolation_and_validation():
    reset_mock_tips()
    client = _mock_client()
    tip = (await client.create_tips([{"title": "A"}], client_key="k1"))["data"][0]
    assert (await client.list_tips(client_key="k2"))["data"] == []
    with pytest.raises(NotFoundError):
        await client.delete_tip(tip["id"], client_key="k2")
    with pytest.raises(ParameterError):
        await client.create_tips([{"title": ""}])
    with pytest.raises(ParameterError):
        await client.create_tips([])
