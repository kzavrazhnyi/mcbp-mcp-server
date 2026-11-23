"""upsert_task_status: respx for the wire format and error mapping, the in-memory mock for state."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import ConflictError, NotConfiguredError, NotFoundError, ParameterError
from mcbp_core.tools import TOOLS, tool_specs

SID = "6f1c2a3e-0000-4000-8000-000000000009"
FID = "6f1c2a3e-0000-4000-8000-000000000001"
URL = "http://test/ai/v1/task-statuses"
BODY = {"data": {"id": SID, "name": None, "description": "Виконується",
                 "deletion_mark": False, "created": True, "changed": True}}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


def _mock_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(user="u", mock=True))


@respx.mock
async def test_create_posts_without_id_and_only_passed_fields():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    assert await client.upsert_task_status(description="Виконується") == BODY
    request = route.calls.last.request
    assert "id=" not in str(request.url)
    assert json.loads(request.content) == {"description": "Виконується"}
    await client.shutdown()


@respx.mock
async def test_update_sends_id_query_and_deletion_mark():
    route = respx.post(f"{URL}?id={SID}").mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    await client.upsert_task_status(SID, deletion_mark=True)
    assert json.loads(route.calls.last.request.content) == {"deletion_mark": True}
    await client.shutdown()


@respx.mock
async def test_errors_are_typed_and_post_is_not_retried():
    route = respx.post(URL).mock(return_value=httpx.Response(
        422, json={"error": {"code": "NOT_CONFIGURED",
                             "message": "Catalog.MCBP_TaskStatuses is missing"}}))
    client = await _live()
    with pytest.raises(NotConfiguredError):
        await client.upsert_task_status(description="x")
    assert route.call_count == 1
    await client.shutdown()


async def test_blank_id_is_rejected_before_the_wire():
    with pytest.raises(ParameterError):
        await _mock_client().upsert_task_status("  ", description="x")


async def test_mock_create_update_repeat_and_status_use():
    client = _mock_client()
    created = (await client.upsert_task_status(description="Виконується"))["data"]
    assert created["created"] is True and created["changed"] is True
    sid = created["id"]
    same = (await client.upsert_task_status(sid, description="Виконується"))["data"]
    assert same["created"] is False and same["changed"] is False
    used = (await client.set_function_status(FID, version="1", status="виконується"))["data"]
    assert used["status"] == "Виконується"
    marked = (await client.upsert_task_status(sid, deletion_mark=True))["data"]
    assert marked["deletion_mark"] is True and marked["changed"] is True
    with pytest.raises(ParameterError, match="Waiting, Done"):
        await client.set_function_status(FID, version="1", status="Виконується")


async def test_mock_validation():
    client = _mock_client()
    with pytest.raises(ParameterError, match="description is required"):
        await client.upsert_task_status()
    with pytest.raises(NotFoundError, match=SID):
        await client.upsert_task_status(SID, description="x")
    with pytest.raises(ParameterError, match="UUID"):
        await client.upsert_task_status("not-a-uuid", description="x")


async def test_mock_duplicate_description_and_length():
    client = _mock_client()
    sid = (await client.upsert_task_status(description="Виконується"))["data"]["id"]
    with pytest.raises(ConflictError, match=sid):
        await client.upsert_task_status(description="виконується")
    with pytest.raises(ConflictError, match="predefined"):
        await client.upsert_task_status(description="Done")
    await client.upsert_task_status(sid, deletion_mark=True)
    again = (await client.upsert_task_status(description="Виконується"))["data"]
    assert again["created"] is True and again["id"] != sid
    with pytest.raises(ParameterError, match="25"):
        await client.upsert_task_status(description="x" * 26)


async def test_spec_is_write_only_mcp_only_and_maps_arguments():
    spec = TOOLS["upsert_task_status"]
    assert spec.read_only is False and spec.surfaces == frozenset({"mcp"})
    assert set(spec.parameters["properties"]) == {"status_id", "description", "deletion_mark"}
    assert spec.parameters["required"] == []
    assert "upsert_task_status" not in {
        s.name for s in tool_specs(surface="backend", include_write=True)}
    client = _mock_client()
    sid = (await spec.executor(client, {"description": "Відкладено"}))["data"]["id"]
    updated = await spec.executor(client, {"status_id": sid, "description": "Відкладено до релізу"})
    assert updated["data"]["description"] == "Відкладено до релізу"
