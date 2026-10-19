"""set_function_status: respx for the wire format and error mapping, the in-memory mock for state."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ForbiddenError,
    NotConfiguredError,
    NotFoundError,
    ParameterError,
)
from mcbp_core.tools import TOOLS

FID = "6f1c2a3e-0000-4000-8000-000000000001"
URL = f"http://test/ai/v1/functions/{FID}/status"
BODY = {"data": {"function": FID, "description": "F", "version": "1.2.3", "status": "Done",
                 "date_execution": "2026-10-16", "disable": False, "created": True,
                 "changed": True}}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


def _mock_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(user="u", mock=True))


@respx.mock
async def test_wire_format_post_with_only_passed_fields():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    assert await client.set_function_status(FID, version="1.2.3", status="Done") == BODY
    assert json.loads(route.calls.last.request.content) == {"version": "1.2.3", "status": "Done"}
    await client.set_function_status(FID, version="1", status="Waiting",
                                     date_execution="2026-10-16", disable=False)
    assert json.loads(route.calls.last.request.content) == {
        "version": "1", "status": "Waiting", "date_execution": "2026-10-16", "disable": False}
    await client.set_function_status(FID, version="1", status="Done", date_execution="")
    assert json.loads(route.calls.last.request.content)["date_execution"] is None
    await client.shutdown()


@respx.mock
async def test_function_id_is_path_quoted():
    route = respx.post("http://test/ai/v1/functions/a%2Fb/status").mock(
        return_value=httpx.Response(200, json=BODY))
    client = await _live()
    await client.set_function_status("a/b", version="1", status="Done")
    assert route.called
    await client.shutdown()


@respx.mock
async def test_errors_are_typed_and_post_is_not_retried():
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "no function"}}),
        httpx.Response(400, json={"error": {"code": "NOT_CONFIGURED", "message": "no register"}}),
        httpx.Response(400, json={"error": {"code": "BAD_PARAMETER",
                                            "message": "one of: Waiting, Done"}}),
        httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "no right"}}),
        httpx.Response(503, text="busy"),
    ])
    client = await _live()
    with pytest.raises(NotFoundError):
        await client.set_function_status(FID, version="1", status="Done")
    with pytest.raises(NotConfiguredError):
        await client.set_function_status(FID, version="1", status="Done")
    with pytest.raises(ParameterError) as exc:
        await client.set_function_status(FID, version="1", status="Started")
    assert "Waiting" in exc.value.message
    with pytest.raises(ForbiddenError):
        await client.set_function_status(FID, version="1", status="Done")
    with pytest.raises(Exception):  # noqa: B017 - only the call count matters here
        await client.set_function_status(FID, version="1", status="Done")
    assert route.call_count == 5
    await client.shutdown()


async def test_mock_upsert_is_idempotent_and_keeps_omitted_fields():
    client = _mock_client()
    first = (await client.set_function_status(FID, version="1.0", status="waiting",
                                              date_execution="2026-10-16T12:30:00",
                                              disable=True))["data"]
    assert first["status"] == "Waiting" and first["date_execution"] == "2026-10-16"
    assert first["created"] is True and first["changed"] is True
    again = (await client.set_function_status(FID, version="1.0", status="Waiting"))["data"]
    assert again["created"] is False and again["changed"] is False
    assert again["disable"] is True and again["date_execution"] == "2026-10-16"
    done = (await client.set_function_status(FID, version="1.0", status="Done",
                                             date_execution=""))["data"]
    assert done["changed"] is True and done["date_execution"] is None


@pytest.mark.parametrize("kwargs, needle", [
    ({"version": "1", "status": "Started"}, "Waiting, Done"),
    ({"version": "", "status": "Done"}, "version"),
    ({"version": "x" * 13, "status": "Done"}, "12"),
    ({"version": "1", "status": "Done", "date_execution": "16.10.2026"}, "YYYY-MM-DD"),
    ({"version": "1", "status": "Done", "date_execution": "2026-13-45"}, "YYYY-MM-DD"),
    ({"version": "1", "status": "Done", "disable": "yes"}, "disable"),
])
async def test_mock_validation(kwargs, needle):
    with pytest.raises(ParameterError) as exc:
        await _mock_client().set_function_status(FID, **kwargs)
    assert needle in exc.value.message


async def test_mock_rejects_unknown_body_key():
    with pytest.raises(ParameterError) as exc:
        await _mock_client()._request("POST", f"/ai/v1/functions/{FID}/status",
                                      json={"version": "1", "status": "Done", "extra": 1})
    assert "Unknown field 'extra'" in exc.value.message


async def test_mock_id_case_reaches_the_same_record():
    client = _mock_client()
    first = (await client.set_function_status(FID.upper(), version="1", status="Done"))["data"]
    again = (await client.set_function_status(FID.lower(), version="1", status="Done"))["data"]
    assert first["function"] == again["function"] == FID.lower()
    assert again["created"] is False and again["changed"] is False


async def test_mock_rejects_non_uuid_id():
    with pytest.raises(ParameterError):
        await _mock_client().set_function_status("not-a-uuid", version="1", status="Done")


async def test_executor_maps_arguments():
    result = await TOOLS["set_function_status"].executor(
        _mock_client(),
        {"function_id": FID, "version": "2.0", "status": "Done", "disable": False},
    )
    assert result["data"]["version"] == "2.0" and result["data"]["disable"] is False
