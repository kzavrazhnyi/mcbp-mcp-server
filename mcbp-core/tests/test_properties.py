"""Additional attributes (`/ai/v1/properties`, contract 4.27-4.29): respx for the wire format and
error mapping, a recording fake for the executors, the registry and masking of the new shapes."""
from __future__ import annotations

import json
import logging
from urllib.parse import unquote

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
from mcbp_core.masking import DEFAULT_POLICY, Masker
from mcbp_core.tools import TOOLS, tool_specs

BASE = "http://test/ai/v1"
SET_ID = "7dbf0eb7-6360-4a32-91b3-85698ca935cf"
PROP_ID = "5b0e1c7a-0000-4000-8000-000000000001"
OWNER_ID = "e3f1d9b6-e1cb-11df-8e61-0013775f0e4a"
VALUES_URL = f"{BASE}/properties/values/Catalogs/Контрагенты/{OWNER_ID}"
NEW_TOOLS = ["list_additional_attributes", "upsert_additional_attribute",
             "set_additional_attribute_values"]


async def _live() -> MCBPClient:
    client = MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p",
                                         mock=False))
    await client.startup()
    return client


def _err(status: int, code: str, message: str = "m") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


def _path(request: httpx.Request) -> str:
    return unquote(request.url.path)


# --- client: wire format ---------------------------------------------------------------------

@respx.mock
async def test_list_properties_is_a_get_with_only_the_passed_filters():
    route = respx.get(f"{BASE}/properties").mock(
        return_value=httpx.Response(200, json={"data": [], "cursor": None}))
    client = await _live()
    try:
        await client.list_properties(owner="Catalog.Контрагенты", q="Кол",
                                     include_deleted=True, limit=10)
        params = route.calls.last.request.url.params
        assert dict(params) == {"owner": "Catalog.Контрагенты", "q": "Кол",
                                "include_deleted": "true", "limit": "10"}
        await client.list_properties(set=SET_ID.upper())
        assert dict(route.calls.last.request.url.params) == {"set": SET_ID}
    finally:
        await client.shutdown()


@respx.mock
async def test_get_property_and_values_paths():
    card = respx.get(f"{BASE}/properties/{PROP_ID}").mock(
        return_value=httpx.Response(200, json={"id": PROP_ID}))
    values = respx.get(url__startswith=f"{BASE}/properties/values/").mock(
        return_value=httpx.Response(200, json={"data": [], "available": []}))
    client = await _live()
    try:
        await client.get_property(PROP_ID)
        await client.get_property_values("Catalogs", "Контрагенты", OWNER_ID)
        assert card.call_count == 1
        assert _path(values.calls.last.request) == (
            f"/ai/v1/properties/values/Catalogs/Контрагенты/{OWNER_ID}")
    finally:
        await client.shutdown()


@respx.mock
async def test_create_property_posts_the_body():
    route = respx.post(f"{BASE}/properties").mock(return_value=httpx.Response(
        201, json={"id": PROP_ID, "created": True}))
    client = await _live()
    try:
        await client.create_property(" Колір ", ["String"], SET_ID, required=True,
                                     comment=None)
        request = route.calls.last.request
        assert request.method == "POST"
        assert json.loads(request.content) == {"title": "Колір", "type": ["String"],
                                               "set": SET_ID, "required": True}
    finally:
        await client.shutdown()


@respx.mock
async def test_patch_property_sends_only_passed_fields():
    route = respx.patch(f"{BASE}/properties/{PROP_ID}").mock(
        return_value=httpx.Response(200, json={"id": PROP_ID, "changed": []}))
    client = await _live()
    try:
        await client.patch_property(PROP_ID, title="Колір товару", deletion_mark=True,
                                    multiline=None)
        assert json.loads(route.calls.last.request.content) == {
            "title": "Колір товару", "deletion_mark": True}
    finally:
        await client.shutdown()


VALUE_ID = "5b0e1c7a-0000-4000-8000-000000000002"
VALUES_ADDED = [{"id": VALUE_ID, "title": "Червоний", "created": True}]


