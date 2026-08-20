"""Recording fake of the client: captures **kwargs** of every call, to prove that the
`mcbp_core.tools` executors pass arguments BY NAME (not positionally — otherwise a reordering
of the client's parameters would slip through unnoticed, see plan R3)."""
from __future__ import annotations

from typing import Any

import pytest

from mcbp_core.tools import TOOLS


class RecordingClient:
    """A fake MCBPClient — method signatures match mcbp_core.client.MCBPClient; every call
    records (method_name, kwargs) and returns a fixed result."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method: str, **kw: Any) -> dict:
        self.calls.append((method, kw))
        return {"data": [], "cursor": None}

    async def list_catalog(self, type_, cursor, limit, q, fields=None):
        return self._record("list_catalog", type_=type_, cursor=cursor, limit=limit, q=q,
                             fields=fields)

    async def list_documents(self, type_, frm, to, cursor, limit, filters=None, fields=None,
                              orderby=None, desc=False, agg=None, groupby=None):
        return self._record("list_documents", type_=type_, frm=frm, to=to, cursor=cursor,
                             limit=limit, filters=filters, fields=fields, orderby=orderby,
                             desc=desc, agg=agg, groupby=groupby)

    async def document_schema(self, type_):
        return self._record("document_schema", type_=type_)

    async def register_balance(self, type_, filters):
        return self._record("register_balance", type_=type_, filters=filters)

    async def filter_catalog(self, type_, filters, orderby, desc, exclude_groups, limit,
                              cursor=None, fields=None, agg=None, groupby=None):
        return self._record("filter_catalog", type_=type_, filters=filters, orderby=orderby,
                             desc=desc, exclude_groups=exclude_groups, limit=limit, cursor=cursor,
                             fields=fields, agg=agg, groupby=groupby)

    async def list_metadata(self, kind):
        return self._record("list_metadata", kind=kind)

    async def describe_metadata(self, kind, type_, tabular_section=None, q=None):
        return self._record("describe_metadata", kind=kind, type_=type_,
                             tabular_section=tabular_section, q=q)

    async def get_object(self, kind, type_, object_id):
        return self._record("get_object", kind=kind, type_=type_, object_id=object_id)

    async def health(self):
        return self._record("health")

    async def create_object(self, type_, body, key=None, object_id=None, base=None):
        return self._record("create_object", type_=type_, body=body, key=key,
                             object_id=object_id, base=base)

    async def push_ai_context(self, body):
        return self._record("push_ai_context", body=body)

    async def patch_object(self, kind, type_, object_id, fields):
        return self._record("patch_object", kind=kind, type_=type_, object_id=object_id,
                             fields=fields)

    async def register_records(self, type_, date_from=None, date_to=None, filters=None,
                                orderby=None, desc=False, limit=None):
        return self._record("register_records", type_=type_, date_from=date_from,
                             date_to=date_to, filters=filters, orderby=orderby, desc=desc,
                             limit=limit)


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


async def test_search_catalog_defaults_limit(client):
    await TOOLS["search_catalog"].executor(client, {"type": "Контрагенты"})
    method, kw = client.calls[0]
    assert method == "list_catalog"
    assert kw == {"type_": "Контрагенты", "cursor": None, "limit": 50, "q": None, "fields": None}


async def test_get_documents_defaults_limit_and_desc_bool(client):
    await TOOLS["get_documents"].executor(client, {"type": "ЗаказПокупателя"})
    method, kw = client.calls[0]
    assert method == "list_documents"
    assert kw["limit"] == 100
    assert kw["desc"] is False
    assert kw["type_"] == "ЗаказПокупателя"


async def test_get_documents_desc_coerced_to_bool(client):
    await TOOLS["get_documents"].executor(client, {"type": "X", "desc": 1})
    _, kw = client.calls[0]
    assert kw["desc"] is True
    assert isinstance(kw["desc"], bool)


async def test_get_schema_maps_type(client):
    await TOOLS["get_schema"].executor(client, {"type": "ЗаказПокупателя"})
    method, kw = client.calls[0]
    assert method == "document_schema"
    assert kw == {"type_": "ЗаказПокупателя"}


async def test_register_balance_merges_top_level_keys_into_filters(client):
    await TOOLS["get_register_balance"].executor(
        client, {"type": "MCBP_Debt", "Контрагент": "uuid-1", "filters": {"Организация": "uuid-2"}}
    )
    method, kw = client.calls[0]
    assert method == "register_balance"
    assert kw["type_"] == "MCBP_Debt"
    assert kw["filters"] == {"Организация": "uuid-2", "Контрагент": "uuid-1"}


async def test_filter_catalog_defaults(client):
    await TOOLS["filter_catalog"].executor(client, {"type": "Контрагенты"})
    method, kw = client.calls[0]
    assert method == "filter_catalog"
    assert kw["filters"] == {}
    assert kw["desc"] is False
    assert kw["exclude_groups"] is False
    assert kw["limit"] == 100


async def test_list_metadata_maps_metadata_key(client):
    await TOOLS["list_metadata"].executor(client, {"metadata": "Catalogs"})
    method, kw = client.calls[0]
    assert method == "list_metadata"
    assert kw == {"kind": "Catalogs"}


async def test_describe_metadata_maps_metadata_and_type(client):
    await TOOLS["describe_metadata"].executor(client, {"metadata": "Catalogs", "type": "Контрагенты"})
    method, kw = client.calls[0]
    assert method == "describe_metadata"
    assert kw == {"kind": "Catalogs", "type_": "Контрагенты", "tabular_section": None, "q": None}


async def test_describe_metadata_maps_tabular_section(client):
    await TOOLS["describe_metadata"].executor(
        client, {"metadata": "Documents", "type": "ЗаказПокупателя", "tabular_section": "Запасы"}
    )
    method, kw = client.calls[0]
    assert method == "describe_metadata"
    assert kw["tabular_section"] == "Запасы"


async def test_describe_metadata_maps_q(client):
    await TOOLS["describe_metadata"].executor(
        client, {"metadata": "Documents", "type": "ЗаказПокупателя", "q": "статус"}
    )
    method, kw = client.calls[0]
    assert method == "describe_metadata"
    assert kw["q"] == "статус"


async def test_get_object_maps_id_to_object_id(client):
    await TOOLS["get_object"].executor(
        client, {"metadata": "Catalogs", "type": "Контрагенты", "id": "uuid-1"}
    )
    method, kw = client.calls[0]
    assert method == "get_object"
    assert kw == {"kind": "Catalogs", "type_": "Контрагенты", "object_id": "uuid-1"}


async def test_health_takes_no_arguments(client):
    await TOOLS["health"].executor(client, {})
    method, kw = client.calls[0]
    assert method == "health"
    assert kw == {}


async def test_write_object_maps_payload_and_id(client):
    await TOOLS["write_object"].executor(
        client, {"type": "Контрагенты", "payload": {"Наименование": "X"}, "id": "uuid-1"}
    )
    method, kw = client.calls[0]
    assert method == "create_object"
    assert kw == {"type_": "Контрагенты", "body": {"Наименование": "X"}, "key": None,
                  "object_id": "uuid-1", "base": None}


async def test_save_context_builds_body(client):
    await TOOLS["save_context"].executor(
        client, {"conversation_id": "c1", "role": "user", "content": "привіт"}
    )
    method, kw = client.calls[0]
    assert method == "push_ai_context"
    assert kw == {"body": {"conversation_id": "c1", "role": "user", "content": "привіт"}}


async def test_patch_object_maps_metadata_type_id_fields(client):
    await TOOLS["patch_object"].executor(
        client, {"metadata": "Documents", "type": "ЗаказПокупателя", "id": "uuid-1",
                 "fields": {"СостояниеЗаказа": "uuid-2"}}
    )
    method, kw = client.calls[0]
    assert method == "patch_object"
    assert kw == {"kind": "Documents", "type_": "ЗаказПокупателя", "object_id": "uuid-1",
                  "fields": {"СостояниеЗаказа": "uuid-2"}}


async def test_register_records_defaults_limit_and_desc_bool(client):
    await TOOLS["get_register_records"].executor(client, {"type": "MCBP_StatusFunctions"})
    method, kw = client.calls[0]
    assert method == "register_records"
    assert kw["type_"] == "MCBP_StatusFunctions"
    assert kw["limit"] == 100
    assert kw["desc"] is False
    assert kw["date_from"] is None
    assert kw["date_to"] is None


async def test_register_records_maps_from_to_filters_orderby(client):
    await TOOLS["get_register_records"].executor(
        client, {"type": "MCBP_StatusFunctions", "from": "2026-01-01", "to": "2026-08-01",
                 "filters": {"Контрагент": "uuid-1"}, "orderby": "Period", "desc": True}
    )
    method, kw = client.calls[0]
    assert method == "register_records"
    assert kw["date_from"] == "2026-01-01"
    assert kw["date_to"] == "2026-08-01"
    assert kw["filters"] == {"Контрагент": "uuid-1"}
    assert kw["orderby"] == "Period"
    assert kw["desc"] is True


async def test_register_records_desc_coerced_to_bool(client):
    await TOOLS["get_register_records"].executor(client, {"type": "X", "desc": 1})
    _, kw = client.calls[0]
    assert kw["desc"] is True
    assert isinstance(kw["desc"], bool)
