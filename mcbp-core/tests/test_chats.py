"""Chat archive methods of MCBPClient: respx for the wire format, the in-memory mock for state."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import LicenseRequiredError, NotFoundError, ParameterError, WriteFailedError

HEAD = {"id": "abc", "title": "T", "pinned": False, "summary": "", "updated": "2026-10-13 10:00:00"}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


@respx.mock
async def test_list_chats_without_and_with_client():
    route = respx.get("http://test/ai/v1/chats").mock(
        return_value=httpx.Response(200, json={"data": [HEAD]})
    )
    client = await _live()
    assert (await client.list_chats()) == {"data": [HEAD]}
    assert route.calls.last.request.url.query == b""
    await client.list_chats(client_key="tg 42+x")
    assert route.calls.last.request.url.query == b"client=tg%2042%2Bx"
    await client.list_chats(client_key="")
    assert route.calls.last.request.url.query == b""
    await client.shutdown()


@respx.mock
async def test_create_chat_sends_only_passed_fields():
    route = respx.post("http://test/ai/v1/chats").mock(
        return_value=httpx.Response(201, json={"data": HEAD})
    )
    client = await _live()
    result = await client.create_chat(title="T", pinned=False, client_key="k")
    req = route.calls.last.request
    assert req.url.query == b"client=k"
    assert json.loads(req.content) == {"title": "T", "pinned": False}
    assert result["data"]["id"] == "abc"
    await client.create_chat()
    assert json.loads(route.calls.last.request.content) == {}
    await client.shutdown()


@respx.mock
async def test_get_chat_limit_and_path_encoding():
    msgs = [{"n": 1, "role": "user", "text": "hi", "date": "2026-10-13 10:00:00"}]
    route = respx.get(url__startswith="http://test/ai/v1/chats/").mock(
        return_value=httpx.Response(200, json={"data": {**HEAD, "messages": msgs}})
    )
    client = await _live()
    result = await client.get_chat("abc", limit=50, client_key="k")
    req = route.calls.last.request
    assert req.url.path == "/ai/v1/chats/abc"
    assert dict(req.url.params) == {"limit": "50", "client": "k"}
    assert result["data"]["messages"] == msgs
    await client.get_chat("a/b c")
    assert route.calls.last.request.url.raw_path.split(b"?")[0] == b"/ai/v1/chats/a%2Fb%20c"
    assert route.calls.last.request.url.query == b""
    await client.shutdown()


@respx.mock
async def test_patch_chat_only_passed_fields():
    route = respx.patch("http://test/ai/v1/chats/abc").mock(
        return_value=httpx.Response(200, json={"data": {**HEAD, "pinned": True}})
    )
    client = await _live()
    result = await client.patch_chat("abc", pinned=True, client_key="k")
    req = route.calls.last.request
    assert req.url.query == b"client=k"
    assert json.loads(req.content) == {"pinned": True}
    assert result["data"]["pinned"] is True
    await client.shutdown()


@respx.mock
async def test_delete_chat():
    route = respx.delete("http://test/ai/v1/chats/abc").mock(
        return_value=httpx.Response(200, json={"data": {"id": "abc", "deleted": True}})
    )
    client = await _live()
    assert (await client.delete_chat("abc")) == {"data": {"id": "abc", "deleted": True}}
    assert route.calls.last.request.url.query == b""
    await client.shutdown()


@respx.mock
async def test_add_chat_messages_body_and_result():
    route = respx.post("http://test/ai/v1/chats/abc/messages").mock(
        return_value=httpx.Response(201, json={"data": {**HEAD, "added": 2, "last_number": 5}})
    )
    client = await _live()
    msgs = [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}]
    result = await client.add_chat_messages("abc", msgs, title="N", client_key="k")
    req = route.calls.last.request
    assert req.url.query == b"client=k"
    assert json.loads(req.content) == {"messages": msgs, "title": "N"}
    assert result["data"]["added"] == 2 and result["data"]["last_number"] == 5
    await client.shutdown()


@respx.mock
async def test_chat_errors_are_typed():
    respx.get("http://test/ai/v1/chats/nope").mock(return_value=httpx.Response(
        404, json={"error": {"code": "NOT_FOUND", "message": "chat not found"}}))
    respx.post("http://test/ai/v1/chats").mock(return_value=httpx.Response(
        403, json={"error": {"code": "LICENSE_REQUIRED", "message": "no license"}}))
    respx.delete("http://test/ai/v1/chats/abc").mock(return_value=httpx.Response(
        500, json={"error": {"code": "WRITE_FAILED", "message": "boom"}}))
    client = await _live()
    with pytest.raises(NotFoundError):
        await client.get_chat("nope")
    with pytest.raises(LicenseRequiredError):
        await client.create_chat()
    with pytest.raises(WriteFailedError):
        await client.delete_chat("abc")
    await client.shutdown()


def _mock_client(user: str = "u") -> MCBPClient:
    return MCBPClient(ConnectionConfig(user=user, mock=True))


async def test_mock_full_cycle_and_numbering():
    client = _mock_client()
    chat = (await client.create_chat(title="A", client_key="k"))["data"]
    assert set(chat) == {"id", "title", "pinned", "summary", "updated"}
    assert chat["pinned"] is False and len(chat["updated"]) == 19
    added = (await client.add_chat_messages(
        chat["id"], [{"role": "user", "text": "1"}, {"role": "assistant", "text": "2"}],
        summary="S", client_key="k"))["data"]
    assert (added["added"], added["last_number"], added["summary"]) == (2, 2, "S")
    again = (await client.add_chat_messages(
        chat["id"], [{"role": "note", "text": "3"}], client_key="k"))["data"]
    assert again["last_number"] == 3
    full = (await client.get_chat(chat["id"], client_key="k"))["data"]
    assert [m["n"] for m in full["messages"]] == [1, 2, 3]
    last = (await client.get_chat(chat["id"], limit=2, client_key="k"))["data"]
    assert [m["n"] for m in last["messages"]] == [2, 3]
    patched = (await client.patch_chat(chat["id"], title="B", client_key="k"))["data"]
    assert patched["title"] == "B" and patched["summary"] == "S"
    assert (await client.delete_chat(chat["id"], client_key="k"))["data"] == {
        "id": chat["id"], "deleted": True}
    assert (await client.list_chats(client_key="k"))["data"] == []


async def test_mock_ownership_split_by_client_and_user():
    client = _mock_client()
    chat = (await client.create_chat(title="A", client_key="k1"))["data"]
    assert (await client.list_chats(client_key="k2"))["data"] == []
    assert (await client.list_chats())["data"] == []
    for call in (
        client.get_chat(chat["id"], client_key="k2"),
        client.patch_chat(chat["id"], title="x", client_key="k2"),
        client.delete_chat(chat["id"], client_key="k2"),
        client.add_chat_messages(chat["id"], [], client_key="k2"),
        _mock_client("other").get_chat(chat["id"], client_key="k1"),
    ):
        with pytest.raises(NotFoundError):
            await call
    assert len((await client.list_chats(client_key="k1"))["data"]) == 1


async def test_mock_list_order_pinned_first_then_newest(monkeypatch):
    from mcbp_core import client as mod

    stamps = iter(["2026-10-13 10:00:00", "2026-10-13 11:00:00", "2026-10-13 12:00:00"])
    monkeypatch.setattr(mod, "_mock_now", lambda: next(stamps))
    client = _mock_client()
    a = (await client.create_chat(title="old", pinned=True))["data"]
    b = (await client.create_chat(title="mid"))["data"]
    c = (await client.create_chat(title="new"))["data"]
    ids = [r["id"] for r in (await client.list_chats())["data"]]
    assert ids == [a["id"], c["id"], b["id"]]


KEY = "device-key-SECRET-123456"


async def _mock() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(user="u", mock=True))
    await client.startup()
    return client


async def test_chat_logs_hide_texts_and_full_key(caplog):
    caplog.set_level("DEBUG")
    client = await _mock()
    created = await client.create_chat(title="TITLE-PRIVATE", client_key=KEY)
    chat_id = created["data"]["id"]
    await client.add_chat_messages(
        chat_id, [{"role": "user", "text": "MSG-PRIVATE"}], title="T2-PRIVATE",
        client_key=KEY,
    )
    await client.patch_chat(chat_id, summary="SUMMARY-PRIVATE", client_key=KEY)
    await client.list_chats(client_key=KEY)
    log = caplog.text
    assert "devi…" in log
    for secret in ("MSG-PRIVATE", "SUMMARY-PRIVATE", "TITLE-PRIVATE", "T2-PRIVATE", KEY):
        assert secret not in log
    assert "messages: list(1)" in log


@respx.mock
async def test_chat_live_logs_and_errors_hide_key(caplog):
    caplog.set_level("DEBUG")
    respx.get("http://test/ai/v1/chats/zz").mock(return_value=httpx.Response(404, text="nope"))
    respx.patch("http://test/ai/v1/chats/abc").mock(
        return_value=httpx.Response(200, json={"data": HEAD})
    )
    client = await _live()
    await client.patch_chat("abc", summary="SUMMARY-PRIVATE", client_key=KEY)
    with pytest.raises(NotFoundError) as exc:
        await client.get_chat("zz", client_key=KEY)
    assert KEY not in str(exc.value) and "?" not in str(exc.value)
    assert KEY not in caplog.text and "SUMMARY-PRIVATE" not in caplog.text
    await client.shutdown()


async def test_mock_rejects_bad_role_limit_and_empty_messages():
    client = await _mock()
    chat_id = (await client.create_chat())["data"]["id"]
    with pytest.raises(ParameterError):
        await client.add_chat_messages(chat_id, [{"role": "bot", "text": "x"}])
    with pytest.raises(ParameterError):
        await client.add_chat_messages(chat_id, [{"text": "x"}])
    with pytest.raises(ParameterError):
        await client.add_chat_messages(chat_id, [])
    for bad in (0, 1001):
        with pytest.raises(ParameterError):
            await client.get_chat(chat_id, limit=bad)
    assert (await client.get_chat(chat_id, limit=1000))["data"]["messages"] == []


async def test_mock_list_order_stable_on_equal_updated(monkeypatch):
    monkeypatch.setattr("mcbp_core.client._mock_now", lambda: "2026-10-14 10:00:00")
    client = await _mock()
    ids = [(await client.create_chat(title=str(i)))["data"]["id"] for i in range(3)]
    listed = [c["id"] for c in (await client.list_chats())["data"]]
    assert listed == ids[::-1]
