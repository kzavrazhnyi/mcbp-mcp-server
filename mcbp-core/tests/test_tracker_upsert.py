"""Project-tracker routes (upsert_project, upsert_bug, set_bug_status and the tracker fields of
upsert_function / upsert_idea): respx for the wire format and errors, the mock for state."""
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
from mcbp_core.tools import TOOLS, tool_specs

PID = "6f1c2a3e-0000-4000-8000-000000000010"
BID = "6f1c2a3e-0000-4000-8000-000000000011"
FID = "6f1c2a3e-0000-4000-8000-000000000012"
BASE = "http://test/ai/v1"
OK = {"data": {"id": PID, "created": True, "changed": True}}


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))
    await client.startup()
    return client


def _mock_client(user: str = "u") -> MCBPClient:
    return MCBPClient(ConnectionConfig(user=user, mock=True))


# --- wire format ---

@respx.mock
async def test_project_create_sends_type_and_only_passed_fields():
    route = respx.post(f"{BASE}/projects").mock(return_value=httpx.Response(200, json=OK))
    client = await _live()
    assert await client.upsert_project(type_="MCBP_Projects", description="P",
                                       is_folder=True) == OK
    request = route.calls.last.request
    assert request.url.params["type"] == "MCBP_Projects" and "id" not in request.url.params
    assert json.loads(request.content) == {"description": "P", "is_folder": True}
    await client.shutdown()


@respx.mock
async def test_project_update_sends_id_without_type_and_blank_parent_as_null():
    route = respx.post(f"{BASE}/projects").mock(return_value=httpx.Response(200, json=OK))
    client = await _live()
    await client.upsert_project(PID, parent_id="", deletion_mark=True)
    request = route.calls.last.request
    assert request.url.params["id"] == PID and "type" not in request.url.params
    assert json.loads(request.content) == {"parent_id": None, "deletion_mark": True}
    await client.shutdown()


@respx.mock
async def test_bug_create_and_update_clear_strings_as_null():
    route = respx.post(f"{BASE}/bugs").mock(return_value=httpx.Response(200, json=OK))
    client = await _live()
    await client.upsert_bug(description="B", project=f"MCBP_Projects:{PID}", function_id=FID)
    request = route.calls.last.request
    assert "id" not in request.url.params
    assert json.loads(request.content) == {"description": "B", "project": f"MCBP_Projects:{PID}",
                                           "function_id": FID}
    await client.shutdown()
    client = await _live()
    await client.upsert_bug(BID, project="", function_id="", details="", source="",
                            fixed_version="")
    request = route.calls.last.request
    assert request.url.params["id"] == BID
    assert json.loads(request.content) == {"project": None, "function_id": None,
                                           "details": None, "source": None,
                                           "fixed_version": None}
    await client.shutdown()


@respx.mock
async def test_bug_status_posts_to_the_status_route():
    route = respx.post(f"{BASE}/bugs/{BID}/status").mock(
        return_value=httpx.Response(200, json=OK))
    client = await _live()
    await client.set_bug_status(BID, status="Fixed", fixed_version="1.2")
    assert json.loads(route.calls.last.request.content) == {"status": "Fixed",
                                                            "fixed_version": "1.2"}
    await client.set_bug_status(BID, status="New")
    assert json.loads(route.calls.last.request.content) == {"status": "New"}
    await client.shutdown()


@respx.mock
async def test_function_and_idea_send_tracker_fields():
    fn = respx.post(f"{BASE}/functions").mock(return_value=httpx.Response(200, json=OK))
    idea = respx.post(f"{BASE}/ideas").mock(return_value=httpx.Response(200, json=OK))
    client = await _live()
    await client.upsert_function(FID, project=f"MCBP_ProjectsUT:{PID}", parent_id="",
                                 is_folder=False, documentation="")
    assert json.loads(fn.calls.last.request.content) == {
        "project": f"MCBP_ProjectsUT:{PID}", "parent_id": None, "is_folder": False,
        "documentation": None}
    await client.upsert_idea(description="I", project="")
    assert json.loads(idea.calls.last.request.content) == {"description": "I", "project": None}
    await client.shutdown()


