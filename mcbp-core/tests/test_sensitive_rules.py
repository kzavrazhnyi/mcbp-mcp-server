"""`MCBPClient.sensitive_rules`: forced field-name substrings from `?sensitive_rules=true`."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from mcbp_core.client import _SENSITIVE_RULES_MAX, ConnectionConfig, MCBPClient
from mcbp_core.errors import ForbiddenError

BASE = "http://bas/hs"
URL = f"{BASE}/ai/v1/metadata/all"


async def _call(response: httpx.Response) -> tuple[list[str] | None, respx.Route]:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(URL, params={"sensitive_rules": "true"}).mock(return_value=response)
        client = MCBPClient(ConnectionConfig(base_url=BASE, user="u", password="p", mock=False))
        await client.startup()
        try:
            return await client.sensitive_rules(), route
        finally:
            await client.shutdown()


async def test_items_are_normalized_and_deduplicated():
    body = {"data": {"forced_substrings": [" ИНН ", "інн", "", "  ", "инн", 7, None, "Паспорт"]}}
    rules, route = await _call(httpx.Response(200, json=body))
    assert route.called
    assert rules == ["инн", "інн", "паспорт"]


async def test_short_items_are_dropped_with_one_warning(caplog):
    body = {"data": {"forced_substrings": ["а", " ИД ", "инн", "xy"]}}
    with caplog.at_level(logging.WARNING, logger="mcbp.api"):
        rules, _ = await _call(httpx.Response(200, json=body))
    assert rules == ["инн"]
    warned = [r for r in caplog.records if "sensitive_rules" in r.getMessage()]
    assert len(warned) == 1
    assert "3 item(s)" in warned[0].getMessage()


async def test_the_list_is_capped(caplog):
    body = {"data": {"forced_substrings": [f"rule{i:03d}" for i in range(250)]}}
    with caplog.at_level(logging.WARNING, logger="mcbp.api"):
        rules, _ = await _call(httpx.Response(200, json=body))
    assert rules is not None
    assert len(rules) == _SENSITIVE_RULES_MAX
    assert rules[0] == "rule000"
    assert rules[-1] == f"rule{_SENSITIVE_RULES_MAX - 1:03d}"
    assert any("capped at 200 of 250" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "body",
    [
        {"catalogs": [{"name": "Банки"}]},
        {"data": [{"name": "Банки"}]},
        {"data": {"other": 1}},
        {"data": {"forced_substrings": "инн"}},
        [],
    ],
)
async def test_an_older_module_answer_means_not_supported(body):
    rules, _ = await _call(httpx.Response(200, json=body))
    assert rules is None


async def test_an_empty_list_is_supported_and_adds_nothing():
    rules, _ = await _call(httpx.Response(200, json={"data": {"forced_substrings": []}}))
    assert rules == []


async def test_an_error_envelope_raises_typed():
    body = {"error": {"code": "FORBIDDEN", "message": "no rights"}}
    with pytest.raises(ForbiddenError):
        await _call(httpx.Response(403, json=body))


async def test_mock_mode_answers_not_supported():
    client = MCBPClient(ConnectionConfig(base_url=BASE, user="u", password="p", mock=True))
    await client.startup()
    try:
        assert await client.sensitive_rules() is None
    finally:
        await client.shutdown()
