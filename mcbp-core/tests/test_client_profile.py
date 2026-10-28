"""/ai/v1/client/profile (GET, POST) and /client/profile/logins — `MCBPClient.get_client_profile`,
`update_client_profile`, `bind_client_login` and their mock."""
from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ConflictError,
    ForbiddenError,
    NotConfirmedError,
    ParameterError,
)
from mcbp_core.tools import tool_specs

_PROFILE = "http://test/ai/v1/client/profile"
_LOGINS = "http://test/ai/v1/client/profile/logins"
_OTHER_LOGIN = "380509990000"


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def _mock_client() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    return client


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code.lower()}})


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


# --- live client over respx -------------------------------------------------------------------

@respx.mock
async def test_get_profile_sends_trimmed_login():
    route = respx.get(_PROFILE).mock(return_value=httpx.Response(200, json={"data": {"x": 1}}))
    client = _live_client()
    await client.startup()
    try:
        assert await client.get_client_profile(" 380501110001 ") == {"data": {"x": 1}}
    finally:
        await client.shutdown()
    assert route.calls.last.request.url.params["login"] == "380501110001"


@respx.mock
async def test_update_sends_only_passed_fields():
    route = respx.post(_PROFILE).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.update_client_profile("a@b.ua", middle_name="Петрович",
                                           birth_date=date(1990, 5, 7))
    finally:
        await client.shutdown()
    assert route.calls.last.request.url.params["login"] == "a@b.ua"
    assert _sent(route) == {"middle_name": "Петрович", "birth_date": "1990-05-07"}


@respx.mock
async def test_update_none_clears_and_string_date_is_normalized():
    route = respx.post(_PROFILE).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.update_client_profile("a@b.ua", name=None, last_name=None,
                                           birth_date=" 2001-12-31 ")
        assert _sent(route) == {"name": None, "last_name": None, "birth_date": "2001-12-31"}
        await client.update_client_profile("a@b.ua", birth_date=None)
        assert _sent(route) == {"birth_date": None}
        await client.update_client_profile("a@b.ua", birth_date=datetime(2000, 1, 2, 3, 4))
        assert _sent(route) == {"birth_date": "2000-01-02"}
    finally:
        await client.shutdown()


@pytest.mark.parametrize("bad", ["07.05.1990", "1990-5-7", "1990-02-30", "19900507", 19900507])
async def test_update_bad_birth_date_is_parameter_error_before_http(bad):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_PROFILE)
        with pytest.raises(ParameterError, match="birth_date"):
            await client.update_client_profile("a@b.ua", birth_date=bad)
        assert not route.called


async def test_update_non_string_name_is_parameter_error():
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_PROFILE)
        with pytest.raises(ParameterError, match="last_name"):
            await client.update_client_profile("a@b.ua", last_name=42)
        assert not route.called


@pytest.mark.parametrize("call", [
    lambda c: c.get_client_profile(""),
    lambda c: c.update_client_profile("  ", name="x"),
    lambda c: c.bind_client_login(None, "a@b.ua"),
    lambda c: c.bind_client_login("a@b.ua", " "),
])
async def test_blank_login_is_parameter_error_before_http(call):
    client = _live_client()
    with respx.mock(assert_all_called=False) as router:
        get_route, post_route = router.get(_PROFILE), router.post(url__startswith=_PROFILE)
        with pytest.raises(ParameterError):
            await call(client)
        assert not get_route.called and not post_route.called