@pytest.mark.parametrize("status, code, exc", [
    (400, "BAD_PARAMETER", ParameterError),
    (403, "FORBIDDEN", ForbiddenError),
    (403, "LICENSE_REQUIRED", LicenseRequiredError),
    (404, "NOT_FOUND", NotFoundError),
    (409, "LOCKED", ConflictError),
    (400, "NOT_CONFIGURED", NotConfiguredError),
])
@pytest.mark.parametrize("path, call", [
    ("/projects", lambda c: c.upsert_project(PID, description="x")),
    ("/bugs", lambda c: c.upsert_bug(BID, description="x")),
    (f"/bugs/{BID}/status", lambda c: c.set_bug_status(BID, status="New")),
])
@respx.mock
async def test_errors_are_typed_and_post_is_not_retried(path, call, status, code, exc):
    route = respx.post(f"{BASE}{path}").mock(return_value=httpx.Response(
        status, json={"error": {"code": code, "message": "m"}}))
    client = await _live()
    with pytest.raises(exc):
        await call(client)
    assert route.call_count == 1
    await client.shutdown()


@pytest.mark.parametrize("call, needle", [
    (lambda c: c.upsert_project(" ", description="x"), "project_id is blank"),
    (lambda c: c.upsert_project(type_=" ", description="x"), "type is blank"),
    (lambda c: c.upsert_bug("", description="x"), "bug_id is blank"),
])
async def test_blank_ids_are_rejected_client_side(call, needle):
    with pytest.raises(ParameterError, match=needle):
        await call(_mock_client())


# --- mock state ---

async def test_mock_project_create_idempotent_folder_and_parent():
    client = _mock_client()
    folder = (await client.upsert_project(type_="Catalog.MCBP_Projects", description="F",
                                          is_folder=True))["data"]
    assert folder["created"] is True and folder["is_folder"] is True
    assert folder["project"] == f"MCBP_Projects:{folder['id']}"
    item = (await client.upsert_project(type_="MCBP_Projects", description="P",
                                        parent_id=folder["id"]))["data"]
    assert item["parent_id"] == folder["id"]
    same = (await client.upsert_project(item["id"], description="P"))["data"]
    assert same["changed"] is False and same["created"] is False
    top = (await client.upsert_project(item["id"], parent_id=""))["data"]
    assert top["parent_id"] is None and top["changed"] is True


@pytest.mark.parametrize("kwargs, exc, needle", [
    ({"description": "x"}, ParameterError, "type is required"),
    ({"type_": "Nope", "description": "x"}, ParameterError, "Unknown project type"),
    ({"type_": "MCBP_Projects"}, ParameterError, "description is required"),
    ({"type_": "MCBP_Projects", "description": "x", "parent_id": PID}, NotFoundError, PID),
])
async def test_mock_project_validation(kwargs, exc, needle):
    with pytest.raises(exc, match=needle):
        await _mock_client().upsert_project(**kwargs)


async def test_mock_project_parent_rules():
    client = _mock_client()
    item = (await client.upsert_project(type_="MCBP_Projects", description="I"))["data"]
    with pytest.raises(ParameterError, match="is an item, not a folder"):
        await client.upsert_project(type_="MCBP_Projects", description="x", parent_id=item["id"])
    folder = (await client.upsert_project(type_="MCBP_Projects", description="F",
                                          is_folder=True))["data"]
    with pytest.raises(ParameterError, match="itself or one of its descendants"):
        await client.upsert_project(folder["id"], parent_id=folder["id"])
    with pytest.raises(ParameterError, match="only be set on create"):
        await client.upsert_project(item["id"], is_folder=True)
    other = (await client.upsert_project(type_="MCBP_ProjectsUT", description="U",
                                         is_folder=True))["data"]
    with pytest.raises(NotFoundError):
        await client.upsert_project(item["id"], parent_id=other["id"])


async def test_mock_bug_create_status_and_author():
    from mcbp_core.client import _MOCK_BUGS

    client = _mock_client()
    project = (await client.upsert_project(type_="MCBP_ProjectsWF", description="P"))["data"]
    fn = (await client.upsert_function(description="F"))["data"]
    bug = (await client.upsert_bug(description="B", project=project["project"],
                                   function_id=fn["id"], details="steps"))["data"]
    assert bug["created"] is True and bug["status"] == "New"
    assert bug["project"] == project["project"] and bug["function_id"] == fn["id"]
    assert bug["modified_date"] and "author" not in bug
    assert _MOCK_BUGS[bug["id"]]["author"] == "u"
    same = (await _mock_client("other").upsert_bug(bug["id"], description="B"))["data"]
    assert same["changed"] is False and _MOCK_BUGS[bug["id"]]["author"] == "u"
    cleared = (await client.upsert_bug(bug["id"], project="", details=""))["data"]
    assert cleared["project"] is None and cleared["details"] is None

    fixed = (await client.set_bug_status(bug["id"], status="fixed", fixed_version="1.0"))["data"]
    assert fixed["status"] == "Fixed" and fixed["fixed_version"] == "1.0"
    assert fixed["created"] is False and fixed["changed"] is True
    again = (await client.set_bug_status(bug["id"], status="Fixed"))["data"]
    assert again["changed"] is False and again["fixed_version"] == "1.0"


