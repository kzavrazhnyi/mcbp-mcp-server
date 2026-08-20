"""Thin async client for the BAS-side MCBP_AI HTTP service.

Two jobs:
  1. Talk to /ai/v1/* over httpx with a connection pool (1C reuses sessions for
     ~20s, so a persistent pool matters). Auth = HTTP Basic (як публікується сервіс).
  2. Normalise responses. The NEW MCBP_AI service returns real HTTP codes, but we
     still defensively detect the legacy "success:false + string in data/answer"
     shape (MCBP_Exchange) and raise typed MCBPError subclasses.

Set ConnectionConfig.mock=True to run the whole client with no BAS at all.

This module has NO dependency on any host application's settings — callers build a
`ConnectionConfig` from their own config (env vars, CLI args, ...) and pass it in.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from mcbp_core.errors import (
    AuthError,
    ConversionChangedError,
    ConversionNotConfiguredError,
    KeyMismatchError,
    MCBPError,
    NotConfiguredError,
    NotFoundError,
    ParameterError,
    PlusRequiredError,
    UpstreamError,
    WriteFailedError,
)

# Map known BAS error strings → typed exceptions.
_ERROR_MARKERS: list[tuple[str, type]] = [
    ("Key not found", KeyMismatchError),
    ("MCBP Plus not found", PlusRequiredError),
    ("not found!", ParameterError),  # "Parameter type not found!" etc.
    ("format YYYYMMDD", ParameterError),
]

# `error.code` (the `/ai/v1` error envelope — .claude/refs/onec-tool-contract.md §2) → typed
# exception. Lets a caller branch on exception TYPE (e.g. distinguish PLUS_REQUIRED from
# CONVERSION_NOT_CONFIGURED) instead of every >=400 response collapsing into UpstreamError.
_ERROR_CODE_MAP: dict[str, type[MCBPError]] = {
    "KEY_MISMATCH": KeyMismatchError,
    "BAD_JSON": ParameterError,
    "BAD_PARAMETER": ParameterError,
    "NOT_FOUND": NotFoundError,
    "WRITE_FAILED": WriteFailedError,
    "PLUS_REQUIRED": PlusRequiredError,
    "CONVERSION_NOT_CONFIGURED": ConversionNotConfiguredError,
    "CONVERSION_CHANGED": ConversionChangedError,
    "NOT_CONFIGURED": NotConfiguredError,
    "UPSTREAM_ERROR": UpstreamError,
}


log = logging.getLogger("mcbp.api")

# Retry only idempotent methods: a POST (create_object/push_ai_context) writes through MCBP
# Plus, and retrying after a partial success could create a duplicate object.
_RETRYABLE_METHODS = frozenset({"GET", "PATCH"})
_MAX_RETRIES = 2


def _typed_error_from_body(text: str) -> Exception | None:
    """Parses the `{"error": {"code","message"}}` envelope BAS sends on 4xx/5xx and maps
    `code` via `_ERROR_CODE_MAP`. Returns None when the body isn't that shape (malformed JSON,
    a non-JSON error page, ...) — the caller then falls back to status-code-only classification,
    so behavior for non-conforming bodies is unchanged."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict) or "code" not in error:
        return None
    message = error.get("message") or text[:300]
    return _ERROR_CODE_MAP.get(error["code"], UpstreamError)(message)


@dataclass
class ConnectionConfig:
    """Explicit connection parameters — no host-app Settings object required."""

    base_url: str = "http://localhost/base/hs"
    user: str = ""
    password: str = ""
    timeout_s: float = 30.0
    pool_max: int = 10
    mock: bool = True
    verify: bool = True


def _classify_legacy_error(text: str) -> Exception:
    for marker, exc in _ERROR_MARKERS:
        if marker.lower() in text.lower():
            return exc(text)
    return UpstreamError(text)


_LOG_BODY_LIMIT = 500
_LOG_RESPONSE_LIMIT = 800


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated, {len(text)} chars total>"