@respx.mock
async def test_add_values_reaches_the_post_and_patch_bodies():
    post = respx.post(f"{BASE}/properties").mock(return_value=httpx.Response(
        201, json={"id": PROP_ID, "created": True, "values_added": VALUES_ADDED}))
    patch = respx.patch(f"{BASE}/properties/{PROP_ID}").mock(return_value=httpx.Response(
        200, json={"id": PROP_ID, "changed": [], "values_added": VALUES_ADDED}))
    client = await _live()
    try:
        created = await TOOLS["upsert_additional_attribute"].executor(client, {
            "title": "Колір", "type": ["ExtValues"], "set": SET_ID,
            "add_values": [" Червоний ", "Синій"]})
        assert json.loads(post.calls.last.request.content) == {
            "title": "Колір", "type": ["ExtValues"], "set": SET_ID,
            "add_values": ["Червоний", "Синій"]}
        assert created["values_added"] == VALUES_ADDED
        changed = await TOOLS["upsert_additional_attribute"].executor(
            client, {"id": PROP_ID, "add_values": ["Червоний"]})
        assert json.loads(patch.calls.last.request.content) == {"add_values": ["Червоний"]}
        assert changed == {"id": PROP_ID, "changed": [], "values_added": VALUES_ADDED}
    finally:
        await client.shutdown()


@respx.mock
async def test_patch_property_values_body():
    route = respx.patch(url__startswith=f"{BASE}/properties/values/").mock(
        return_value=httpx.Response(200, json={"id": OWNER_ID, "changed": []}))
    client = await _live()
    try:
        await client.patch_property_values(
            "Catalogs", "Контрагенты", OWNER_ID,
            set=[{"property": "Колір", "value": "Червоний"}], remove=["Розмір"])
        request = route.calls.last.request
        assert request.method == "PATCH"
        assert _path(request).endswith(f"/Catalogs/Контрагенты/{OWNER_ID}")
        assert json.loads(request.content) == {
            "set": [{"property": "Колір", "value": "Червоний"}], "remove": ["Розмір"]}
    finally:
        await client.shutdown()


# --- client: no retry on non-idempotent writes, retry on the idempotent PATCH ------------------

@respx.mock
async def test_create_property_is_not_retried():
    route = respx.post(f"{BASE}/properties").mock(return_value=httpx.Response(503))
    client = await _live()
    try:
        with pytest.raises(Exception):  # noqa: B017 - only the call count matters
            await client.create_property("Колір", ["String"], SET_ID)
        assert route.call_count == 1
    finally:
        await client.shutdown()


@respx.mock
async def test_patch_property_values_is_not_retried():
    route = respx.patch(url__startswith=f"{BASE}/properties/values/").mock(
        return_value=httpx.Response(503))
    client = await _live()
    try:
        with pytest.raises(Exception):  # noqa: B017 - only the call count matters
            await client.patch_property_values("Catalogs", "Контрагенты", OWNER_ID,
                                               remove=["Колір"])
        assert route.call_count == 1
    finally:
        await client.shutdown()


@respx.mock
async def test_patch_property_is_retried_after_a_503():
    route = respx.patch(f"{BASE}/properties/{PROP_ID}").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json={"id": PROP_ID, "changed": []})])
    client = await _live()
    try:
        assert (await client.patch_property(PROP_ID, title="X"))["id"] == PROP_ID
        assert route.call_count == 2
    finally:
        await client.shutdown()


# --- client: request bodies never reach the log ----------------------------------------------

SECRET = "S3cret-value-2026"


@respx.mock
async def test_property_bodies_are_logged_as_shapes_only(caplog):
    respx.patch(url__startswith=f"{BASE}/properties/values/").mock(
        return_value=httpx.Response(422, json={"error": {"code": "WRITE_FAILED",
                                                         "message": "failed"}}))
    respx.post(f"{BASE}/properties").mock(return_value=httpx.Response(201, json={}))
    respx.patch(f"{BASE}/properties/{PROP_ID}").mock(return_value=httpx.Response(200, json={}))
    client = await _live()
    caplog.set_level(logging.DEBUG, logger="mcbp.api")
    try:
        with pytest.raises(WriteFailedError):
            await client.patch_property_values(
                "Catalogs", "Контрагенты", OWNER_ID,
                set=[{"property": "Пароль", "value": SECRET}])
        await client.create_property(SECRET, ["String"], SET_ID, comment=SECRET)
        await client.patch_property(PROP_ID, title=SECRET, tooltip=SECRET)
    finally:
        await client.shutdown()
    assert caplog.records, "the requests were not logged at all"
    assert SECRET not in caplog.text
    assert "body_keys=" in caplog.text


