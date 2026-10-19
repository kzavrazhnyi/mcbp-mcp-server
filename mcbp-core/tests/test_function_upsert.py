"""upsert_function: respx for the wire format and error mapping, the in-memory mock for state."""
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

FID = "6f1c2a3e-0000-4000-8000-000000000002"
URL = "http://test/ai/v1/functions"
BODY = {"data": {"id": FID, "description": "Probe", "functions_type": "Basic",
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
    assert await client.upsert_function(description="Probe") == BODY
    request = route.calls.last.request
    assert "id" not in request.url.params
    assert json.loads(request.content) == {"description": "Probe"}
    await client.shutdown()


@respx.mock
async def test_update_passes_id_as_query_and_empty_type_as_null():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    await client.upsert_function(FID, functions_type="", deletion_mark=True)
    request = route.calls.last.request
    assert request.url.params["id"] == FID
    assert json.loads(request.content) == {"functions_type": None, "deletion_mark": True}
    await client.shutdown()


@respx.mock
async def test_errors_are_typed_and_post_is_not_retried():
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "no function"}}),
        httpx.Response(400, json={"error": {"code": "NOT_CONFIGURED", "message": "no catalog"}}),
        httpx.Response(400, json={"error": {"code": "BAD_PARAMETER",
                                            "message": "one of: Basic, Common"}}),
        httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "no right"}}),
        httpx.Response(403, json={"error": {"code": "LICENSE_REQUIRED", "message": "licence"}}),
        httpx.Response(409, json={"error": {"code": "LOCKED", "message": "locked"}}),
        httpx.Response(503, text="busy"),
    ])
    client = await _live()
    with pytest.raises(NotFoundError):
        await client.upsert_function(FID, description="x")
    with pytest.raises(NotConfiguredError):
        await client.upsert_function(description="x")
    with pytest.raises(ParameterError) as exc:
        await client.upsert_function(description="x", functions_type="Nope")
    assert "Basic" in exc.value.message
    with pytest.raises(ForbiddenError):
        await client.upsert_function(description="x")
    with pytest.raises(LicenseRequiredError):
        await client.upsert_function(description="x")
    with pytest.raises(ConflictError):
        await client.upsert_function(FID, description="x")
    with pytest.raises(Exception):  # noqa: B017 - only the call count matters here
        await client.upsert_function(description="x")
    assert route.call_count == 7
    await client.shutdown()


async def test_mock_create_then_idempotent_update_then_mark():
    client = _mock_client()
    first = (await client.upsert_function(description=" Probe ", functions_type="basic"))["data"]
    assert first["description"] == "Probe" and first["functions_type"] == "Basic"
    assert first["created"] is True and first["changed"] is True
    assert first["deletion_mark"] is False
    fid = first["id"]
    again = (await client.upsert_function(fid, description="Probe",
                                          functions_type="Basic"))["data"]
    assert again["id"] == fid and again["created"] is False and again["changed"] is False
    marked = (await client.upsert_function(fid.upper(), deletion_mark=True))["data"]
    assert marked["changed"] is True and marked["deletion_mark"] is True
    assert marked["description"] == "Probe" and marked["functions_type"] == "Basic"
    cleared = (await client.upsert_function(fid, functions_type=""))["data"]
    assert cleared["functions_type"] is None and cleared["changed"] is True


async def test_mock_create_twice_makes_two_items():
    client = _mock_client()
    one = (await client.upsert_function(description="Same"))["data"]["id"]
    two = (await client.upsert_function(description="Same"))["data"]["id"]
    assert one != two


@pytest.mark.parametrize("function_id, kwargs, needle", [
    (None, {}, "description is required"),
    (None, {"description": "  "}, "non-empty"),
    (None, {"description": "x" * 151}, "150"),
    (None, {"description": "x", "functions_type": "Nope"}, "ArtificialIntelligence"),
    (None, {"description": "x", "deletion_mark": "yes"}, "deletion_mark"),
    ("not-a-uuid", {"description": "x"}, "UUID"),
])
async def test_mock_validation(function_id, kwargs, needle):
    with pytest.raises(ParameterError) as exc:
        await _mock_client().upsert_function(function_id, **kwargs)
    assert needle in exc.value.message


async def test_mock_unknown_id_is_not_found():
    with pytest.raises(NotFoundError):
        await _mock_client().upsert_function(FID, description="x")


async def test_mock_rejects_unknown_body_key():
    with pytest.raises(ParameterError) as exc:
        await _mock_client()._request("POST", "/ai/v1/functions",
                                      json={"description": "x", "Ideas": []})
    assert "Unknown field 'Ideas'" in exc.value.message


async def test_executor_maps_arguments():
    created = await TOOLS["upsert_function"].executor(
        _mock_client(), {"description": "Probe", "functions_type": "Common"},
    )
    fid = created["data"]["id"]
    updated = await TOOLS["upsert_function"].executor(
        _mock_client(), {"function_id": fid, "deletion_mark": True},
    )
    assert updated["data"]["deletion_mark"] is True
    assert updated["data"]["functions_type"] == "Common"


@respx.mock
@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_function_id_is_rejected_not_a_create(blank):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    with pytest.raises(ParameterError, match="blank"):
        await client.upsert_function(blank, description="x")
    with pytest.raises(ParameterError, match="blank"):
        await TOOLS["upsert_function"].executor(client, {"function_id": blank, "description": "x"})
    assert route.call_count == 0
    await client.shutdown()