def _kw_brief(kw: dict) -> str:
    """Опис параметрів запиту для лога. Тіла запитів до MCBP_AI малі й не містять секретів,
    тож для dict-тіла логується сам JSON (обрізаний), а не лише ключі — значення часто
    і є причиною відмови (напр. складене посилальне поле без "ІмяТипу:")."""
    parts = []
    if kw.get("params"):
        parts.append(f"params={kw['params']}")
    body = kw.get("json")
    if body is not None:
        if isinstance(body, dict):
            body_str = _truncate(json.dumps(body, ensure_ascii=False), _LOG_BODY_LIMIT)
            parts.append(f"body={body_str}")
        else:
            parts.append(f"body_type={type(body).__name__}")
    return " ".join(parts)


def _matches_q(attr: dict, q_lower: str) -> bool:
    name = attr.get("name") or ""
    synonym = attr.get("synonym") or ""
    return q_lower in name.lower() or q_lower in synonym.lower()


def _filter_header_attributes(view: dict, q: str) -> dict:
    """Narrows `standard_attributes`/`attributes` (header reqisites only — tabular sections are
    untouched) to those matching `q` by name OR synonym, case-insensitive. No match at all does
    NOT return emptiness or the full tree: it falls back to a flat list of every available name
    (`available_names`), per `onec-universality` — a narrowed/empty view must list real names so
    the caller can self-correct."""
    q_lower = q.lower()
    std = view.get("standard_attributes") or []
    attrs = view.get("attributes") or []
    std_matched = [a for a in std if _matches_q(a, q_lower)]
    attrs_matched = [a for a in attrs if _matches_q(a, q_lower)]
    total = len(std) + len(attrs)
    matched_count = len(std_matched) + len(attrs_matched)

    if matched_count == 0:
        available_names = [a.get("name") for a in std] + [a.get("name") for a in attrs]
        return {
            **view,
            "standard_attributes": [],
            "attributes": [],
            "q": q,
            "attributes_total": total,
            "attributes_omitted": total,
            "available_names": available_names,
        }

    return {
        **view,
        "standard_attributes": std_matched,
        "attributes": attrs_matched,
        "q": q,
        "attributes_total": total,
        "attributes_omitted": total - matched_count,
    }


def _describe_metadata_view(full: dict, tabular_section: str | None, q: str | None = None) -> dict:
    """Builds the response `describe_metadata` returns from the cached full tree — see
    `MCBPClient.describe_metadata` for the summarize-by-default and `q`-filter rationale."""
    view = full
    sections = full.get("tabular_sections")
    if sections:
        if tabular_section:
            matched = [s for s in sections if s.get("name") == tabular_section]
            if not matched:
                names = [s.get("name") for s in sections]
                raise ParameterError(
                    f"tabular_section '{tabular_section}' not found in {full.get('type')}; "
                    f"available: {names}"
                )
            view = {**full, "tabular_sections": matched}
        else:
            summary = [
                {"name": s.get("name"), "synonym": s.get("synonym"),
                 "attributes_count": len(s.get("attributes") or [])}
                for s in sections
            ]
            view = {**full, "tabular_sections": summary}
    if q:
        view = _filter_header_attributes(view, q)
    return view