async def test_mock_mode_logs_property_bodies_as_shapes_only(caplog):
    client = MCBPClient(ConnectionConfig(user="u", mock=True))
    caplog.set_level(logging.DEBUG, logger="mcbp.api")
    await client.patch_property_values("Catalogs", "Контрагенты", OWNER_ID,
                                       set=[{"property": "Пароль", "value": SECRET}])
    assert caplog.records and SECRET not in caplog.text


# --- client: error mapping --------------------------------------------------------------------

@pytest.mark.parametrize("code", ["BAD_TYPE", "PROPERTY_NOT_IN_SET", "PROPERTY_IN_USE",
                                  "AMBIGUOUS", "NOT_SUPPORTED"])
@respx.mock
async def test_422_property_codes_are_parameter_errors_with_the_code_kept(code):
    respx.post(f"{BASE}/properties").mock(return_value=_err(422, code, "why: X, Y"))
    client = await _live()
    try:
        with pytest.raises(ParameterError) as exc:
            await client.create_property("Колір", ["String"], SET_ID)
        assert exc.value.code == code
        assert exc.value.http_status == 422
        assert "X, Y" in exc.value.message
    finally:
        await client.shutdown()


@pytest.mark.parametrize(("status", "code", "error"), [
    (422, "NOT_CONFIGURED", NotConfiguredError),
    (422, "WRITE_FAILED", WriteFailedError),
    (403, "FORBIDDEN", ForbiddenError),
    (403, "LICENSE_REQUIRED", LicenseRequiredError),
    (409, "LOCKED", LockedError),
    (404, "NOT_FOUND", NotFoundError),
])
@respx.mock
async def test_existing_codes_map_on_the_properties_routes(status, code, error):
    respx.patch(f"{BASE}/properties/{PROP_ID}").mock(return_value=_err(status, code))
    client = await _live()
    try:
        with pytest.raises(error):
            await client.patch_property(PROP_ID, title="X")
    finally:
        await client.shutdown()


# --- client: validation before HTTP -----------------------------------------------------------

async def test_client_validates_before_any_request():
    client = MCBPClient(ConnectionConfig(user="u", mock=True))
    with pytest.raises(ParameterError):
        await client.create_property("  ", ["String"], SET_ID)
    with pytest.raises(ParameterError):
        await client.create_property("Колір", ["String"], "not-a-uuid")
    with pytest.raises(ParameterError):
        await client.get_property("abc")
    with pytest.raises(ParameterError):
        await client.patch_property(PROP_ID)
    with pytest.raises(ParameterError):
        await client.patch_property_values("Catalogs", "Контрагенты", OWNER_ID)


async def test_mock_mode_answers_every_route():
    client = MCBPClient(ConnectionConfig(user="u", mock=True))
    assert (await client.list_properties())["data"][0]["types"] == ["String"]
    assert (await client.create_property("Колір", ["String"], SET_ID))["created"] is True
    assert (await client.get_property(PROP_ID))["id"] == PROP_ID
    changed = await client.patch_property(PROP_ID, title="Новий")
    assert changed["changed"][0]["field"] == "title"
    values = await client.get_property_values("Catalogs", "Контрагенты", OWNER_ID)
    assert values["available"] and values["data"][0]["property"] == PROP_ID
    out = await client.patch_property_values("Catalogs", "Контрагенты", OWNER_ID,
                                             set=[{"property": "Колір", "value": "Синій"}])
    assert out["changed"][0]["new"] == "Синій"
    with pytest.raises(ParameterError) as exc:
        await client.list_properties(kind="info")
    assert exc.value.code == "NOT_SUPPORTED"


# --- executors --------------------------------------------------------------------------------

class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"ok": True}
        return method


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


async def test_list_tool_modes(client):
    await TOOLS["list_additional_attributes"].executor(client, {"owner": "Catalog.Контрагенты"})
    assert client.calls[-1] == ("list_properties", (), {
        "set": None, "owner": "Catalog.Контрагенты", "q": None, "include_deleted": False,
        "limit": 100, "cursor": None})
    await TOOLS["list_additional_attributes"].executor(client, {"id": PROP_ID})
    assert client.calls[-1] == ("get_property", (PROP_ID,), {})
    await TOOLS["list_additional_attributes"].executor(
        client, {"metadata": "Catalogs", "type": "Контрагенты", "object_id": OWNER_ID})
    assert client.calls[-1] == ("get_property_values", ("Catalogs", "Контрагенты", OWNER_ID), {})