@respx.mock
async def test_bind_sends_new_login():
    route = respx.post(_LOGINS).mock(return_value=httpx.Response(200, json={"data": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.bind_client_login("380501110001", " new@b.ua ")
    finally:
        await client.shutdown()
    assert route.calls.last.request.url.params["login"] == "380501110001"
    assert _sent(route) == {"new_login": "new@b.ua"}


@pytest.mark.parametrize("method, url, call", [
    ("GET", _PROFILE, lambda c: c.get_client_profile("380501110001")),
    ("POST", _PROFILE, lambda c: c.update_client_profile("380501110001", name="x")),
    ("POST", _LOGINS, lambda c: c.bind_client_login("380501110001", "n@b.ua")),
])
async def test_not_confirmed_is_typed(method, url, call):
    with respx.mock() as router:
        route = router.route(method=method, url=url).mock(
            return_value=_error(403, "NOT_CONFIRMED"))
        client = _live_client()
        await client.startup()
        try:
            with pytest.raises(NotConfirmedError) as exc:
                await call(client)
        finally:
            await client.shutdown()
        assert route.call_count == 1
    assert isinstance(exc.value, ForbiddenError)
    assert exc.value.code == "NOT_CONFIRMED"


async def test_bind_conflict_is_conflict_error_and_not_retried():
    with respx.mock() as router:
        route = router.post(_LOGINS).mock(return_value=_error(409, "CONFLICT"))
        client = _live_client()
        await client.startup()
        try:
            with pytest.raises(ConflictError) as exc:
                await client.bind_client_login("380501110001", "taken@b.ua")
        finally:
            await client.shutdown()
        assert route.call_count == 1
    assert exc.value.code == "CONFLICT" and exc.value.http_status == 409


def test_profile_methods_are_not_tools():
    names = {s.name for surface in ("backend", "mcp")
             for s in tool_specs(surface=surface, include_write=True)}
    assert not names & {"get_client_profile", "update_client_profile", "bind_client_login"}


# --- mock -------------------------------------------------------------------------------------

async def _mock_with_logins(*logins: str) -> MCBPClient:
    client = await _mock_client()
    for login in logins:
        await client.start_client_auth(login, confirmed=True, chat_id=7)
    return client


async def test_mock_unknown_login_is_not_confirmed():
    client = await _mock_client()
    with pytest.raises(NotConfirmedError):
        await client.get_client_profile("never-confirmed@example.ua")
    with pytest.raises(NotConfirmedError):
        await client.bind_client_login("never-confirmed@example.ua", "x@example.ua")


async def test_mock_update_is_shared_by_contact_rows_and_none_clears():
    client = await _mock_with_logins("380501230001", "prof1@example.ua")
    data = (await client.update_client_profile("380501230001", name="Іван",
                                               birth_date="1990-05-07"))["data"]
    assert data["changed"] is True and data["name"] == "Іван"
    again = (await client.update_client_profile("380501230001", name=" Іван "))["data"]
    assert again["changed"] is False
    sibling = (await client.get_client_profile("prof1@example.ua"))["data"]
    assert sibling["name"] == "Іван" and sibling["birth_date"] == "1990-05-07"
    assert sibling["last_name"] is None
    current = [r["login"] for r in sibling["logins"] if r["is_current"]]
    assert current == ["prof1@example.ua"]
    assert {"380501230001", "prof1@example.ua"} <= {r["login"] for r in sibling["logins"]}
    cleared = (await client.update_client_profile("prof1@example.ua", name=None))["data"]
    assert cleared["changed"] is True and cleared["name"] is None


async def test_mock_bind_adds_row_then_is_idempotent():
    client = await _mock_with_logins("380501230002")
    data = (await client.bind_client_login("380501230002", "New2@Example.ua"))["data"]
    assert data["changed"] is True
    assert "new2@example.ua" in {r["login"] for r in data["logins"]}
    again = (await client.bind_client_login("380501230002", "new2@example.ua"))["data"]
    assert again["changed"] is False
    bound = (await client.get_client_profile("new2@example.ua"))["data"]
    assert bound["contact"] == data["contact"]


async def test_mock_bind_login_of_another_contact_is_conflict():
    client = await _mock_with_logins("380501230003")
    with pytest.raises(ConflictError):
        await client.bind_client_login("380501230003", _OTHER_LOGIN)
    other = (await client.get_client_profile(_OTHER_LOGIN))["data"]
    assert [r["login"] for r in other["logins"]] == [_OTHER_LOGIN]
    with pytest.raises(ConflictError):
        await client.bind_client_login(_OTHER_LOGIN, "380501230003")
