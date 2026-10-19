"""upsert_idea: respx for the wire format and error mapping, the in-memory mock for state."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ConflictError,
    ForbiddenError,
    LicenseRequiredError,
    NotConfiguredError,
    NotFoundError,
    ParameterError,
)
from mcbp_core.tools import TOOLS

IID = "6f1c2a3e-0000-4000-8000-000000000003"
URL = "http://test/ai/v1/ideas"
BODY = {"data": {"id": IID, "description": "Idea", "specification": None,
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
    assert await client.upsert_idea(description="Idea") == BODY
    request = route.calls.last.request
    assert "id" not in request.url.params
    assert json.loads(request.content) == {"description": "Idea"}
    await client.shutdown()


@respx.mock
async def test_update_passes_id_and_empty_specification_as_null():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    await client.upsert_idea(IID, specification="", deletion_mark=True)
    request = route.calls.last.request
    assert request.url.params["id"] == IID
    assert json.loads(request.content) == {"specification": None, "deletion_mark": True}
    await client.shutdown()


@pytest.mark.parametrize("status, code, exc", [
    (400, "BAD_PARAMETER", ParameterError),
    (403, "FORBIDDEN", ForbiddenError),
    (403, "LICENSE_REQUIRED", LicenseRequiredError),
    (404, "NOT_FOUND", NotFoundError),
    (409, "LOCKED", ConflictError),
    (422, "NOT_CONFIGURED", NotConfiguredError),
])
@respx.mock
async def test_errors_are_typed_and_post_is_not_retried(status, code, exc):
    route = respx.post(URL).mock(return_value=httpx.Response(
        status, json={"error": {"code": code, "message": "m"}}))
    client = await _live()
    with pytest.raises(exc):
        await client.upsert_idea(IID, description="x")
    assert route.call_count == 1
    await client.shutdown()


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_idea_id_is_rejected_not_a_create(blank):
    with pytest.raises(ParameterError, match="idea_id is blank"):
        await _mock_client().upsert_idea(blank, description="x")


async def test_mock_create_idempotent_update_then_mark():
    client = _mock_client()
    created = (await client.upsert_idea(description="Idea", specification="Why"))["data"]
    assert created["created"] is True and created["specification"] == "Why"
    iid = created["id"]
    same = (await client.upsert_idea(iid, description="Idea", specification="Why"))["data"]
    assert same["changed"] is False and same["created"] is False
    cleared = (await client.upsert_idea(iid, specification=""))["data"]
    assert cleared["specification"] is None and cleared["changed"] is True
    marked = (await client.upsert_idea(iid, deletion_mark=True))["data"]
    assert marked["deletion_mark"] is True and marked["description"] == "Idea"


@pytest.mark.parametrize("params, body, needle", [
    (None, {}, "description is required when creating an idea"),
    (None, {"description": "x" * 151}, "longer than 150"),
    (None, {"description": "x", "Description": "y"}, "Unknown field 'Description'"),
    (None, {"description": "x", "specification": 5}, "specification must be a string or null"),
    (None, {"description": "x", "deletion_mark": "yes"}, "deletion_mark must be true or false"),
    ({"id": " "}, {"description": "x"}, "id is blank"),
    ({"id": "nope"}, {"description": "x"}, "id must be a MCBP_Ideas item UUID"),
])
async def test_mock_validation(params, body, needle):
    with pytest.raises(ParameterError, match=needle):
        await _mock_client()._request("POST", "/ai/v1/ideas", params=params, json=body)


async def test_mock_author_is_set_on_create_and_kept_on_update():
    from mcbp_core.client import _MOCK_IDEAS

    iid = (await _mock_client().upsert_idea(description="Idea"))["data"]["id"]
    assert _MOCK_IDEAS[iid]["author"] == "u"
    other = MCBPClient(ConnectionConfig(user="someone-else", mock=True))
    await other.upsert_idea(iid, specification="Edited")
    assert _MOCK_IDEAS[iid]["author"] == "u"


async def test_mock_unknown_id_is_not_found():
    with pytest.raises(NotFoundError, match=IID):
        await _mock_client().upsert_idea(IID, description="x")


async def test_executor_maps_arguments():
    spec = TOOLS["upsert_idea"]
    assert set(spec.parameters["properties"]) == {
        "idea_id", "description", "specification", "deletion_mark"}
    client = _mock_client()
    created = await spec.executor(client, {"description": "Idea", "specification": "Why"})
    iid = created["data"]["id"]
    updated = await spec.executor(client, {"idea_id": iid, "deletion_mark": True})
    assert updated["data"]["deletion_mark"] is True and updated["data"]["specification"] == "Why"