class MCBPClient:
    def __init__(self, config: ConnectionConfig):
        self._cfg = config
        self._mock = config.mock
        self._client: httpx.AsyncClient | None = None
        # Metadata is configuration structure, not data: it cannot change while a connection is
        # open (a configuration change needs the base restarted/updated), so the same tree was
        # being re-fetched on every model turn for nothing. Cache lives on the client instance,
        # so a re-login builds a new client and therefore a fresh cache — that is the invalidation.
        self._meta_cache: dict[str, Any] = {}
        self._meta_hits = 0
        self._meta_misses = 0

    async def startup(self) -> None:
        if self._mock:
            return
        auth = base64.b64encode(
            f"{self._cfg.user}:{self._cfg.password}".encode()
        ).decode()
        self._client = httpx.AsyncClient(
            base_url=self._cfg.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            timeout=self._cfg.timeout_s,
            limits=httpx.Limits(max_connections=self._cfg.pool_max),
            verify=self._cfg.verify,
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kw) -> Any:
        path = path.lstrip("/")  # httpx base_url requires relative paths
        brief = _kw_brief(kw)
        if self._mock:
            log.info("[mock] %s %s %s", method, path, brief)
            return _mock_response(method, path, kw)
        assert self._client is not None, "MCBPClient.startup() not called"

        # 1C does not decode '+' as a space, and httpx builds its query with quote_plus — so any
        # filter value containing a space matched nothing at all, with no error to notice. Encode
        # the query ourselves (space -> %20) instead of handing `params` to httpx.
        params = kw.pop("params", None)
        if params:
            path = f"{path}?{urlencode(params, quote_via=quote)}"

        # Up to _MAX_RETRIES retries on 503 / ConnectError, GET and PATCH only (see
        # _RETRYABLE_METHODS above). Backoff grows with each attempt so a flapping upstream
        # doesn't get hammered.
        attempt = 0
        while True:
            start = time.monotonic()
            try:
                resp = await self._client.request(method, path, **kw)
            except httpx.ConnectError as e:
                if method in _RETRYABLE_METHODS and attempt < _MAX_RETRIES:
                    log.warning(
                        "%s %s %s ✗ connect error, retrying (%d/%d): %s",
                        method, path, brief, attempt + 1, _MAX_RETRIES, e,
                    )
                    await asyncio.sleep(0.3 * (attempt + 1))
                    attempt += 1
                    continue
                log.warning("%s %s %s ✗ transport error: %s", method, path, brief, e)
                raise UpstreamError(f"transport error: {e}") from e
            except httpx.HTTPError as e:
                log.warning("%s %s %s ✗ transport error: %s", method, path, brief, e)
                raise UpstreamError(f"transport error: {e}") from e

            elapsed_ms = (time.monotonic() - start) * 1000
            log.info("%s %s %s → %s (%.0f ms)", method, path, brief, resp.status_code, elapsed_ms)

            if resp.status_code == 503 and method in _RETRYABLE_METHODS and attempt < _MAX_RETRIES:
                log.warning(
                    "%s %s %s ✗ 503, retrying (%d/%d)",
                    method, path, brief, attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(0.3 * (attempt + 1))
                attempt += 1
                continue
            break

        if resp.status_code >= 400:
            typed = _typed_error_from_body(resp.text)
            body_note = "" if typed is not None else " (body is not the {\"error\":{...}} envelope)"
            log.warning(
                "%s %s → %s%s: %s",
                method, path, resp.status_code, body_note,
                _truncate(resp.text, _LOG_RESPONSE_LIMIT),
            )
            if typed is not None:
                raise typed
            # Body doesn't match the {"error":{"code",...}} envelope — fall back to the
            # status-code-only classification (unchanged from before).
            if resp.status_code == 404:
                raise NotFoundError(path)
            if resp.status_code in (401, 403):
                raise AuthError(f"BAS rejected credentials ({resp.status_code}): {resp.text[:200]}")
            raise UpstreamError(f"BAS returned {resp.status_code}: {resp.text[:300]}")

        payload = resp.json()
        # Defensive: detect the "success:false" failure shape (legacy MCBP_Exchange, or a Plus
        # call that returns 200 with {success:false, data:...}). Raise regardless of whether the
        # detail is a string or a structure, so a failed result never passes as a valid answer.
        if isinstance(payload, dict) and payload.get("success") is False:
            blob = payload.get("data") or payload.get("answer") or payload.get("error")
            if isinstance(blob, str) and blob:
                raise _classify_legacy_error(blob)
            raise UpstreamError(f"upstream reported success=false: {str(blob)[:200]}")
        return payload

    # --- High-level methods (the surface the rest of the app uses) ---
    async def health(self) -> dict:
        # Robust to a body missing "status"/"service"/"key" (older base) — never raise on shape.
        # `key:false` means every route but /health answers 403 KEY_MISMATCH, so this field must
        # reflect what BAS actually said, not a hardcoded guess.
        payload = await self._request("GET", "/ai/v1/health")
        if not isinstance(payload, dict):
            payload = {}
        return {
            "status": payload.get("status", "ok"),
            "service": payload.get("service", "MCBP_AI"),
            "key": bool(payload.get("key", True)),
        }

    async def list_catalog(self, type_: str, cursor: str | None, limit: int, q: str | None,
                           fields: list | str | None = None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if q:
            params["q"] = q
        if fields:
            params["fields"] = ",".join(fields) if isinstance(fields, list) else fields
        return await self._request("GET", f"/ai/v1/catalogs/{type_}", params=params)

    async def filter_catalog(self, type_: str, filters: dict | None, orderby: str | None,
                             desc: bool, exclude_groups: bool, limit: int,
                             cursor: str | None = None, fields: list | str | None = None,
                             agg: list[str] | str | None = None,
                             groupby: str | None = None) -> dict:
        """Server-side filter by ANY field (+optional sort / group-exclusion). Field names
        are the real metadata names (from describe_metadata); filtering/typing happens in BAS.
        Reference fields are filtered by passing the object's Ref UUID as the value.

        `agg` (e.g. ["count"], ["sum:СуммаДокумента"]) switches the response to
        {"aggregates": [...], "groupby": ...} instead of {"data": [...], "cursor": ...} — see
        .claude/refs/onec-tool-contract.md. Incompatible with `cursor`. `groupby` groups the
        aggregate by one field; without `agg` it is ignored."""
        params: dict[str, Any] = {"limit": limit}
        for k, v in (filters or {}).items():
            params[f"f.{k}"] = v  # 'f.' prefix keeps filters separate from reserved params
        if orderby:
            params["orderby"] = orderby
        if desc:
            params["desc"] = "true"
        if exclude_groups:
            params["excludeGroups"] = "true"
        if cursor:
            params["cursor"] = cursor
        if fields:
            params["fields"] = ",".join(fields) if isinstance(fields, list) else fields
        if agg:
            params["agg"] = ",".join(agg) if isinstance(agg, list) else agg
        if groupby:
            params["groupby"] = groupby
        return await self._request("GET", f"/ai/v1/catalogs/{type_}", params=params)

    async def list_documents(self, type_: str, frm: str | None, to: str | None,
                             cursor: str | None, limit: int,
                             filters: dict | None = None, fields: list | str | None = None,
                             orderby: str | None = None, desc: bool = False,
                             agg: list[str] | str | None = None,
                             groupby: str | None = None) -> dict:
        """Documents of a type over a period, with optional server-side field filters
        (incl. reference fields, e.g. {"Контрагент": "<uuid>"}) and extra header fields
        returned in each row (e.g. ["Контрагент", "СуммаДокумента"]).
        `orderby`/`desc` sort server-side (e.g. by "Дата"); without `orderby` row order is
        arbitrary (internal ref id), NOT chronological.

        `agg` (e.g. ["count"], ["sum:СуммаДокумента"]) switches the response to
        {"aggregates": [...], "groupby": ...} instead of {"data": [...], "cursor": ...} — see
        .claude/refs/onec-tool-contract.md. Incompatible with `cursor`. `groupby` groups the
        aggregate by one field; without `agg` it is ignored."""
        params: dict[str, Any] = {"limit": limit}
        if frm:
            params["from"] = frm
        # An omitted `to` used to be read upstream as "the single day `from`", so "everything
        # since 2000" answered 0 rows with no error. Fixed in BSL (open upper bound), but sent
        # explicitly here too: the fix only reaches a base after a manual Конфігуратор transfer,
        # and an explicit bound behaves identically on both versions.
        params["to"] = to if to else "3999-12-31"
        if cursor:
            params["cursor"] = cursor
        for k, v in (filters or {}).items():
            params[f"f.{k}"] = v  # 'f.' prefix keeps filters separate from reserved params
        if fields:
            params["fields"] = ",".join(fields) if isinstance(fields, list) else fields
        if orderby:
            params["orderby"] = orderby
        if desc:
            params["desc"] = "true"
        if agg:
            params["agg"] = ",".join(agg) if isinstance(agg, list) else agg
        if groupby:
            params["groupby"] = groupby
        return await self._request("GET", f"/ai/v1/documents/{type_}", params=params)

    async def document_schema(self, type_: str) -> dict:
        # Structure, not data — cached like the other metadata routes (see `_cached_get`).
        return await self._cached_get(f"/ai/v1/documents/{type_}/schema")

    async def create_object(self, type_: str, body: dict, key: str | None = None,
                            object_id: str | None = None, base: str | None = None) -> dict:
        """Write one object through MCBP Plus and its conversion rules.

        `body` is flat {field: value} — BAS builds the exchange envelope. `key` is the exchange
        key (same key = same object next time, omitted = a new one), `object_id` points at an
        object that already exists in the base, `base` names another exchange base by Identifier.
        """
        params: dict[str, Any] = {}
        if key:
            params["key"] = key
        if object_id:
            params["id"] = object_id
        if base:
            params["base"] = base
        return await self._request(
            "POST", f"/ai/v1/objects/{type_}", params=params or None, json=body,
        )

    async def register_balance(self, type_: str, filters: dict) -> dict:
        return await self._request("GET", f"/ai/v1/registers/{type_}/balance", params=filters)

    async def register_records(self, type_: str, date_from: str | None = None,
                               date_to: str | None = None, filters: dict | None = None,
                               orderby: str | None = None, desc: bool = False,
                               limit: int | None = None) -> dict:
        """Raw rows of an information register (no Ref — this is the ONLY route that can read
        them) or, for an accumulation register, raw movements (unlike `register_balance`'s
        computed balance). `date_from`/`date_to` are BOTH optional — an open bound when
        omitted. No cursor pagination exists (no Ref, no stable key); `truncated: true` in the
        response means narrow the filter, not "fetch the next page"."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to
        for k, v in (filters or {}).items():
            params[f"f.{k}"] = v  # 'f.' prefix keeps filters separate from reserved params
        if orderby:
            params["orderby"] = orderby
        if desc:
            params["desc"] = "true"
        return await self._request("GET", f"/ai/v1/registers/{type_}/records", params=params)

    async def push_ai_context(self, body: dict) -> dict:
        return await self._request("POST", "/ai/v1/ai/context", json=body)

    # --- Configuration introspection (works for ANY 1C configuration) ---
    async def _cached_get(self, path: str) -> Any:
        """GET a metadata route through the per-connection cache."""
        if path in self._meta_cache:
            self._meta_hits += 1
            log.info("GET %s → cache hit", path.lstrip("/"))
            return self._meta_cache[path]
        self._meta_misses += 1
        payload = await self._request("GET", path)
        self._meta_cache[path] = payload
        return payload

    async def prefetch_metadata(self) -> int:
        """Warm the cache with the whole-configuration object inventory (one request).

        Only the INVENTORY is prefetched, not every object's attribute tree: `all` is a single
        ~0.5 s call, while describing all ~1800 objects of a typical configuration would be ~1800.
        Per-object trees stay lazy — cached on first use. Returns the number of cached entries."""
        await self._cached_get("/ai/v1/metadata/all")
        return len(self._meta_cache)

    def metadata_cache_stats(self) -> dict:
        return {"entries": len(self._meta_cache), "hits": self._meta_hits,
                "misses": self._meta_misses}

    async def list_metadata(self, kind: str) -> dict:
        """kind='all' → object lists of every kind; kind='Catalogs'/'Documents'/... → list of one kind."""
        return await self._cached_get(f"/ai/v1/metadata/{kind}")

    async def describe_metadata(self, kind: str, type_: str,
                                tabular_section: str | None = None,
                                q: str | None = None) -> dict:
        """Attribute tree of one metadata object. The full tree (all tabular sections with
        their nested attributes) is fetched once and kept in `_meta_cache` regardless of
        `tabular_section`/`q` — only the VIEW returned to the caller changes:

        - `tabular_section` omitted (default): tabular sections are summarized to
          name/synonym/attribute count — no nested attributes. This is the majority of a
          typical document's payload and is rarely needed for every section at once.
        - `tabular_section` given: the full attribute list of that ONE section, nothing
          summarized. Unknown name -> ParameterError listing the real section names, so a
          caller can self-correct instead of guessing.
        - `q` given: narrows HEADER attributes (`standard_attributes`/`attributes`, not tabular
          sections) to those whose name OR synonym contains `q` (case-insensitive) — a document
          can carry 100+ header attributes, most of which the caller never needs, and `q` lets a
          Ukrainian synonym ("статус") find the internal name (`СостояниеЗаказа`) without
          returning the whole tree. No match -> a flat fallback list of every available name
          instead of emptiness or the full tree.
        """
        full = await self._cached_get(f"/ai/v1/metadata/{kind}/{type_}")
        return _describe_metadata_view(full, tabular_section, q)

    async def get_object(self, kind: str, type_: str, object_id: str) -> dict:
        """FULL data of one object (all attributes + tabular sections) by its Ref UUID.
        Lists return only standard fields; this is the drill-down for a specific record."""
        return await self._request("GET", f"/ai/v1/object/{kind}/{type_}/{object_id}")

    async def patch_object(self, kind: str, type_: str, object_id: str, fields: dict) -> dict:
        """Natively changes ONLY the named header attributes of an EXISTING object — no MCBP
        Plus, no conversion rule needed (unlike `create_object`). Every other field on the
        object is left untouched; tabular sections are out of scope (BAD_PARAMETER if named).
        Posted state is preserved automatically by BAS — a posted document is re-written with
        posting, an unposted one stays unposted; `fields` cannot itself toggle it.

        `fields` is a flat {field: value} dict — reference-field values are the target's Ref
        UUID, or "MetadataName:UUID" for a composite reference type (e.g. `СостояниеЗаказа`,
        which resolves to one of several catalogs). Response: {"metadata","type","id","changed"
        [{"field","old","new"}, ...], "posted"}."""
        return await self._request(
            "PATCH", f"/ai/v1/object/{kind}/{type_}/{object_id}", json=fields,
        )


# --- Mock data so the backend boots and the AI loop is testable without BAS ---
# Форма навмисно повторює реальний зріз demo-бази basmbdemo (українська типова):
# кириличні типи (Контрагенты / ЗаказПокупателя), латинізовані ключі полів,
# посилання як {Presentation, Data, Metadata}, курсор = UUID останнього запису.
def _mock_extra_fields(kw: dict) -> dict:
    """Echo the requested `fields` back into a mock row, so callers/tests can see they were sent."""
    fields = (kw.get("params") or {}).get("fields")
    if not fields:
        return {}
    names = fields.split(",") if isinstance(fields, str) else fields
    return {name.strip(): f"<mock:{name.strip()}>" for name in names if name and name.strip()}


def _mock_aggregates(kw: dict) -> dict | None:
    """Mirrors the BAS `agg=`/`groupby=` response shape ({"aggregates": [...], "groupby": ...})
    so tests/tool-loop can exercise it without a live base. Returns None when `agg` is absent —
    caller then falls through to the normal data/cursor mock."""
    params = kw.get("params") or {}
    agg = params.get("agg")
    if not agg:
        return None
    specs = agg.split(",") if isinstance(agg, str) else agg
    groupby = params.get("groupby")
    row: dict[str, Any] = {}
    for spec in specs:
        func = spec.split(":", 1)[0]
        if func == "count":
            row["count"] = 2
        else:
            field = spec.split(":", 1)[1]
            row[f"{func}_{field}"] = 1234.5
    if groupby:
        return {"aggregates": [dict(row, group="<mock-group-1>"),
                                dict(row, group="<mock-group-2>")], "groupby": groupby}
    return {"aggregates": [row], "groupby": None}


def _mock_response(method: str, path: str, kw: dict) -> Any:
    if path.endswith("/health"):
        return {"status": "ok", "service": "MCBP_AI", "key": True, "mock": True}
    if "/metadata/" in path:
        return _mock_metadata(path)
    if method == "PATCH" and "ai/v1/object/" in path:
        parts = path.rstrip("/").split("/")
        kind, type_, object_id = parts[-3], parts[-2], parts[-1]
        body = kw.get("json") or {}
        changed = [{"field": k, "old": "<mock:old>", "new": str(v)} for k, v in body.items()]
        return {"metadata": kind.lower(), "type": type_, "id": object_id,
                "changed": changed, "posted": True}
    if "/ai/v1/object/" in path:  # singular: full data of one object
        return {"metadata": "catalog", "type": path.split("/")[-2],
                 "data": [{"Kod": "000000002"}, {"Naimenovanie": "Альфа Трейд, ТОВ"},
                          {"Pokupatel": "true"}, {"KodPoEDRPOU": "314159265"}]}
    if "/ai/context" in path:
        return {"accepted": True, "conversation_id": "mock-conv"}
    if "/catalogs/" in path or "/documents/" in path:
        agg_result = _mock_aggregates(kw)
        if agg_result is not None:
            return agg_result
    if "/catalogs/" in path:
        # Compact: standard attributes only (canonical English keys) + Ref for drill-down,
        # plus any requested `fields` (echoed so tests can assert they were forwarded).
        rows = [
            {"Ref": {"Presentation": "Фурнітура південь, ТОВ", "Data": "e3f1d9b6-0001",
                     "Metadata": "Контрагенты"}, "Code": "000000001",
             "Description": "Фурнітура південь, ТОВ", "DeletionMark": False, "IsFolder": False},
            {"Ref": {"Presentation": "Альфа Трейд, ТОВ", "Data": "e3f1d9b6-0002",
                     "Metadata": "Контрагенты"}, "Code": "000000002",
             "Description": "Альфа Трейд, ТОВ", "DeletionMark": False, "IsFolder": False},
        ]
        for row in rows:
            row.update(_mock_extra_fields(kw))
        return {"data": rows, "cursor": None}
    if "/documents/" in path and path.endswith("/schema"):
        # /schema тепер віддає рівно ту саму форму, що й ai_metadata_get деталь
        # (MCBP_AI.MetadataDetail) — без окремого поля "fields".
        return {
            "metadata": "document",
            "type": path.split("/")[-2],
            "synonym": "Замовлення покупця",
            "standard_attributes": [
                {"name": "Проведен", "synonym": "Проведений", "types": ["Булево"]},
                {"name": "Ссылка", "synonym": "Посилання", "types": ["Документ.ЗаказПокупателя"]},
                {"name": "ПометкаУдаления", "synonym": "Позначка вилучення", "types": ["Булево"]},
                {"name": "Дата", "synonym": "Дата", "types": ["Дата"]},
                {"name": "Номер", "synonym": "Номер", "types": ["Рядок"], "length": 11},
            ],
            "attributes": [
                {"name": "Автор", "synonym": "Автор", "types": ["Справочник.Пользователи"]},
                {"name": "АдресДоставки", "synonym": "Адреса доставки", "types": ["Рядок"],
                 "length": 500},
                {"name": "Вес", "synonym": "Вага брутто (кг)", "types": ["Число"],
                 "digits": 16, "fraction_digits": 4},
            ],
            "tabular_sections": [
                {"name": "Запасы", "synonym": "Запаси", "attributes": [
                    {"name": "Номенклатура", "synonym": "Номенклатура",
                     "types": ["Справочник.Номенклатура", "Рядок"], "length": 150},
                    {"name": "Количество", "synonym": "Кількість", "types": ["Число"],
                     "digits": 15, "fraction_digits": 3},
                ]},
            ],
        }
    if "/documents/" in path:
        # Compact: standard document fields only (Number/Date/Posted/...) + Ref,
        # plus any requested `fields` (echoed so tests can assert they were forwarded).
        rows = [
            {"Ref": {"Presentation": "ЗП-00001", "Data": "doc-0001", "Metadata": "ЗаказПокупателя"},
             "Number": "ЗП-00001", "Date": "2026-05-04 00:00:00", "Posted": True, "DeletionMark": False},
            {"Ref": {"Presentation": "ЗП-00002", "Data": "doc-0002", "Metadata": "ЗаказПокупателя"},
             "Number": "ЗП-00002", "Date": "2026-05-18 00:00:00", "Posted": True, "DeletionMark": False},
        ]
        for row in rows:
            row.update(_mock_extra_fields(kw))
        return {"data": rows, "cursor": None}
    if "/registers/" in path and path.endswith("/balance"):
        return {"type": path.split("/")[-2], "data": {"balance": 6200.0, "currency": "UAH"}}
    if "/registers/" in path and path.endswith("/records"):
        rows = [
            {"Period": "2026-08-01 00:00:00",
             "Recorder": {"Presentation": "ЗП-00001", "Data": "doc-0001",
                          "Metadata": "ЗаказПокупателя"},
             "Контрагент": {"Presentation": "Альфа Трейд, ТОВ", "Data": "e3f1d9b6-0002",
                            "Metadata": "Контрагенты"},
             "LineNumber": 1, "Active": True},
        ]
        return {"metadata": "informationregister", "type": path.split("/")[-2],
                "data": rows, "truncated": False}
    if method == "POST" and "/objects/" in path:
        return {"type": path.split("/")[-1], "data": {"ref": "new-ref-001", "created": True}}
    return {"data": None}


# Mock для інтроспекції структури — форма повторює реальний ai_metadata_get.
def _mock_metadata(path: str) -> Any:
    tail = path.split("/metadata/", 1)[1]
    parts = [p for p in tail.split("/") if p]
    if parts and parts[0].lower() == "all":
        return {
            "catalogs": [{"name": "Контрагенты", "synonym": "Контрагенти"},
                         {"name": "Номенклатура", "synonym": "Номенклатура"}],
            "documents": [{"name": "ЗаказПокупателя", "synonym": "Замовлення покупця"}],
            "informationregisters": [{"name": "MCBP_StatusFunctions", "synonym": "Статус функцій"}],
        }
    if len(parts) == 1:
        return {"metadata": parts[0].lower(), "count": 2,
                "items": [{"name": "Контрагенты", "synonym": "Контрагенти"},
                          {"name": "Номенклатура", "synonym": "Номенклатура"}]}
    # detail — documents carry tabular sections (so tests exercise the summarize/select
    # view in `_describe_metadata_view`); catalogs typically don't.
    is_document = parts[0].lower() == "documents"
    return {
        "metadata": "document" if is_document else "catalog", "type": parts[1],
        "synonym": "Замовлення покупця" if is_document else "Контрагенти",
        "standard_attributes": [
            {"name": "Naimenovanie", "synonym": "Найменування", "types": ["Рядок"], "length": 100},
            {"name": "Kod", "synonym": "Код", "types": ["Рядок"], "length": 11},
        ],
        "attributes": [
            {"name": "Pokupatel", "synonym": "Покупець", "types": ["Булево"]},
            {"name": "Postavshchik", "synonym": "Постачальник", "types": ["Булево"]},
            {"name": "KodPoEDRPOU", "synonym": "Код за ЄДРПОУ", "types": ["Рядок"], "length": 10},
        ],
        "tabular_sections": [
            {"name": "Запасы", "synonym": "Запаси", "attributes": [
                {"name": "Nomenklatura", "synonym": "Номенклатура",
                 "types": ["Справочник.Номенклатура"]},
                {"name": "Kolichestvo", "synonym": "Кількість", "types": ["Число"],
                 "digits": 15, "fraction_digits": 3},
            ]},
            {"name": "Skidki", "synonym": "Знижки", "attributes": [
                {"name": "ProcentSkidki", "synonym": "Відсоток знижки", "types": ["Число"]},
            ]},
        ] if is_document else [],
    }
