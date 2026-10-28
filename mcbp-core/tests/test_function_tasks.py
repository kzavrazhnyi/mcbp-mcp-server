"""list_function_tasks / upsert_function_task: respx for the wire format and error mapping, the
in-memory mock for state."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ForbiddenError,
    LicenseRequiredError,
    LockedError,
    NotConfiguredError,
    NotFoundError,
    ParameterError,
    WriteFailedError,
)
from mcbp_core.tools import TOOLS, tool_specs

FID = "6f1c2a3e-0000-4000-8000-000000000001"
TID = "6f1c2a3e-0000-4000-8000-000000000009"
URL = f"http://test/ai/v1/functions/{FID}/tasks"
BODY = {"data": {"id": TID, "description": "Task", "function": FID,
                 "task_type": "MCBP_PerformerTask", "created": True, "changed": True}}
LIST = {"data": [], "function": FID, "task_type": "MCBP_PerformerTask", "limit": 50}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


def _mock_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(user="u", mock=True))


async def _mock_function(client: MCBPClient) -> str:
    return (await client.upsert_function(description="Function"))["data"]["id"]


@respx.mock
async def test_list_sends_limit_only_when_given():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=LIST))
    client = await _live()
    assert await client.list_function_tasks(FID) == LIST
    assert "limit" not in route.calls.last.request.url.params
    await client.list_function_tasks(FID, limit=7)
    assert route.calls.last.request.url.params["limit"] == "7"
    await client.shutdown()


@respx.mock
async def test_create_posts_without_task_and_only_passed_fields():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    assert await client.upsert_function_task(FID, description="Task") == BODY
    request = route.calls.last.request
    assert "task" not in request.url.params and "id" not in request.url.params
    assert json.loads(request.content) == {"description": "Task"}
    await client.shutdown()


@respx.mock
async def test_update_passes_task_param_and_empty_strings_as_null():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    await client.upsert_function_task(FID, TID, specification="", deadline="", importance="",
                                      executed=True, deletion_mark=False)
    request = route.calls.last.request
    assert request.url.params["task"] == TID
    assert "id" not in request.url.params
    assert json.loads(request.content) == {"specification": None, "deadline": None,
                                           "importance": None, "executed": True,
                                           "deletion_mark": False}
    await client.shutdown()


@pytest.mark.parametrize("status, code, exc", [
    (400, "BAD_PARAMETER", ParameterError),
    (400, "TASK_IN_BUSINESS_PROCESS", ParameterError),
    (400, "NOT_CONFIGURED", NotConfiguredError),
    (403, "FORBIDDEN", ForbiddenError),
    (403, "LICENSE_REQUIRED", LicenseRequiredError),
    (404, "NOT_FOUND", NotFoundError),
    (409, "LOCKED", LockedError),
    (500, "WRITE_FAILED", WriteFailedError),
])
@respx.mock
async def test_errors_are_typed_and_post_is_not_retried(status, code, exc):
    route = respx.post(URL).mock(return_value=httpx.Response(
        status, json={"error": {"code": code, "message": "m"}}))
    client = await _live()
    with pytest.raises(exc):
        await client.upsert_function_task(FID, TID, executed=True)
    assert route.call_count == 1
    await client.shutdown()


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_task_id_is_rejected_not_a_create(blank):
    with pytest.raises(ParameterError, match="task_id is blank"):
        await _mock_client().upsert_function_task(FID, blank, description="x")


async def test_mock_create_list_update_idempotent():
    client = _mock_client()
    fid = await _mock_function(client)
    assert (await client.list_function_tasks(fid))["data"] == []
    first = (await client.upsert_function_task(fid, description="First", importance="high",
                                               deadline="2026-10-20"))["data"]
    assert first["created"] is True and first["task_type"] == "MCBP_PerformerTask"
    assert first["function"] == fid and first["importance"] == "High"
    assert first["author"] == "u" and first["executor"] is None
    assert first["number"] and first["in_business_process"] is False
    second = (await client.upsert_function_task(fid, description="Second"))["data"]
    assert second["importance"] == "Plain"
    listed = await client.list_function_tasks(fid)
    assert [r["id"] for r in listed["data"]] == [second["id"], first["id"]]
    assert listed["function"] == fid and listed["limit"] == 50
    assert "function" not in listed["data"][0] and "created" not in listed["data"][0]
    tid = first["id"]
    same = (await client.upsert_function_task(fid, tid, description="First"))["data"]
    assert same["changed"] is False and same["created"] is False
    done = (await client.upsert_function_task(fid, tid, executed=True, deadline=""))["data"]
    assert done["changed"] is True
    assert done["executed"] is True and done["deadline"] is None


async def test_mock_limit_is_capped_at_50():
    client = _mock_client()
    fid = await _mock_function(client)
    for i in range(52):
        await client.upsert_function_task(fid, description=f"T{i}")
    capped = await client.list_function_tasks(fid, limit=500)
    assert capped["limit"] == 50 and len(capped["data"]) == 50
    assert len((await client.list_function_tasks(fid, limit=3))["data"]) == 3


async def test_mock_unknown_function_is_not_found():
    client = _mock_client()
    with pytest.raises(NotFoundError, match=FID):
        await client.list_function_tasks(FID)
    with pytest.raises(NotFoundError, match=FID):
        await client.upsert_function_task(FID, description="x")


async def test_mock_task_of_another_function_is_not_found():
    client = _mock_client()
    fid_a, fid_b = await _mock_function(client), await _mock_function(client)
    tid = (await client.upsert_function_task(fid_a, description="A"))["data"]["id"]
    with pytest.raises(NotFoundError, match=tid):
        await client.upsert_function_task(fid_b, tid, executed=True)
    assert (await client.list_function_tasks(fid_b))["data"] == []


async def test_mock_reads_task_param_not_id():
    client = _mock_client()
    fid = await _mock_function(client)
    tid = (await client.upsert_function_task(fid, description="A"))["data"]["id"]
    path = f"/ai/v1/functions/{fid}/tasks"
    updated = await client._request("POST", path, params={"task": tid}, json={"executed": True})
    assert updated["data"]["created"] is False and updated["data"]["id"] == tid
    with pytest.raises(ParameterError, match=r"use \?task="):
        await client._request("POST", path, params={"id": tid}, json={"description": "B"})


@pytest.mark.parametrize("params, body, needle", [
    (None, {}, "description is required when creating a task"),
    (None, {"description": "x", "Description": "y"}, "Unknown field 'Description'"),
    (None, {"description": "x", "deadline": "20.10.2026"}, "deadline must be"),
    (None, {"description": "x", "importance": "Urgent"}, "Unknown importance 'Urgent'"),
    (None, {"description": "x", "executed": "yes"}, "executed must be true or false"),
    ({"task": " "}, {"description": "x"}, "task is blank"),
    ({"task": "nope"}, {"description": "x"}, "task must be a MCBP_PerformerTask UUID"),
])
async def test_mock_validation(params, body, needle):
    client = _mock_client()
    fid = await _mock_function(client)
    with pytest.raises(ParameterError, match=needle):
        await client._request("POST", f"/ai/v1/functions/{fid}/tasks", params=params, json=body)


def test_specs_are_mcp_only_appended_after_event_log_and_upsert_is_write_gated():
    names = list(TOOLS)
    assert names[names.index("get_event_log") + 1:] == ["list_function_tasks",
                                                       "upsert_function_task",
                                                       "set_register_record",
                                                       "get_client_orders",
                                                       "get_client_bonus"]
    listing, upsert = TOOLS["list_function_tasks"], TOOLS["upsert_function_task"]
    assert listing.read_only is True and upsert.read_only is False
    assert listing.surfaces == upsert.surfaces == frozenset({"mcp"})
    backend = {s.name for s in tool_specs(surface="backend", include_write=True)}
    assert not {"list_function_tasks", "upsert_function_task"} & backend
    mcp_read_only = {s.name for s in tool_specs(surface="mcp", include_write=False)}
    assert "list_function_tasks" in mcp_read_only and "upsert_function_task" not in mcp_read_only


async def test_executors_map_arguments():
    client = _mock_client()
    fid = await _mock_function(client)
    upsert = TOOLS["upsert_function_task"]
    assert set(upsert.parameters["properties"]) == {
        "function_id", "task_id", "description", "specification", "deadline", "importance",
        "executed", "deletion_mark"}
    created = await upsert.executor(client, {"function_id": fid, "description": "T",
                                             "specification": "Why"})
    tid = created["data"]["id"]
    updated = await upsert.executor(client, {"function_id": fid, "task_id": tid,
                                             "deletion_mark": True})
    assert updated["data"]["deletion_mark"] is True and updated["data"]["specification"] == "Why"
    listed = await TOOLS["list_function_tasks"].executor(client, {"function_id": fid})
    assert [r["id"] for r in listed["data"]] == [tid]
