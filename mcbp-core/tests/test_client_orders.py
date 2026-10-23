"""GET /ai/v1/client/orders — `MCBPClient.get_client_orders`, its mock, and the tool."""
from __future__ import annotations

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import ForbiddenError, NotConfirmedError, NotFoundError, ParameterError
from mcbp_core.tools import TOOLS, tool_specs

_ROW_KEYS = {"id", "number", "date", "amount", "currency", "state", "posted", "paid", "to_pay",
             "counterparty"}
_ORDER_ID = "7c1e0a52-0000-4000-8000-000000000002"
_CP_ALFA = "e3f1d9b6-0000-4000-8000-000000000002"
_CP_BETA = "e3f1d9b6-0000-4000-8000-000000000003"
_FOREIGN = "00000000-0000-4000-8000-000000000000"
_URL = "http://test/ai/v1/client/orders"


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def _mock_client() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    return client


async def test_mock_list_has_row_shape_and_no_items():
    client = await _mock_client()
    result = await client.get_client_orders("0974553663")
    assert isinstance(result["data"], list) and result["data"]
    for row in result["data"]:
        assert set(row) == _ROW_KEYS
    assert result["data"][0]["date"] == "2026-10-18 14:05:00"
    assert {r["counterparty"]["id"] for r in result["data"]} == {_CP_ALFA, _CP_BETA}
    assert [c["id"] for c in result["counterparties"]] == [_CP_ALFA, _CP_BETA]
    for cp in result["counterparties"]:
        assert set(cp) == {"id", "type", "name"}


async def test_mock_filters_by_counterparty_and_keeps_all_allowed_in_envelope():
    client = await _mock_client()
    result = await client.get_client_orders("0974553663", counterparty=_CP_BETA.upper())
    assert [r["counterparty"]["id"] for r in result["data"]] == [_CP_BETA]
    assert len(result["counterparties"]) == 2


async def test_mock_foreign_counterparty_is_not_found():
    client = await _mock_client()
    with pytest.raises(NotFoundError, match="counterparty|Counterparty"):
        await client.get_client_orders("0974553663", counterparty=_FOREIGN)


async def test_mock_order_outside_counterparty_filter_is_not_found():
    client = await _mock_client()
    with pytest.raises(NotFoundError, match="Order"):
        await client.get_client_orders("0974553663", order_id=_ORDER_ID, counterparty=_CP_BETA)


async def test_mock_mutating_result_does_not_leak_into_the_next_call():
    client = await _mock_client()
    first = await client.get_client_orders("0974553663")
    first["data"][0]["counterparty"]["name"] = "changed"
    second = await client.get_client_orders("0974553663")
    assert second["data"][0]["counterparty"]["name"] != "changed"


async def test_mock_one_order_has_items():
    client = await _mock_client()
    result = await client.get_client_orders("0974553663", order_id=_ORDER_ID)
    order = result["data"]
    assert set(order) == _ROW_KEYS | {"items"}
    assert order["id"] == _ORDER_ID
    assert order["counterparty"]["id"] == _CP_ALFA
    assert set(order["items"][0]) == {"product", "quantity", "price", "amount"}
    assert set(order["items"][0]["product"]) == {"id", "type", "name"}


async def test_mock_unknown_order_is_not_found():
    client = await _mock_client()
    with pytest.raises(NotFoundError):
        await client.get_client_orders("0974553663",
                                       order_id=_FOREIGN)


@pytest.mark.parametrize("login", ["", "   "])
async def test_blank_login_is_parameter_error_before_http(login):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_URL)
        await client.startup()
        try:
            with pytest.raises(ParameterError, match="login"):
                await client.get_client_orders(login)
        finally:
            await client.shutdown()
        assert not route.called


async def test_non_uuid_order_id_is_parameter_error_before_http():
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_URL)
        await client.startup()
        try:
            with pytest.raises(ParameterError, match="order_id"):
                await client.get_client_orders("a@b.ua", order_id="123")
        finally:
            await client.shutdown()
        assert not route.called


@pytest.mark.parametrize("counterparty", ["123", "", "not-a-uuid"])
async def test_non_uuid_counterparty_is_parameter_error_before_http(counterparty):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_URL)
        await client.startup()
        try:
            with pytest.raises(ParameterError, match="counterparty"):
                await client.get_client_orders("a@b.ua", counterparty=counterparty)
        finally:
            await client.shutdown()
        assert not route.called


@respx.mock
async def test_sends_counterparty_as_normalised_query_param():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    client = _live_client()
    await client.startup()
    try:
        await client.get_client_orders("0974553663", counterparty=f" {_CP_ALFA.upper()} ")
        assert dict(route.calls[0].request.url.params) == {"login": "0974553663",
                                                           "counterparty": _CP_ALFA}
    finally:
        await client.shutdown()


@respx.mock
async def test_old_service_without_counterparty_fields_passes_through():
    body = {"data": [{"id": _ORDER_ID, "number": "1", "date": "2026-10-18 14:05:00",
                      "amount": 1.0, "currency": "UAH", "state": None, "posted": True,
                      "paid": None, "to_pay": None}]}
    respx.get(_URL).mock(return_value=httpx.Response(200, json=body))
    client = _live_client()
    await client.startup()
    try:
        assert await client.get_client_orders("0974553663") == body
    finally:
        await client.shutdown()


