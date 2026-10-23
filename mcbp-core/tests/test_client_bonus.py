"""GET /ai/v1/client/bonus — `MCBPClient.get_client_bonus`, its mock, and the tool."""
from __future__ import annotations

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ForbiddenError,
    NotConfiguredError,
    NotConfirmedError,
    NotFoundError,
    ParameterError,
)
from mcbp_core.tools import TOOLS, tool_specs

_CP_ALFA = "e3f1d9b6-0000-4000-8000-000000000002"
_FOREIGN = "00000000-0000-4000-8000-000000000000"
_URL = "http://test/ai/v1/client/bonus"


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def _mock_client() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    return client


@respx.mock
async def test_happy_path_returns_body_unchanged():
    body = {"data": {"balance": 120.5, "accrued": 300, "unit": "points"}}
    respx.get(_URL).mock(return_value=httpx.Response(200, json=body))
    client = _live_client()
    await client.startup()
    try:
        assert await client.get_client_bonus("0974553663") == body
    finally:
        await client.shutdown()


@respx.mock
async def test_null_data_passes_through():
    body = {"data": {"balance": None, "accrued": None, "unit": "points"}}
    respx.get(_URL).mock(return_value=httpx.Response(200, json=body))
    client = _live_client()
    await client.startup()
    try:
        assert await client.get_client_bonus("0974553663") == body
    finally:
        await client.shutdown()


@respx.mock
async def test_sends_login_only_when_counterparty_unset():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.get_client_bonus(" a+b@x.ua ")
        assert dict(route.calls[0].request.url.params) == {"login": "a+b@x.ua"}
    finally:
        await client.shutdown()


@respx.mock
async def test_sends_counterparty_as_normalised_query_param():
    route = respx.get(_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.get_client_bonus("0974553663", counterparty=f" {_CP_ALFA.upper()} ")
        assert dict(route.calls[0].request.url.params) == {"login": "0974553663",
                                                           "counterparty": _CP_ALFA}
    finally:
        await client.shutdown()


@pytest.mark.parametrize("login", ["", "   "])
async def test_blank_login_is_parameter_error_before_http(login):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_URL)
        await client.startup()
        try:
            with pytest.raises(ParameterError, match="login"):
                await client.get_client_bonus(login)
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
                await client.get_client_bonus("a@b.ua", counterparty=counterparty)
        finally:
            await client.shutdown()
        assert not route.called


@respx.mock
async def test_not_confirmed_is_forbidden_subclass_with_its_own_code():
    respx.get(_URL).mock(return_value=httpx.Response(403, json={"error": {
        "code": "NOT_CONFIRMED", "message": "Client login is not confirmed"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotConfirmedError, match="not confirmed") as exc_info:
            await client.get_client_bonus("0974553663")
    finally:
        await client.shutdown()
    assert isinstance(exc_info.value, ForbiddenError)
    assert (exc_info.value.code, exc_info.value.http_status) == ("NOT_CONFIRMED", 403)


@respx.mock
async def test_foreign_counterparty_404_is_not_found():
    respx.get(_URL).mock(return_value=httpx.Response(404, json={"error": {
        "code": "NOT_FOUND", "message": "counterparty is not among this client's"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotFoundError, match="counterparty"):
            await client.get_client_bonus("0974553663", counterparty=_FOREIGN)
    finally:
        await client.shutdown()


@respx.mock
async def test_server_side_bad_parameter_400_is_parameter_error():
    respx.get(_URL).mock(return_value=httpx.Response(400, json={"error": {
        "code": "BAD_PARAMETER", "message": "Parameter 'login' is required"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ParameterError, match="login"):
            await client.get_client_bonus("0974553663")
    finally:
        await client.shutdown()


@respx.mock
async def test_not_configured_400_is_not_configured_error():
    respx.get(_URL).mock(return_value=httpx.Response(400, json={"error": {
        "code": "NOT_CONFIGURED", "message": "MCBP_ClientLogins is missing"}}))
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotConfiguredError, match="MCBP_ClientLogins"):
            await client.get_client_bonus("0974553663")
    finally:
        await client.shutdown()


async def test_mock_returns_null_points():
    client = await _mock_client()
    assert await client.get_client_bonus("0974553663") == {
        "data": {"balance": None, "accrued": None, "unit": "points"}}


async def test_mock_foreign_counterparty_is_not_found():
    client = await _mock_client()
    with pytest.raises(NotFoundError, match="Counterparty"):
        await client.get_client_bonus("0974553663", counterparty=_FOREIGN)


def test_tool_is_read_only_mcp_only_and_appended_last():
    spec = TOOLS["get_client_bonus"]
    assert spec.read_only is True
    assert spec.surfaces == frozenset({"mcp"})
    assert list(TOOLS)[-1] == "get_client_bonus"
    assert spec.parameters["required"] == ["login"]
    assert set(spec.parameters["properties"]) == {"login", "counterparty"}
    assert "points" in spec.description_en and "no bonus data in this base" in spec.description_en
    assert spec in tool_specs(surface="mcp")
    assert spec not in tool_specs(surface="backend", include_write=True)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"login": "0974553663"}, None),
        ({"login": "0974553663", "counterparty": _CP_ALFA}, _CP_ALFA),
    ],
)
async def test_executor_passes_arguments_by_name(monkeypatch, args, expected):
    client = _live_client()
    seen: list[tuple] = []

    async def fake(login, counterparty=None):
        seen.append((login, counterparty))
        return {"data": {}}

    monkeypatch.setattr(client, "get_client_bonus", fake)
    await TOOLS["get_client_bonus"].executor(client, args)
    assert seen == [("0974553663", expected)]


async def test_masker_keeps_point_numbers_and_nulls():
    from mcbp_core.masking import Masker

    masker = Masker()
    masked = masker.mask({"data": {"balance": 120.5, "accrued": 300, "unit": "points"}})["data"]
    assert (masked["balance"], masked["accrued"]) == (120.5, 300)
    empty = masker.mask({"data": {"balance": None, "accrued": None, "unit": "points"}})["data"]
    assert (empty["balance"], empty["accrued"]) == (None, None)
