"""Thin async client for the BAS-side MCBP_AI HTTP service.

Two jobs:
  1. Talk to /ai/v1/* over httpx with a connection pool (1C reuses sessions for
     ~20s, so a persistent pool matters). Auth = HTTP Basic (that's how the service is published).
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
import itertools
import json
import logging
import re
import time
import uuid
from datetime import date
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, unquote, urlencode

import httpx

from mcbp_core.errors import (
    AuthError,
    ConflictError,
    ConversionChangedError,
    ConversionNotConfiguredError,
    ForbiddenError,
    KeyMismatchError,
    LicenseRequiredError,
    MCBPError,
    NotConfiguredError,
    NotConfirmedError,
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
    "LICENSE_REQUIRED": LicenseRequiredError,
    "BAD_JSON": ParameterError,
    "BAD_PARAMETER": ParameterError,
    "NOT_FOUND": NotFoundError,
    "FORBIDDEN": ForbiddenError,
    "NOT_CONFIRMED": NotConfirmedError,
    "WRITE_FAILED": WriteFailedError,
    "PLUS_REQUIRED": PlusRequiredError,
    "CONVERSION_NOT_CONFIGURED": ConversionNotConfiguredError,
    "CONVERSION_CHANGED": ConversionChangedError,
    "CONFLICT": ConflictError,
    "LOCKED": ConflictError,
    "NOT_CONFIGURED": NotConfiguredError,
    "TASK_IN_BUSINESS_PROCESS": ParameterError,
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


def _mask_client(value: Any) -> str:
    text = str(value)
    return text[:4] + "…" if len(text) > 4 else "…"


class _RedactClientKey(logging.Filter):
    """httpx logs every request URL at INFO — `?client=<device key>` included. Masks that value
    in its records, the same way `_kw_brief` does for ours."""

    _PATTERN = re.compile(r"([?&]client=)([^&\s\"]+)")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "client=" in message:
            record.msg = self._PATTERN.sub(
                lambda m: m.group(1) + _mask_client(unquote(m.group(2))), message
            )
            record.args = None
        return True


logging.getLogger("httpx").addFilter(_RedactClientKey())


def _value_shape(value: Any) -> str:
    if isinstance(value, (str, list, dict)):
        return f"{type(value).__name__}({len(value)})"
    return type(value).__name__


def _kw_brief(kw: dict, chat: bool = False) -> str:
    """Describes a request's parameters for the log. Request bodies to MCBP_AI are small and
    carry no secrets, so a dict body is logged as the actual JSON (truncated), not just its
    keys — the value itself is often the reason for a rejection (e.g. a composite reference
    field missing "TypeName:").

    Exception: chat archive routes (`chat=True`). Their bodies are the user's own message texts,
    titles and summaries, and `params["client"]` is the device key — the only pass to a chat
    when the archive is split per device. There the log gets body keys with value lengths only,
    and the `client` value is masked down to its first 4 characters."""
    parts = []
    params = kw.get("params")
    if params:
        if chat and "client" in params:
            params = {**params, "client": _mask_client(params["client"])}
        parts.append(f"params={params}")
    body = kw.get("json")
    if body is not None:
        if isinstance(body, dict):
            if chat:
                shape = ", ".join(f"{k}: {_value_shape(v)}" for k, v in body.items())
                parts.append(f"body_keys={{{shape}}}")
            else:
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


# `metadata` of a single-kind list -> key of the same kind in `list_metadata("all")`.
_KIND_TO_ALL_KEY = {
    "catalog": "catalogs",
    "document": "documents",
    "informationregister": "informationregisters",
    "accumulationregister": "accumulationregisters",
    "enum": "enums",
    "task": "tasks",
    "chartofcharacteristictypes": "chartsofcharacteristictypes",
    "constant": "constants",
}


class MetadataCache:
    """Cache for the metadata routes, shareable between clients of the SAME base.

    Metadata is configuration structure, not data. On the BAS side `ai_metadata_get` and
    `ai_document_schema_get` run under `SetPrivilegedMode(True)`, so what they return does
    not depend on who asks — unlike the data routes, which check `AccessRight("Read", ...)`.
    A single cache can therefore serve every user of one base: the first login warms it, the
    rest get hits. Data stays per-user because each client carries its own HTTP Basic auth.

    Sharing one instance across DIFFERENT bases is a caller bug — key your caches by base.

    Concurrent misses of the same path are collapsed into one request (single-flight), so
    N sessions logging in together still warm the configuration inventory once.
    """

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}
        self._hits = 0
        self._misses = 0
        # One lock per path. Kept after use: the key space is the set of metadata routes,
        # the same order of magnitude as `_entries` itself.
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, path: str, fetch: Callable[[str], Awaitable[Any]]) -> Any:
        if path in self._entries:
            self._hits += 1
            log.info("GET %s → cache hit", path.lstrip("/"))
            return self._entries[path]

        lock = self._locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[path] = lock

        async with lock:
            # Another caller may have filled it while we waited on the lock.
            if path in self._entries:
                self._hits += 1
                log.info("GET %s → cache hit", path.lstrip("/"))
                return self._entries[path]
            self._misses += 1
            payload = await fetch(path)
            self._entries[path] = payload
            return payload

    def stats(self) -> dict:
        return {"entries": len(self._entries), "hits": self._hits, "misses": self._misses}


class MCBPClient:
    def __init__(self, config: ConnectionConfig, metadata_cache: MetadataCache | None = None):
        self._cfg = config
        self._mock = config.mock
        self._client: httpx.AsyncClient | None = None
        # Metadata is configuration structure, not data: it cannot change while a connection is
        # open (a configuration change needs the base restarted/updated), so the same tree was
        # being re-fetched on every model turn for nothing.
        #
        # Left alone, the cache lives on this client, so a re-login builds a new client and
        # therefore a fresh cache — that is the invalidation. Pass `metadata_cache` to share one
        # across clients of the SAME base (see MetadataCache); invalidating it is then the
        # caller's job — drop the instance when the base configuration changes.
        self._meta = metadata_cache if metadata_cache is not None else MetadataCache()
        # Names this user may READ, per `list_metadata("all")` key — see load_readable_metadata.
        # None = not loaded: the first list_readable_metadata call loads it once, and a failed
        # load leaves it None for good — nothing is filtered then.
        self._readable: dict[str, set[str]] | None = None
        self._readable_tried = False

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
        is_tips = path.startswith("ai/v1/tips")
        is_dash = path.startswith("ai/v1/dashboard/layout")
        is_chat = path.startswith("ai/v1/chats") or is_tips or is_dash
        # client/auth bodies carry the client's phone/e-mail and Telegram chat id: log shapes only.
        brief = _kw_brief(kw, chat=is_chat or path.startswith("ai/v1/client/auth"))
        if self._mock:
            log.info("[mock] %s %s %s", method, path, brief)
            if is_dash:
                return _mock_dashboard(self._cfg.user, method, kw)
            if is_tips:
                return _mock_tips(self._cfg.user, method, path, kw)
            if is_chat:
                return _mock_chats(self._cfg.user, method, path, kw)
            if method == "POST" and path.rstrip("/") == "ai/v1/ideas":
                return _mock_idea_upsert(kw, self._cfg.user)
            if path.startswith("ai/v1/functions/") and path.rstrip("/").endswith("/tasks"):
                return _mock_function_tasks(method, path, kw, self._cfg.user)
            return _mock_response(method, path, kw)
        assert self._client is not None, "MCBPClient.startup() not called"

        # 1C does not decode '+' as a space, and httpx builds its query with quote_plus — so any
        # filter value containing a space matched nothing at all, with no error to notice. Encode
        # the query ourselves (space -> %20) instead of handing `params` to httpx.
        params = kw.pop("params", None)
        bare_path = path
        if params:
            path = f"{path}?{urlencode(params, quote_via=quote)}"
        log_path = bare_path if is_chat else path

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
                        method, log_path, brief, attempt + 1, _MAX_RETRIES, e,
                    )
                    await asyncio.sleep(0.3 * (attempt + 1))
                    attempt += 1
                    continue
                log.warning("%s %s %s ✗ transport error: %s", method, log_path, brief, e)
                raise UpstreamError(f"transport error: {e}") from e
            except httpx.HTTPError as e:
                log.warning("%s %s %s ✗ transport error: %s", method, log_path, brief, e)
                raise UpstreamError(f"transport error: {e}") from e

            elapsed_ms = (time.monotonic() - start) * 1000
            log.info("%s %s %s → %s (%.0f ms)", method, log_path, brief, resp.status_code, elapsed_ms)

            if resp.status_code == 503 and method in _RETRYABLE_METHODS and attempt < _MAX_RETRIES:
                log.warning(
                    "%s %s %s ✗ 503, retrying (%d/%d)",
                    method, log_path, brief, attempt + 1, _MAX_RETRIES,
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
                method, log_path, resp.status_code, body_note,
                _truncate(resp.text, _LOG_RESPONSE_LIMIT),
            )
            if typed is not None:
                raise typed
            # Body doesn't match the {"error":{"code",...}} envelope — fall back to the
            # status-code-only classification (unchanged from before).
            if resp.status_code == 404:
                raise NotFoundError(bare_path)
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
        # explicitly here too: the fix only reaches a base after a manual Configurator transfer,
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

    async def register_balance(self, type_: str, filters: dict, on: str | None = None) -> dict:
        """Accumulation-register balance. Every query param is a dimension filter EXCEPT `on`
        (YYYY-MM-DD), which is the date to compute the balance at — omitted means current."""
        params = dict(filters)
        if on:
            params["on"] = on
        return await self._request("GET", f"/ai/v1/registers/{type_}/balance", params=params)

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

    async def get_event_log(self, levels: list[str] | str | None = None,
                            date_from: str | None = None, date_to: str | None = None,
                            event: str | None = None, user: str | None = None,
                            metadata: str | None = None, limit: int | None = None) -> dict:
        """Read-only event log slice under the caller's own rights (no EventLog right -> 403).
        `levels` - error|warning|information|note (list or csv); omitted means the server
        default error,warning. `date_from`/`date_to` YYYY-MM-DD, at most 31 days apart; omitted
        means the last 24 hours. `event` ending in "*" is a prefix. Newest first, limit <= 50."""
        params: dict[str, Any] = {}
        if levels:
            params["level"] = levels if isinstance(levels, str) else ",".join(levels)
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to
        if event:
            params["event"] = event
        if user:
            params["user"] = user
        if metadata:
            params["metadata"] = metadata
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/ai/v1/eventlog", params=params)

    async def get_client_orders(self, login: str, order_id: str | None = None,
                                counterparty: str | None = None) -> dict:
        """Sales orders of a confirmed client `login` (phone or e-mail), under the caller's
        rights. Without `order_id` - {"data": [rows], "counterparties": [...]}, newest 50, no
        lines; with it - one order {"data": {...row, "items": [...]}}. Rows carry `counterparty`
        {id, type, name}; `counterparty` (UUID) narrows to one of the client's allowed
        counterparties. Older services omit both fields. Unconfirmed login -> 403 NOT_CONFIRMED
        (NotConfirmedError, a ForbiddenError); an id or counterparty not among this client's ->
        NotFoundError."""
        if not isinstance(login, str) or not login.strip():
            raise ParameterError("login is required: the client's phone or e-mail")
        params: dict[str, Any] = {"login": login.strip()}
        if order_id is not None:
            try:
                params["id"] = str(uuid.UUID(str(order_id).strip()))
            except ValueError:
                raise ParameterError(f"order_id must be an order UUID, got {order_id!r}") from None
        if counterparty is not None:
            try:
                params["counterparty"] = str(uuid.UUID(str(counterparty).strip()))
            except ValueError:
                raise ParameterError(
                    f"counterparty must be a counterparty UUID, got {counterparty!r}"
                ) from None
        return await self._request("GET", "/ai/v1/client/orders", params=params)

    async def get_client_bonus(self, login: str, counterparty: str | None = None) -> dict:
        """Bonus points of a confirmed client `login` (phone or e-mail), under the caller's
        rights: {"data": {"balance", "accrued", "unit": "points"}}. `balance`/`accrued` are null
        when the base has no bonus data (no register, no read right, no client dimension).
        `counterparty` (UUID) narrows to one of the client's allowed counterparties. Errors as
        `get_client_orders`: NOT_CONFIRMED (NotConfirmedError), a foreign counterparty ->
        NotFoundError."""
        if not isinstance(login, str) or not login.strip():
            raise ParameterError("login is required: the client's phone or e-mail")
        params: dict[str, Any] = {"login": login.strip()}
        if counterparty is not None:
            try:
                params["counterparty"] = str(uuid.UUID(str(counterparty).strip()))
            except ValueError:
                raise ParameterError(
                    f"counterparty must be a counterparty UUID, got {counterparty!r}"
                ) from None
        return await self._request("GET", "/ai/v1/client/bonus", params=params)

    async def start_client_auth(self, login: str, *, confirmed: bool,
                                chat_id: int | str | None = None,
                                name: str | None = None,
                                last_name: str | None = None) -> dict:
        """POST /ai/v1/client/auth/start — a service route for the backend, NOT a model tool.
        `confirmed=False` only looks the login up (no write); `confirmed=True` binds it to the one
        matching contact and saves the record: with `chat_id` and a contact the status becomes
        Active (never over Disabled), otherwise a new record is Pending. A `chat_id` different
        from the one already stored is not saved: the record goes Pending and the reply carries
        `chat_id_conflict: true`. Returns {"data": {login, known, status, chat_id, name, contact,
        counterparty, contact_created, allowed, matches, chat_id_conflict, session_epoch}};
        `session_epoch` is 0 when the base has no epoch resource. POST is never retried."""
        if not isinstance(login, str) or not login.strip():
            raise ParameterError("login is required: the client's phone or e-mail")
        if not isinstance(confirmed, bool):
            raise ParameterError(f"confirmed must be true or false, got {confirmed!r}")
        body: dict[str, Any] = {"login": login.strip(), "confirmed": confirmed}
        if chat_id is not None:
            body["chat_id"] = chat_id
        if name is not None:
            body["name"] = name
        if last_name is not None:
            body["last_name"] = last_name
        return await self._request("POST", "/ai/v1/client/auth/start", json=body)

    async def revoke_client_auth(self, login: str) -> dict:
        """POST /ai/v1/client/auth/revoke — a service route for the backend, NOT a model tool.
        Bumps the login's session epoch so every client session issued before it is invalid.
        Returns {"data": {login, session_epoch}} with the new epoch; an unknown login is
        NOT_FOUND. POST is never retried."""
        if not isinstance(login, str) or not login.strip():
            raise ParameterError("login is required: the client's phone or e-mail")
        return await self._request("POST", "/ai/v1/client/auth/revoke",
                                   json={"login": login.strip()})

    async def push_ai_context(self, body: dict) -> dict:
        return await self._request("POST", "/ai/v1/ai/context", json=body)

    # --- Chat archive: service routes for the web app, NOT model tools (not in the registry).
    # Every method returns the whole response body, like its neighbours: {"data": ...}. The owner
    # is the BAS user plus the optional `client` key; None/empty means the key is not sent.
    @staticmethod
    def _chat_params(client_key: str | None, **extra: Any) -> dict[str, Any] | None:
        params = {k: v for k, v in extra.items() if v is not None}
        if client_key:
            params["client"] = client_key
        return params or None

    @staticmethod
    def _chat_path(chat_id: str, tail: str = "") -> str:
        return f"/ai/v1/chats/{quote(chat_id, safe='')}{tail}"

    async def list_chats(self, client_key: str | None = None) -> dict:
        """{"data": [{id, title, pinned, summary, updated}, ...]} — pinned first, then newest."""
        return await self._request(
            "GET", "/ai/v1/chats", params=self._chat_params(client_key),
        )

    async def create_chat(self, title: str | None = None, summary: str | None = None,
                          pinned: bool | None = None, client_key: str | None = None) -> dict:
        """{"data": {id, title, pinned, summary, updated}} (HTTP 201)."""
        body = {k: v for k, v in
                {"title": title, "summary": summary, "pinned": pinned}.items() if v is not None}
        return await self._request(
            "POST", "/ai/v1/chats", params=self._chat_params(client_key), json=body,
        )

    async def get_chat(self, chat_id: str, limit: int | None = None,
                       client_key: str | None = None) -> dict:
        """{"data": {id, title, pinned, summary, updated, "messages": [{n, role, text, date}]}} —
        the last `limit` messages (BAS default 200, 1..1000), oldest first."""
        return await self._request(
            "GET", self._chat_path(chat_id),
            params=self._chat_params(client_key, limit=limit),
        )

    async def patch_chat(self, chat_id: str, *, title: str | None = None,
                         summary: str | None = None, pinned: bool | None = None,
                         client_key: str | None = None) -> dict:
        """{"data": {id, title, pinned, summary, updated}}. Only the passed fields are sent."""
        body = {k: v for k, v in
                {"title": title, "summary": summary, "pinned": pinned}.items() if v is not None}
        return await self._request(
            "PATCH", self._chat_path(chat_id), params=self._chat_params(client_key), json=body,
        )

    async def delete_chat(self, chat_id: str, client_key: str | None = None) -> dict:
        """{"data": {"id": ..., "deleted": true}}."""
        return await self._request(
            "DELETE", self._chat_path(chat_id), params=self._chat_params(client_key),
        )

    async def add_chat_messages(self, chat_id: str, messages: list[dict], *,
                                title: str | None = None, summary: str | None = None,
                                client_key: str | None = None) -> dict:
        """Appends [{"role": "user"|"assistant"|"note", "text": str}, ...]. Returns
        {"data": {id, title, pinned, summary, updated, "added": int, "last_number": int}}
        (HTTP 201). POST is never retried, so a transport failure may leave the write unknown."""
        body: dict[str, Any] = {"messages": messages}
        if title is not None:
            body["title"] = title
        if summary is not None:
            body["summary"] = summary
        return await self._request(
            "POST", self._chat_path(chat_id, "/messages"),
            params=self._chat_params(client_key), json=body,
        )

    # --- Business tips: service routes for the web app, NOT model tools. Same owner model and
    # log redaction as the chat archive (texts are the user's data; `client` is the device key).
    @staticmethod
    def _tip_path(tip_id: str) -> str:
        return f"/ai/v1/tips/{quote(tip_id, safe='')}"

    async def list_tips(self, hidden: bool = False, client_key: str | None = None) -> dict:
        """{"data": [{id, title, text, basis, topic, date, read, pinned, hidden, chat}, ...]} —
        pinned first, then newest, max 100. Hidden tips only with `hidden=True`."""
        return await self._request(
            "GET", "/ai/v1/tips",
            params=self._chat_params(client_key, hidden="true" if hidden else None),
        )

    async def create_tips(self, tips: list[dict], client_key: str | None = None) -> dict:
        """Stores 1..20 [{"title", "text"?, "basis"?, "topic"?}] all-or-nothing. Returns
        {"data": [tip, ...]} (HTTP 201). POST is never retried."""
        return await self._request(
            "POST", "/ai/v1/tips", params=self._chat_params(client_key), json={"tips": tips},
        )

    # --- Dashboard layout: ONE JSON blob per owner, optimistic revision (409 CONFLICT on mismatch).
    async def get_dashboard_layout(self, client_key: str | None = None) -> dict:
        """{"data": {"tiles": [...], "revision": N, "updated": str}} — nothing stored means
        tiles [] and revision 0."""
        return await self._request(
            "GET", "/ai/v1/dashboard/layout", params=self._chat_params(client_key),
        )

    async def put_dashboard_layout(self, tiles: list[dict], revision: int,
                                   client_key: str | None = None) -> dict:
        """Replaces the stored tiles if `revision` equals the stored one; returns the same shape
        with revision+1. A stale revision raises ConflictError; more than 24 tiles or a body over
        64 KB raises ParameterError. PUT is not retried."""
        return await self._request(
            "PUT", "/ai/v1/dashboard/layout", params=self._chat_params(client_key),
            json={"tiles": tiles, "revision": revision},
        )

    async def patch_tip(self, tip_id: str, *, read: bool | None = None,
                        pinned: bool | None = None, hidden: bool | None = None,
                        chat: str | None = None, rating: int | None = None,
                        client_key: str | None = None) -> dict:
        """{"data": tip}. Only the passed fields are sent; `chat=""` unlinks the chat;
        `rating` is 1 (like), -1 (dislike) or 0 (cleared), anything else raises ParameterError."""
        body = {k: v for k, v in
                {"read": read, "pinned": pinned, "hidden": hidden, "chat": chat,
                 "rating": rating}.items()
                if v is not None}
        return await self._request(
            "PATCH", self._tip_path(tip_id), params=self._chat_params(client_key), json=body,
        )

    async def delete_tip(self, tip_id: str, client_key: str | None = None) -> dict:
        """{"data": {"id": ..., "deleted": true}}."""
        return await self._request(
            "DELETE", self._tip_path(tip_id), params=self._chat_params(client_key),
        )

    # --- Configuration introspection (works for ANY 1C configuration) ---
    async def _cached_get(self, path: str) -> Any:
        """GET a metadata route through the metadata cache (per-client, or shared per base)."""
        return await self._meta.get(path, lambda p: self._request("GET", p))

    async def prefetch_metadata(self) -> int:
        """Warm the cache with the whole-configuration object inventory (one request).

        Only the INVENTORY is prefetched, not every object's attribute tree: `all` is a single
        ~0.5 s call, while describing all ~1800 objects of a typical configuration would be ~1800.
        Per-object trees stay lazy — cached on first use. Returns the number of cached entries."""
        await self._cached_get("/ai/v1/metadata/all")
        return self._meta.stats()["entries"]

    def metadata_cache_stats(self) -> dict:
        return self._meta.stats()

    async def list_metadata(self, kind: str) -> dict:
        """kind='all' → object lists of every kind; kind='Catalogs'/'Documents'/... → list of one kind."""
        return await self._cached_get(f"/ai/v1/metadata/{kind}")

    async def load_readable_metadata(self) -> int:
        """Load which objects THIS user has the Read right on (`?access=read`, one request).

        Deliberately outside the metadata cache: the cache is shared by every user of the base,
        this answer is per user. A base build without `access=read` ignores the parameter and
        returns everything, which simply hides nothing. Returns the number of readable names."""
        self._readable_tried = True
        body = await self._request("GET", "/ai/v1/metadata/all", params={"access": "read"})
        self._readable = {
            key: {str(it.get("name")) for it in items if isinstance(it, dict)}
            for key, items in (body or {}).items()
            if isinstance(items, list)
        }
        return sum(len(names) for names in self._readable.values())

    def get_readable_metadata(self) -> dict[str, set[str]] | None:
        """Snapshot of the loaded read-rights map, or None if never loaded on this client.

        For a host app that keeps its own cache of this answer (e.g. per base+user, surviving
        past this client's lifetime) to read out after `load_readable_metadata` and later
        restore on a fresh client via `set_readable_metadata` — skipping the request."""
        return self._readable

    def set_readable_metadata(self, readable: dict[str, set[str]]) -> None:
        """Install a read-rights map obtained elsewhere (e.g. the host's own cache) without an
        HTTP round trip. Marks it as loaded, so `list_readable_metadata` will not fetch it."""
        self._readable = readable
        self._readable_tried = True

    async def list_readable_metadata(self, kind: str) -> dict:
        """`list_metadata` narrowed to what this user may read — the view for the MODEL, so it
        does not propose objects that answer 403. Masking must keep using the full
        `list_metadata`: it classifies referenced types, readable or not.

        Returns copies — the underlying lists belong to the cache shared across users. Rights
        are loaded on first use unless the host already did it (the backend does, at login)."""
        if self._readable is None and not self._readable_tried:
            try:
                await self.load_readable_metadata()
            except Exception as e:  # noqa: BLE001 — no rights answer means an unfiltered view
                log.warning("readable metadata unavailable, list stays unfiltered: %s", e)
        data = await self.list_metadata(kind)
        readable = self._readable
        if readable is None or not isinstance(data, dict):
            return data
        if isinstance(data.get("items"), list):
            names = readable.get(_KIND_TO_ALL_KEY.get(str(data.get("metadata")), ""))
            if names is None:
                return data
            items = [it for it in data["items"] if it.get("name") in names]
            return {**data, "items": items, "count": len(items)}
        return {
            key: [it for it in value if it.get("name") in readable[key]]
            if isinstance(value, list) and key in readable else value
            for key, value in data.items()
        }

    async def describe_metadata(self, kind: str, type_: str,
                                tabular_section: str | None = None,
                                q: str | None = None) -> dict:
        """Attribute tree of one metadata object. The full tree (all tabular sections with
        their nested attributes) is fetched once and kept in the metadata cache regardless of
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

    async def set_register_record(self, type_: str, fields: dict) -> dict:
        """Upserts ONE information-register record natively (no MCBP Plus). `fields` is a flat
        {field: value} dict: the key is every dimension plus `Period` for a periodic register;
        any other field is a resource/attribute and only the passed ones change. Values follow
        `patch_object` (Ref UUID, "MetadataName:UUID" for a composite type, an enum value name,
        None clears). A recorder-subordinate register is BAD_PARAMETER; no delete. POST is not
        retried. Response (no `data` wrapper): {"register","created","key","changed"
        [{"field","old","new"}, ...]} — an unchanged repeat gives `changed: []`."""
        return await self._request(
            "POST", f"/ai/v1/registers/{type_}/records", json=fields,
        )

    async def set_function_status(self, function_id: str, *, version: str, status: str,
                                  date_execution: str | None = None,
                                  disable: bool | None = None) -> dict:
        """Upserts the support-tracker state of one MCBP_Functions item (UUID `function_id`).
        `status` is a predefined MCBP_TaskStatuses name (Waiting / Done); `date_execution` is
        YYYY-MM-DD (BAS keeps the date only), `""` clears it, None leaves it as stored; `disable`
        None leaves it as stored. Same body twice changes nothing. POST is not retried.
        {"data": {"function","description","version","status","date_execution","disable",
        "created","changed"}}."""
        body: dict[str, Any] = {"version": version, "status": status}
        if date_execution is not None:
            body["date_execution"] = date_execution or None
        if disable is not None:
            body["disable"] = disable
        return await self._request(
            "POST", f"/ai/v1/functions/{quote(function_id, safe='')}/status", json=body,
        )

    async def upsert_function(self, function_id: str | None = None, *,
                              description: str | None = None,
                              functions_type: str | None = None,
                              deletion_mark: bool | None = None,
                              specification: str | None = None,
                              ideas: list[str] | None = None) -> dict:
        """Creates (no `function_id`) or updates one MCBP_Functions catalog item natively — no
        MCBP Plus conversion. `description` is required on create; `functions_type` is a value
        NAME of the item's enum attribute, `""` clears it; None leaves a field as stored.
        `ideas` is the FULL ordered list of MCBP_Ideas UUIDs for the Ideas rows: the rows are
        replaced, `[]` clears them, duplicates are dropped, an unknown UUID is NOT_FOUND.
        `specification` is the text of the idea in the item's first Ideas row (the idea is created
        when there is none); `""` clears the text and keeps the link. With both, `ideas` is
        applied first and `specification` then edits the idea of the new first row.
        `deletion_mark=True` is how a probe item is cleaned up. A repeated update with the same
        fields changes nothing; every create adds a new item. POST is not retried.
        {"data": {"id","description","functions_type","deletion_mark","specification","ideas",
        "created","changed"}}."""
        if function_id is not None and not function_id.strip():
            raise ParameterError("function_id is blank - omit it to create a function, "
                                 "or pass the item UUID to update one")
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if functions_type is not None:
            body["functions_type"] = functions_type or None
        if deletion_mark is not None:
            body["deletion_mark"] = deletion_mark
        if specification is not None:
            body["specification"] = specification or None
        if ideas is not None:
            body["ideas"] = list(ideas)
        params = {"id": function_id} if function_id is not None else None
        return await self._request("POST", "/ai/v1/functions", params=params, json=body)

    async def upsert_idea(self, idea_id: str | None = None, *,
                          description: str | None = None,
                          specification: str | None = None,
                          deletion_mark: bool | None = None) -> dict:
        """Creates (no `idea_id`) or updates one MCBP_Ideas catalog item natively — no MCBP Plus
        conversion. `description` is required on create; `specification` `""` clears the text;
        None leaves a field as stored. ModifiedDate is set by BAS on every write. Linking an idea
        to functions is `upsert_function(ideas=[...])`. A repeated update with the same fields
        changes nothing; every create adds a new item. POST is not retried.
        {"data": {"id","description","specification","deletion_mark","created","changed"}}."""
        if idea_id is not None and not idea_id.strip():
            raise ParameterError("idea_id is blank - omit it to create an idea, "
                                 "or pass the item UUID to update one")
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if specification is not None:
            body["specification"] = specification or None
        if deletion_mark is not None:
            body["deletion_mark"] = deletion_mark
        params = {"id": idea_id} if idea_id is not None else None
        return await self._request("POST", "/ai/v1/ideas", params=params, json=body)

    async def list_function_tasks(self, function_id: str, *, limit: int | None = None) -> dict:
        """Tracker tasks of one MCBP_Functions item (UUID `function_id`), newest first, under the
        caller's rights. `limit` defaults to and is capped at 50 by the server.
        {"data": [...], "function", "task_type", "limit"}."""
        params = {"limit": limit} if limit is not None else None
        return await self._request(
            "GET", f"/ai/v1/functions/{quote(function_id, safe='')}/tasks", params=params,
        )

    async def upsert_function_task(self, function_id: str, task_id: str | None = None, *,
                                   description: str | None = None,
                                   specification: str | None = None,
                                   deadline: str | None = None,
                                   importance: str | None = None,
                                   executed: bool | None = None,
                                   deletion_mark: bool | None = None) -> dict:
        """Creates (no `task_id`) or updates one tracker task whose subject is the MCBP_Functions
        item `function_id`. `description` is required on create; `specification`, `deadline`
        (YYYY-MM-DD or YYYY-MM-DDTHH:MM) and `importance` (enum value name) take `""` to clear;
        None leaves a field as stored. A task of another function is NOT_FOUND. POST is not
        retried. {"data": {...row, "function", "task_type", "created", "changed"}}."""
        if task_id is not None and not task_id.strip():
            raise ParameterError("task_id is blank - omit it to create a task, "
                                 "or pass the task UUID to update one")
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if specification is not None:
            body["specification"] = specification or None
        if deadline is not None:
            body["deadline"] = deadline or None
        if importance is not None:
            body["importance"] = importance or None
        if executed is not None:
            body["executed"] = executed
        if deletion_mark is not None:
            body["deletion_mark"] = deletion_mark
        params = {"task": task_id} if task_id is not None else None
        return await self._request(
            "POST", f"/ai/v1/functions/{quote(function_id, safe='')}/tasks",
            params=params, json=body,
        )


# --- Mock data so the backend boots and the AI loop is testable without BAS ---
# Fixtures deliberately mirror a real BAS response slice: Cyrillic type names
# (Контрагенты / ЗаказПокупателя), canonical English field keys, references as
# {Presentation, Data, Metadata}, cursor = UUID of the last row. Do NOT "simplify"
# these shapes — a convenient fixture makes the suite green and the live base fail.
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


# In-memory chat archive for mock mode. Module-level so every mock client of one process sees the
# same archive (the backend builds a client per session). Owner = (BAS user, `client` key): a
# foreign owner gets an empty list / NotFoundError, exactly like BAS.
_MOCK_CHATS: dict[tuple[str, str], dict[str, dict]] = {}


def reset_mock_chats() -> None:
    _MOCK_CHATS.clear()


_MOCK_ROLES = ("user", "assistant", "note")
_MOCK_SEQ = itertools.count(1)


def _mock_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _chat_head(chat: dict) -> dict:
    return {k: chat[k] for k in ("id", "title", "pinned", "summary", "updated")}


def _mock_chats(user: str, method: str, path: str, kw: dict) -> dict:
    owner = (user, str((kw.get("params") or {}).get("client") or ""))
    chats = _MOCK_CHATS.setdefault(owner, {})
    body = kw.get("json") or {}
    parts = [p for p in path.split("?", 1)[0].split("/") if p][3:]  # after ai/v1/chats
    if not parts:
        if method == "GET":
            rows = sorted(chats.values(), key=lambda c: (c["updated"], c["seq"]), reverse=True)
            rows.sort(key=lambda c: not c["pinned"])
            return {"data": [_chat_head(c) for c in rows]}
        chat = {"id": str(uuid.uuid4()), "title": body.get("title") or "",
                "pinned": bool(body.get("pinned", False)), "summary": body.get("summary") or "",
                "updated": _mock_now(), "messages": [], "seq": next(_MOCK_SEQ)}
        chats[chat["id"]] = chat
        return {"data": _chat_head(chat)}
    chat = chats.get(parts[0])
    if chat is None:
        raise NotFoundError(f"chat {parts[0]} not found")
    if len(parts) == 2 and parts[1] == "messages" and method == "POST":
        added = body.get("messages")
        if not isinstance(added, list) or not added:
            raise ParameterError("messages must be a non-empty array")
        for m in added:
            role = m.get("role") if isinstance(m, dict) else None
            if role not in _MOCK_ROLES:
                raise ParameterError(f"role must be one of {list(_MOCK_ROLES)}, got {role!r}")
        for m in added:
            chat["messages"].append({"n": len(chat["messages"]) + 1, "role": m["role"],
                                     "text": m.get("text") or "", "date": _mock_now()})
        for key in ("title", "summary"):
            if key in body:
                chat[key] = body[key]
        chat["updated"] = _mock_now()
        return {"data": {**_chat_head(chat), "added": len(added),
                         "last_number": len(chat["messages"])}}
    if method == "DELETE":
        del chats[parts[0]]
        return {"data": {"id": parts[0], "deleted": True}}
    if method == "PATCH":
        for key in ("title", "summary", "pinned"):
            if key in body:
                chat[key] = body[key]
        chat["updated"] = _mock_now()
        return {"data": _chat_head(chat)}
    raw_limit = (kw.get("params") or {}).get("limit")
    try:
        limit = 200 if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError):
        raise ParameterError(f"limit must be an integer 1..1000, got {raw_limit!r}") from None
    if not 1 <= limit <= 1000:
        raise ParameterError(f"limit must be an integer 1..1000, got {limit}")
    return {"data": {**_chat_head(chat), "messages": chat["messages"][-limit:]}}


_MOCK_TIPS: dict[tuple[str, str], dict[str, dict]] = {}


def reset_mock_tips() -> None:
    _MOCK_TIPS.clear()


def _mock_tips(user: str, method: str, path: str, kw: dict) -> dict:
    owner = (user, str((kw.get("params") or {}).get("client") or ""))
    tips = _MOCK_TIPS.setdefault(owner, {})
    params = kw.get("params") or {}
    body = kw.get("json") or {}
    parts = [p for p in path.split("?", 1)[0].split("/") if p][3:]  # after ai/v1/tips

    def head(t: dict) -> dict:
        return {k: v for k, v in t.items() if k != "seq"}

    if not parts:
        if method == "GET":
            show_hidden = str(params.get("hidden") or "").lower() in ("true", "1")
            rows = [t for t in tips.values() if show_hidden or not t["hidden"]]
            rows.sort(key=lambda t: (t["date"], t["seq"]), reverse=True)
            rows.sort(key=lambda t: not t["pinned"])
            return {"data": [head(t) for t in rows[:100]]}
        items = body.get("tips")
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ParameterError("tips must be an array of 1..20 items")
        for it in items:
            title = it.get("title") if isinstance(it, dict) else None
            if not isinstance(title, str) or not title.strip() or len(title) > 150:
                raise ParameterError("title is required (max 150 characters)")
            if len(it.get("topic") or "") > 50:
                raise ParameterError("topic is at most 50 characters")
        created = []
        for it in items:
            tip = {"id": str(uuid.uuid4()), "title": it["title"], "text": it.get("text") or "",
                   "basis": it.get("basis") or "", "topic": it.get("topic") or "",
                   "date": _mock_now(), "read": False, "pinned": False, "hidden": False,
                   "chat": "", "rating": 0, "seq": next(_MOCK_SEQ)}
            tips[tip["id"]] = tip
            created.append(head(tip))
        return {"data": created}
    tip = tips.get(parts[0])
    if tip is None:
        raise NotFoundError(f"tip {parts[0]} not found")
    if method == "DELETE":
        del tips[parts[0]]
        return {"data": {"id": parts[0], "deleted": True}}
    if method == "PATCH":
        rating = body.get("rating", 0)
        if type(rating) is not int or rating not in (-1, 0, 1):
            raise ParameterError("rating must be 1 (like), -1 (dislike) or 0 (none)")
        for key in ("read", "pinned", "hidden", "chat", "rating"):
            if key in body:
                tip[key] = body[key]
        return {"data": head(tip)}
    return {"data": head(tip)}


_MOCK_DASH: dict[tuple[str, str], dict] = {}
_DASH_MAX_TILES = 24
_DASH_MAX_BYTES = 64 * 1024


def reset_mock_dashboard() -> None:
    _MOCK_DASH.clear()


def _mock_dashboard(user: str, method: str, kw: dict) -> dict:
    owner = (user, str((kw.get("params") or {}).get("client") or ""))
    row = _MOCK_DASH.setdefault(owner, {"tiles": [], "revision": 0, "updated": ""})
    if method == "PUT":
        body = kw.get("json") or {}
        tiles = body.get("tiles")
        if not isinstance(tiles, list) or len(tiles) > _DASH_MAX_TILES:
            raise ParameterError(f"tiles must be an array of at most {_DASH_MAX_TILES} items")
        if len(json.dumps(body, ensure_ascii=False).encode()) > _DASH_MAX_BYTES:
            raise ParameterError("body is larger than 64 KB")
        if body.get("revision") != row["revision"]:
            raise ConflictError(
                f"stored revision is {row['revision']}, got {body.get('revision')!r}"
            )
        row.update(tiles=tiles, revision=row["revision"] + 1, updated=_mock_now())
    return {"data": {"tiles": row["tiles"], "revision": row["revision"],
                     "updated": row["updated"]}}


_MOCK_FUNCTION_STATUS: dict[str, dict] = {}
_FUNCTION_STATUSES = ("Waiting", "Done")
_FUNCTION_VERSION_MAX = 12
_FUNCTION_STATUS_FIELDS = ("version", "status", "date_execution", "disable")


def reset_mock_function_status() -> None:
    _MOCK_FUNCTION_STATUS.clear()


def _mock_function_status(path: str, kw: dict) -> dict:
    function_id = unquote(path.rstrip("/").split("/")[-2])
    body = kw.get("json")
    if not isinstance(body, dict):
        raise ParameterError("Body must be a JSON object: " + ", ".join(_FUNCTION_STATUS_FIELDS))
    for key in body:
        if key not in _FUNCTION_STATUS_FIELDS:
            raise ParameterError(
                f"Unknown field '{key}' - accepted: " + ", ".join(_FUNCTION_STATUS_FIELDS)
            )
    version = body.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ParameterError("version is required: a non-empty string")
    version = version.strip()
    if len(version) > _FUNCTION_VERSION_MAX:
        raise ParameterError(f"version is longer than {_FUNCTION_VERSION_MAX} characters")
    raw_status = body.get("status")
    status = next((s for s in _FUNCTION_STATUSES
                   if isinstance(raw_status, str) and s.lower() == raw_status.strip().lower()),
                  None)
    if status is None:
        raise ParameterError(
            f"Unknown status '{raw_status}' - one of: " + ", ".join(_FUNCTION_STATUSES)
        )
    try:
        function_id = str(uuid.UUID(function_id))
    except ValueError:
        raise ParameterError("id must be a MCBP_Functions item UUID") from None

    stored = _MOCK_FUNCTION_STATUS.get(function_id)
    created = stored is None
    old = stored or {"version": "", "status": None, "date_execution": None, "disable": False}
    new = {"version": version, "status": status,
           "date_execution": old["date_execution"], "disable": old["disable"]}
    if "date_execution" in body:
        raw_date = body["date_execution"]
        if raw_date is not None:
            if not isinstance(raw_date, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}([T ].*)?", raw_date.strip()
            ):
                raise ParameterError("date_execution must be a YYYY-MM-DD string or null")
            raw_date = raw_date.strip()[:10]
            try:
                date.fromisoformat(raw_date)
            except ValueError:
                raise ParameterError(
                    "date_execution must be a YYYY-MM-DD string or null"
                ) from None
        new["date_execution"] = raw_date
    if "disable" in body:
        if not isinstance(body["disable"], bool):
            raise ParameterError("disable must be true or false")
        new["disable"] = body["disable"]
    changed = created or new != old
    _MOCK_FUNCTION_STATUS[function_id] = new
    return {"data": {"function": function_id, "description": "<mock function>", **new,
                     "created": created, "changed": changed}}