async def test_list_tool_object_id_needs_the_owner_type(client):
    with pytest.raises(ParameterError):
        await TOOLS["list_additional_attributes"].executor(client, {"object_id": OWNER_ID})
    assert client.calls == []


@pytest.mark.parametrize("args", [
    {"metadata": "Catalogs", "type": "Контрагенты"},
    {"type": "Контрагенты"},
    {"metadata": "Catalogs", "type": "Контрагенты", "q": "Кол"},
])
async def test_list_tool_owner_type_without_object_id_points_to_owner(client, args):
    with pytest.raises(ParameterError) as exc:
        await TOOLS["list_additional_attributes"].executor(client, args)
    assert "owner" in exc.value.message and "object_id" in exc.value.message
    assert client.calls == []


@pytest.mark.parametrize("args", [
    {"id": PROP_ID, "metadata": "Catalogs", "type": "Контрагенты", "object_id": OWNER_ID},
    {"id": PROP_ID, "owner": "Catalog.Контрагенты"},
    {"metadata": "Catalogs", "type": "Контрагенты", "object_id": OWNER_ID, "set": SET_ID},
])
async def test_list_tool_refuses_mixed_modes(client, args):
    with pytest.raises(ParameterError) as exc:
        await TOOLS["list_additional_attributes"].executor(client, args)
    assert "one mode" in exc.value.message
    assert client.calls == []


async def test_upsert_without_id_creates_and_wraps_a_lone_type(client):
    await TOOLS["upsert_additional_attribute"].executor(
        client, {"title": "Колір", "type": "String", "set": SET_ID, "required": True})
    assert client.calls[-1] == ("create_property", ("Колір", ["String"], SET_ID),
                                {"required": True})


async def test_upsert_with_id_patches_only_passed_fields(client):
    await TOOLS["upsert_additional_attribute"].executor(
        client, {"id": PROP_ID, "deletion_mark": True, "type": ["CatalogRef.Номенклатура"]})
    assert client.calls[-1] == ("patch_property", (PROP_ID,), {
        "deletion_mark": True, "type": ["CatalogRef.Номенклатура"]})


async def test_upsert_wraps_a_lone_add_value_on_both_paths(client):
    await TOOLS["upsert_additional_attribute"].executor(
        client, {"title": "Колір", "type": "ExtValues", "set": SET_ID, "add_values": "Червоний"})
    assert client.calls[-1] == ("create_property", ("Колір", ["ExtValues"], SET_ID),
                                {"add_values": ["Червоний"]})
    await TOOLS["upsert_additional_attribute"].executor(
        client, {"id": PROP_ID, "title": "Колір", "add_values": ["Синій"]})
    assert client.calls[-1] == ("patch_property", (PROP_ID,), {
        "title": "Колір", "add_values": ["Синій"]})


@pytest.mark.parametrize("args", [
    {"title": "", "type": ["String"], "set": SET_ID},
    {"title": "   ", "type": ["String"], "set": SET_ID},
    {"title": "Колір", "type": ["String"], "set": "not-a-uuid"},
    {"title": "Колір", "set": SET_ID},
    {"title": "Колір", "type": [], "set": SET_ID},
    {"title": "Колір", "type": ["String"]},
    {"title": "Колір", "type": ["String"], "set": SET_ID, "deletion_mark": True},
    {"id": PROP_ID},
    {"id": PROP_ID, "set": "abc"},
    {"id": "abc", "title": "X"},
    {"id": PROP_ID, "add_values": []},
    {"id": PROP_ID, "add_values": ["Червоний", " "]},
    {"id": PROP_ID, "add_values": [1]},
    {"id": PROP_ID, "add_values": {"a": "b"}},
    {"title": "Колір", "type": ["ExtValues"], "set": SET_ID, "add_values": [""]},
])
async def test_upsert_rejects_bad_arguments_before_any_call(client, args):
    with pytest.raises(ParameterError):
        await TOOLS["upsert_additional_attribute"].executor(client, args)
    assert client.calls == []


async def test_set_values_turns_the_map_into_rows(client):
    await TOOLS["set_additional_attribute_values"].executor(client, {
        "metadata": "Catalogs", "type": "Контрагенты", "id": OWNER_ID,
        "set": {"Колір": "Червоний", PROP_ID: None}, "remove": ["Розмір"]})
    assert client.calls[-1] == ("patch_property_values", ("Catalogs", "Контрагенты", OWNER_ID), {
        "set": [{"property": "Колір", "value": "Червоний"}, {"property": PROP_ID, "value": None}],
        "remove": ["Розмір"]})