@pytest.mark.parametrize("kwargs, exc, needle", [
    ({}, ParameterError, "description is required when creating a bug"),
    ({"description": "x", "project": PID}, ParameterError, "TypeName:UUID"),
    ({"description": "x", "project": f"MCBP_Projects:{PID}"}, NotFoundError, PID),
    ({"description": "x", "function_id": FID}, NotFoundError, FID),
    ({"description": "x", "function_id": "nope"}, ParameterError, "function_id must be"),
])
async def test_mock_bug_validation(kwargs, exc, needle):
    with pytest.raises(exc, match=needle):
        await _mock_client().upsert_bug(**kwargs)


async def test_mock_bug_unknown_field_and_status():
    client = _mock_client()
    with pytest.raises(ParameterError, match="Unknown field 'status'"):
        await client._request("POST", "/ai/v1/bugs", json={"description": "x", "status": "New"})
    bug = (await client.upsert_bug(description="B"))["data"]
    with pytest.raises(ParameterError, match="New, Confirmed, Fixed, Verified, Rejected"):
        await client.set_bug_status(bug["id"], status="Done")
    with pytest.raises(NotFoundError, match=BID):
        await client.set_bug_status(BID, status="New")


async def test_mock_function_tracker_fields():
    client = _mock_client()
    project = (await client.upsert_project(type_="MCBP_Projects", description="P"))["data"]
    folder = (await client.upsert_function(description="G", is_folder=True))["data"]
    assert folder["is_folder"] is True and folder["parent_id"] is None
    fn = (await client.upsert_function(description="F", parent_id=folder["id"],
                                       project=project["project"],
                                       documentation="Doc"))["data"]
    assert fn["parent_id"] == folder["id"] and fn["project"] == project["project"]
    assert fn["documentation"] == "Doc" and fn["is_folder"] is False
    same = (await client.upsert_function(fn["id"], documentation="Doc"))["data"]
    assert same["changed"] is False
    cleared = (await client.upsert_function(fn["id"], documentation="", project="",
                                            parent_id=""))["data"]
    assert cleared["documentation"] is None and cleared["project"] is None
    assert cleared["parent_id"] is None
    with pytest.raises(ParameterError, match="only be set on create"):
        await client.upsert_function(fn["id"], is_folder=True)


async def test_mock_idea_project():
    client = _mock_client()
    project = (await client.upsert_project(type_="MCBP_ProjectsUT", description="P"))["data"]
    idea = (await client.upsert_idea(description="I", project=project["project"]))["data"]
    assert idea["project"] == project["project"]
    cleared = (await client.upsert_idea(idea["id"], project=""))["data"]
    assert cleared["project"] is None and cleared["changed"] is True


# --- registry ---

@pytest.mark.parametrize("name, required", [
    ("upsert_project", []),
    ("upsert_bug", []),
    ("set_bug_status", ["bug_id", "status"]),
])
def test_new_tools_are_write_only_and_mcp_only(name, required):
    spec = TOOLS[name]
    assert spec.read_only is False
    assert spec.surfaces == frozenset({"mcp"})
    assert spec.parameters["required"] == required
    assert spec.description_en
    assert name not in {s.name for s in tool_specs(surface="backend", include_write=True)}
    assert name not in {s.name for s in tool_specs(surface="mcp", include_write=False)}
    assert name in {s.name for s in tool_specs(surface="mcp", include_write=True)}


async def test_executors_map_arguments():
    client = _mock_client()
    project = await TOOLS["upsert_project"].executor(
        client, {"type": "MCBP_Projects", "description": "P"})
    ref = project["data"]["project"]
    fn = await TOOLS["upsert_function"].executor(
        client, {"description": "F", "project": ref, "documentation": "D"})
    assert fn["data"]["project"] == ref and fn["data"]["documentation"] == "D"
    bug = await TOOLS["upsert_bug"].executor(
        client, {"description": "B", "project": ref, "function_id": fn["data"]["id"],
                 "source": "chat"})
    assert bug["data"]["source"] == "chat"
    status = await TOOLS["set_bug_status"].executor(
        client, {"bug_id": bug["data"]["id"], "status": "Rejected"})
    assert status["data"]["status"] == "Rejected"
    assert set(TOOLS["upsert_bug"].parameters["properties"]) == {
        "bug_id", "description", "project", "function_id", "details", "source",
        "fixed_version", "deletion_mark"}
    assert set(TOOLS["upsert_project"].parameters["properties"]) == {
        "type", "project_id", "description", "parent_id", "is_folder", "deletion_mark"}