_MOCK_FUNCTIONS: dict[str, dict] = {}
_MOCK_IDEAS: dict[str, dict] = {}
# Value names of Enum.MCBP_FunctionsTypes as exported from the base; the real route reads them
# from metadata.
_FUNCTION_TYPES = ("Basic", "Common", "AutomaticActions", "DataExchange",
                   "ArtificialIntelligence", "SupportService", "AdvancedAnalytics")
_FUNCTION_DESCRIPTION_MAX = 150
_FUNCTION_FIELDS = ("description", "functions_type", "deletion_mark", "specification", "ideas")
_IDEA_FIELDS = ("description", "specification", "deletion_mark")
_IDEA_DESCRIPTION_MAX = 150


def reset_mock_functions() -> None:
    """Clears the mock functions, the mock ideas they link to and their mock tasks."""
    _MOCK_FUNCTIONS.clear()
    _MOCK_IDEAS.clear()
    _MOCK_FUNCTION_TASKS.clear()


def _mock_function_upsert(kw: dict) -> dict:
    body = kw.get("json")
    if not isinstance(body, dict):
        raise ParameterError("Body must be a JSON object: " + ", ".join(_FUNCTION_FIELDS))
    for key in body:
        if key not in _FUNCTION_FIELDS:
            raise ParameterError(
                f"Unknown field '{key}' - accepted: " + ", ".join(_FUNCTION_FIELDS)
            )
    raw_id = (kw.get("params") or {}).get("id")
    if raw_id is not None and not str(raw_id).strip():
        raise ParameterError("id is blank - omit it to create a function, or pass the item UUID "
                             "to update one")
    is_create = raw_id is None
    new: dict[str, Any] = {}
    if "description" in body:
        description = body["description"]
        if not isinstance(description, str) or not description.strip():
            raise ParameterError("description must be a non-empty string")
        description = description.strip()
        if len(description) > _FUNCTION_DESCRIPTION_MAX:
            raise ParameterError(
                f"description is longer than {_FUNCTION_DESCRIPTION_MAX} characters"
            )
        new["description"] = description
    elif is_create:
        raise ParameterError("description is required when creating a function (no id given)")
    if "functions_type" in body:
        raw_type = body["functions_type"]
        if raw_type is None or (isinstance(raw_type, str) and not raw_type.strip()):
            new["functions_type"] = None
        else:
            match = next((t for t in _FUNCTION_TYPES if isinstance(raw_type, str)
                          and t.lower() == raw_type.strip().lower()), None)
            if match is None:
                raise ParameterError(
                    f"Unknown functions_type '{raw_type}' - one of: " + ", ".join(_FUNCTION_TYPES)
                )
            new["functions_type"] = match
    if "deletion_mark" in body:
        if not isinstance(body["deletion_mark"], bool):
            raise ParameterError("deletion_mark must be true or false")
        new["deletion_mark"] = body["deletion_mark"]
    spec_given = "specification" in body
    spec_text = ""
    if spec_given:
        raw_spec = body["specification"]
        if raw_spec is not None and not isinstance(raw_spec, str):
            raise ParameterError("specification must be a string or null")
        spec_text = raw_spec if raw_spec and raw_spec.strip() else ""
    if "ideas" in body:
        new["ideas"] = _mock_idea_list(body["ideas"])

    if is_create:
        function_id = str(uuid.uuid4())
        old = None
        row = {"description": "", "functions_type": None, "deletion_mark": False, "ideas": []}
    else:
        try:
            function_id = str(uuid.UUID(raw_id))
        except ValueError:
            raise ParameterError("id must be a MCBP_Functions item UUID") from None
        old = _MOCK_FUNCTIONS.get(function_id)
        if old is None:
            raise NotFoundError(f"Catalog MCBP_Functions {function_id} not found")
        row = dict(old)
    row.update(new)
    changed = is_create or row != old
    # Same order as BSL: rows first, then the specification edits the first row's idea.
    if spec_given:
        if row["ideas"]:
            idea = _MOCK_IDEAS[row["ideas"][0]]
            if idea["specification"] != spec_text:
                idea["specification"] = spec_text
                changed = True
        elif spec_text:
            idea_id = str(uuid.uuid4())
            _MOCK_IDEAS[idea_id] = {"description": row["description"][:_IDEA_DESCRIPTION_MAX],
                                    "specification": spec_text, "deletion_mark": False}
            row["ideas"] = [idea_id]
            changed = True
    _MOCK_FUNCTIONS[function_id] = row
    first = _MOCK_IDEAS.get(row["ideas"][0]) if row["ideas"] else None
    specification = first["specification"] or None if first else None
    return {"data": {"id": function_id, **row, "specification": specification,
                     "ideas": list(row["ideas"]), "created": is_create, "changed": changed}}