@pytest.mark.parametrize("blank", ["", "   "])
async def test_mock_blank_id_query_is_rejected_like_bsl(blank):
    client = _mock_client()
    with pytest.raises(ParameterError, match="blank"):
        await client._request("POST", "/ai/v1/functions", params={"id": blank},
                              json={"description": "x"})


@respx.mock
async def test_specification_is_sent_and_empty_clears_as_null():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    await client.upsert_function(description="Probe", specification="Must export orders")
    assert json.loads(route.calls.last.request.content) == {
        "description": "Probe", "specification": "Must export orders",
    }
    await client.upsert_function(FID, specification="")
    assert json.loads(route.calls.last.request.content) == {"specification": None}
    await client.upsert_function(FID, description="Probe")
    assert "specification" not in json.loads(route.calls.last.request.content)
    await client.shutdown()


async def test_mock_specification_set_idempotent_then_cleared():
    client = _mock_client()
    created = (await client.upsert_function(description="Probe"))["data"]
    assert created["specification"] is None
    fid = created["id"]
    first = (await client.upsert_function(fid, specification="Spec v1"))["data"]
    assert first["specification"] == "Spec v1" and first["changed"] is True
    again = (await client.upsert_function(fid, specification="Spec v1"))["data"]
    assert again["changed"] is False
    kept = (await client.upsert_function(fid, description="Renamed"))["data"]
    assert kept["specification"] == "Spec v1"
    cleared = (await client.upsert_function(fid, specification=""))["data"]
    assert cleared["specification"] is None and cleared["changed"] is True
    with_create = (await client.upsert_function(description="New", specification="Do X"))["data"]
    assert with_create["specification"] == "Do X" and with_create["created"] is True


async def test_mock_specification_must_be_string_or_null():
    with pytest.raises(ParameterError, match="specification must be a string or null"):
        await _mock_client()._request("POST", "/ai/v1/functions",
                                      json={"description": "x", "specification": 5})


async def test_executor_passes_specification_and_schema_lists_it():
    spec = TOOLS["upsert_function"]
    assert "specification" in spec.parameters["properties"]
    created = await spec.executor(
        _mock_client(), {"description": "Probe", "specification": "Must do Y"},
    )
    assert created["data"]["specification"] == "Must do Y"


@respx.mock
async def test_ideas_are_sent_as_full_list_and_empty_list_clears():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    client = await _live()
    await client.upsert_function(FID, ideas=["a", "b"])
    assert json.loads(route.calls.last.request.content) == {"ideas": ["a", "b"]}
    await client.upsert_function(FID, ideas=[])
    assert json.loads(route.calls.last.request.content) == {"ideas": []}
    await client.upsert_function(FID, description="X")
    assert "ideas" not in json.loads(route.calls.last.request.content)
    await client.shutdown()


async def _two_ideas(client: MCBPClient) -> tuple[str, str]:
    one = (await client.upsert_idea(description="One", specification="Text one"))["data"]["id"]
    two = (await client.upsert_idea(description="Two"))["data"]["id"]
    return one, two


async def test_mock_ideas_replace_dedup_reorder_and_clear():
    client = _mock_client()
    one, two = await _two_ideas(client)
    fid = (await client.upsert_function(description="Fn"))["data"]["id"]
    linked = (await client.upsert_function(fid, ideas=[one, two, one.upper()]))["data"]
    assert linked["ideas"] == [one, two] and linked["changed"] is True
    assert linked["specification"] == "Text one"
    again = (await client.upsert_function(fid, ideas=[one, two]))["data"]
    assert again["changed"] is False
    swapped = (await client.upsert_function(fid, ideas=[two, one]))["data"]
    assert swapped["ideas"] == [two, one] and swapped["changed"] is True
    assert swapped["specification"] is None
    cleared = (await client.upsert_function(fid, ideas=[]))["data"]
    assert cleared["ideas"] == [] and cleared["changed"] is True


async def test_mock_ideas_unknown_uuid_is_not_found_naming_it():
    client = _mock_client()
    fid = (await client.upsert_function(description="Fn"))["data"]["id"]
    ghost = "6f1c2a3e-0000-4000-8000-0000000000ff"
    with pytest.raises(NotFoundError, match=ghost):
        await client.upsert_function(fid, ideas=[ghost])


@pytest.mark.parametrize("ideas, needle", [
    ("not-a-list", "ideas must be an array"),
    (["nope"], "ideas must hold MCBP_Ideas item UUIDs - got 'nope'"),
    ([5], "ideas must hold MCBP_Ideas item UUIDs"),
])
async def test_mock_ideas_validation(ideas, needle):
    with pytest.raises(ParameterError, match=needle):
        await _mock_client()._request("POST", "/ai/v1/functions",
                                      json={"description": "x", "ideas": ideas})


async def test_mock_ideas_applied_before_specification():
    client = _mock_client()
    one, two = await _two_ideas(client)
    fid = (await client.upsert_function(description="Fn", ideas=[one]))["data"]["id"]
    result = (await client.upsert_function(fid, ideas=[two, one], specification="New"))["data"]
    assert result["ideas"] == [two, one] and result["specification"] == "New"
    back = (await client.upsert_function(fid, ideas=[one]))["data"]
    assert back["specification"] == "Text one"


async def test_executor_passes_ideas_and_schema_lists_it():
    spec = TOOLS["upsert_function"]
    assert spec.parameters["properties"]["ideas"]["type"] == "array"
    client = _mock_client()
    one, _ = await _two_ideas(client)
    created = await spec.executor(client, {"description": "Fn", "ideas": [one]})
    assert created["data"]["ideas"] == [one]
