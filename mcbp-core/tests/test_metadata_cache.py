"""MetadataCache: sharing one cache between clients of the same base, and single-flight.

Why this is safe to share: on the BAS side the metadata routes run privileged
(`SetPrivilegedMode(True)` in `ai_metadata_get` / `ai_document_schema_get`), so their answer
does not depend on the caller's rights — unlike the data routes, which check `AccessRight`.
"""
from __future__ import annotations

import asyncio

import httpx
import respx

from mcbp_core.client import ConnectionConfig, MCBPClient, MetadataCache

INVENTORY = {"data": {"Catalogs": ["Номенклатура"]}}


def _client(cache: MetadataCache | None = None) -> MCBPClient:
    return MCBPClient(
        ConnectionConfig(base_url="http://test", user="u", password="p", mock=False),
        metadata_cache=cache,
    )


@respx.mock
async def test_shared_cache_serves_a_second_client_without_a_request():
    """Two users of one base: the first warms the inventory, the second only gets hits."""
    route = respx.get("http://test/ai/v1/metadata/all").mock(
        return_value=httpx.Response(200, json=INVENTORY)
    )
    cache = MetadataCache()
    first, second = _client(cache), _client(cache)
    await first.startup()
    await second.startup()
    try:
        assert await first.prefetch_metadata() == 1
        assert await second.prefetch_metadata() == 1
        assert route.call_count == 1, "second client must not re-fetch the inventory"
        assert second.metadata_cache_stats()["hits"] == 1
    finally:
        await first.shutdown()
        await second.shutdown()


@respx.mock
async def test_separate_caches_stay_isolated():
    """Default behaviour is unchanged: no shared cache passed → each client fetches its own."""
    route = respx.get("http://test/ai/v1/metadata/all").mock(
        return_value=httpx.Response(200, json=INVENTORY)
    )
    first, second = _client(), _client()
    await first.startup()
    await second.startup()
    try:
        await first.prefetch_metadata()
        await second.prefetch_metadata()
        assert route.call_count == 2
    finally:
        await first.shutdown()
        await second.shutdown()


@respx.mock
async def test_concurrent_misses_collapse_into_one_request():
    """N sessions logging in together must warm the inventory once, not N times."""
    calls = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=INVENTORY)

    respx.get("http://test/ai/v1/metadata/all").mock(side_effect=slow)

    cache = MetadataCache()
    clients = [_client(cache) for _ in range(5)]
    for c in clients:
        await c.startup()
    try:
        await asyncio.gather(*(c.prefetch_metadata() for c in clients))
        assert calls == 1, f"expected single-flight, got {calls} requests"
        assert cache.stats() == {"entries": 1, "hits": 4, "misses": 1}
    finally:
        for c in clients:
            await c.shutdown()


@respx.mock
async def test_data_routes_are_not_cached():
    """Only metadata is shareable — data must still hit the base on every call."""
    route = respx.get("http://test/ai/v1/catalogs/Номенклатура").mock(
        return_value=httpx.Response(200, json={"data": [], "cursor": None})
    )
    cache = MetadataCache()
    client = _client(cache)
    await client.startup()
    try:
        await client.list_catalog("Номенклатура", None, 10, None)
        await client.list_catalog("Номенклатура", None, 10, None)
        assert route.call_count == 2
        assert cache.stats()["entries"] == 0
    finally:
        await client.shutdown()