@pytest.mark.parametrize("extra", [
    {},
    {"set": {}, "remove": []},
    {"set": [{"property": "Колір", "value": 1}]},
    {"remove": "Колір"},
    {"remove": [""]},
])
async def test_set_values_rejects_bad_arguments(client, extra):
    with pytest.raises(ParameterError):
        await TOOLS["set_additional_attribute_values"].executor(
            client, {"metadata": "Catalogs", "type": "Контрагенты", "id": OWNER_ID, **extra})
    assert client.calls == []


async def test_set_values_rejects_a_non_uuid_owner(client):
    with pytest.raises(ParameterError):
        await TOOLS["set_additional_attribute_values"].executor(
            client, {"metadata": "Catalogs", "type": "Контрагенты", "id": "abc",
                     "remove": ["Колір"]})


# --- NOT_FOUND hint (error text only) ---------------------------------------------------------

class NotFoundClient:
    def __getattr__(self, name):
        async def method(*args, **kwargs):
            raise NotFoundError("Catalog ДополнительныеРеквизитыИСведения not found",
                                http_status=404)
        return method


@pytest.mark.parametrize(("tool", "args"), [
    ("search_catalog", {"type": "ДополнительныеРеквизитыИСведения"}),
    ("filter_catalog", {"type": "ДополнительныеРеквизитыИСведения"}),
    ("get_object", {"metadata": "Catalogs", "type": "ДополнительныеРеквизитыИСведения",
                    "id": PROP_ID}),
])
async def test_not_found_on_a_catalog_points_to_charts_and_the_new_tool(tool, args):
    with pytest.raises(NotFoundError) as exc:
        await TOOLS[tool].executor(NotFoundClient(), args)
    message = exc.value.message
    assert message.startswith("Catalog ДополнительныеРеквизитыИСведения not found")
    assert "ChartsOfCharacteristicTypes" in message
    assert "list_additional_attributes" in message
    assert exc.value.code == "NOT_FOUND" and exc.value.http_status == 404


async def test_get_object_on_a_document_keeps_the_plain_not_found():
    with pytest.raises(NotFoundError) as exc:
        await TOOLS["get_object"].executor(
            NotFoundClient(), {"metadata": "Documents", "type": "X", "id": PROP_ID})
    assert "ChartsOfCharacteristicTypes" not in exc.value.message


def test_hint_is_not_in_the_descriptions():
    for name in ("search_catalog", "filter_catalog", "get_object"):
        assert "list_additional_attributes" not in TOOLS[name].description
        assert "list_additional_attributes" not in (TOOLS[name].description_en or "")


# --- registry ---------------------------------------------------------------------------------

def test_new_tools_are_appended_last_on_both_surfaces():
    assert next(iter(TOOLS)) == "search_catalog"
    assert list(TOOLS)[-3:] == NEW_TOOLS
    for name in NEW_TOOLS:
        assert TOOLS[name].surfaces == frozenset({"backend", "mcp"})
        assert TOOLS[name].description_en


def test_write_tools_are_gated():
    assert TOOLS["list_additional_attributes"].read_only is True
    assert TOOLS["upsert_additional_attribute"].read_only is False
    assert TOOLS["set_additional_attribute_values"].read_only is False
    for surface in ("backend", "mcp"):
        names = {s.name for s in tool_specs(surface=surface)}
        assert "list_additional_attributes" in names
        assert not names & {"upsert_additional_attribute", "set_additional_attribute_values"}
        names = {s.name for s in tool_specs(surface=surface, include_write=True)}
        assert set(NEW_TOOLS) <= names


def test_descriptions_carry_type_examples():
    for text in (TOOLS["upsert_additional_attribute"].description,
                 TOOLS["upsert_additional_attribute"].description_en):
        assert '"String"' in text and '"CatalogRef.Номенклатура"' in text
        assert "deletion_mark" in text


# --- masking ----------------------------------------------------------------------------------

CATALOG = "Справочник.Контрагенты"


def _masker() -> Masker:
    # An open catalog: plain strings stay readable, so only the by-name rule can mask a value.
    policy = DEFAULT_POLICY.merge({"open_type_exact": [CATALOG]})
    return Masker(secret="t", policy=policy,
                  type_classifier=lambda n: {"Контрагенты": CATALOG}.get(n))