def _mock_idea_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ParameterError("ideas must be an array of MCBP_Ideas item UUIDs")
    result: list[str] = []
    for item in raw:
        try:
            idea_id = str(uuid.UUID(item.strip())) if isinstance(item, str) else None
        except ValueError:
            idea_id = None
        if idea_id is None:
            raise ParameterError(f"ideas must hold MCBP_Ideas item UUIDs - got '{item}'")
        if idea_id not in result:
            result.append(idea_id)
    for idea_id in result:
        if idea_id not in _MOCK_IDEAS:
            raise NotFoundError(
                f"Catalog MCBP_Ideas {idea_id} not found or not accessible to this user"
            )
    return result


def _mock_idea_upsert(kw: dict, user: str) -> dict:
    body = kw.get("json")
    if not isinstance(body, dict):
        raise ParameterError("Body must be a JSON object: " + ", ".join(_IDEA_FIELDS))
    for key in body:
        if key not in _IDEA_FIELDS:
            raise ParameterError(f"Unknown field '{key}' - accepted: " + ", ".join(_IDEA_FIELDS))
    raw_id = (kw.get("params") or {}).get("id")
    if raw_id is not None and not str(raw_id).strip():
        raise ParameterError("id is blank - omit it to create an idea, or pass the item UUID "
                             "to update one")
    is_create = raw_id is None
    new: dict[str, Any] = {}
    if "description" in body:
        description = body["description"]
        if not isinstance(description, str) or not description.strip():
            raise ParameterError("description must be a non-empty string")
        description = description.strip()
        if len(description) > _IDEA_DESCRIPTION_MAX:
            raise ParameterError(f"description is longer than {_IDEA_DESCRIPTION_MAX} characters")
        new["description"] = description
    elif is_create:
        raise ParameterError("description is required when creating an idea (no id given)")
    if "specification" in body:
        raw_spec = body["specification"]
        if raw_spec is not None and not isinstance(raw_spec, str):
            raise ParameterError("specification must be a string or null")
        new["specification"] = raw_spec if raw_spec and raw_spec.strip() else ""
    if "deletion_mark" in body:
        if not isinstance(body["deletion_mark"], bool):
            raise ParameterError("deletion_mark must be true or false")
        new["deletion_mark"] = body["deletion_mark"]

    if is_create:
        idea_id = str(uuid.uuid4())
        old = None
        row = {"description": "", "specification": "", "deletion_mark": False, "author": user}
    else:
        try:
            idea_id = str(uuid.UUID(raw_id))
        except ValueError:
            raise ParameterError("id must be a MCBP_Ideas item UUID") from None
        old = _MOCK_IDEAS.get(idea_id)
        if old is None:
            raise NotFoundError(f"Catalog MCBP_Ideas {idea_id} not found")
        row = dict(old)
    row.update(new)
    changed = is_create or row != old
    _MOCK_IDEAS[idea_id] = row
    return {"data": {"id": idea_id, "description": row["description"],
                     "specification": row["specification"] or None,
                     "deletion_mark": row["deletion_mark"], "created": is_create,
                     "changed": changed}}


