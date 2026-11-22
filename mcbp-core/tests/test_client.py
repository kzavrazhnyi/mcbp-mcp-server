"""MCBPClient: typed error classification (`error.code` → exception type) and `patch_object`."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ConflictError,
    ConversionNotConfiguredError,
    ForbiddenError,
    LicenseRequiredError,
    LockedError,
    NotFoundError,
    ParameterError,
    PlusRequiredError,
    UpstreamError,
)


def _live_client() -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url="http://test", user="u", password="p", mock=False))


async def test_get_object_mock_mode_returns_dict_shape():
    """The mock must match the real `GET /ai/v1/object/{metadata}/{type}/{id}` shape
    (.claude/refs/onec-tool-contract.md §4.10): `data` is a flat DICT, not a list of
    single-key dicts."""
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.get_object("Catalogs", "Контрагенты", "e3f1d9b6-0002")
    assert result["type"] == "Контрагенты"
    assert isinstance(result["data"], dict)
    assert result["data"]["Ref"]["Presentation"] == "Альфа Трейд, ТОВ"
    assert result["data"]["Code"] == "000000002"


async def test_patch_object_mock_mode():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.patch_object(
        "Documents", "ЗаказПокупателя", "e3f1d9b6-0001", {"СостояниеЗаказа": "some-uuid"},
    )
    assert result["type"] == "ЗаказПокупателя"
    assert result["id"] == "e3f1d9b6-0001"
    assert result["changed"] == [{"field": "СостояниеЗаказа", "old": "<mock:old>", "new": "some-uuid"}]
    assert result["posted"] is True


@respx.mock
async def test_patch_object_sends_patch_method_with_json_body():
    route = respx.patch(
        "http://test/ai/v1/object/Documents/ЗаказПокупателя/e3f1d9b6-0001-11ef-a850-cc52afc9fc6f"
    ).mock(return_value=httpx.Response(200, json={
        "metadata": "document", "type": "ЗаказПокупателя", "id": "e3f1d9b6-...",
        "changed": [{"field": "СостояниеЗаказа", "old": "В роботі", "new": "Завершений"}],
        "posted": True,
    }))
    client = _live_client()
    await client.startup()
    try:
        result = await client.patch_object(
            "Documents", "ЗаказПокупателя", "e3f1d9b6-0001-11ef-a850-cc52afc9fc6f",
            {"СостояниеЗаказа": "8c68b071-0983-11e7-9103-005056c00008"},
        )
        assert route.calls[0].request.method == "PATCH"
        assert result["changed"][0]["new"] == "Завершений"
    finally:
        await client.shutdown()


async def test_set_register_record_mock_mode():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.set_register_record("MCBP_StatusFunctions", {"Function": "uuid-1"})
    assert result["register"] == "MCBP_StatusFunctions"
    assert result["created"] is True
    assert result["changed"] == [{"field": "Function", "old": None, "new": "uuid-1"}]


@respx.mock
async def test_set_register_record_posts_flat_json_body():
    body = {"Function": "uuid-1", "Status": "Done", "Version": "1.0"}
    route = respx.post("http://test/ai/v1/registers/MCBP_StatusFunctions/records").mock(
        return_value=httpx.Response(200, json={
            "register": "MCBP_StatusFunctions", "created": False, "key": {"Function": "uuid-1"},
            "changed": [{"field": "Status", "old": "Waiting", "new": "Done"}],
        }))
    client = _live_client()
    await client.startup()
    try:
        result = await client.set_register_record("MCBP_StatusFunctions", body)
        request = route.calls[0].request
        assert request.method == "POST"
        assert json.loads(request.content) == body
        assert result["created"] is False
        assert result["changed"][0]["new"] == "Done"
    finally:
        await client.shutdown()


@respx.mock
async def test_set_register_record_bad_parameter_is_typed_and_not_retried():
    route = respx.post("http://test/ai/v1/registers/X/records").mock(
        return_value=httpx.Response(400, json={
            "error": {"code": "BAD_PARAMETER", "message": "Incomplete key: missing Period"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ParameterError) as exc_info:
            await client.set_register_record("X", {"Function": "uuid-1"})
        assert "Incomplete key" in str(exc_info.value)
        assert route.call_count == 1
    finally:
        await client.shutdown()


@respx.mock
async def test_register_balance_sends_on_alongside_dimension_filters():
    route = respx.get(url__startswith="http://test/ai/v1/registers/MCBP_Debt/balance").mock(
        return_value=httpx.Response(200, json={"type": "MCBP_Debt", "data": []})
    )
    client = _live_client()
    await client.startup()
    try:
        await client.register_balance("MCBP_Debt", {"Контрагент": "uuid-1"}, on="2026-01-01")
        params = route.calls[0].request.url.params
        assert params["on"] == "2026-01-01"
        assert params["Контрагент"] == "uuid-1"
    finally:
        await client.shutdown()


@respx.mock
async def test_register_balance_omits_on_when_not_given():
    route = respx.get(url__startswith="http://test/ai/v1/registers/MCBP_Debt/balance").mock(
        return_value=httpx.Response(200, json={"type": "MCBP_Debt", "data": []})
    )
    client = _live_client()
    await client.startup()
    try:
        await client.register_balance("MCBP_Debt", {"Контрагент": "uuid-1"})
        assert "on" not in route.calls[0].request.url.params
    finally:
        await client.shutdown()


@respx.mock
async def test_bad_parameter_envelope_typed_as_parameter_error():
    respx.patch(url__startswith="http://test/ai/v1/object/").mock(
        return_value=httpx.Response(422, json={
            "error": {"code": "BAD_PARAMETER", "message": "Unknown field 'Foo' for X - ..."}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ParameterError) as exc_info:
            await client.patch_object("Documents", "X", "e3f1d9b6-0001", {"Foo": "bar"})
        assert "Unknown field" in str(exc_info.value)
    finally:
        await client.shutdown()


@respx.mock
async def test_license_required_envelope_typed_not_upstream_error():
    # Live regression (11.09.2026): the gated MCBP_AI answers 403 LICENSE_REQUIRED, which used to
    # land in UpstreamError — the model read a missing licence as a temporary BAS failure.
    respx.get("http://test/ai/v1/metadata/Catalogs").mock(
        return_value=httpx.Response(403, json={
            "error": {"code": "LICENSE_REQUIRED",
                      "message": "MCBP+ license for ArtificialIntelligence is required"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(LicenseRequiredError) as exc_info:
            await client.list_metadata(kind="Catalogs")
        assert exc_info.value.code == "LICENSE_REQUIRED"
        assert "license" in str(exc_info.value).lower()
    finally:
        await client.shutdown()


@respx.mock
async def test_conversion_not_configured_envelope_typed():
    respx.post("http://test/ai/v1/objects/ЗаказПокупателя").mock(
        return_value=httpx.Response(422, json={
            "error": {"code": "CONVERSION_NOT_CONFIGURED",
                      "message": "MCBP Plus accepted the request but wrote nothing: ..."}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ConversionNotConfiguredError):
            await client.create_object("ЗаказПокупателя", {})
    finally:
        await client.shutdown()


@respx.mock
async def test_plus_required_envelope_typed():
    respx.post("http://test/ai/v1/objects/X").mock(
        return_value=httpx.Response(501, json={
            "error": {"code": "PLUS_REQUIRED", "message": "Object write requires the MCBP Plus extension"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(PlusRequiredError):
            await client.create_object("X", {})
    finally:
        await client.shutdown()


@respx.mock
async def test_non_envelope_body_falls_back_to_status_code_classification():
    # 500 with plain text (not the {"error":...} JSON envelope) - old behavior preserved: UpstreamError.
    respx.get("http://test/ai/v1/catalogs/X").mock(
        return_value=httpx.Response(500, text="Internal Server Error, not JSON")
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(UpstreamError):
            await client.list_catalog("X", None, 50, None)
    finally:
        await client.shutdown()


@respx.mock
async def test_404_without_envelope_still_not_found_error():
    respx.get("http://test/ai/v1/catalogs/X").mock(
        return_value=httpx.Response(404, text="not json")
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotFoundError):
            await client.list_catalog("X", None, 50, None)
    finally:
        await client.shutdown()


async def test_describe_metadata_mock_summarizes_tabular_sections_by_default():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.describe_metadata("Documents", "ЗаказПокупателя")
    sections = result["tabular_sections"]
    assert sections  # mock fixture carries >=1 section for a document
    for section in sections:
        assert set(section.keys()) == {"name", "synonym", "attributes_count"}


async def test_describe_metadata_mock_tabular_section_returns_full_attributes():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.describe_metadata("Documents", "ЗаказПокупателя", tabular_section="Запасы")
    sections = result["tabular_sections"]
    assert len(sections) == 1
    assert sections[0]["name"] == "Запасы"
    assert "attributes" in sections[0]
    assert len(sections[0]["attributes"]) > 0


async def test_describe_metadata_mock_unknown_tabular_section_raises_parameter_error():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    with pytest.raises(ParameterError) as exc_info:
        await client.describe_metadata("Documents", "ЗаказПокупателя", tabular_section="Немає")
    assert "Запасы" in str(exc_info.value)  # lists the real names


async def test_describe_metadata_q_matches_internal_name():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.describe_metadata("Documents", "ЗаказПокупателя", q="pokupatel")
    assert [a["name"] for a in result["attributes"]] == ["Pokupatel"]
    assert result["standard_attributes"] == []
    assert result["q"] == "pokupatel"
    assert result["attributes_total"] == 5  # 2 standard + 3 attributes in the mock fixture
    assert result["attributes_omitted"] == 4


async def test_describe_metadata_q_matches_ukrainian_synonym():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    # "Покупатель" (name) carries synonym "Покупець" — searching the synonym must find it too.
    result = await client.describe_metadata("Documents", "ЗаказПокупателя", q="Покупець")
    assert [a["name"] for a in result["attributes"]] == ["Pokupatel"]


async def test_describe_metadata_q_case_insensitive():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.describe_metadata("Documents", "ЗаказПокупателя", q="POKUPATEL")
    assert [a["name"] for a in result["attributes"]] == ["Pokupatel"]


async def test_describe_metadata_q_no_match_returns_available_names_fallback():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.describe_metadata("Documents", "ЗаказПокупателя", q="немаєтакогополя")
    assert result["standard_attributes"] == []
    assert result["attributes"] == []
    assert result["attributes_omitted"] == result["attributes_total"] == 5
    assert set(result["available_names"]) == {
        "Naimenovanie", "Kod", "Pokupatel", "Postavshchik", "KodPoEDRPOU",
    }


async def test_describe_metadata_q_combined_with_tabular_section():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.describe_metadata(
        "Documents", "ЗаказПокупателя", tabular_section="Запасы", q="pokupatel",
    )
    # tabular_section is applied in full (untouched by q — q only narrows header attributes)
    assert result["tabular_sections"][0]["name"] == "Запасы"
    assert "attributes" in result["tabular_sections"][0]
    # header attributes are narrowed by q
    assert [a["name"] for a in result["attributes"]] == ["Pokupatel"]


async def test_describe_metadata_q_none_is_byte_for_byte_unchanged():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    with_q_none = await client.describe_metadata("Documents", "ЗаказПокупателя")
    without_q_param = await client.describe_metadata("Documents", "ЗаказПокупателя", q=None)
    assert with_q_none == without_q_param
    assert "q" not in with_q_none
    assert "attributes_total" not in with_q_none
    assert "available_names" not in with_q_none


@respx.mock
async def test_error_envelope_body_logged_at_warning(caplog):
    respx.patch(url__startswith="http://test/ai/v1/object/").mock(
        return_value=httpx.Response(422, json={
            "error": {"code": "BAD_PARAMETER", "message": "Unknown field 'Foo' for X - ..."}})
    )
    client = _live_client()
    await client.startup()
    try:
        with caplog.at_level("WARNING", logger="mcbp.api"):
            with pytest.raises(ParameterError):
                await client.patch_object("Documents", "X", "e3f1d9b6-0001", {"Foo": "bar"})
        assert any("Unknown field" in r.message for r in caplog.records)
    finally:
        await client.shutdown()


@respx.mock
async def test_non_envelope_html_body_flagged_in_warning(caplog):
    respx.get("http://test/ai/v1/catalogs/X").mock(
        return_value=httpx.Response(500, text="<html>...BAF application error...</html>")
    )
    client = _live_client()
    await client.startup()
    try:
        with caplog.at_level("WARNING", logger="mcbp.api"):
            with pytest.raises(UpstreamError):
                await client.list_catalog("X", None, 50, None)
        assert any("not the" in r.message and "envelope" in r.message for r in caplog.records)
    finally:
        await client.shutdown()


@respx.mock
async def test_transport_error_log_names_exception_class(caplog):
    respx.get("http://test/ai/v1/catalogs/X").mock(side_effect=httpx.ReadTimeout("timed out"))
    client = _live_client()
    await client.startup()
    try:
        with caplog.at_level("WARNING", logger="mcbp.api"):
            with pytest.raises(UpstreamError):
                await client.list_catalog("X", None, 50, None)
        lines = [r.getMessage() for r in caplog.records if "transport error" in r.getMessage()]
        assert lines and "ReadTimeout" in lines[-1]
        assert "u:p@" not in lines[-1] and "Authorization" not in lines[-1]
    finally:
        await client.shutdown()


@respx.mock
async def test_patch_body_value_logged_not_only_keys(caplog):
    respx.patch(
        "http://test/ai/v1/object/Documents/ЗаказПокупателя/e3f1d9b6-0001-11ef-a850-cc52afc9fc6f"
    ).mock(return_value=httpx.Response(200, json={
        "metadata": "document", "type": "ЗаказПокупателя", "id": "e3f1d9b6-...",
        "changed": [], "posted": True,
    }))
    client = _live_client()
    await client.startup()
    try:
        with caplog.at_level("INFO", logger="mcbp.api"):
            await client.patch_object(
                "Documents", "ЗаказПокупателя", "e3f1d9b6-0001-11ef-a850-cc52afc9fc6f",
                {"СостояниеЗаказа": "СостоянияЗаказовПокупателей:70c31e69-0000"},
            )
        assert any(
            "СостоянияЗаказовПокупателей:70c31e69-0000" in r.message for r in caplog.records
        )
    finally:
        await client.shutdown()


@respx.mock
async def test_long_response_body_truncated_in_warning(caplog):
    long_text = "x" * 2000
    respx.get("http://test/ai/v1/catalogs/X").mock(
        return_value=httpx.Response(500, text=long_text)
    )
    client = _live_client()
    await client.startup()
    try:
        with caplog.at_level("WARNING", logger="mcbp.api"):
            with pytest.raises(UpstreamError):
                await client.list_catalog("X", None, 50, None)
        warning = next(r for r in caplog.records if r.levelname == "WARNING")
        assert "truncated" in warning.message
        assert len(warning.message) < 2000
    finally:
        await client.shutdown()


async def test_register_records_mock_mode():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.register_records("MCBP_StatusFunctions")
    assert result["metadata"] == "informationregister"
    assert result["type"] == "MCBP_StatusFunctions"
    assert result["truncated"] is False
    assert result["data"]


@respx.mock
async def test_register_records_sends_filters_orderby_desc_and_period():
    route = respx.get("http://test/ai/v1/registers/MCBP_StatusFunctions/records").mock(
        return_value=httpx.Response(200, json={
            "metadata": "informationregister", "type": "MCBP_StatusFunctions",
            "data": [], "truncated": False,
        })
    )
    client = _live_client()
    await client.startup()
    try:
        await client.register_records(
            "MCBP_StatusFunctions", date_from="2026-01-01", date_to="2026-08-01",
            filters={"Контрагент": "uuid-1"}, orderby="Period", desc=True, limit=10,
        )
        request_url = route.calls[0].request.url
        params = dict(request_url.params)
        assert params["from"] == "2026-01-01"
        assert params["to"] == "2026-08-01"
        assert params["f.Контрагент"] == "uuid-1"
        assert params["orderby"] == "Period"
        assert params["desc"] == "true"
        assert params["limit"] == "10"
    finally:
        await client.shutdown()


@respx.mock
async def test_register_records_omits_from_to_when_not_given():
    route = respx.get("http://test/ai/v1/registers/MCBP_StatusFunctions/records").mock(
        return_value=httpx.Response(200, json={
            "metadata": "informationregister", "type": "MCBP_StatusFunctions",
            "data": [], "truncated": False,
        })
    )
    client = _live_client()
    await client.startup()
    try:
        await client.register_records("MCBP_StatusFunctions")
        params = dict(route.calls[0].request.url.params)
        assert "from" not in params
        assert "to" not in params
        assert "limit" not in params
    finally:
        await client.shutdown()


@respx.mock
async def test_register_records_truncated_true_passed_through():
    respx.get("http://test/ai/v1/registers/MCBP_StatusFunctions/records").mock(
        return_value=httpx.Response(200, json={
            "metadata": "informationregister", "type": "MCBP_StatusFunctions",
            "data": [{"Period": "2026-08-01 00:00:00"}], "truncated": True,
        })
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.register_records("MCBP_StatusFunctions", limit=1)
        assert result["truncated"] is True
    finally:
        await client.shutdown()


@respx.mock
async def test_register_records_unknown_register_404():
    respx.get("http://test/ai/v1/registers/NoSuchRegister/records").mock(
        return_value=httpx.Response(404, json={
            "error": {"code": "NOT_FOUND", "message": "Register 'NoSuchRegister' not found"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotFoundError):
            await client.register_records("NoSuchRegister")
    finally:
        await client.shutdown()


@respx.mock
async def test_register_records_unknown_field_typed_as_parameter_error():
    respx.get("http://test/ai/v1/registers/MCBP_StatusFunctions/records").mock(
        return_value=httpx.Response(422, json={
            "error": {"code": "BAD_PARAMETER",
                      "message": "Unknown field 'Foo' for MCBP_StatusFunctions - ..."}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ParameterError) as exc_info:
            await client.register_records("MCBP_StatusFunctions", filters={"Foo": "bar"})
        assert "Unknown field" in str(exc_info.value)
    finally:
        await client.shutdown()


@respx.mock
async def test_retries_503_then_succeeds(monkeypatch):
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("mcbp_core.client.asyncio.sleep", _fake_sleep)

    route = respx.get("http://test/ai/v1/catalogs/X").mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, json={"data": [], "cursor": None}),
        ]
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.list_catalog("X", None, 50, None)
        assert result == {"data": [], "cursor": None}
        assert route.call_count == 3
        assert sleeps == [0.3, 0.6]
    finally:
        await client.shutdown()


@respx.mock
async def test_retries_exhausted_on_503_raises(monkeypatch):
    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("mcbp_core.client.asyncio.sleep", _fake_sleep)

    route = respx.get("http://test/ai/v1/catalogs/X").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(UpstreamError):
            await client.list_catalog("X", None, 50, None)
        assert route.call_count == 3
    finally:
        await client.shutdown()


@respx.mock
async def test_retries_connect_error_then_succeeds(monkeypatch):
    async def _fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("mcbp_core.client.asyncio.sleep", _fake_sleep)

    route = respx.get("http://test/ai/v1/catalogs/X").mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json={"data": [], "cursor": None}),
        ]
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.list_catalog("X", None, 50, None)
        assert result == {"data": [], "cursor": None}
        assert route.call_count == 3
    finally:
        await client.shutdown()


@respx.mock
async def test_post_is_not_retried_on_503(monkeypatch):
    async def _fake_sleep(seconds: float) -> None:
        raise AssertionError("POST must not retry, so sleep must never be called")

    monkeypatch.setattr("mcbp_core.client.asyncio.sleep", _fake_sleep)

    route = respx.post("http://test/ai/v1/objects/X").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(UpstreamError):
            await client.create_object("X", {})
        assert route.call_count == 1
    finally:
        await client.shutdown()


@respx.mock
async def test_successful_first_call_makes_exactly_one_request(monkeypatch):
    async def _fake_sleep(seconds: float) -> None:
        raise AssertionError("no retry expected on the happy path")

    monkeypatch.setattr("mcbp_core.client.asyncio.sleep", _fake_sleep)

    route = respx.get("http://test/ai/v1/catalogs/X").mock(
        return_value=httpx.Response(200, json={"data": [], "cursor": None})
    )
    client = _live_client()
    await client.startup()
    try:
        await client.list_catalog("X", None, 50, None)
        assert route.call_count == 1
    finally:
        await client.shutdown()


@respx.mock
async def test_health_returns_real_key_true():
    respx.get("http://test/ai/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "service": "MCBP_AI", "key": True})
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.health()
        assert result == {"status": "ok", "service": "MCBP_AI", "key": True, "is_admin": False,
                          "allow_data_change": None}
    finally:
        await client.shutdown()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("true", False), (1, False)],
)
@respx.mock
async def test_health_is_admin_only_on_json_true(raw: object, expected: bool):
    respx.get("http://test/ai/v1/health").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "service": "MCBP_AI", "key": True, "is_admin": raw}
        )
    )
    client = _live_client()
    await client.startup()
    try:
        assert (await client.health())["is_admin"] is expected
    finally:
        await client.shutdown()


@respx.mock
async def test_health_passes_version_when_sent():
    respx.get("http://test/ai/v1/health").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "service": "MCBP_AI", "key": True, "version": "1.0.40.7"}
        )
    )
    client = _live_client()
    await client.startup()
    try:
        assert (await client.health())["version"] == "1.0.40.7"
    finally:
        await client.shutdown()


@pytest.mark.parametrize("body_extra", [{}, {"version": None}, {"version": ""}])
@respx.mock
async def test_health_omits_version_when_base_sends_none(body_extra: dict):
    respx.get("http://test/ai/v1/health").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "service": "MCBP_AI", "key": True, **body_extra}
        )
    )
    client = _live_client()
    await client.startup()
    try:
        assert "version" not in await client.health()
    finally:
        await client.shutdown()


async def _health_with(body_extra: dict) -> dict:
    respx.get("http://test/ai/v1/health").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "service": "MCBP_AI", "key": True, **body_extra}
        )
    )
    client = _live_client()
    await client.startup()
    try:
        return await client.health()
    finally:
        await client.shutdown()


@respx.mock
async def test_health_passes_platform_and_configuration_when_sent():
    conf = {"name": "MCBP", "synonym": "MCBP+", "version": "1.0.40.7"}
    result = await _health_with({"platform": "8.3.19.1529", "configuration": conf})
    assert result["platform"] == "8.3.19.1529"
    assert result["configuration"] == conf


@pytest.mark.parametrize("body_extra", [
    {}, {"platform": None}, {"platform": ""}, {"platform": 8319}, {"platform": ["8.3"]},
])
@respx.mock
async def test_health_omits_platform_when_absent_or_wrong_type(body_extra: dict):
    assert "platform" not in await _health_with(body_extra)


@pytest.mark.parametrize("body_extra", [
    {}, {"configuration": None}, {"configuration": "MCBP"}, {"configuration": ["MCBP"]},
    {"configuration": {}}, {"configuration": {"name": 1, "synonym": None, "version": [1]}},
])
@respx.mock
async def test_health_omits_configuration_when_absent_or_wrong_type(body_extra: dict):
    assert "configuration" not in await _health_with(body_extra)


@respx.mock
async def test_health_configuration_keeps_only_string_known_fields():
    result = await _health_with({"configuration": {
        "name": "MCBP", "synonym": 5, "version": None, "extra": "x",
    }})
    assert result["configuration"] == {"name": "MCBP"}


@respx.mock
async def test_health_returns_real_key_false():
    respx.get("http://test/ai/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "service": "MCBP_AI", "key": False})
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.health()
        assert result["key"] is False
    finally:
        await client.shutdown()


@respx.mock
async def test_health_response_without_key_does_not_crash():
    respx.get("http://test/ai/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "service": "MCBP_AI"})
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.health()
        assert result["status"] == "ok"
        assert result["service"] == "MCBP_AI"
        assert result["key"] is True  # sensible default for an older base with no key field
        assert result["is_admin"] is False
    finally:
        await client.shutdown()


async def test_health_mock_mode_still_usable():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.health()
    assert result["status"] == "ok"
    assert result["service"] == "MCBP_AI"
    assert result["key"] is True
    assert result["is_admin"] is True
    assert isinstance(result["platform"], str)
    assert set(result["configuration"]) == {"name", "synonym", "version"}


async def test_connection_config_verify_defaults_true():
    assert ConnectionConfig().verify is True


async def test_startup_passes_verify_false_to_httpx_client(monkeypatch):
    captured: dict = {}
    real_async_client = httpx.AsyncClient

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("mcbp_core.client.httpx.AsyncClient", _spy)
    client = MCBPClient(
        ConnectionConfig(base_url="http://test", user="u", password="p", mock=False, verify=False)
    )
    await client.startup()
    try:
        assert captured["verify"] is False
    finally:
        await client.shutdown()


@respx.mock
async def test_describe_metadata_full_tree_cached_once_across_views():
    """Both the summary call and the tabular_section call must hit BAS only once — the
    view is derived from the cached full tree, not re-fetched."""
    route = respx.get("http://test/ai/v1/metadata/Documents/X").mock(
        return_value=httpx.Response(200, json={
            "metadata": "document", "type": "X", "synonym": "X",
            "standard_attributes": [], "attributes": [],
            "tabular_sections": [
                {"name": "Rows", "synonym": "Rows", "attributes": [
                    {"name": "A", "synonym": "A", "types": ["Число"]},
                ]},
            ],
        })
    )
    client = _live_client()
    await client.startup()
    try:
        summary = await client.describe_metadata("Documents", "X")
        assert summary["tabular_sections"] == [
            {"name": "Rows", "synonym": "Rows", "attributes_count": 1}
        ]
        detail = await client.describe_metadata("Documents", "X", tabular_section="Rows")
        assert detail["tabular_sections"][0]["attributes"] == [
            {"name": "A", "synonym": "A", "types": ["Число"]}
        ]
        assert route.call_count == 1
    finally:
        await client.shutdown()


@respx.mock
async def test_forbidden_envelope_typed_as_forbidden_not_upstream():
    """A read route now answers 403 FORBIDDEN when the BAS account lacks the right on that
    object. Without the mapping it fell through to the status-only branch and the model saw a
    generic upstream failure — "the base is broken" instead of "you may not read this one"."""
    # ASCII path on purpose: respx matches `url__startswith` against the ENCODED request URL,
    # so a Cyrillic type name there would never match. The message keeps the real one.
    respx.get(url__startswith="http://test/ai/v1/catalogs/Nomenclature").mock(
        return_value=httpx.Response(403, json={
            "error": {"code": "FORBIDDEN",
                      "message": "No read right on catalog ВидыНачисленийИУдержаний"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ForbiddenError) as exc_info:
            await client.list_catalog("Nomenclature", None, 10, None)
        assert not isinstance(exc_info.value, UpstreamError)
        assert str(exc_info.value) == "No read right on catalog ВидыНачисленийИУдержаний"
        assert exc_info.value.code == "FORBIDDEN"
        assert exc_info.value.http_status == 403
    finally:
        await client.shutdown()


@respx.mock
async def test_locked_envelope_typed_as_locked_error():
    respx.post("http://test/ai/v1/objects/X").mock(
        return_value=httpx.Response(409, json={"error": {"code": "LOCKED", "message": "locked"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(LockedError) as exc:
            await client.create_object("X", {})
    finally:
        await client.shutdown()
    assert isinstance(exc.value, ConflictError)
    assert exc.value.code == "LOCKED" and exc.value.http_status == 409


@respx.mock
async def test_conflict_envelope_typed_as_plain_conflict_error():
    respx.post("http://test/ai/v1/objects/X").mock(
        return_value=httpx.Response(409, json={"error": {"code": "CONFLICT", "message": "stale"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ConflictError) as exc:
            await client.create_object("X", {})
    finally:
        await client.shutdown()
    assert not isinstance(exc.value, LockedError)
    assert exc.value.code == "CONFLICT"


@respx.mock
async def test_register_records_sends_agg_and_groupby_csv():
    route = respx.get("http://test/ai/v1/registers/Бюджеты/records").mock(
        return_value=httpx.Response(200, json={
            "metadata": "accumulationregister", "type": "Бюджеты",
            "groupby": ["Статья", "Period:month"],
            "aggregates": [{"Статья": {"Presentation": "X"}, "Period": "2026-01-01",
                            "sum_Сумма": 1.0, "count": 1}],
            "truncated": False,
        })
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.register_records(
            "Бюджеты", agg=["sum:Сумма", "count"], groupby=["Статья", "Period:month"])
        params = dict(route.calls[0].request.url.params)
        assert params["agg"] == "sum:Сумма,count"
        assert params["groupby"] == "Статья,Period:month"
        assert result["aggregates"][0]["sum_Сумма"] == 1.0
    finally:
        await client.shutdown()


@respx.mock
async def test_register_records_agg_on_old_bsl_raises_instead_of_raw_rows():
    respx.get("http://test/ai/v1/registers/Бюджеты/records").mock(
        return_value=httpx.Response(200, json={
            "metadata": "accumulationregister", "type": "Бюджеты",
            "data": [{"Сумма": 1.0}], "truncated": True,
        })
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ParameterError) as exc_info:
            await client.register_records("Бюджеты", agg=["sum:*"])
        assert "predates register aggregation" in str(exc_info.value)
    finally:
        await client.shutdown()


async def test_register_records_groupby_without_agg_rejected_locally():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    with pytest.raises(ParameterError):
        await client.register_records("Бюджеты", groupby=["Статья"])


async def test_register_records_mock_agg_rows_keyed_by_group_field():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.register_records(
        "Бюджеты", agg=["sum:*", "count"], groupby=["Статья", "Period:month"])
    assert "data" not in result
    assert result["groupby"] == ["Статья", "Period:month"]
    row = result["aggregates"][0]
    assert row["Period"] == "2026-01-01"
    assert row["Статья"]["Presentation"]
    assert row["count"] == 2
    total = await client.register_records("Бюджеты", agg=["count"])
    assert total["groupby"] is None
    assert len(total["aggregates"]) == 1


_FULL_OBJECT = {
    "metadata": "document", "type": "Бюджет",
    "data": {
        "Ref": {"Presentation": "Бюджет 1", "Data": "uuid-1", "Metadata": "Бюджет"},
        "Number": "1", "Date": "2026-01-01 00:00:00", "Posted": True,
        "Сценарий": {"Presentation": "План"}, "Комментарий": "x",
        "tabular_sections": {
            "Обороты": [{"Сумма": float(i)} for i in range(10)],
            "Показатели": [{"Значение": 1}],
        },
    },
}


@respx.mock
async def test_get_object_sends_selectors():
    route = respx.get("http://test/ai/v1/object/Documents/Бюджет/uuid-1").mock(
        return_value=httpx.Response(200, json={
            **_FULL_OBJECT, "tabular_section_counts": {"Обороты": 10, "Показатели": 1}})
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.get_object("Documents", "Бюджет", "uuid-1", fields=["Сценарий"],
                                         sections=[], rows=50, offset=50)
        params = dict(route.calls[0].request.url.params)
        assert params == {"fields": "Сценарий", "sections": "none", "rows": "50",
                          "offset": "50"}
        assert result["tabular_section_counts"] == {"Обороты": 10, "Показатели": 1}
    finally:
        await client.shutdown()


@respx.mock
async def test_get_object_without_selectors_sends_no_params():
    route = respx.get("http://test/ai/v1/object/Documents/Бюджет/uuid-1").mock(
        return_value=httpx.Response(200, json=_FULL_OBJECT))
    client = _live_client()
    await client.startup()
    try:
        result = await client.get_object("Documents", "Бюджет", "uuid-1")
        assert not route.calls[0].request.url.params
        assert "tabular_section_counts" not in result
        assert len(result["data"]["tabular_sections"]["Обороты"]) == 10
    finally:
        await client.shutdown()


@respx.mock
async def test_get_object_old_bsl_trimmed_client_side():
    respx.get("http://test/ai/v1/object/Documents/Бюджет/uuid-1").mock(
        return_value=httpx.Response(200, json=_FULL_OBJECT))
    client = _live_client()
    await client.startup()
    try:
        result = await client.get_object("Documents", "Бюджет", "uuid-1", fields=["Сценарий"],
                                         sections=["Обороты"], rows=3, offset=4)
        data = result["data"]
        assert result["tabular_section_counts"] == {"Обороты": 10, "Показатели": 1}
        assert data["tabular_sections"] == {"Обороты": [{"Сумма": 4.0}, {"Сумма": 5.0},
                                                        {"Сумма": 6.0}]}
        assert data["Сценарий"] == {"Presentation": "План"}
        assert "Комментарий" not in data
        assert data["Number"] == "1" and data["Posted"] is True
        with pytest.raises(ParameterError):
            await client.get_object("Documents", "Бюджет", "uuid-1", sections=["Нема"])
        none = await client.get_object("Documents", "Бюджет", "uuid-1", sections=[])
        assert none["data"]["tabular_sections"] == {}
        assert none["data"]["Комментарий"] == "x"
    finally:
        await client.shutdown()


@respx.mock
async def test_get_object_old_bsl_fields_case_insensitive_unknown_and_empty():
    respx.get("http://test/ai/v1/object/Documents/Бюджет/uuid-1").mock(
        return_value=httpx.Response(200, json=_FULL_OBJECT))
    client = _live_client()
    await client.startup()
    try:
        result = await client.get_object("Documents", "Бюджет", "uuid-1", fields=["сценарий"],
                                         rows=1)
        assert result["data"]["Сценарий"] == {"Presentation": "План"}
        assert "Комментарий" not in result["data"]
        with pytest.raises(ParameterError, match="Нема"):
            await client.get_object("Documents", "Бюджет", "uuid-1", fields=["Нема"], rows=1)
        empty = await client.get_object("Documents", "Бюджет", "uuid-1", fields=[], rows=1)
        assert empty["data"]["Комментарий"] == "x"
        assert empty["data"]["Сценарий"] == {"Presentation": "План"}
    finally:
        await client.shutdown()


async def test_get_object_mock_honours_selectors():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.get_object("Catalogs", "Контрагенты", "e3f1d9b6-0002", rows=1, offset=1)
    assert result["tabular_section_counts"] == {"КонтактнаяИнформация": 3}
    assert result["data"]["tabular_sections"]["КонтактнаяИнформация"] == [
        {"Вид": "Телефон", "Представление": "<mock-2>"}]


# --- Accounting registers on GET /registers/{type}/balance and /records ----------------

_ACCOUNTING_ANSWER = {"metadata": "accountingregister", "type": "Хозрасчетный",
                      "table": "balance_and_turnovers", "data": [], "truncated": False}


@respx.mock
async def test_register_balance_serialises_accounting_arguments():
    route = respx.get(url__startswith="http://test/ai/v1/registers/").mock(
        return_value=httpx.Response(200, json=_ACCOUNTING_ANSWER)
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.register_balance(
            "Хозрасчетный", {"Организация": "org-1"}, table="balance_and_turnovers",
            date_from="2026-01-01", date_to="2026-03-31", account=["361", "631"],
            groupby=["ExtDimension1", "Period:month"], resources=["Сумма"], limit=20)
        params = route.calls[0].request.url.params
        assert params["table"] == "balance_and_turnovers"
        assert params["from"] == "2026-01-01"
        assert params["to"] == "2026-03-31"
        assert params["account"] == "361,631"
        assert params["groupby"] == "ExtDimension1,Period:month"
        assert params["resources"] == "Сумма"
        assert params["limit"] == "20"
        assert params["Организация"] == "org-1"  # dimension filters stay bare
        assert "on" not in params
        assert result["table"] == "balance_and_turnovers"
    finally:
        await client.shutdown()


@respx.mock
async def test_register_balance_without_accounting_args_sends_no_reserved_params():
    route = respx.get(url__startswith="http://test/ai/v1/registers/MCBP_Debt/balance").mock(
        return_value=httpx.Response(200, json={"type": "MCBP_Debt", "data": []})
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.register_balance("MCBP_Debt", {"Контрагент": "uuid-1"})
        assert dict(route.calls[0].request.url.params) == {"Контрагент": "uuid-1"}
        assert result == {"type": "MCBP_Debt", "data": []}  # no guard on accumulation answers
    finally:
        await client.shutdown()


@pytest.mark.parametrize("kwargs, needle", [
    ({"table": "balance", "corr_account": ["701"]}, "corr_account"),
    ({"corr_account": "701"}, "corr_account"),
    ({"table": "turnovers", "account_dr": ["361"]}, "account_dr"),
    ({"table": "drcr_turnovers", "account": ["361"]}, "account_dr / account_cr"),
])
async def test_register_balance_client_side_errors_before_http(kwargs, needle):
    client = _live_client()
    await client.startup()
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(url__startswith="http://test/ai/v1/registers/")
            with pytest.raises(ParameterError) as exc_info:
                await client.register_balance("Хозрасчетный", {}, **kwargs)
            assert not route.called
        assert needle in str(exc_info.value)
    finally:
        await client.shutdown()


@respx.mock
async def test_register_balance_old_service_answer_without_table_raises():
    # An MCBP_AI that predates accounting balances reads table=/account= as dimensions; a 200
    # without the `table` key must never pass for an accounting answer.
    respx.get(url__startswith="http://test/ai/v1/registers/").mock(
        return_value=httpx.Response(200, json={"type": "Хозрасчетный", "data": []})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(ParameterError) as exc_info:
            await client.register_balance("Хозрасчетный", {}, table="balance", account="361")
        assert "predates accounting balances" in str(exc_info.value)
    finally:
        await client.shutdown()


async def test_mock_accounting_balance_shapes():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    balance = await client.register_balance("Хозрасчетный", {}, on="2026-04-01",
                                            table="balance", account=["361"])
    assert balance["metadata"] == "accountingregister"
    assert balance["table"] == "balance"
    row = balance["data"][0]
    assert row["Account"]["Code"] == "361"
    assert {"СуммаBalanceDr", "СуммаBalanceCr"} <= row.keys()
    assert balance["ext_dimension_types"] == {"361": ["Контрагенты", "Договоры"]}

    osv = await client.register_balance("Хозрасчетный", {}, table="balance_and_turnovers",
                                        date_from="2026-01-01", groupby=["ExtDimension1"])
    row = osv["data"][0]
    assert osv["groupby"] == ["Account", "ExtDimension1"]
    assert row["ExtDimension1"]["Metadata"] == "Контрагенты"
    assert {"СуммаOpeningBalanceDr", "СуммаTurnoverCr", "СуммаClosingBalanceDr"} <= row.keys()

    drcr = await client.register_balance("Хозрасчетный", {}, table="drcr_turnovers",
                                         date_from="2026-01-01", account_dr=["361"])
    assert drcr["data"][0]["AccountCr"]["Code"] == "701"
    assert "СуммаTurnover" in drcr["data"][0]

    with pytest.raises(ParameterError):
        await client.register_balance("Хозрасчетный", {}, table="turnovers")  # no from


async def test_mock_drcr_turnovers_account_cr_with_dimension_filter():
    # account_cr alone (no account_dr) plus a dimension filter: both reach the request as query
    # params and the Cr side of the answer is the requested account.
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    drcr = await client.register_balance("Хозрасчетный", {"Организация": "org-1"},
                                         table="drcr_turnovers", date_from="2023-01-01",
                                         date_to="2023-03-31", account_cr=["631"])
    row = drcr["data"][0]
    assert drcr["table"] == "drcr_turnovers"
    assert row["AccountCr"]["Code"] == "631"
    assert "AccountDr" in row and "СуммаTurnover" in row


@respx.mock
async def test_drcr_turnovers_account_cr_with_filter_sends_params():
    route = respx.get(url__startswith="http://test/ai/v1/registers/").mock(
        return_value=httpx.Response(200, json={"table": "drcr_turnovers", "data": []}))
    client = _live_client()
    await client.startup()
    try:
        await client.register_balance("Хозрасчетный", {"Организация": "org-1"},
                                      table="drcr_turnovers", date_from="2023-01-01",
                                      date_to="2023-03-31", account_cr=["631"])
        sent = route.calls[0].request.url.params
        assert sent["account_cr"] == "631" and sent["Организация"] == "org-1"
        assert "account_dr" not in sent and "account" not in sent
    finally:
        await client.shutdown()


async def test_mock_accounting_non_balanced_resource_only_on_flagged_account():
    # Количество is a non-balanced resource kept only on accounts with the Количественный flag:
    # 281 carries real Dr/Cr values; 361 lacks the flag and reads 0, as the platform answers at
    # account level - the column is present, not omitted.
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    balance = await client.register_balance("Хозрасчетный", {}, table="balance")
    rows = {r["Account"]["Code"]: r for r in balance["data"]}
    assert rows["281"]["КоличествоBalanceDr"] == 40.0
    assert rows["361"]["КоличествоBalanceDr"] == 0
    assert rows["361"]["КоличествоBalanceCr"] == 0
    assert all(r["ВалютнаяСуммаBalanceDr"] == 0 and r["ВалютнаяСуммаBalanceCr"] == 0
               for r in rows.values())
    assert balance["ext_dimension_types"]["281"] == ["Номенклатура"]

    osv = await client.register_balance("Хозрасчетный", {}, table="balance_and_turnovers",
                                        date_from="2026-01-01", account=["281"])
    assert [r["Account"]["Code"] for r in osv["data"]] == ["281"]
    assert {"КоличествоOpeningBalanceDr", "КоличествоClosingBalanceCr"} <= osv["data"][0].keys()

    explicit = await client.register_balance("Хозрасчетный", {}, table="balance",
                                             account=["281"], resources=["Количество"])
    assert explicit["resources"] == ["Количество"]
    assert explicit["data"][0]["КоличествоBalanceDr"] == 40.0

    drcr = await client.register_balance("Хозрасчетный", {}, table="drcr_turnovers",
                                         date_from="2026-01-01", account_dr=["281"])
    assert {"КоличествоTurnoverDr", "КоличествоTurnoverCr", "СуммаTurnover"} \
        <= drcr["data"][0].keys()


async def test_mock_accounting_corr_account_groupby_on_turnovers_only():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    turnovers = await client.register_balance("Хозрасчетный", {}, table="turnovers",
                                              date_from="2026-01-01", account=["361"],
                                              corr_account=["701"], groupby=["CorrAccount"])
    assert turnovers["groupby"] == ["Account", "CorrAccount"]
    assert turnovers["data"][0]["CorrAccount"]["Code"] == "701"
    with pytest.raises(ParameterError):
        await client.register_balance("Хозрасчетный", {}, table="balance",
                                      groupby=["CorrAccount"])


@respx.mock
async def test_register_balance_sends_corr_account_under_api_name():
    # The BSL side maps CorrAccount to BalancedAccount in the query text; on the wire it stays
    # corr_account / groupby=CorrAccount.
    route = respx.get(url__startswith="http://test/ai/v1/registers/").mock(
        return_value=httpx.Response(200, json={"metadata": "accountingregister",
                                               "table": "turnovers", "data": []})
    )
    client = _live_client()
    await client.startup()
    try:
        await client.register_balance("Хозрасчетный", {}, table="turnovers",
                                      date_from="2026-01-01", corr_account=["701"],
                                      groupby=["CorrAccount"])
        params = route.calls[0].request.url.params
        assert params["corr_account"] == "701"
        assert params["groupby"] == "CorrAccount"
    finally:
        await client.shutdown()


async def test_mock_accounting_records_need_from_or_recorder():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    with pytest.raises(ParameterError) as exc_info:
        await client.register_records("Хозрасчетный")
    assert "from" in str(exc_info.value)
    result = await client.register_records("Хозрасчетный", date_from="2026-08-01")
    assert result["metadata"] == "accountingregister"
    row = result["data"][0]
    assert row["AccountDr"]["Code"] == "361" and row["AccountCr"]["Code"] == "701"
    assert row["ExtDimensionDr1"]["Metadata"] == "Контрагенты"
    by_recorder = await client.register_records("Хозрасчетный",
                                                filters={"Recorder": "doc-0101"})
    assert by_recorder["data"]
    totals = await client.register_records("Хозрасчетный", date_from="2026-08-01",
                                           agg=["sum:*"], groupby=["AccountDr"])
    assert totals["metadata"] == "accountingregister"


async def test_mock_describe_accounting_register_carries_query_fields():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    detail = await client.describe_metadata("AccountingRegisters", "Хозрасчетный")
    assert detail["metadata"] == "accountingregister"
    assert detail["correspondence"] is True
    assert detail["chart_of_accounts"]["max_ext_dimension_count"] == 3
    assert "AccountDr" in detail["query_fields"]
    assert any(r["name"] == "ВалютнаяСумма" and r["balance"] is False
               for r in detail["resources"])


@respx.mock
async def test_register_balance_lone_table_balance_passes_for_accumulation_register():
    # table="balance" is what an accumulation register computes anyway: its answer has no
    # `table` key, and that must not trip the old-service guard.
    route = respx.get(url__startswith="http://test/ai/v1/registers/").mock(
        return_value=httpx.Response(200, json={"type": "ТоварыНаСкладах", "data": []})
    )
    client = _live_client()
    await client.startup()
    try:
        result = await client.register_balance("ТоварыНаСкладах", {"Склад": "s-1"},
                                               table="balance")
        assert route.calls[0].request.url.params["table"] == "balance"
        assert result == {"type": "ТоварыНаСкладах", "data": []}
    finally:
        await client.shutdown()


@respx.mock
async def test_register_balance_not_found_with_accounting_args_hints_at_update():
    # An MCBP_AI that predates accounting support looks only at accumulation registers and
    # answers 404 for an accounting one — the guard never sees a 200, so the 404 carries the hint.
    respx.get(url__startswith="http://test/ai/v1/registers/").mock(
        return_value=httpx.Response(404, json={"error": {
            "code": "NOT_FOUND", "message": "register Хозрасчетный not found"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotFoundError) as exc_info:
            await client.register_balance("Хозрасчетный", {}, table="turnovers",
                                          date_from="2026-01-01")
        message = str(exc_info.value)
        assert "register Хозрасчетный not found" in message
        assert "update the MCBP_AI module" in message
        assert exc_info.value.code == "NOT_FOUND"
    finally:
        await client.shutdown()


@respx.mock
async def test_register_balance_not_found_without_accounting_args_is_unchanged():
    respx.get(url__startswith="http://test/ai/v1/registers/").mock(
        return_value=httpx.Response(404, json={"error": {
            "code": "NOT_FOUND", "message": "register Нема not found"}})
    )
    client = _live_client()
    await client.startup()
    try:
        with pytest.raises(NotFoundError) as exc_info:
            await client.register_balance("Нема", {})
        assert "MCBP_AI" not in str(exc_info.value)
    finally:
        await client.shutdown()


async def test_mock_accumulation_balance_accepts_lone_table_balance_only():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.register_balance("ТоварыНаСкладах", {}, table="balance")
    assert result["type"] == "ТоварыНаСкладах"
    with pytest.raises(ParameterError) as exc_info:
        await client.register_balance("ТоварыНаСкладах", {}, table="balance", account=["361"])
    assert "accounting registers only" in str(exc_info.value)


async def test_mock_accumulation_turnovers_need_from():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.register_balance("ТоварыНаСкладах", {}, table="turnovers",
                                           date_from="2026-01-01", groupby=["Period:month"],
                                           limit=10)
    assert result["table"] == "turnovers"
    with pytest.raises(ParameterError):
        await client.register_balance("ТоварыНаСкладах", {}, table="turnovers")


@respx.mock
async def test_list_documents_sends_q():
    route = respx.get(url__startswith="http://test/ai/v1/documents/X").mock(
        return_value=httpx.Response(200, json={"data": [], "cursor": None}))
    client = _live_client()
    await client.startup()
    try:
        await client.list_documents("X", "2000-01-01", None, None, 10, q="15")
        assert route.calls.last.request.url.params["q"] == "15"
    finally:
        await client.shutdown()


@respx.mock
async def test_related_documents_params_and_old_module_404():
    ok = respx.get(url__startswith="http://test/ai/v1/related/Catalogs/Partners/u-1").mock(
        return_value=httpx.Response(200, json={"data": [], "truncated": False}))
    client = _live_client()
    await client.startup()
    try:
        await client.related_documents("Catalogs", "Partners", "u-1",
                                       documents=["A", "B"], sources="documents", limit=5)
        q = ok.calls.last.request.url.params
        assert (q["documents"], q["sources"], q["limit"]) == ("A,B", "documents", "5")
        respx.get(url__startswith="http://test/ai/v1/related/").mock(
            return_value=httpx.Response(404, text="<html>no route</html>"))
        with pytest.raises(NotFoundError) as exc_info:
            await client.related_documents("Catalogs", "X", "u-2")
        assert "older than 1.0.41.6" in str(exc_info.value)
        respx.get(url__startswith="http://test/ai/v1/related/").mock(
            return_value=httpx.Response(404, json={"error": {
                "code": "NOT_FOUND", "message": "Object X u-3 not found"}}))
        with pytest.raises(NotFoundError) as exc_info:
            await client.related_documents("Catalogs", "X", "u-3")
        assert "older" not in str(exc_info.value)
    finally:
        await client.shutdown()


@respx.mock
async def test_get_object_additional_false_and_fields_for_records():
    obj = respx.get(url__startswith="http://test/ai/v1/object/Catalogs/X/u-1").mock(
        return_value=httpx.Response(200, json={"data": {}}))
    rec = respx.get(url__startswith="http://test/ai/v1/registers/R/records").mock(
        return_value=httpx.Response(200, json={"data": []}))
    client = _live_client()
    await client.startup()
    try:
        await client.get_object("Catalogs", "X", "u-1", additional=False)
        assert obj.calls.last.request.url.params["additional"] == "false"
        await client.register_records("R", fields=["A", "B"])
        assert rec.calls.last.request.url.params["fields"] == "A,B"
    finally:
        await client.shutdown()


@respx.mock
async def test_only_flag_sent_on_lists_and_object():
    cat = respx.get(url__startswith="http://test/ai/v1/catalogs/X").mock(
        return_value=httpx.Response(200, json={"data": []}))
    doc = respx.get(url__startswith="http://test/ai/v1/documents/X").mock(
        return_value=httpx.Response(200, json={"data": []}))
    obj = respx.get(url__startswith="http://test/ai/v1/object/Catalogs/X/u-1").mock(
        return_value=httpx.Response(200, json={"data": {}, "tabular_section_counts": {}}))
    client = _live_client()
    await client.startup()
    try:
        await client.list_catalog("X", None, 5, None, fields=["A"], only=True)
        assert cat.calls.last.request.url.params["only"] == "true"
        await client.filter_catalog("X", None, None, False, False, 5, fields=["A"], only=True)
        assert cat.calls.last.request.url.params["only"] == "true"
        await client.list_documents("X", "2000-01-01", None, None, 5, fields=["A"], only=True)
        assert doc.calls.last.request.url.params["only"] == "true"
        await client.get_object("Catalogs", "X", "u-1", fields=["A"], only=True, additional=True)
        q = obj.calls.last.request.url.params
        assert q["only"] == "true" and q["additional"] == "true"
        await client.list_catalog("X", None, 5, None)
        assert "only" not in cat.calls.last.request.url.params
    finally:
        await client.shutdown()


@respx.mock
async def test_related_documents_404_envelope_passes_through_without_envelope_is_old_module():
    client = _live_client()
    await client.startup()
    try:
        # Envelope 404 whose text has no "not found": still a real answer, not "old module".
        respx.get(url__startswith="http://test/ai/v1/related/").mock(
            return_value=httpx.Response(404, json={"error": {
                "code": "NOT_FOUND", "message": "Объект не найден"}}))
        with pytest.raises(NotFoundError) as exc_info:
            await client.related_documents("Catalogs", "X", "u-1")
        assert "older" not in str(exc_info.value)
        assert "Объект не найден" in str(exc_info.value)
        # Platform "no URL template": no envelope, whatever the text says.
        respx.get(url__startswith="http://test/ai/v1/related/").mock(
            return_value=httpx.Response(404, text="Resource not found"))
        with pytest.raises(NotFoundError) as exc_info:
            await client.related_documents("Catalogs", "X", "u-2")
        assert "older than 1.0.41.6" in str(exc_info.value)
    finally:
        await client.shutdown()


@respx.mock
async def test_related_documents_path_encodes_cyrillic_type_and_reserved_id():
    route = respx.get(url__startswith="http://test/ai/v1/related/").mock(
        return_value=httpx.Response(200, json={"data": []}))
    client = _live_client()
    await client.startup()
    try:
        await client.related_documents("Catalogs", "Контрагенты", "a/b?c#d e")
        raw = route.calls.last.request.url.raw_path.decode()
        assert raw == ("/ai/v1/related/Catalogs/%D0%9A%D0%BE%D0%BD%D1%82%D1%80%D0%B0%D0%B3"
                       "%D0%B5%D0%BD%D1%82%D1%8B/a%2Fb%3Fc%23d%20e")
    finally:
        await client.shutdown()


@respx.mock
async def test_full_text_search_params_and_errors():
    ok = respx.get(url__startswith="http://test/ai/v1/metadata/all").mock(
        return_value=httpx.Response(200, json={
            "query": "ромашка ТОВ", "index_up_to_date": True, "index_updated": False,
            "metadata_filter_applied": None, "total": 0, "count": 0, "truncated": False,
            "data": []}))
    client = _live_client()
    await client.startup()
    try:
        res = await client.full_text_search("ромашка ТОВ", limit=5,
                                            metadata=["Catalog.X", "Document.Y"])
        assert res["count"] == 0
        url = str(ok.calls.last.request.url)
        assert "fts=%D1%80" in url and "%20" in url and "+" not in url.split("?", 1)[1]
        q = ok.calls.last.request.url.params
        assert (q["limit"], q["metadata"]) == ("5", "Catalog.X,Document.Y")
        # Old module ignores fts and returns the metadata list.
        respx.get(url__startswith="http://test/ai/v1/metadata/all").mock(
            return_value=httpx.Response(200, json={"catalogs": []}))
        with pytest.raises(UpstreamError) as exc_info:
            await client.full_text_search("x")
        assert "older than 1.0.41.6" in str(exc_info.value)
        for status in (400, 422):
            respx.get(url__startswith="http://test/ai/v1/metadata/all").mock(
                return_value=httpx.Response(status, json={"error": {
                    "code": "FTS_DISABLED", "message": "full-text search is disabled"}}))
            with pytest.raises(UpstreamError) as exc_info:
                await client.full_text_search("x")
            assert exc_info.value.code == "FTS_DISABLED"
    finally:
        await client.shutdown()


async def test_full_text_search_mock_mode():
    client = MCBPClient(ConnectionConfig(mock=True))
    res = await client.full_text_search("абв")
    assert res["query"] == "абв" and res["data"][0]["type"].startswith("Catalog.")
