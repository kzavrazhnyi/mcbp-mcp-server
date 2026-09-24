"""Перелік структури за правами користувача: `?access=read` на сеанс, поверх спільного кешу."""

from __future__ import annotations

import httpx
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient, MetadataCache

BASE = "http://bas/hs"
ALL = {
    "catalogs": [{"name": "Банки"}, {"name": "Валюты"}, {"name": "Контрагенты"}],
    "documents": [{"name": "ЗаказПокупателя"}],
    "enums": [{"name": "ВидыОплат"}],
}
READABLE = {"catalogs": [{"name": "Валюты"}], "documents": [], "enums": [{"name": "ВидыОплат"}]}
CATALOGS = {"metadata": "catalog", "count": 3, "items": ALL["catalogs"]}


def _client(cache: MetadataCache | None = None) -> MCBPClient:
    return MCBPClient(ConnectionConfig(base_url=BASE, user="u", password="p", mock=False), cache)


def _routes(mock: respx.MockRouter) -> respx.Route:
    readable = mock.get(f"{BASE}/ai/v1/metadata/all", params={"access": "read"}).mock(
        return_value=httpx.Response(200, json=READABLE)
    )
    mock.get(f"{BASE}/ai/v1/metadata/all").mock(return_value=httpx.Response(200, json=ALL))
    mock.get(f"{BASE}/ai/v1/metadata/Catalogs").mock(return_value=httpx.Response(200, json=CATALOGS))
    return readable


async def test_readable_view_hides_what_the_user_cannot_read_in_all_and_in_one_kind():
    with respx.mock(assert_all_called=False) as mock:
        _routes(mock)
        client = _client()
        await client.startup()
        try:
            assert await client.load_readable_metadata() == 2
            everything = await client.list_readable_metadata("all")
            catalogs = await client.list_readable_metadata("Catalogs")
        finally:
            await client.shutdown()
    assert everything["catalogs"] == [{"name": "Валюты"}]
    assert everything["documents"] == []
    assert everything["enums"] == [{"name": "ВидыОплат"}]
    assert catalogs["items"] == [{"name": "Валюты"}]
    assert catalogs["count"] == 1


async def test_filtering_never_touches_the_cache_shared_with_other_users():
    cache = MetadataCache()
    with respx.mock(assert_all_called=False) as mock:
        _routes(mock)
        restricted, admin = _client(cache), _client(cache)
        await restricted.startup()
        await admin.startup()
        try:
            await restricted.load_readable_metadata()
            await restricted.list_readable_metadata("all")
            await restricted.list_readable_metadata("Catalogs")
            full = await admin.list_metadata("all")
            full_catalogs = await admin.list_metadata("Catalogs")
            masking_view = await restricted.list_metadata("all")
        finally:
            await restricted.shutdown()
            await admin.shutdown()
    assert len(full["catalogs"]) == 3, "фільтр одного користувача зіпсував спільний кеш"
    assert full_catalogs["count"] == 3
    assert len(masking_view["catalogs"]) == 3, "list_metadata лишається повним — для маскування"


async def test_the_rights_answer_is_per_user_and_not_cached():
    cache = MetadataCache()
    with respx.mock(assert_all_called=False) as mock:
        readable = _routes(mock)
        a, b = _client(cache), _client(cache)
        await a.startup()
        await b.startup()
        try:
            await a.load_readable_metadata()
            await b.load_readable_metadata()
        finally:
            await a.shutdown()
            await b.shutdown()
    assert readable.call_count == 2


async def test_old_base_ignoring_access_param_hides_nothing():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/ai/v1/metadata/all").mock(return_value=httpx.Response(200, json=ALL))
        client = _client()
        await client.startup()
        try:
            await client.load_readable_metadata()
            assert await client.list_readable_metadata("all") == ALL
        finally:
            await client.shutdown()


async def test_rights_are_loaded_once_on_first_use_when_the_host_did_not_load_them():
    """MCP-сервер не вантажить права сам — клієнт робить це при першому зверненні."""
    with respx.mock(assert_all_called=False) as mock:
        readable = _routes(mock)
        client = _client()
        await client.startup()
        try:
            assert (await client.list_readable_metadata("Catalogs"))["count"] == 1
            await client.list_readable_metadata("all")
        finally:
            await client.shutdown()
    assert readable.call_count == 1


async def test_failed_rights_load_leaves_the_list_unfiltered_and_is_not_retried():
    with respx.mock(assert_all_called=False) as mock:
        readable = mock.get(f"{BASE}/ai/v1/metadata/all", params={"access": "read"}).mock(
            return_value=httpx.Response(500, json={"error": {"code": "X", "message": "boom"}})
        )
        mock.get(f"{BASE}/ai/v1/metadata/Catalogs").mock(
            return_value=httpx.Response(200, json=CATALOGS)
        )
        client = _client()
        await client.startup()
        try:
            assert (await client.list_readable_metadata("Catalogs"))["count"] == 3
            assert (await client.list_readable_metadata("Catalogs"))["count"] == 3
        finally:
            await client.shutdown()
    assert readable.call_count == 1