_MOCK_FUNCTION_TASKS: dict[str, dict] = {}
_TASK_TYPE = "MCBP_PerformerTask"
_TASK_FIELDS = ("description", "specification", "deadline", "importance", "executed",
                "deletion_mark")
# Value names of Enum.MCBP_TaskImportanceOptions; the real route reads them from metadata.
_TASK_IMPORTANCE = ("Low", "Plain", "High")
_TASK_LIMIT_MAX = 50
_task_seq = itertools.count(1)


def _mock_task_view(task_id: str, row: dict) -> dict:
    return {"id": task_id, **{k: v for k, v in row.items() if k not in ("seq", "function")}}


def _mock_function_tasks(method: str, path: str, kw: dict, user: str) -> dict:
    raw_function = unquote(path.rstrip("/").split("/")[-2])
    try:
        function_id = str(uuid.UUID(raw_function))
    except ValueError:
        raise ParameterError("id must be a MCBP_Functions item UUID") from None
    if function_id not in _MOCK_FUNCTIONS:
        raise NotFoundError(f"Catalog MCBP_Functions {function_id} not found")
    params = kw.get("params") or {}

    if method == "GET":
        raw_limit = params.get("limit", _TASK_LIMIT_MAX)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            raise ParameterError("limit must be a positive integer") from None
        if limit < 1:
            raise ParameterError("limit must be a positive integer")
        limit = min(limit, _TASK_LIMIT_MAX)
        rows = sorted(((tid, r) for tid, r in _MOCK_FUNCTION_TASKS.items()
                       if r["function"] == function_id),
                      key=lambda item: item[1]["seq"], reverse=True)
        return {"data": [_mock_task_view(tid, r) for tid, r in rows[:limit]],
                "function": function_id, "task_type": _TASK_TYPE, "limit": limit}

    body = kw.get("json")
    if not isinstance(body, dict):
        raise ParameterError("Body must be a JSON object: " + ", ".join(_TASK_FIELDS))
    for key in body:
        if key not in _TASK_FIELDS:
            raise ParameterError(f"Unknown field '{key}' - accepted: " + ", ".join(_TASK_FIELDS))
    if "id" in params:
        raise ParameterError("the task id goes in ?task=, not ?id= - use ?task=")
    raw_task = params.get("task")
    if raw_task is not None and not str(raw_task).strip():
        raise ParameterError("task is blank - omit it to create a task, or pass the task UUID "
                             "to update one")
    is_create = raw_task is None
    new: dict[str, Any] = {}
    if "description" in body:
        description = body["description"]
        if not isinstance(description, str) or not description.strip():
            raise ParameterError("description must be a non-empty string")
        new["description"] = description.strip()
    elif is_create:
        raise ParameterError("description is required when creating a task (no task given)")
    if "specification" in body:
        raw_spec = body["specification"]
        if raw_spec is not None and not isinstance(raw_spec, str):
            raise ParameterError("specification must be a string or null")
        new["specification"] = raw_spec if raw_spec and raw_spec.strip() else None
    if "deadline" in body:
        raw_deadline = body["deadline"]
        if raw_deadline is None or (isinstance(raw_deadline, str) and not raw_deadline.strip()):
            new["deadline"] = None
        elif not isinstance(raw_deadline, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?", raw_deadline.strip()
        ):
            raise ParameterError("deadline must be YYYY-MM-DD or YYYY-MM-DDTHH:MM, or null")
        else:
            new["deadline"] = raw_deadline.strip()
    if "importance" in body:
        raw_importance = body["importance"]
        if raw_importance is None or (isinstance(raw_importance, str)
                                      and not raw_importance.strip()):
            new["importance"] = None
        else:
            match = next((v for v in _TASK_IMPORTANCE if isinstance(raw_importance, str)
                          and v.lower() == raw_importance.strip().lower()), None)
            if match is None:
                raise ParameterError(
                    f"Unknown importance '{raw_importance}' - one of: "
                    + ", ".join(_TASK_IMPORTANCE)
                )
            new["importance"] = match
    for flag in ("executed", "deletion_mark"):
        if flag in body:
            if not isinstance(body[flag], bool):
                raise ParameterError(f"{flag} must be true or false")
            new[flag] = body[flag]

    if is_create:
        task_id = str(uuid.uuid4())
        old = None
        seq = next(_task_seq)
        row = {"function": function_id, "number": f"{seq:09d}", "date": _mock_now(),
               "description": "", "specification": None, "deadline": None,
               "importance": "Plain", "executed": False, "deletion_mark": False,
               "in_business_process": False, "author": user, "executor": None, "seq": seq}
    else:
        try:
            task_id = str(uuid.UUID(str(raw_task).strip()))
        except ValueError:
            raise ParameterError(f"task must be a {_TASK_TYPE} UUID") from None
        old = _MOCK_FUNCTION_TASKS.get(task_id)
        if old is None or old["function"] != function_id:
            raise NotFoundError(f"Task {_TASK_TYPE} {task_id} of function {function_id} "
                                "not found")
        row = dict(old)
    row.update(new)
    changed = is_create or row != old
    _MOCK_FUNCTION_TASKS[task_id] = row
    return {"data": {**_mock_task_view(task_id, row), "function": function_id,
                     "task_type": _TASK_TYPE, "created": is_create, "changed": changed}}