def test_values_answer_masks_a_password_property_but_not_a_colour():
    payload = {"metadata": "catalog", "type": "Контрагенты", "id": OWNER_ID,
               "data": [{"property": PROP_ID, "title": "Пароль", "value": "Тест-Пароль-2026"},
                        {"property": SET_ID, "title": "Колір", "value": "Червоний"}],
               "available": [{"property": PROP_ID, "title": "Пароль", "types": ["Строка"]},
                             {"property": SET_ID, "title": "Колір", "types": ["Строка"]}]}
    out = _masker().mask(payload)
    password, colour = out["data"]
    assert password["value"].startswith("Значення·")
    assert password["title"] == "Пароль" and password["property"] == PROP_ID
    assert colour["value"] == "Червоний"
    assert out["available"] == payload["available"]


def test_values_patch_changes_mask_by_the_property_title():
    payload = {"metadata": "catalog", "type": "Контрагенты", "id": OWNER_ID,
               "changed": [{"property": PROP_ID, "title": "Пароль", "old": None,
                            "new": "Тест-Пароль-2026"},
                           {"property": SET_ID, "title": "Колір", "old": "Синій",
                            "new": "Червоний"}]}
    # No `data`, so not an envelope: the caller passes the owner type, as for patch_object.
    password, colour = _masker().mask(payload, type_name="Контрагенты", kind="Catalogs")["changed"]
    assert password["old"] is None and password["new"].startswith("Значення·")
    assert (colour["old"], colour["new"]) == ("Синій", "Червоний")


def test_property_definitions_stay_readable():
    masker = Masker(secret="t", policy=DEFAULT_POLICY)  # nothing open
    listing = {"data": [{"id": PROP_ID, "title": "Пароль", "name": "Пароль_ab12",
                         "types": ["Строка"], "set": {"id": SET_ID, "title": "Контрагенти"},
                         "deletion_mark": False}], "cursor": None}
    assert masker.mask(listing) == listing
    created = {"id": PROP_ID, "name": "Колір_ab12", "title": "Колір", "type": ["String"],
               "set": SET_ID, "created": True}
    assert masker.mask(created) == created
    card = {"id": PROP_ID, "title": "Колір", "name": "Колір_ab12", "types": ["Строка"],
            "set": None, "sets": [], "deletion_mark": False, "ext_values": False,
            "comment": "Іваненко просив"}
    out = masker.mask(card)
    assert out["title"] == "Колір" and out["comment"] != "Іваненко просив"


def test_property_patch_keeps_structural_changes_and_masks_free_text():
    masker = Masker(secret="t", policy=DEFAULT_POLICY)
    payload = {"id": PROP_ID, "changed": [
        {"field": "title", "old": "Колір", "new": "Колір товару"},
        {"field": "comment", "old": None, "new": "Іваненко просив"}]}
    title, comment = masker.mask(payload)["changed"]
    assert (title["old"], title["new"]) == ("Колір", "Колір товару")
    assert comment["new"] != "Іваненко просив"


def test_values_added_stays_readable_and_keeps_the_patch_shape():
    masker = Masker(secret="t", policy=DEFAULT_POLICY)  # nothing open
    payload = {"id": PROP_ID, "changed": [
        {"field": "title", "old": "Колір", "new": "Колір товару"},
        {"field": "comment", "old": None, "new": "Іваненко просив"}],
        "values_added": VALUES_ADDED}
    out = masker.mask(payload)
    title, comment = out["changed"]
    assert (title["old"], title["new"]) == ("Колір", "Колір товару")
    assert comment["new"] != "Іваненко просив"
    assert out["values_added"] == VALUES_ADDED
    only_values = {"id": PROP_ID, "changed": [], "values_added": VALUES_ADDED}
    assert masker.mask(only_values) == only_values
    created = {"id": PROP_ID, "name": "Колір_ab12", "title": "Колір", "type": ["ExtValues"],
               "set": SET_ID, "created": True, "values_added": VALUES_ADDED}
    assert masker.mask(created) == created


async def test_mock_mode_answers_values_added():
    client = MCBPClient(ConnectionConfig(user="u", mock=True))
    created = await client.create_property("Колір", ["ExtValues"], SET_ID,
                                           add_values=["Червоний"])
    assert [v["title"] for v in created["values_added"]] == ["Червоний"]
    changed = await client.patch_property(PROP_ID, add_values=["Синій"])
    assert changed["changed"] == []
    assert [v["title"] for v in changed["values_added"]] == ["Синій"]