@respx.mock
async def test_server_side_bad_counterparty_422_is_parameter_error():
    respx.get(_URL).mock(return_value=httpx.Response(422, json={"error": {
        "code": "BAD_PARAMETER", "message": "Parameter 'counterparty' must be a UUID"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ParameterError, match="counterparty"):
            await client.get_client_orders("0974553663", counterparty=_CP_ALFA)
    finally:
        await client.shutdown()


@respx.mock
async def test_foreign_counterparty_404_is_not_found():
    respx.get(_URL).mock(return_value=httpx.Response(404, json={"error": {
        "code": "NOT_FOUND", "message": "counterparty is not among this client's"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotFoundError, match="counterparty"):
            await client.get_client_orders("0974553663", counterparty=_FOREIGN)
    finally:
        await client.shutdown()


@respx.mock
async def test_sends_login_and_id_as_query_string():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.get_client_orders("a+b@x.ua", order_id=_ORDER_ID.upper())
        assert dict(route.calls[0].request.url.params) == {"login": "a+b@x.ua",
                                                           "id": _ORDER_ID}
    finally:
        await client.shutdown()


@respx.mock
async def test_omits_id_when_unset():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    client = _live_client()
    await client.startup()
    try:
        assert await client.get_client_orders("0974553663") == {"data": []}
        assert dict(route.calls[0].request.url.params) == {"login": "0974553663"}
    finally:
        await client.shutdown()


@respx.mock
async def test_not_confirmed_is_forbidden_subclass_with_its_own_code():
    respx.get(_URL).mock(return_value=httpx.Response(403, json={"error": {
        "code": "NOT_CONFIRMED", "message": "Client login is not confirmed"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotConfirmedError, match="not confirmed") as exc_info:
            await client.get_client_orders("0974553663")
    finally:
        await client.shutdown()
    assert isinstance(exc_info.value, ForbiddenError)
    assert (exc_info.value.code, exc_info.value.http_status) == ("NOT_CONFIRMED", 403)


@respx.mock
async def test_order_of_another_client_is_not_found():
    respx.get(_URL).mock(return_value=httpx.Response(404, json={"error": {
        "code": "NOT_FOUND", "message": "Order not found"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotFoundError, match="Order not found"):
            await client.get_client_orders("0974553663", order_id=_ORDER_ID)
    finally:
        await client.shutdown()


def test_tool_is_read_only_mcp_only_and_appended_last():
    spec = TOOLS["get_client_orders"]
    assert spec.read_only is True
    assert spec.surfaces == frozenset({"mcp"})
    assert list(TOOLS)[-2:] == ["get_client_orders", "get_client_bonus"]
    assert list(TOOLS)[0] == "search_catalog"
    assert spec.parameters["required"] == ["login"]
    assert set(spec.parameters["properties"]) == {"login", "order_id", "counterparty"}
    assert spec.parameters["properties"]["counterparty"]["type"] == "string"
    assert "counterparties" in spec.description_en and "NOT_FOUND" in spec.description_en
    assert spec in tool_specs(surface="mcp")
    assert spec not in tool_specs(surface="backend", include_write=True)


async def test_executor_passes_arguments_by_name():
    client = await _mock_client()
    listed = await TOOLS["get_client_orders"].executor(client, {"login": "0974553663"})
    assert isinstance(listed["data"], list)
    one = await TOOLS["get_client_orders"].executor(
        client, {"login": "0974553663", "order_id": _ORDER_ID},
    )
    assert one["data"]["id"] == _ORDER_ID and one["data"]["items"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"login": "0974553663"}, {"order_id": None, "counterparty": None}),
        ({"login": "0974553663", "counterparty": _CP_BETA},
         {"order_id": None, "counterparty": _CP_BETA}),
        ({"login": "0974553663", "order_id": _ORDER_ID, "counterparty": _CP_ALFA},
         {"order_id": _ORDER_ID, "counterparty": _CP_ALFA}),
    ],
)
async def test_executor_forwards_counterparty_only_when_given(monkeypatch, args, expected):
    client = _live_client()
    seen: list[tuple] = []

    async def fake(login, order_id=None, counterparty=None):
        seen.append((login, {"order_id": order_id, "counterparty": counterparty}))
        return {"data": []}

    monkeypatch.setattr(client, "get_client_orders", fake)
    await TOOLS["get_client_orders"].executor(client, args)
    assert seen == [("0974553663", expected)]


async def test_executor_counterparty_reaches_the_query_string():
    client = _live_client()
    await client.startup()
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(_URL).mock(return_value=httpx.Response(200, json={"data": []}))
            await TOOLS["get_client_orders"].executor(client, {"login": "0974553663"})
            await TOOLS["get_client_orders"].executor(
                client, {"login": "0974553663", "counterparty": _CP_BETA},
            )
        first, second = (c.request.url.params for c in route.calls)
        assert "counterparty" not in first
        assert second["counterparty"] == _CP_BETA
    finally:
        await client.shutdown()


async def test_masker_keeps_numbers_and_ids_and_hides_product_names():
    from mcbp_core.masking import Masker

    client = await _mock_client()
    result = await client.get_client_orders("0974553663", order_id=_ORDER_ID)
    masked = Masker().mask(result)["data"]
    original = result["data"]
    for key in ("id", "amount", "posted", "paid", "to_pay"):
        assert masked[key] == original[key], key
    item, masked_item = original["items"][0], masked["items"][0]
    assert masked_item["product"]["id"] == item["product"]["id"]
    assert masked_item["product"]["name"] != item["product"]["name"]
    assert (masked_item["quantity"], masked_item["amount"]) == (item["quantity"], item["amount"])