_MOCK_EVENT_LOG_LEVELS = ("error", "warning", "information", "note")
_MOCK_EVENT_LOG = (
    {"date": "2026-08-01 10:15:00", "level": "error", "event": "_$Data$_.Update",
     "user": "Іваненко Петро", "metadata": "Catalog.Контрагенты",
     "data_presentation": "Альфа Трейд, ТОВ", "comment": "Помилка запису: Петренко О.",
     "session": 12, "application": "1CV8C", "computer": "WS-01"},
    {"date": "2026-08-01 09:40:00", "level": "warning", "event": "_$Session$_.Authentication",
     "user": "Іваненко Петро", "metadata": "", "data_presentation": "",
     "comment": "", "session": 11, "application": "WSConnection", "computer": "WS-01"},
    {"date": "2026-08-01 09:00:00", "level": "information", "event": "_$Session$_.Start",
     "user": "Адміністратор", "metadata": "", "data_presentation": "",
     "comment": "", "session": 10, "application": "1CV8C", "computer": "SRV"},
)


def _mock_event_log(kw: dict) -> dict:
    params = kw.get("params") or {}
    raw = params.get("level") or "error,warning"
    levels = [p.strip().lower() for p in str(raw).split(",") if p.strip()]
    for level in levels:
        if level not in _MOCK_EVENT_LOG_LEVELS:
            raise ParameterError(
                f"Unknown level '{level}' - allowed: " + ", ".join(_MOCK_EVENT_LOG_LEVELS)
            )
    if not levels:
        raise ParameterError("Parameter 'level' is empty - allowed: "
                             + ", ".join(_MOCK_EVENT_LOG_LEVELS))
    date_from, date_to = params.get("from"), params.get("to")
    if date_from and date_to and str(date_to) < str(date_from):
        raise ParameterError(f"'to' ({date_to}) is earlier than 'from' ({date_from})")
    event = str(params.get("event") or "")
    user = str(params.get("user") or "")
    limit = min(int(params.get("limit") or 50), 50)
    rows = [dict(r) for r in _MOCK_EVENT_LOG if r["level"] in levels
            and (not event or (r["event"].startswith(event[:-1]) if event.endswith("*")
                               else r["event"] == event))
            and (not user or r["user"] == user)]
    return {"data": rows[:limit], "truncated": len(rows) > limit,
            "from": f"{date_from or '2026-07-31'} 00:00:00",
            "to": f"{date_to or '2026-08-01'} 23:59:59"}


