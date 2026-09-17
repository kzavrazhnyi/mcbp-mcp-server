"""MCBPClient: typed error classification (`error.code` → exception type) and `patch_object`."""
from __future__ import annotations

import httpx
import pytest
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient
from mcbp_core.errors import (
    ConversionNotConfiguredError,
    ForbiddenError,
    LicenseRequiredError,
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
        assert result == {"status": "ok", "service": "MCBP_AI", "key": True}
    finally:
        await client.shutdown()


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
    finally:
        await client.shutdown()


async def test_health_mock_mode_still_usable():
    client = MCBPClient(ConnectionConfig(mock=True))
    await client.startup()
    result = await client.health()
    assert result["status"] == "ok"
    assert result["service"] == "MCBP_AI"
    assert result["key"] is True


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