_MOCK_CLIENT_COUNTERPARTIES = (
    {"id": "e3f1d9b6-0000-4000-8000-000000000002", "type": "Контрагенты",
     "name": "Альфа Трейд, ТОВ"},
    {"id": "e3f1d9b6-0000-4000-8000-000000000003", "type": "Контрагенты",
     "name": "Бета Меблі, ФОП"},
)
_MOCK_CLIENT_ORDERS = (
    {"id": "7c1e0a52-0000-4000-8000-000000000002", "number": "00000000012",
     "date": "2026-10-18 14:05:00", "amount": 1250.0, "currency": "UAH",
     "state": "В роботі", "posted": True, "paid": 500.0, "to_pay": 750.0,
     "counterparty": _MOCK_CLIENT_COUNTERPARTIES[0]},
    {"id": "7c1e0a52-0000-4000-8000-000000000001", "number": "00000000011",
     "date": "2026-10-10 09:30:00", "amount": 300.0, "currency": "UAH",
     "state": "Виконано", "posted": True, "paid": 300.0, "to_pay": 0.0,
     "counterparty": _MOCK_CLIENT_COUNTERPARTIES[1]},
)
_MOCK_CLIENT_ORDER_ITEMS = {
    "7c1e0a52-0000-4000-8000-000000000002": [
        {"product": {"id": "5b2d9f10-0000-4000-8000-000000000001", "type": "Номенклатура",
                     "name": "Петля меблева"}, "quantity": 10, "price": 75.0, "amount": 750.0},
        {"product": {"id": "5b2d9f10-0000-4000-8000-000000000002", "type": "Номенклатура",
                     "name": "Ручка-скоба 128 мм"}, "quantity": 5, "price": 100.0,
         "amount": 500.0},
    ],
    "7c1e0a52-0000-4000-8000-000000000001": [
        {"product": {"id": "5b2d9f10-0000-4000-8000-000000000003", "type": "Номенклатура",
                     "name": "Шуруп 4x16"}, "quantity": 100, "price": 3.0, "amount": 300.0},
    ],
}


def _mock_client_orders(kw: dict) -> dict:
    params = kw.get("params") or {}
    if not str(params.get("login") or "").strip():
        raise ParameterError("Parameter 'login' is required")
    rows = list(_MOCK_CLIENT_ORDERS)
    raw_cp = params.get("counterparty")
    if raw_cp is not None:
        try:
            cp_id = str(uuid.UUID(str(raw_cp)))
        except ValueError:
            raise ParameterError("Parameter 'counterparty' must be a counterparty UUID") from None
        if not any(c["id"] == cp_id for c in _MOCK_CLIENT_COUNTERPARTIES):
            raise NotFoundError(f"Counterparty {cp_id} is not among this client's")
        rows = [r for r in rows if r["counterparty"]["id"] == cp_id]
    raw_id = params.get("id")
    if raw_id is None:
        return {"data": [{**r, "counterparty": dict(r["counterparty"])} for r in rows],
                "counterparties": [dict(c) for c in _MOCK_CLIENT_COUNTERPARTIES]}
    try:
        order_id = str(uuid.UUID(str(raw_id)))
    except ValueError:
        raise ParameterError("Parameter 'id' must be an order UUID") from None
    row = next((r for r in rows if r["id"] == order_id), None)
    if row is None:
        raise NotFoundError(f"Order {order_id} not found among this client's orders")
    items = [{**i, "product": dict(i["product"])} for i in _MOCK_CLIENT_ORDER_ITEMS[order_id]]
    return {"data": {**row, "counterparty": dict(row["counterparty"]), "items": items}}


def _mock_client_bonus(kw: dict) -> dict:
    params = kw.get("params") or {}
    if not str(params.get("login") or "").strip():
        raise ParameterError("Parameter 'login' is required")
    raw_cp = params.get("counterparty")
    if raw_cp is not None:
        try:
            cp_id = str(uuid.UUID(str(raw_cp)))
        except ValueError:
            raise ParameterError("Parameter 'counterparty' must be a counterparty UUID") from None
        if not any(c["id"] == cp_id for c in _MOCK_CLIENT_COUNTERPARTIES):
            raise NotFoundError(f"Counterparty {cp_id} is not among this client's")
    return {"data": {"balance": None, "accrued": None, "unit": "points"}}


# login -> session epoch; a login is known to the mock once a confirmed start saved it.
_MOCK_CLIENT_EPOCH: dict[str, int] = {}


def _mock_client_login(kw: dict) -> str:
    login = str((kw.get("json") or {}).get("login") or "").strip()
    if not login:
        raise ParameterError("login is required: a phone number or an e-mail")
    return login.lower() if "@" in login else "".join(ch for ch in login if ch in "0123456789")


def _mock_client_auth_start(kw: dict) -> dict:
    body = kw.get("json") or {}
    login = _mock_client_login(kw)
    confirmed = body.get("confirmed")
    if not isinstance(confirmed, bool):
        raise ParameterError("confirmed is required: true or false")
    result: dict[str, Any] = {
        "login": login, "known": False, "status": None, "chat_id": None, "name": None,
        "contact": None, "counterparty": None, "contact_created": False, "allowed": False,
        "matches": None, "chat_id_conflict": False,
        "session_epoch": _MOCK_CLIENT_EPOCH.get(login, 0),
    }
    if not confirmed:
        return {"data": result}
    _MOCK_CLIENT_EPOCH.setdefault(login, 0)
    chat_id = body.get("chat_id")
    chat_text = None if chat_id is None else str(chat_id).strip() or None
    result.update(
        known=True, matches=1, chat_id=chat_text, name=body.get("name") or None,
        status="Active" if chat_text else "Pending",
        contact={"id": "4a6c8e20-0000-4000-8000-000000000001", "type": "КонтактныеЛица",
                 "name": "Контактна особа (mock)"},
        counterparty={"id": "e3f1d9b6-0000-4000-8000-000000000002", "type": "Контрагенты",
                      "name": "Альфа Трейд, ТОВ"},
    )
    return {"data": result}


def _mock_client_auth_revoke(kw: dict) -> dict:
    login = _mock_client_login(kw)
    if login not in _MOCK_CLIENT_EPOCH:
        raise NotFoundError(f"Client login {login} not found")
    _MOCK_CLIENT_EPOCH[login] += 1
    return {"data": {"login": login, "session_epoch": _MOCK_CLIENT_EPOCH[login]}}


def _mock_response(method: str, path: str, kw: dict) -> Any:
    if path.endswith("/health"):
        return {"status": "ok", "service": "MCBP_AI", "key": True, "mock": True}
    if method == "POST" and path.rstrip("/") == "ai/v1/client/auth/start":
        return _mock_client_auth_start(kw)
    if method == "POST" and path.rstrip("/") == "ai/v1/client/auth/revoke":
        return _mock_client_auth_revoke(kw)
    if path.rstrip("/") == "ai/v1/eventlog":
        return _mock_event_log(kw)
    if path.rstrip("/") == "ai/v1/client/orders":
        return _mock_client_orders(kw)
    if path.rstrip("/") == "ai/v1/client/bonus":
        return _mock_client_bonus(kw)
    if method == "POST" and path.rstrip("/") == "ai/v1/functions":
        return _mock_function_upsert(kw)
    if method == "POST" and path.startswith("ai/v1/functions/") and path.endswith("/status"):
        return _mock_function_status(path, kw)
    if "/metadata/" in path:
        return _mock_metadata(path)
    if method == "PATCH" and "ai/v1/object/" in path:
        parts = path.rstrip("/").split("/")
        kind, type_, object_id = parts[-3], parts[-2], parts[-1]
        body = kw.get("json") or {}
        changed = [{"field": k, "old": "<mock:old>", "new": str(v)} for k, v in body.items()]
        return {"metadata": kind.lower(), "type": type_, "id": object_id,
                "changed": changed, "posted": True}
    if "ai/v1/object/" in path:  # singular: full data of one object (dict, not a list — §4.10)
        return {"metadata": "catalog", "type": path.split("/")[-2],
                 "data": {
                     "Ref": {"Presentation": "Альфа Трейд, ТОВ", "Data": "e3f1d9b6-0002",
                              "Metadata": "Контрагенты"},
                     "Code": "000000002", "Description": "Альфа Трейд, ТОВ",
                     "DeletionMark": False, "IsFolder": False,
                     "Pokupatel": True, "KodPoEDRPOU": "314159265",
                 }}
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
        # /schema now returns exactly the same shape as the ai_metadata_get detail
        # (MCBP_AI.MetadataDetail) — no separate "fields" key.
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
    if method == "POST" and "/registers/" in path and path.endswith("/records"):
        body = kw.get("json") or {}
        changed = [{"field": k, "old": None, "new": v} for k, v in body.items()]
        return {"register": path.split("/")[-2], "created": True, "key": {},
                "changed": changed}
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


# Mock for structure introspection — the shape mirrors the real ai_metadata_get.
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
