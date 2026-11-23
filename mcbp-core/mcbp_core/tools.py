"""Shared tool registry: the bridge between LLM tool-calls and the BAS MCBP_AI service.

Each `ToolSpec` is (a) what a caller (backend LLM loop, mcp-server) exposes to a model, and
(b) an async executor that calls `MCBPClient`. The model decides WHICH data to pull and in
what order — that is the 'data flows are driven by the model' design from the requirement.

Descriptions are copied VERBATIM from `backend/app/services/tools.py` — do not edit wording
or key order here without a dedicated task; any change invalidates the Anthropic prompt cache
(`providers.py` caches the system+tools prefix). New tools go at the END of `TOOLS` only —
`search_catalog` must stay `TOOLS[0]` (`MockProvider` picks `tools[0]`).

This module does NOT import `app.*` or `mcp.*` — it is consumed by both the backend adapter
(`app/services/tools.py`) and the (future) standalone `mcp-server/`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from mcbp_core.client import MCBPClient
from mcbp_core.errors import NotFoundError, ParameterError

Executor = Callable[[MCBPClient, dict[str, Any]], Awaitable[Any]]

_BOTH_SURFACES = frozenset({"backend", "mcp"})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    executor: Executor
    read_only: bool = True
    surfaces: frozenset[str] = field(default_factory=lambda: _BOTH_SURFACES)
    description_en: str | None = None

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        """Raise `ParameterError` naming the missing keys and the accepted ones.

        Executors read required arguments as `a["type"]`, so a model that misspells a parameter
        gets a bare `KeyError`. That escapes as an opaque protocol error whose message is just
        `'type'` — the model cannot tell a wrong name from a broken server, so it does not
        self-correct. Callers validate here BEFORE the executor runs.
        """
        missing = [name for name in self.parameters.get("required", []) if name not in arguments]
        if not missing:
            return
        accepted = ", ".join(self.parameters.get("properties", {})) or "(none)"
        raise ParameterError(
            f"{self.name}: missing required parameter(s): {', '.join(missing)}. "
            f"Accepted parameters: {accepted}",
        )


def _params(properties: dict, required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_PROJECT_REF = {"type": "string",
                "description": "Проєкт як \"ІмяТипу:UUID\" (ІмяТипу — довідник проєктів бази, "
                               "напр. MCBP_Projects; поле `project` з upsert_project); "
                               "\"\" очищає"}


def _extra(a: dict, *names: str) -> dict:
    """Optional newer-module arguments, forwarded only when the model passed them."""
    return {n: a[n] for n in names if a.get(n) is not None}


# A chart of characteristic types (e.g. the BSP ДополнительныеРеквизитыИСведения) passed where a
# catalog is expected: an older MCBP_AI answers NOT_FOUND. The hint lives in the error text only
# — the tool descriptions stay as they are (prompt cache).
_CHART_HINT = (" Якщо це план видів характеристик (напр. ДополнительныеРеквизитыИСведения) - "
               "metadata=ChartsOfCharacteristicTypes; додаткові реквізити - "
               "list_additional_attributes.")
_CATALOG_KINDS = ("catalog", "справочник", "довідник")


def _with_chart_hint(exc: NotFoundError) -> NotFoundError:
    return NotFoundError(exc.message + _CHART_HINT, code=exc.code, http_status=exc.http_status)


# --- Executors (kwargs only — the client's parameter order is NOT a contract) ---
async def _search_catalog(c: MCBPClient, a: dict) -> Any:
    try:
        return await c.list_catalog(
            type_=a["type"], cursor=a.get("cursor"), limit=a.get("limit", 50),
            q=a.get("q"), fields=a.get("fields"), **_extra(a, "only"),
        )
    except NotFoundError as exc:
        raise _with_chart_hint(exc) from exc


# The BSL answers 422 to a document list without `from`; the tool lets the model omit it (no
# period named = since 2000-01-01), so the lower bound is filled in here.
_DOCUMENTS_EARLIEST_FROM = "2000-01-01"


async def _get_documents(c: MCBPClient, a: dict) -> Any:
    return await c.list_documents(
        type_=a["type"], frm=a.get("from") or _DOCUMENTS_EARLIEST_FROM, to=a.get("to"),
        cursor=a.get("cursor"), limit=a.get("limit", 100),
        filters=a.get("filters"), fields=a.get("fields"),
        orderby=a.get("orderby"), desc=bool(a.get("desc", False)),
        agg=a.get("agg"), groupby=a.get("groupby"), **_extra(a, "q", "only"),
    )


async def _get_schema(c: MCBPClient, a: dict) -> Any:
    return await c.document_schema(type_=a["type"])


# Accounting-register arguments of get_register_balance -> register_balance kwargs. Everything
# else outside type/filters/on stays a dimension filter, as before.
_ACCOUNTING_BALANCE_ARGS = {"table": "table", "from": "date_from", "to": "date_to",
                            "account": "account", "corr_account": "corr_account",
                            "account_dr": "account_dr", "account_cr": "account_cr",
                            "groupby": "groupby", "resources": "resources", "limit": "limit"}

# Rows of an accounting answer when the model sets no limit: six amounts per resource per row.
_ACCOUNTING_DEFAULT_LIMIT = 50


async def _register_balance(c: MCBPClient, a: dict) -> Any:
    # Accept dimensions either nested under "filters" or as top-level keys (besides "type"/"on"
    # and the accounting arguments).
    filters = dict(a.get("filters") or {})
    accounting: dict[str, Any] = {}
    for k, v in a.items():
        if k in ("type", "filters", "on"):
            continue
        if k in _ACCOUNTING_BALANCE_ARGS:
            if v not in (None, "", []):
                accounting[_ACCOUNTING_BALANCE_ARGS[k]] = v
        else:
            filters[k] = v
    # Only for a call that is accounting for sure: a lone table="balance" may target an
    # accumulation register, whose balance rejects `limit`.
    table = accounting.get("table")
    if ("account" in accounting or table not in (None, "balance")) and "limit" not in accounting:
        accounting["limit"] = _ACCOUNTING_DEFAULT_LIMIT
    return await c.register_balance(type_=a["type"], filters=filters, on=a.get("on"),
                                    **accounting)


async def _register_records(c: MCBPClient, a: dict) -> Any:
    return await c.register_records(
        type_=a["type"], date_from=a.get("from"), date_to=a.get("to"),
        filters=a.get("filters"), orderby=a.get("orderby"), desc=bool(a.get("desc", False)),
        limit=a.get("limit", 100), agg=a.get("agg"), groupby=a.get("groupby"),
        **_extra(a, "fields"),
    )


async def _full_text_search(c: MCBPClient, a: dict) -> Any:
    return await c.full_text_search(a["text"], limit=a.get("limit"), metadata=a.get("metadata"))


async def _related_documents(c: MCBPClient, a: dict) -> Any:
    return await c.related_documents(
        kind=a["metadata"], type_=a["type"], object_id=a["id"], documents=a.get("documents"),
        sources=a.get("sources"), limit=a.get("limit"),
    )


async def _get_event_log(c: MCBPClient, a: dict) -> Any:
    return await c.get_event_log(
        levels=a.get("level"), date_from=a.get("from"), date_to=a.get("to"),
        event=a.get("event"), user=a.get("user"), metadata=a.get("metadata"),
        limit=a.get("limit", 50),
    )


async def _filter_catalog(c: MCBPClient, a: dict) -> Any:
    try:
        return await c.filter_catalog(
            type_=a["type"], filters=a.get("filters") or {}, orderby=a.get("orderby"),
            desc=bool(a.get("desc", False)), exclude_groups=bool(a.get("exclude_groups", False)),
            limit=a.get("limit", 100), cursor=a.get("cursor"), fields=a.get("fields"),
            agg=a.get("agg"), groupby=a.get("groupby"), **_extra(a, "only"),
        )
    except NotFoundError as exc:
        raise _with_chart_hint(exc) from exc


async def _list_metadata(c: MCBPClient, a: dict) -> Any:
    return await c.list_readable_metadata(kind=a["metadata"])


async def _describe_metadata(c: MCBPClient, a: dict) -> Any:
    return await c.describe_metadata(
        kind=a["metadata"], type_=a["type"], tabular_section=a.get("tabular_section"),
        q=a.get("q"),
    )


# Tool-layer default for rows per tabular section: a 1.68M-char budget document in one tool
# result is useless to the model; it pages with `offset` using `tabular_section_counts`.
_OBJECT_ROWS_DEFAULT = 200


async def _get_object(c: MCBPClient, a: dict) -> Any:
    try:
        return await c.get_object(
            kind=a["metadata"], type_=a["type"], object_id=a["id"], fields=a.get("fields"),
            sections=a.get("tabular_sections"), rows=a.get("rows_limit", _OBJECT_ROWS_DEFAULT),
            offset=a.get("offset"), **_extra(a, "additional", "only"),
        )
    except NotFoundError as exc:
        if str(a.get("metadata", "")).strip().lower().startswith(_CATALOG_KINDS):
            raise _with_chart_hint(exc) from exc
        raise


async def _health(c: MCBPClient, a: dict) -> Any:
    return await c.health()


async def _write_object(c: MCBPClient, a: dict) -> Any:
    return await c.create_object(
        type_=a["type"], body=a["payload"], key=a.get("key"),
        object_id=a.get("id"), base=a.get("base"),
    )


async def _save_context(c: MCBPClient, a: dict) -> Any:
    return await c.push_ai_context(
        {"conversation_id": a["conversation_id"], "role": a["role"], "content": a["content"]}
    )


async def _patch_object(c: MCBPClient, a: dict) -> Any:
    return await c.patch_object(
        kind=a["metadata"], type_=a["type"], object_id=a["id"], fields=a["fields"],
    )


async def _set_register_record(c: MCBPClient, a: dict) -> Any:
    return await c.set_register_record(type_=a["type"], fields=a["fields"])


def _uuid(name: str, value: Any) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except ValueError:
        raise ParameterError(f"{name} is not a UUID: {value!r}") from None


def _type_list(value: Any) -> list[str] | None:
    """`type` as a list of type names: a lone string is one type."""
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(t, str) and t.strip() for t in items):
        raise ParameterError("type must be a type name or a list of them, e.g. \"String\", "
                             "[\"CatalogRef.Номенклатура\"]")
    if not items:
        raise ParameterError("type must not be empty")
    return [t.strip() for t in items]


def _add_values(value: Any) -> list[str] | None:
    """`add_values` as a list of value names: a lone string is one value."""
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or not all(
            isinstance(v, str) and v.strip() for v in items):
        raise ParameterError("add_values must be a non-empty list of value names, "
                             "e.g. [\"Червоний\"]")
    return [v.strip() for v in items]


# Arguments of each mode of list_additional_attributes; a call mixing modes is refused rather
# than silently answering one of them.
_ATTRIBUTE_LIST_MODES = {
    "id": ("id",),
    "object_id": ("object_id", "metadata", "type"),
    "list": ("owner", "set", "q", "include_deleted", "limit", "cursor"),
}


async def _list_additional_attributes(c: MCBPClient, a: dict) -> Any:
    given = {mode for mode, names in _ATTRIBUTE_LIST_MODES.items()
             if any(a.get(n) not in (None, "", False) for n in names)}
    if (a.get("metadata") or a.get("type")) and not a.get("object_id"):
        raise ParameterError("metadata/type only go with object_id (the owner object's values); "
                             "for the attributes of a type pass owner, e.g. "
                             "owner=\"Catalog.Контрагенты\"")
    if len(given) > 1:
        raise ParameterError("pick one mode: id (one attribute's card), object_id with "
                             "metadata+type (one object's values), or owner/set/q (a list); "
                             "got arguments of " + ", ".join(sorted(given)))
    if a.get("id"):
        return await c.get_property(a["id"])
    if a.get("object_id"):
        if not a.get("metadata") or not a.get("type"):
            raise ParameterError("object_id needs metadata and type of the owner object, "
                                 "e.g. metadata=\"Catalogs\", type=\"Контрагенты\"")
        return await c.get_property_values(a["metadata"], a["type"], a["object_id"])
    return await c.list_properties(
        set=a.get("set"), owner=a.get("owner"), q=a.get("q"),
        include_deleted=bool(a.get("include_deleted", False)),
        limit=a.get("limit", 100), cursor=a.get("cursor"),
    )


_ATTRIBUTE_OPTIONAL = ("comment", "tooltip", "required", "multiline")


async def _upsert_additional_attribute(c: MCBPClient, a: dict) -> Any:
    title = a.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ParameterError("title must not be blank")
    types = _type_list(a.get("type"))
    add_values = _add_values(a.get("add_values"))
    if not a.get("id"):
        missing = [n for n, v in (("title", title), ("type", types), ("set", a.get("set")))
                   if not v]
        if missing:
            raise ParameterError("creating an additional attribute needs "
                                 + ", ".join(missing) + " (pass `id` to change an existing one)")
        if a.get("deletion_mark") is not None:
            raise ParameterError("deletion_mark applies to an existing attribute - pass `id`")
        return await c.create_property(title.strip(), types, _uuid("set", a["set"]),
                                       **_extra(a, *_ATTRIBUTE_OPTIONAL),
                                       **({"add_values": add_values} if add_values else {}))
    fields = _extra(a, "title", "set", "deletion_mark", *_ATTRIBUTE_OPTIONAL)
    if "set" in fields:
        fields["set"] = _uuid("set", fields["set"])
    if types is not None:
        fields["type"] = types
    if add_values:
        fields["add_values"] = add_values
    if not fields:
        raise ParameterError("nothing to change - pass at least one field besides `id`")
    return await c.patch_property(_uuid("id", a["id"]), **fields)


async def _set_additional_attribute_values(c: MCBPClient, a: dict) -> Any:
    values = a.get("set") or {}
    remove = a.get("remove") or []
    if not isinstance(values, dict):
        raise ParameterError("set must be an object {property: value}")
    if not isinstance(remove, list) or not all(isinstance(r, str) and r.strip() for r in remove):
        raise ParameterError("remove must be a list of property UUIDs or titles")
    if not values and not remove:
        raise ParameterError("pass `set` and/or `remove` - both are empty")
    return await c.patch_property_values(
        a["metadata"], a["type"], _uuid("id", a["id"]),
        set=[{"property": k, "value": v} for k, v in values.items()] or None,
        remove=remove or None,
    )


async def _set_function_status(c: MCBPClient, a: dict) -> Any:
    return await c.set_function_status(
        function_id=a["function_id"], version=a["version"], status=a["status"],
        date_execution=a.get("date_execution"), disable=a.get("disable"),
    )


async def _upsert_function(c: MCBPClient, a: dict) -> Any:
    return await c.upsert_function(
        a.get("function_id"), description=a.get("description"),
        functions_type=a.get("functions_type"), deletion_mark=a.get("deletion_mark"),
        specification=a.get("specification"), ideas=a.get("ideas"),
        project=a.get("project"), parent_id=a.get("parent_id"), is_folder=a.get("is_folder"),
        documentation=a.get("documentation"),
    )


async def _upsert_idea(c: MCBPClient, a: dict) -> Any:
    return await c.upsert_idea(
        a.get("idea_id"), description=a.get("description"),
        specification=a.get("specification"), deletion_mark=a.get("deletion_mark"),
        project=a.get("project"),
    )


async def _upsert_task_status(c: MCBPClient, a: dict) -> Any:
    return await c.upsert_task_status(
        a.get("status_id"), description=a.get("description"),
        deletion_mark=a.get("deletion_mark"),
    )


async def _upsert_project(c: MCBPClient, a: dict) -> Any:
    return await c.upsert_project(
        a.get("project_id"), type_=a.get("type"), description=a.get("description"),
        parent_id=a.get("parent_id"), is_folder=a.get("is_folder"),
        deletion_mark=a.get("deletion_mark"),
    )


async def _upsert_bug(c: MCBPClient, a: dict) -> Any:
    return await c.upsert_bug(
        a.get("bug_id"), description=a.get("description"), project=a.get("project"),
        function_id=a.get("function_id"), details=a.get("details"), source=a.get("source"),
        fixed_version=a.get("fixed_version"), deletion_mark=a.get("deletion_mark"),
    )


async def _set_bug_status(c: MCBPClient, a: dict) -> Any:
    return await c.set_bug_status(
        a["bug_id"], status=a["status"], fixed_version=a.get("fixed_version"),
    )


async def _list_function_tasks(c: MCBPClient, a: dict) -> Any:
    return await c.list_function_tasks(a["function_id"], limit=a.get("limit", 50))


async def _get_client_orders(c: MCBPClient, a: dict) -> Any:
    return await c.get_client_orders(a["login"], order_id=a.get("order_id"),
                                     counterparty=a.get("counterparty"))


async def _get_client_bonus(c: MCBPClient, a: dict) -> Any:
    return await c.get_client_bonus(a["login"], counterparty=a.get("counterparty"))


async def _upsert_function_task(c: MCBPClient, a: dict) -> Any:
    return await c.upsert_function_task(
        a["function_id"], a.get("task_id"), description=a.get("description"),
        specification=a.get("specification"), deadline=a.get("deadline"),
        importance=a.get("importance"), executed=a.get("executed"),
        deletion_mark=a.get("deletion_mark"),
    )


async def _exchange_enqueue(c: MCBPClient, a: dict) -> Any:
    return await c.exchange_enqueue(a["base"], metadata=a["metadata"], type_=a["type"],
                                    object_id=a["id"])


async def _exchange_run(c: MCBPClient, a: dict) -> Any:
    return await c.exchange_run(a["base"])


async def _exchange_result(c: MCBPClient, a: dict) -> Any:
    return await c.exchange_result(a["base"], metadata=a["metadata"], type_=a["type"],
                                   object_id=a["id"])


_EXCHANGE_BASE = {"type": "string",
                  "description": "Identifier вузла обміну (елемент довідника "
                                 "MCBP_InformationBases), напр. DemoBASTrade"}
_EXCHANGE_OBJECT_PARAMS = _params(
    {"base": _EXCHANGE_BASE,
     "metadata": {"type": "string", "description": "Вид: catalog, document, ..."},
     "type": {"type": "string", "description": "Ім'я об'єкта метаданих, напр. РеализацияТоваровУслуг"},
     "id": {"type": "string", "description": "UUID наявного об'єкта (Ref.Data)"}},
    ["base", "metadata", "type", "id"],
)
_EXCHANGE_COMMON_UA = (
    "Потрібні ліцензії CyberneticManagement, IntegrationExchange, IntegrationTransformation "
    "(інакше LICENSE_REQUIRED) і право Update на черзі обміну (інакше FORBIDDEN)."
)
_EXCHANGE_COMMON_EN = (
    "Needs the CyberneticManagement, IntegrationExchange and IntegrationTransformation licences "
    "(else LICENSE_REQUIRED) and the Update right on the exchange queue (else FORBIDDEN)."
)


# Short English descriptions of the busiest backend tools, shared by both surfaces: every
# character here is sent on every LLM step. Keep constraints, drop prose.
_ONLY = {"type": "boolean",
         "description": "With fields: return only those columns; unknown name gives 422"}
_AGG_DESC = "Instead of rows: count, sum/avg/min/max:<numeric field>"

_SEARCH_CATALOG_DESC = (
    "Search catalog items by name/code. Rows carry only standard fields (Ref, Code, "
    "Description, IsFolder, Parent, DeletionMark); `fields` adds attributes (unknown names go to "
    "`unknown_fields`, not an error). An empty field in a row does not mean the attribute is "
    "empty: read values with get_object by Ref. To count, do not count rows: use filter_catalog "
    "with agg=[\"count\"] and exclude_groups=true."
)
_GET_DOCUMENTS_DESC = (
    "Documents of one type. Dates YYYY-MM-DD. No period named: omit `from` (searches since "
    "2000-01-01). Single latest/first document: omit `from`, use the recipe below. A 12-month "
    "window only for a list of recent (останні/нещодавні) documents. Rows carry only standard "
    "fields (Number, Date, Posted...); `fields` adds header attributes (unknown names go to "
    "`unknown_fields`). Filter by a reference field with `filters` {field: UUID from Ref.Data} "
    "(composite type: \"TypeName:UUID\"); find the UUID via search_catalog/filter_catalog, "
    "field names via describe_metadata. Presentation is display text, not a filter key; by "
    "number use `q` (digits match without leading zeros), check Date. Full data and tabular "
    "sections: get_object(type=Metadata, id=Data). Without `orderby` row order is arbitrary, "
    "not chronological. Latest document: orderby=\"Дата\", desc=true, limit=1 (first: "
    "desc=false), no `from`; \"Дата\" is typical in Russian-named configurations, "
    "not guaranteed: a wrong name gives 422 BAD_PARAMETER naming the real field, retry with it, "
    "else describe_metadata with q=\"дата\". `cursor` does not work with `orderby` or `agg`. "
    "For how many/sum/average/max/min use `agg` (e.g. [\"count\"], [\"sum:СуммаДокумента\"]) "
    "instead of rows; the answer is {\"aggregates\":[...]}."
)
_REGISTER_BALANCE_DESC = (
    "Balance/turnovers of an accumulation or accounting register. Dimension names come from "
    "describe_metadata, never guessed; reference values are UUIDs from Ref.Data, e.g. "
    "filters={\"Контрагент\":\"<uuid>\"}. ACCUMULATION `table`: balance (default, at `on`), "
    "turnovers (turnover-type registers allow only these), balance_and_turnovers; turnover "
    "tables need `from`, groupby Period:month etc. ACCOUNTING (AccountingRegisters): balance "
    "(at `on`), turnovers, balance_and_turnovers (trial balance), drcr_turnovers (Dr->Cr); the "
    "last three need `from`, no `to` = today. account=[\"361\"] includes subaccounts; one row "
    "per account unless groupby=[\"ExtDimension1\"] (`ext_dimension_types` names each "
    "ExtDimensionN); drcr_turnovers uses ExtDimensionDr1..N/ExtDimensionCr1..N. "
    "resources=[\"<main amount>\"] keeps the answer small. Postings: get_register_records. An "
    "'MCBP_AI outdated' error means the configuration needs an update."
)
_DESCRIBE_METADATA_DESC = (
    "Structure of one metadata object, no record values: attributes with types; registers: "
    "dimensions/resources/attributes; enums: values; accounting registers also "
    "chart_of_accounts, correspondence and query_fields (names such as AccountDr, "
    "ExtDimensionCr1, <Dimension>Dr for filters/groupby). Use it for exact field names before "
    "filtering; never guess them. Tabular sections come as a list (name, synonym, attribute "
    "count); pass tabular_section=\"<name>\" for their attributes. Without `q` all header "
    "attributes come back (100+ on a document); `q` narrows by internal name or Ukrainian "
    "synonym; no match returns `available_names` as \"Name (Synonym)\". The user's wording may "
    "be a form caption that differs from the synonym: on a miss pick the field by meaning from "
    "those synonyms, then pass its Name."
)
_FILTER_CATALOG_DESC = (
    "Server-side catalog selection filtered and sorted by any fields; use it instead of "
    "search_catalog to filter/sort by a field. `filters` {field: value}: equality, a string "
    "with % is LIKE; field names from describe_metadata. Rows carry standard fields plus "
    "`fields` (unknown names go to `unknown_fields`); an empty field in a row does not mean the "
    "attribute is empty: read values with get_object. For how many/sum/average/max/min use "
    "`agg` (answer {\"aggregates\":[...]}), never count rows. Item count: agg=[\"count\"] with "
    "exclude_groups=true; for a hierarchical catalog count again without exclude_groups and "
    "report the difference as groups."
)
_REGISTER_RECORDS_DESC = (
    "Rows of an information register (no Ref: only this tool reads them), or raw movements of "
    "an accumulation register (computed balance: get_register_balance). `from`/`to` "
    "YYYY-MM-DD, both optional (open bounds); passed for a non-periodic register: 422 "
    "BAD_PARAMETER. `filters` {field: value} on any dimension/resource/attribute or standard "
    "field (internal name or English alias Period/Recorder/Active/LineNumber/RecordType); "
    "reference = UUID from Ref.Data; % in a string = LIKE. Unknown field in filters/orderby: 422 "
    "BAD_PARAMETER listing the real fields. Without `orderby` a periodic register comes newest "
    "first (latest record: limit=1). No cursor: `truncated: true` means narrow the filter. "
    "Totals/comparisons: always `agg` (+`groupby`), e.g. agg=[\"sum:*\"], "
    "groupby=[\"<dimension>\",\"Period:month\"]; answer {\"aggregates\":[...]}, inactive "
    "movements excluded, a balance register splits groups by RecordType "
    "(`record_type_split: true`). ACCOUNTING register: rows are postings (AccountDr/AccountCr, "
    "ExtDimensionDr1..); `from` or filters.Recorder is REQUIRED; filters.AccountDr=\"361\" "
    "includes subaccounts; filters.Account / filters.ExtDimension = either side / any position. "
    "Per-account totals: get_register_balance table=turnovers (agg groups by the exact account)."
)


# --- Registry ---
# ORDER MATTERS: MockProvider calls TOOLS[0], so search_catalog must stay first.
# Introspection tools were appended at the end; the "structure first" priority is set in
# SYSTEM_PROMPT instead. health/write_object/save_context were appended at the END too (stage 5)
# — so as not to shift TOOLS[0].
TOOLS: dict[str, ToolSpec] = {
    "search_catalog": ToolSpec(
        "search_catalog",
        _SEARCH_CATALOG_DESC,
        _params(
            {"type": {"type": "string", "description": "Catalog name, e.g. Контрагенты"},
             "q": {"type": "string",
                   "description": "Name/code; digits also match a code without leading zeros"},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Extra attributes per row"},
             "only": _ONLY,
             "limit": {"type": "integer", "default": 50},
             "cursor": {"type": "string"}},
            ["type"],
        ),
        _search_catalog,
        description_en=_SEARCH_CATALOG_DESC),
    "get_documents": ToolSpec(
        "get_documents",
        _GET_DOCUMENTS_DESC,
        _params(
            {"type": {"type": "string", "description": "Document type, e.g. ЗаказПокупателя"},
             "from": {"type": "string",
                      "description": "Period start YYYY-MM-DD. Omit when no period is named "
                                     "(since 2000-01-01) and for a single latest/first "
                                     "document; 12 months back only for a recent list"},
             "to": {"type": "string", "description": "Period end YYYY-MM-DD; omitted = open"},
             "filters": {"type": "object", "description": "{field: value}; reference = UUID"},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Extra header attributes per row"},
             "orderby": {"type": "string", "description": "Sort field, e.g. Дата"},
             "desc": {"type": "boolean", "default": False},
             "limit": {"type": "integer", "default": 100},
             "cursor": {"type": "string"},
             "only": _ONLY,
             "q": {"type": "string",
                   "description": "Number search; digits match without leading zeros"},
             "agg": {"type": "array", "items": {"type": "string"},
                     "description": _AGG_DESC + ", e.g. [\"sum:СуммаДокумента\"]"},
             "groupby": {"type": "string",
                         "description": "Group field for agg (top-N: orderby=aggregate "
                                        "alias); ignored without agg"}},
            ["type"],
        ),
        _get_documents,
        description_en=_GET_DOCUMENTS_DESC),
    "get_schema": ToolSpec(
        "get_schema",
        "Повертає СТРУКТУРУ документа: імена полів і їх типи, БЕЗ значень жодного конкретного "
        "запису. Наявність поля тут не каже, заповнене воно чи порожнє — корисно лише щоб "
        "зрозуміти структуру перед читанням/записом.",
        _params({"type": {"type": "string"}}, ["type"]),
        _get_schema,
        description_en=(
            "Returns the document's STRUCTURE: field names and their types, WITHOUT the values "
            "of any specific record. A field being present here does not say whether it is "
            "filled or empty — useful only to understand the structure before reading/writing."
        )),
    "get_register_balance": ToolSpec(
        "get_register_balance",
        _REGISTER_BALANCE_DESC,
        _params(
            {"type": {"type": "string",
                      "description": "Accumulation or accounting register (from list_metadata)"},
             "filters": {"type": "object",
                         "description": "{dimension: value}; reference = UUID. Accounting: "
                                        "dimension or ExtDimension1..N (any position); "
                                        "drcr_turnovers: ExtDimensionDr1..N/ExtDimensionCr1..N"},
             "on": {"type": "string",
                    "description": "Balance date YYYY-MM-DD (omitted = current); a date only, "
                                   "not a dimension filter"},
             "table": {"type": "string",
                       "enum": ["balance", "turnovers", "balance_and_turnovers", "drcr_turnovers"],
                       "description": "drcr_turnovers: accounting registers only"},
             "from": {"type": "string", "description": "Turnover tables: start YYYY-MM-DD"},
             "to": {"type": "string", "description": "Turnover tables: end YYYY-MM-DD"},
             "account": {"type": "array", "items": {"type": "string"},
                         "description": "Account codes (e.g. 361) or UUIDs; subaccounts included"},
             "corr_account": {"type": "array", "items": {"type": "string"},
                              "description": "table=turnovers only"},
             "account_dr": {"type": "array", "items": {"type": "string"},
                            "description": "table=drcr_turnovers only"},
             "account_cr": {"type": "array", "items": {"type": "string"},
                            "description": "table=drcr_turnovers only"},
             "groupby": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                         "description": "ExtDimension1..N, a dimension, CorrAccount, "
                                        "Period:day|week|month|quarter|year (not with balance); "
                                        "account is always a group"},
             "resources": {"type": "array", "items": {"type": "string"},
                           "description": "Amount resources (default: all numeric)"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 1000,
                       "description": "Rows (default 50); truncated=true: narrow the query"}},
            ["type"],
        ),
        _register_balance,
        description_en=_REGISTER_BALANCE_DESC),
    "list_metadata": ToolSpec(
        "list_metadata",
        "Структура конфігурації BAS. metadata='all' → перелік об'єктів усіх видів одразу; "
        "metadata=Catalogs|Documents|InformationRegisters|AccumulationRegisters|"
        "AccountingRegisters|ChartsOfAccounts|Enums|Tasks|... → "
        "список об'єктів цього виду (ім'я + синонім, БЕЗ реквізитів і БЕЗ значень). "
        "Працює для будь-якої конфігурації. Виклич, щоб дізнатися реальні імена типів, "
        "замість того щоб їх вгадувати.",
        _params(
            {"metadata": {"type": "string",
                          "description": "Вид метаданих (Catalogs, Documents, InformationRegisters, ...) або 'all'"}},
            ["metadata"],
        ),
        _list_metadata,
        description_en=(
            "BAS configuration structure. metadata='all' → list of objects of every kind at "
            "once; metadata=Catalogs|Documents|InformationRegisters|AccumulationRegisters|"
            "AccountingRegisters|ChartsOfAccounts|Enums|Tasks|... → list of objects of that kind (name + synonym, WITHOUT attributes "
            "and WITHOUT values). Works for any configuration. Call this to learn the real type "
            "names instead of guessing them."
        )),
    "describe_metadata": ToolSpec(
        "describe_metadata",
        _DESCRIBE_METADATA_DESC,
        _params(
            {"metadata": {"type": "string",
                          "description": "Kind: Catalogs, Documents, InformationRegisters, ..."},
             "type": {"type": "string", "description": "Object name, e.g. Контрагенты"},
             "tabular_section": {"type": "string",
                                 "description": "One tabular section: returns only its "
                                                "attributes"},
             "q": {"type": "string",
                   "description": "Case-insensitive substring of a header attribute's name or "
                                  "synonym (a form caption may differ; on a miss match by "
                                  "meaning)"}},
            ["metadata", "type"],
        ),
        _describe_metadata,
        description_en=_DESCRIBE_METADATA_DESC),
    "filter_catalog": ToolSpec(
        "filter_catalog",
        _FILTER_CATALOG_DESC,
        _params(
            {"type": {"type": "string", "description": "Catalog name, e.g. Контрагенты"},
             "filters": {"type": "object",
                         "description": "{field: value}, e.g. {\"Покупатель\": true}"},
             "orderby": {"type": "string", "description": "Sort field"},
             "desc": {"type": "boolean", "default": False},
             "exclude_groups": {"type": "boolean", "default": False,
                                "description": "Skip folder groups (IsFolder items)"},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Extra attributes per row"},
             "only": _ONLY,
             "limit": {"type": "integer", "default": 100},
             "agg": {"type": "array", "items": {"type": "string"}, "description": _AGG_DESC},
             "groupby": {"type": "string",
                         "description": "Group field for agg (top-N: orderby=aggregate "
                                        "alias); ignored without agg"}},
            ["type"],
        ),
        _filter_catalog,
        description_en=_FILTER_CATALOG_DESC),
    "get_object": ToolSpec(
        "get_object",
        "ЄДИНИЙ спосіб отримати РЕАЛЬНІ ЗНАЧЕННЯ реквізитів одного конкретного об'єкта (усі "
        "реквізити + табличні частини). Списки (search_catalog/filter_catalog/get_documents) і "
        "структурні інструменти (describe_metadata/list_metadata) НЕ містять значень "
        "реквізитів — не роби висновок «поле порожнє» доти, доки не перевірив саме тут. "
        "Відкриває БУДЬ-ЯКЕ посилання {Presentation, Data, Metadata} з результату — Ref рядка "
        "списку, Recorder/Регистратор, документ у рядку регістру, рядок табличної частини, "
        "реквізит: type=<Metadata>, id=<Data>; metadata=Documents для Recorder чи «… № … від "
        "…», інакше Catalogs (NOT_FOUND — вид Metadata дізнайся з list_metadata/describe_metadata). Presentation — лише текст, НЕ шукай за ним списком. Великі документи: передай `tabular_sections` (імена з "
        "describe_metadata; [] — без табличних частин) і `rows_limit` (типово 200 рядків на "
        "частину); `tabular_section_counts` у відповіді показує повний розмір кожної частини — "
        "решту гортай через `offset`. Додаткові реквізити/відомості приходять разом "
        "з об'єктом (`additional`) — окремо не шукай.",
        _params(
            {"metadata": {"type": "string", "description": "Вид: Catalogs, Documents, Tasks, ..."},
             "type": {"type": "string",
                      "description": "Тип об'єкта, напр. Контрагенты; у посилання — його Metadata"},
             "id": {"type": "string", "description": "UUID: Data посилання (Ref.Data чи інше поле)"},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Лише ці реквізити шапки (стандартні — Ref, Code/Number, "
                                       "Description/Date… — є завжди); без поля — усі"},
             "tabular_sections": {"type": "array", "items": {"type": "string"},
                                  "description": "Лише ці табличні частини; [] — жодної; без "
                                                 "поля — усі"},
             "rows_limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000,
                            "description": "Максимум рядків на кожну табличну частину"},
             "offset": {"type": "integer", "minimum": 0,
                        "description": "Пропустити перші N рядків кожної табличної частини "
                                       "(наступна сторінка)"},
             "only": {"type": "boolean", "description": "With fields: return only those columns; unknown name gives 422 (module ≥ 1.0.41.6)"},
             "additional": {"type": "boolean", "description": "Additional attributes/info: false disables them; with only, shown only if true (module ≥ 1.0.41.6)"}},
            ["metadata", "type", "id"],
        ),
        _get_object,
        description_en=(
            "The ONLY way to get the REAL VALUES of a single specific object's attributes (all "
            "attributes + tabular sections). Lists (search_catalog/filter_catalog/get_documents) "
            "and structure tools (describe_metadata/list_metadata) do NOT contain "
            "attribute values — never conclude 'the field is empty' until you've checked here. "
            "Opens ANY reference {Presentation, Data, Metadata} found in a result — a list "
            "row's Ref, Recorder/Регистратор, a document in a register row, a tabular-section "
            "line, an attribute: type=<Metadata>, id=<Data>; metadata=Documents for a Recorder "
            "or a «… № … від …» presentation, else Catalogs (on NOT_FOUND find the kind of Metadata via list_metadata/describe_metadata). Presentation is display text only "
            "— never re-search a list by it. Large documents: pass `tabular_sections` (names "
            "from describe_metadata; [] — no tabular sections) and `rows_limit` (default 200 "
            "rows per section); `tabular_section_counts` in the response shows the full size of "
            "each section — page through the rest with `offset`. Additional attributes/info come with the object "
            "(`additional`) — don't search for them separately."
        )),
    "health": ToolSpec(
        "health",
        "Перевіряє доступність сервісу MCBP_AI (живий пінг BAS). Використовується лише на "
        "поверхнях, де немає окремого health-роута (напр. MCP-клієнт); у backend для цього є "
        "GET /api/v1/system/health.",
        _params({}, []),
        _health,
        read_only=True,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Checks availability of the MCBP_AI service (a live ping to BAS). Used only on "
            "surfaces without a dedicated health route (e.g. an MCP client); the backend has "
            "GET /api/v1/system/health for that instead."
        )),
    "write_object": ToolSpec(
        "write_object",
        "Записує один об'єкт через MCBP Plus і його правила конвертації (потребує розширення "
        "«MCBP Plus» на боці BAS — інакше 501 PLUS_REQUIRED). `payload` — плоский словник "
        "{поле: значення}, BAS сам будує конверт обміну. `key` — ключ обміну: той самий `key` "
        "наступного разу веде на ТОЙ САМИЙ об'єкт (без нього — завжди новий). `id` вказує на "
        "об'єкт, що вже існує в базі. `base` — Identifier іншої бази обміну "
        "(`InformationBase` — застарілий синонім `base`, не використовуй). Посилальний "
        "реквізит (і посилання в рядку табличної частини) — вкладена структура "
        "{\"KEY\": \"<uuid>\", \"CatalogObject_<ІмяТипу>\": {\"Ref\": \"<uuid>\"}} (документ — "
        "`DocumentObject_<ІмяТипу>`), не голий UUID: голий UUID лишає реквізит порожнім.",
        _params(
            {"type": {"type": "string", "description": "Тип об'єкта, напр. Контрагенты"},
             "payload": {"type": "object", "description": "{ім'я_поля: значення}"},
             "key": {"type": "string", "description": "Ключ обміну — той самий ключ = той самий об'єкт"},
             "id": {"type": "string", "description": "UUID вже наявного об'єкта в базі"},
             "base": {"type": "string", "description": "Identifier іншої бази обміну"}},
            ["type", "payload"],
        ),
        _write_object,
        read_only=False,
        description_en=(
            "Writes a single object through MCBP Plus and its conversion rules (requires the "
            "'MCBP Plus' extension on the BAS side — otherwise 501 PLUS_REQUIRED). `payload` is "
            "a flat dict {field: value}; BAS itself builds the exchange envelope. `key` is the "
            "exchange key: the SAME `key` next time leads to the SAME object (without it — "
            "always a new one). `id` points to an object that already exists in the base. "
            "`base` is the Identifier of another exchange base (`InformationBase` is a "
            "deprecated synonym of `base` — don't use it). A reference attribute (also a "
            "reference inside a tabular-section row) is the nested structure "
            "{\"KEY\": \"<uuid>\", \"CatalogObject_<TypeName>\": {\"Ref\": \"<uuid>\"}} (a "
            "document: `DocumentObject_<TypeName>`), not a bare UUID string — a bare UUID leaves "
            "the attribute empty."
        )),
    "save_context": ToolSpec(
        "save_context",
        "Передає повідомлення діалогу в MCBP_AI. УВАГА: на боці BAS це поки скелет — сервіс "
        "відповідає 202 і НІЧОГО не зберігає (персистенції ще немає), тому покладатись на цей "
        "інструмент як на пам'ять розмови не можна.",
        _params(
            {"conversation_id": {"type": "string"},
             "role": {"type": "string", "description": "напр. user, assistant"},
             "content": {"type": "string"}},
            ["conversation_id", "role", "content"],
        ),
        _save_context,
        read_only=False,
        description_en=(
            "Passes a dialogue message to MCBP_AI. WARNING: on the BAS side this is still a "
            "skeleton — the service answers 202 and stores NOTHING (there is no persistence "
            "yet), so this tool cannot be relied on as conversation memory."
        )),
    "patch_object": ToolSpec(
        "patch_object",
        "Нативно записує реквізити шапки і табличні частини об'єкта — БЕЗ MCBP Plus і БЕЗ правила "
        "конвертації (write_object іде через Plus і без правила дає 501 PLUS_REQUIRED/422 "
        "CONVERSION_NOT_CONFIGURED). Змінюються лише названі в `fields` ключі. `id` — UUID "
        "об'єкта (Ref.Data з search_catalog/get_documents), або \"new\" — СТВОРИТИ новий: лише "
        "власні довідники й документи MCBP_* (інші типи створюй через write_object). Значення "
        "шапки: посилання — UUID; СКЛАДЕНЕ посилання (кілька типів, напр. СостояниеЗаказа) — "
        "завжди \"ІмяТипу:UUID\" (типи з describe_metadata); перечислення — ім'я значення; дата — "
        "YYYY-MM-DD[ HH:MM[:SS]]; null/\"\" очищає. Табчастина: масив рядків {колонка: значення} "
        "ЗАМІНЮЄ її ([] очищає), {\"mode\":\"append\",\"rows\":[...]} ДОДАЄ рядки (не "
        "ідемпотентно — після незрозумілого результату не повторюй, перевір get_object). "
        "`IsFolder: true` при створенні — група довідника. Документ: `Posted: true` проводить, "
        "`false` скасовує проведення; без ключа проведений лишається проведеним. "
        "`DeletionMark: true|false` (лише Boolean, інше — BAD_PARAMETER) ставить/знімає позначку "
        "вилучення (SetDeletionMark) після запису в тій самій транзакції; не при створенні; потрібне право "
        "InteractiveSetDeletionMark (інакше FORBIDDEN). Документ: `Posted: true` разом з "
        "`DeletionMark: true` відхиляється; позначка проведеного документа скасовує його "
        "проведення (поведінка платформи). `Ref` змінити не можна. Збій запису/проведення — "
        "422 WRITE_FAILED, нічого не "
        "змінено. Відповідь: старе й нове значення КОЖНОГО зміненого поля (для табчастин — "
        "кількість рядків), `created` і підсумковий `posted` — підтверди результат користувачу "
        "саме за цими даними, не вигадуй.",
        _params(
            {"metadata": {"type": "string", "description": "Вид: Catalogs, Documents, Tasks, ..."},
             "type": {"type": "string", "description": "Тип об'єкта, напр. ЗаказПокупателя"},
             "id": {"type": "string",
                    "description": "UUID об'єкта (Ref.Data зі списку/get_object) або \"new\" "
                                   "для створення (лише MCBP_*)"},
             "fields": {"type": "object",
                        "description": "{поле: значення, табчастина: [{колонка: значення}]}, напр. "
                                       "{\"СостояниеЗаказа\": \"СостоянияЗаказовПокупателей:<uuid>\"}. "
                                       "Лише названі ключі змінюються."}},
            ["metadata", "type", "id", "fields"],
        ),
        _patch_object,
        read_only=False,
        description_en=(
            "Natively writes an object's header attributes and tabular sections — WITHOUT MCBP "
            "Plus and WITHOUT a conversion rule (write_object goes through Plus and without a "
            "rule gives 501 PLUS_REQUIRED/422 CONVERSION_NOT_CONFIGURED). Only the keys named in "
            "`fields` change. `id` is the object's UUID (Ref.Data from search_catalog/"
            "get_documents), or \"new\" to CREATE one: own MCBP_* catalogs and documents only "
            "(create any other type with write_object). Header values: a reference is a UUID; a "
            "COMPOSITE reference (several types, e.g. СостояниеЗаказа) is always "
            "\"TypeName:UUID\" (types from describe_metadata); an enum is its value name; a date "
            "is YYYY-MM-DD[ HH:MM[:SS]]; null/\"\" clears. A tabular section: an array of "
            "{column: value} rows REPLACES it ([] clears it), {\"mode\":\"append\",\"rows\":[...]} "
            "ADDS rows (not idempotent — after an unclear result don't repeat, check with "
            "get_object). `IsFolder: true` on create makes a catalog folder. Documents: "
            "`Posted: true` posts, `false` unposts; without the key a posted document stays "
            "posted. `DeletionMark: true|false` (Boolean only, anything else is BAD_PARAMETER) "
            "sets/clears the deletion mark (SetDeletionMark) after the write, in the same transaction; not "
            "allowed on create; needs the InteractiveSetDeletionMark right (else FORBIDDEN). "
            "Documents: `Posted: true` together with `DeletionMark: true` is rejected; marking "
            "a posted document unposts it (platform behaviour). `Ref` cannot be set. A failed "
            "write/post is 422 WRITE_FAILED "
            "and changes nothing. The response shows old and new of EACH changed field (row "
            "counts for sections), `created` and the final `posted` — confirm the result to the "
            "user from that data, don't make it up."
        )),
    "get_register_records": ToolSpec(
        "get_register_records",
        _REGISTER_RECORDS_DESC,
        _params(
            {"type": {"type": "string",
                      "description": "Information, accumulation or accounting register "
                                     "(from list_metadata)"},
             "from": {"type": "string",
                      "description": "Start YYYY-MM-DD; accounting registers: required unless "
                                     "filters.Recorder"},
             "to": {"type": "string", "description": "End YYYY-MM-DD"},
             "filters": {"type": "object", "description": "{field: value}; reference = UUID"},
             "orderby": {"type": "string",
                         "description": "Sort field, e.g. Period; with agg an aggregate alias "
                                        "(sum_<F>, count) or a group field"},
             "desc": {"type": "boolean", "default": False},
             "limit": {"type": "integer", "default": 100,
                       "description": "Rows (groups with agg), 1..1000"},
             "agg": {"type": "array", "items": {"type": "string"},
                     "description": _AGG_DESC + "; sum:* = all numeric resources"},
             "groupby": {"type": "array", "items": {"type": "string"},
                         "description": "Up to 4: dimension/attribute or "
                                        "Period:day|week|month|quarter|year; needs agg"},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Only these columns plus key ones; unknown name gives "
                                       "422"}},
            ["type"],
        ),
        _register_records,
        description_en=_REGISTER_RECORDS_DESC),
    "get_related_documents": ToolSpec(
        "get_related_documents",
        "Де використовується об'єкт: документи (і записи регістрів), що посилаються на нього. "
        "metadata/type/id — як у get_object. found_in — поле/регістр; `truncated`/`failed_sources` — "
        "звузь `documents`/`sources`. (модуль ≥ 1.0.41.6)",
        _params(
            {"metadata": {"type": "string", "description": "Kind: Catalogs, Documents, ..."},
             "type": {"type": "string", "description": "Object type name"},
             "id": {"type": "string", "description": "Object UUID (Ref.Data)"},
             "documents": {"type": "array", "items": {"type": "string"},
                           "description": "Scan only these document types (module ≥ 1.0.41.6)"},
             "sources": {"type": "string", "enum": ["documents", "registers", "all"],
                         "description": "Where to look, default all (module ≥ 1.0.41.6)"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50}},
            ["metadata", "type", "id"],
        ),
        _related_documents,
        description_en=(
            "Where an object is used: documents (and register records) that reference it. "
            "metadata/type/id as in get_object. Rows: type, ref, number, date, deleted, found_in "
            "(field/register); `truncated`, `failed_sources` — narrow `documents`/`sources`. "
            "(module ≥ 1.0.41.6)"
        )),
    "full_text_search": ToolSpec(
        "full_text_search",
        "Повнотекстовий пошук по всій базі через платформений індекс: першим, коли користувач "
        "дав довільний текст і тип невідомий; знайдені ref відкривай get_object. (модуль ≥ 1.0.41.6)",
        _params(
            {"text": {"type": "string", "description": "Text to find in the full-text index"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
             "metadata": {"type": "array", "items": {"type": "string"},
                          "description": "Restrict to full metadata names, e.g. Catalog.X, Document.Y"}},
            ["text"],
        ),
        _full_text_search,
        description_en=(
            "Free-text search across the whole infobase via the platform full-text index; use it "
            "first when the user gives arbitrary text and the type is unknown; follow returned "
            "refs with get_object. (module ≥ 1.0.41.6)"
        )),
    "set_function_status": ToolSpec(
        "set_function_status",
        "Встановлює стан функції MCBP у трекері підтримки: один запис регістра "
        "MCBP_StatusFunctions на елемент довідника MCBP_Functions (лише останній стан, без "
        "історії). `function_id` — UUID елемента MCBP_Functions (Ref.Data зі search_catalog). "
        "`status` — ім'я або найменування елемента довідника MCBP_TaskStatuses (напр. Waiting, "
        "Done або стан, доданий через upsert_task_status); невідома назва — BAD_PARAMETER з "
        "переліком допустимих. "
        "`version` — до 12 символів. `date_execution` — YYYY-MM-DD (зберігається лише дата), \"\" очищає; "
        "не передане поле `date_execution`/`disable` лишається як було. Той самий виклик удруге "
        "нічого не змінює (`changed: false`). Невідома функція — NOT_FOUND; база без цього "
        "регістра — NOT_CONFIGURED; немає права зміни регістра — FORBIDDEN.",
        _params(
            {"function_id": {"type": "string",
                             "description": "UUID елемента довідника MCBP_Functions"},
             "version": {"type": "string", "description": "Версія, до 12 символів"},
             "status": {"type": "string",
                        "description": "Назва елемента MCBP_TaskStatuses, напр. Waiting, "
                                       "Done; приймається будь-який стан бази"},
             "date_execution": {"type": "string",
                                "description": "YYYY-MM-DD; \"\" очищає дату"},
             "disable": {"type": "boolean"}},
            ["function_id", "version", "status"],
        ),
        _set_function_status,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Sets the support-tracker state of one MCBP function: the single "
            "MCBP_StatusFunctions register record of an MCBP_Functions catalog item (latest "
            "state only, no history). `function_id` is the MCBP_Functions item UUID (Ref.Data "
            "from search_catalog). `status` is the name or description of an MCBP_TaskStatuses "
            "item (e.g. Waiting, Done, or a status added via upsert_task_status); an unknown "
            "one is "
            "BAD_PARAMETER from 1C listing the allowed names. `version` is at most 12 characters. "
            "`date_execution` is YYYY-MM-DD (only the date is stored), \"\" clears it; an omitted "
            "`date_execution`/`disable` keeps its stored value. Repeating the same call changes "
            "nothing (`changed: false`). Unknown function -> NOT_FOUND; a base without this "
            "register -> NOT_CONFIGURED; no update right on the register -> FORBIDDEN."
        )),
    "upsert_function": ToolSpec(
        "upsert_function",
        "Створює або змінює елемент довідника MCBP_Functions напряму, БЕЗ правил конвертації "
        "MCBP Plus (write_object для цього довідника не працює). Без `function_id` — новий "
        "елемент (`description` обов'язковий; кожен виклик створює ще один); з `function_id` "
        "(UUID, Ref.Data зі search_catalog) — зміна лише переданих полів. `functions_type` — "
        "ім'я значення перелічення реквізиту FunctionsType (список — describe_metadata або "
        "текст BAD_PARAMETER), \"\" очищає. `deletion_mark: true` ставить позначку вилучення — "
        "так прибирають тестовий елемент. `specification` — ТЗ функції: що саме вона має робити "
        "(поведінка, вхідні дані, результат, обмеження), а не повтор назви; текст пишеться в ідею "
        "першого рядка табличної частини Ideas (ідея створюється, якщо її немає), \"\" очищає "
        "текст. `ideas` — ПОВНИЙ упорядкований список UUID елементів MCBP_Ideas для рядків "
        "Ideas: рядки замінюються цілком, [] очищає, дублікати відкидаються, невідомий UUID — "
        "NOT_FOUND з його назвою; спершу застосовуються `ideas`, потім `specification` змінює "
        "ідею НОВОГО першого рядка. `project` — \"ІмяТипу:UUID\" елемента довідника проєктів, "
        "\"\" очищає; `parent_id` — UUID групи (\"\" — верхній рівень); `is_folder: true` лише "
        "при створенні — група; `documentation` — документація, \"\" очищає. "
        "Та сама зміна удруге нічого не змінює "
        "(`changed: false`). Невідомий `function_id` — NOT_FOUND; база без довідника — "
        "NOT_CONFIGURED; немає права додавання/зміни — FORBIDDEN.",
        _params(
            {"function_id": {"type": "string",
                             "description": "UUID елемента MCBP_Functions; без нього — створення"},
             "description": {"type": "string",
                             "description": "Найменування, не довше за DescriptionLength довідника; обов'язкове при створенні"},
             "functions_type": {"type": "string",
                                "description": "Ім'я значення перелічення FunctionsType; \"\" очищає"},
             "deletion_mark": {"type": "boolean"},
             "specification": {"type": "string",
                               "description": "ТЗ: що функція має робити (поведінка, дані, результат), не повтор назви; \"\" очищає"},
             "ideas": {"type": "array", "items": {"type": "string"},
                       "description": "Повний упорядкований список UUID елементів MCBP_Ideas (рядки Ideas замінюються); [] очищає"},
             "project": _PROJECT_REF,
             "parent_id": {"type": "string",
                           "description": "UUID групи MCBP_Functions; \"\" — верхній рівень"},
             "is_folder": {"type": "boolean",
                           "description": "Лише при створенні: true створює групу"},
             "documentation": {"type": "string",
                               "description": "Документація функції; \"\" очищає"}},
            [],
        ),
        _upsert_function,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Creates or updates one MCBP_Functions catalog item natively, WITHOUT MCBP Plus "
            "conversion rules (write_object cannot write this catalog). No `function_id` -> a "
            "new item (`description` required; every such call creates another one); with "
            "`function_id` (UUID, Ref.Data from search_catalog) -> only the passed fields "
            "change. `functions_type` is a value name of the FunctionsType enumeration "
            "attribute (list via describe_metadata or the BAD_PARAMETER text), \"\" clears it. "
            "`deletion_mark: true` sets the deletion mark - that is how a probe item is cleaned "
            "up. `specification` is the function's spec: write what it must do (behaviour, "
            "inputs, result, constraints), not a repeat of its name; it is stored in the idea of "
            "the first Ideas row (created when there is none), \"\" clears the text. "
            "`ideas` is the FULL ordered list of MCBP_Ideas item UUIDs for the Ideas rows: the "
            "rows are replaced, [] clears them, duplicates are dropped, an unknown UUID -> "
            "NOT_FOUND naming it; `ideas` is applied first, then `specification` edits the idea "
            "of the NEW first row. `project` is \"TypeName:UUID\" of a project catalog item "
            "(TypeName = a project catalog of the base, e.g. MCBP_Projects; the `project` field "
            "of upsert_project), \"\" clears it, an unknown project -> NOT_FOUND. `parent_id` is "
            "a folder UUID of MCBP_Functions, \"\" moves the item to the top level; an item, the "
            "item itself or its descendant -> BAD_PARAMETER. `is_folder: true` (create only) "
            "creates a folder. `documentation` is the function's documentation text, \"\" clears "
            "it. A field not applicable to a folder/item -> BAD_PARAMETER. "
            "Repeating the same change changes nothing (`changed: false`). Unknown "
            "`function_id` -> NOT_FOUND; a base without the catalog (or, when `specification` "
            "is given, without the ideas catalog) -> NOT_CONFIGURED; no insert/update right "
            "-> FORBIDDEN."
        )),
    "upsert_idea": ToolSpec(
        "upsert_idea",
        "Створює або змінює елемент довідника MCBP_Ideas (ідея) напряму, БЕЗ правил "
        "конвертації MCBP Plus. Без `idea_id` — нова ідея (`description` обов'язковий; кожен "
        "виклик створює ще одну); з `idea_id` (UUID) — зміна лише переданих полів. "
        "`specification` — текст ідеї (що і навіщо), \"\" очищає. `deletion_mark: true` ставить "
        "позначку вилучення. Дата зміни ставиться автоматично. Щоб прив'язати ідею до функції — "
        "upsert_function з `ideas`. `project` — \"ІмяТипу:UUID\" елемента довідника проєктів, "
        "\"\" очищає. Та сама зміна удруге нічого не змінює (`changed: false`). "
        "Невідомий `idea_id` — NOT_FOUND; база без довідника — NOT_CONFIGURED; немає права "
        "додавання/зміни — FORBIDDEN.",
        _params(
            {"idea_id": {"type": "string",
                         "description": "UUID елемента MCBP_Ideas; без нього — створення"},
             "description": {"type": "string",
                             "description": "Найменування ідеї, не довше за DescriptionLength довідника; обов'язкове при створенні"},
             "specification": {"type": "string",
                               "description": "Текст ідеї: що і навіщо; \"\" очищає"},
             "deletion_mark": {"type": "boolean"},
             "project": _PROJECT_REF},
            [],
        ),
        _upsert_idea,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Creates or updates one MCBP_Ideas catalog item (an idea) natively, WITHOUT MCBP "
            "Plus conversion rules. No `idea_id` -> a new idea (`description` required; every "
            "such call creates another one); with `idea_id` (UUID) -> only the passed fields "
            "change. `specification` is the idea text (what and why), \"\" clears it. "
            "`deletion_mark: true` sets the deletion mark. The modification date is set "
            "automatically. To link an idea to a function use upsert_function with `ideas`. "
            "`project` is \"TypeName:UUID\" of a project catalog item (TypeName = a project "
            "catalog of the base, e.g. MCBP_Projects), \"\" clears it, an unknown project -> "
            "NOT_FOUND. "
            "Repeating the same change changes nothing (`changed: false`). Unknown `idea_id` -> "
            "NOT_FOUND; a base without the catalog -> NOT_CONFIGURED; no insert/update right -> "
            "FORBIDDEN."
        )),
    "upsert_task_status": ToolSpec(
        "upsert_task_status",
        "Створює або змінює елемент довідника MCBP_TaskStatuses (стан функції в трекері) "
        "напряму, БЕЗ правил конвертації MCBP Plus (write_object для цього довідника не "
        "працює). Без `status_id` — новий стан (`description` обов'язковий; кожен виклик "
        "створює ще один); з `status_id` (UUID) — зміна лише переданих полів. "
        "`deletion_mark: true` ставить позначку вилучення. Найменування нового стану потім "
        "приймає set_function_status як `status`. Передвизначені стани (Waiting, Done) "
        "лишають своє ім'я і не позначаються на вилучення. Два непозначені стани з однаковим "
        "найменуванням — CONFLICT. Та сама зміна удруге нічого не "
        "змінює (`changed: false`). Невідомий `status_id` — NOT_FOUND; база без довідника — "
        "NOT_CONFIGURED; немає права додавання/зміни — FORBIDDEN.",
        _params(
            {"status_id": {"type": "string",
                           "description": "UUID елемента MCBP_TaskStatuses; без нього — "
                                          "створення"},
             "description": {"type": "string",
                             "description": "Найменування стану, напр. Виконується; "
                                            "обов'язкове при створенні"},
             "deletion_mark": {"type": "boolean"}},
            [],
        ),
        _upsert_task_status,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Creates or updates one MCBP_TaskStatuses catalog item (a support-tracker function "
            "state) natively, WITHOUT MCBP Plus conversion rules (write_object does not work for "
            "this catalog). No `status_id` -> a new status (`description` required; every such "
            "call creates another one); with `status_id` (UUID) -> only the passed fields "
            "change. `deletion_mark: true` sets the deletion mark. set_function_status then "
            "accepts the new item's description as `status`. Predefined statuses (Waiting, "
            "Done) keep their name and cannot be marked for deletion. Two unmarked statuses with "
            "the same description -> CONFLICT. Repeating the same change "
            "changes nothing (`changed: false`). Unknown `status_id` -> NOT_FOUND; a base "
            "without the catalog -> NOT_CONFIGURED; no insert/update right -> FORBIDDEN."
        )),
    "upsert_project": ToolSpec(
        "upsert_project",
        "Створює або змінює елемент довідника проєктів (тип реквізиту Project у MCBP_Functions, "
        "MCBP_Bugs, MCBP_Ideas — з метаданих бази, напр. MCBP_Projects) напряму. "
        "Без `project_id` — новий елемент (`type` і `description` обов'язкові; кожен виклик "
        "створює ще один); з `project_id` (UUID) — зміна лише переданих полів, `type` потрібен "
        "лише коли UUID є в кількох довідниках. `parent_id` — UUID групи того самого довідника, "
        "\"\" — верхній рівень; `is_folder: true` лише при створенні — група. Поле `project` "
        "відповіді (\"ІмяТипу:UUID\") передається як `project` в upsert_function, upsert_idea, "
        "upsert_bug. Та сама зміна удруге нічого не змінює (`changed: false`). Невідомий "
        "`project_id` — NOT_FOUND; невідомий `type` — BAD_PARAMETER з переліком; база без "
        "довідників проєктів — NOT_CONFIGURED; немає права додавання/зміни — FORBIDDEN.",
        _params(
            {"type": {"type": "string",
                      "description": "Довідник проєктів (напр. MCBP_Projects або Catalog.X); "
                                     "обов'язковий при створенні"},
             "project_id": {"type": "string",
                            "description": "UUID елемента; без нього — створення"},
             "description": {"type": "string",
                             "description": "Найменування; обов'язкове при створенні"},
             "parent_id": {"type": "string",
                           "description": "UUID групи того самого довідника; \"\" — верхній рівень"},
             "is_folder": {"type": "boolean",
                           "description": "Лише при створенні: true створює групу"},
             "deletion_mark": {"type": "boolean"}},
            [],
        ),
        _upsert_project,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Creates or updates one item of a project catalog (a catalog type of the `Project` "
            "attribute of MCBP_Functions, MCBP_Bugs and MCBP_Ideas, read from the base metadata, "
            "e.g. MCBP_Projects) natively. No `project_id` -> a new item (`type` "
            "and `description` required; every such call creates another one); with "
            "`project_id` (UUID) -> only the passed fields change, and `type` is needed only when "
            "the UUID exists in several project catalogs. `parent_id` is a folder UUID of the "
            "same catalog, \"\" moves the item to the top level; `is_folder: true` (create only) "
            "creates a folder. The `project` field of the response (\"TypeName:UUID\") is the "
            "value to pass as `project` to upsert_function, upsert_idea and upsert_bug. "
            "Repeating the same change changes nothing (`changed: false`). Unknown `project_id` "
            "-> NOT_FOUND; unknown `type` -> BAD_PARAMETER listing the types; a base without "
            "project catalogs -> NOT_CONFIGURED; no insert/update right -> FORBIDDEN."
        )),
    "upsert_bug": ToolSpec(
        "upsert_bug",
        "Створює або змінює елемент довідника MCBP_Bugs (помилка) напряму. Без `bug_id` — нова "
        "помилка (`description` обов'язковий; кожен виклик створює ще одну; статус — перше "
        "значення перелічення, автор — поточний користувач); з `bug_id` (UUID) — зміна лише "
        "переданих полів. `project` — \"ІмяТипу:UUID\" елемента довідника проєктів; "
        "`function_id` — UUID елемента MCBP_Functions; `details` — опис відтворення, `source` — "
        "звідки помилка, `fixed_version` — версія виправлення; \"\" очищає кожне з них. Статус "
        "змінює лише set_bug_status. Та сама зміна удруге нічого не змінює (`changed: false`). "
        "Невідомий `bug_id`, проєкт або функція — NOT_FOUND; база без довідника або реквізиту — "
        "NOT_CONFIGURED; немає права додавання/зміни — FORBIDDEN.",
        _params(
            {"bug_id": {"type": "string",
                        "description": "UUID елемента MCBP_Bugs; без нього — створення"},
             "description": {"type": "string",
                             "description": "Найменування помилки; обов'язкове при створенні"},
             "project": _PROJECT_REF,
             "function_id": {"type": "string",
                             "description": "UUID елемента MCBP_Functions; \"\" очищає"},
             "details": {"type": "string", "description": "Деталі / кроки; \"\" очищає"},
             "source": {"type": "string", "description": "Джерело; \"\" очищає"},
             "fixed_version": {"type": "string",
                               "description": "Версія виправлення; \"\" очищає"},
             "deletion_mark": {"type": "boolean"}},
            [],
        ),
        _upsert_bug,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Creates or updates one MCBP_Bugs catalog item (a bug) natively. No `bug_id` -> a "
            "new bug (`description` required; every such call creates another one; the status "
            "starts at the first enum value and the author is the current user); with `bug_id` "
            "(UUID) -> only the passed fields change. `project` is \"TypeName:UUID\" of a "
            "project catalog item (TypeName = a project catalog of the base, e.g. "
            "MCBP_Projects); `function_id` is an MCBP_Functions item UUID; `details` holds the "
            "reproduction details, `source` where the bug came from, `fixed_version` the version "
            "that fixes it; \"\" clears each of them. The status changes only through "
            "set_bug_status. Repeating the same change changes nothing (`changed: false`). "
            "Unknown `bug_id`, project or function -> NOT_FOUND; a base without the catalog or "
            "the attribute -> NOT_CONFIGURED; no insert/update right -> FORBIDDEN."
        )),
    "set_bug_status": ToolSpec(
        "set_bug_status",
        "Встановлює статус елемента довідника MCBP_Bugs. `bug_id` — UUID помилки; `status` — "
        "ім'я значення перелічення реквізиту Status (New, Confirmed, Fixed, Verified, Rejected; "
        "без урахування регістру; інше — BAD_PARAMETER з переліком); `fixed_version` — версія "
        "виправлення, \"\" очищає, не передане лишається як було. Той самий виклик удруге "
        "нічого не змінює (`changed: false`). Невідома помилка — NOT_FOUND; база без "
        "перелічення — NOT_CONFIGURED; немає права зміни — FORBIDDEN.",
        _params(
            {"bug_id": {"type": "string", "description": "UUID елемента MCBP_Bugs"},
             "status": {"type": "string",
                        "enum": ["New", "Confirmed", "Fixed", "Verified", "Rejected"]},
             "fixed_version": {"type": "string",
                               "description": "Версія виправлення; \"\" очищає"}},
            ["bug_id", "status"],
        ),
        _set_bug_status,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Sets the status of one MCBP_Bugs catalog item. `bug_id` is the bug UUID; `status` "
            "is a value name of the Status attribute's enumeration (New, Confirmed, Fixed, "
            "Verified, Rejected; case-insensitive; anything else -> BAD_PARAMETER listing the "
            "names); `fixed_version` is the version that fixes it, \"\" clears it, an omitted "
            "one keeps its stored value. Repeating the same call changes nothing "
            "(`changed: false`). Unknown bug -> NOT_FOUND; a base without the enumeration -> "
            "NOT_CONFIGURED; no update right -> FORBIDDEN."
        )),
    "get_event_log": ToolSpec(
        "get_event_log",
        "Читає журнал реєстрації бази (лише читання) з правами ПОТОЧНОГО користувача: немає "
        "права «Журнал реєстрації» — FORBIDDEN (адміністратор має його надати). Для питань "
        "«які були помилки», «хто змінював об'єкт», «чому не записалось». `level` — масив з "
        "error/warning/information/note; без нього — error і warning. `from`/`to` "
        "(YYYY-MM-DD) — без них останні 24 години; не більше 31 дня і `to` не раніше `from`, "
        "інакше BAD_PARAMETER. `event` — точна назва події (напр. _$Data$_.Update) або префікс "
        "із * у кінці (_$Data$_.*). `user` — ім'я користувача. `metadata` — повне ім'я "
        "об'єкта метаданих (Catalog.<Ім'я>, Document.<Ім'я>; невідоме — NOT_FOUND). Найновіші "
        "першими, `limit` до 50; `truncated: true` — звузь період, рівні чи подію.",
        _params(
            {"level": {"type": "array",
                       "items": {"type": "string",
                                 "enum": ["error", "warning", "information", "note"]},
                       "description": "Рівні подій; за замовчуванням [\"error\", \"warning\"]"},
             "from": {"type": "string", "description": "Початок періоду YYYY-MM-DD (необов'язково)"},
             "to": {"type": "string", "description": "Кінець періоду YYYY-MM-DD (необов'язково)"},
             "event": {"type": "string",
                       "description": "Точна назва події або префікс із * у кінці"},
             "user": {"type": "string", "description": "Ім'я користувача бази"},
             "metadata": {"type": "string",
                          "description": "Повне ім'я об'єкта метаданих, напр. Catalog.<Ім'я>"},
             "limit": {"type": "integer", "default": 50, "maximum": 50}},
            [],
        ),
        _get_event_log,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Reads the base's event log (read-only) under the CURRENT user's rights: without "
            "the EventLog right -> FORBIDDEN (an administrator must grant it). For questions "
            "like 'what errors happened', 'who changed this object', 'why did it not save'. "
            "`level` is an array of error/warning/information/note; omitted -> error and "
            "warning. `from`/`to` (YYYY-MM-DD) - omitted -> the last 24 hours; at most 31 days "
            "and `to` not before `from`, otherwise BAD_PARAMETER. `event` is an exact event "
            "name (e.g. _$Data$_.Update) or a prefix ending in * (_$Data$_.*). `user` is a user "
            "name. `metadata` is a full metadata object name (Catalog.<Name>, Document.<Name>; "
            "unknown -> NOT_FOUND). Newest first, `limit` up to 50; `truncated: true` -> narrow "
            "the period, levels or event."
        )),
    "list_function_tasks": ToolSpec(
        "list_function_tasks",
        "Список задач трекера (задачі виконавця), предметом яких є елемент довідника "
        "MCBP_Functions, з правами поточного користувача. `function_id` — UUID елемента "
        "MCBP_Functions (Ref.Data зі search_catalog). Найновіші першими, `limit` до 50. "
        "Невідома функція — NOT_FOUND; база без об'єкта задач — NOT_CONFIGURED; немає права "
        "читання — FORBIDDEN.",
        _params(
            {"function_id": {"type": "string", "description": "UUID елемента MCBP_Functions"},
             "limit": {"type": "integer", "default": 50, "maximum": 50}},
            ["function_id"],
        ),
        _list_function_tasks,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Lists the tracker tasks (performer tasks) whose subject is one MCBP_Functions "
            "catalog item, under the current user's rights. `function_id` is the MCBP_Functions "
            "item UUID (Ref.Data from search_catalog). Newest first, `limit` up to 50. Unknown "
            "function -> NOT_FOUND; a base without a task object -> NOT_CONFIGURED; no read "
            "right -> FORBIDDEN."
        )),
    "upsert_function_task": ToolSpec(
        "upsert_function_task",
        "Створює або змінює задачу трекера (задачу виконавця) функції MCBP_Functions напряму. "
        "`function_id` — UUID функції. Без `task_id` — нова задача (`description` "
        "обов'язковий; автор — поточний користувач, виконавець лишається порожнім, важливість "
        "за замовчуванням — звичайна); з `task_id` (UUID задачі цієї функції) — зміна лише "
        "переданих полів. `specification` — опис задачі, `deadline` — "
        "YYYY-MM-DD або YYYY-MM-DDTHH:MM, `importance` — ім'я значення перелічення важливості; "
        "\"\" очищає кожне з них. `executed: true` позначає задачу виконаною (задача "
        "бізнес-процесу — TASK_IN_BUSINESS_PROCESS). `deletion_mark: true` ставить позначку "
        "вилучення. Та сама зміна удруге нічого не змінює (`changed: false`). Невідома функція "
        "або задача іншої функції — NOT_FOUND; база без об'єкта задач — NOT_CONFIGURED; немає "
        "права — FORBIDDEN; задача заблокована — LOCKED.",
        _params(
            {"function_id": {"type": "string", "description": "UUID елемента MCBP_Functions"},
             "task_id": {"type": "string",
                         "description": "UUID задачі цієї функції; без нього — створення"},
             "description": {"type": "string",
                             "description": "Назва задачі; обов'язкова при створенні"},
             "specification": {"type": "string",
                               "description": "Опис задачі; \"\" очищає"},
             "deadline": {"type": "string",
                          "description": "Термін YYYY-MM-DD або YYYY-MM-DDTHH:MM; \"\" очищає"},
             "importance": {"type": "string",
                            "description": "Ім'я значення перелічення важливості; \"\" очищає"},
             "executed": {"type": "boolean"},
             "deletion_mark": {"type": "boolean"}},
            ["function_id"],
        ),
        _upsert_function_task,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Creates or updates one tracker task (performer task) of an MCBP_Functions item "
            "natively. `function_id` is the function UUID. No `task_id` -> a new task "
            "(`description` required; author = the current user, executor stays empty, "
            "importance defaults to the plain value); with `task_id` (UUID of a task of THIS "
            "function) -> only the passed fields change. "
            "`specification` is the task text, `deadline` is YYYY-MM-DD or YYYY-MM-DDTHH:MM, "
            "`importance` is a value name of the importance enumeration; \"\" clears each of "
            "them. `executed: true` marks the task done (a business-process task -> "
            "TASK_IN_BUSINESS_PROCESS). `deletion_mark: true` sets the deletion mark. Repeating "
            "the same change changes nothing (`changed: false`). Unknown function, or a task of "
            "another function -> NOT_FOUND; a base without a task object -> NOT_CONFIGURED; no "
            "right -> FORBIDDEN; locked -> LOCKED."
        )),
    "set_register_record": ToolSpec(
        "set_register_record",
        "Створює або змінює ОДИН запис регістра відомостей напряму, БЕЗ MCBP Plus (лише upsert, "
        "видалення немає). `fields` — плоский {поле: значення}: ключ запису — УСІ виміри "
        "регістра плюс `Period` для періодичного (неповний ключ — BAD_PARAMETER); решта полів — "
        "ресурси/реквізити, змінюються лише передані. Посилання — UUID (Ref.Data), для "
        "складеного типу — \"ІмяМетаданих:UUID\"; перелік — ім'я значення; null очищає. Імена "
        "полів — з describe_metadata. Регістр, підпорядкований реєстратору, — BAD_PARAMETER. "
        "Відповідь: `created` і `changed` (старе/нове) — підтверди результат саме за ними.",
        _params(
            {"type": {"type": "string",
                      "description": "Ім'я регістру відомостей (з list_metadata)"},
             "fields": {"type": "object",
                        "description": "{поле: значення}: усі виміри (+ Period) і ресурси/"
                                       "реквізити, що змінюються"}},
            ["type", "fields"],
        ),
        _set_register_record,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Creates or updates ONE information-register record natively, WITHOUT MCBP Plus "
            "(upsert only, no delete). `fields` is a flat {field: value}: the record key is ALL "
            "dimensions of the register plus `Period` for a periodic one (incomplete key -> "
            "BAD_PARAMETER); other fields are resources/attributes and only the passed ones "
            "change. A reference is a UUID (Ref.Data), for a composite type \"MetadataName:UUID\"; "
            "an enum is the value name (e.g. \"Active\"); null clears. Field names come from "
            "describe_metadata. A recorder-subordinate register -> BAD_PARAMETER. The response "
            "has `created` and `changed` (old/new) — confirm the result to the user from those."
        )),
    "get_client_orders": ToolSpec(
        "get_client_orders",
        "Замовлення покупця підтвердженого клієнта (лише читання) з правами поточного "
        "користувача. `login` — телефон або e-mail клієнта. Без `order_id` — до 50 найновіших "
        "замовлень без рядків: номер, дата, сума, валюта, стан, проведено, сплачено (`paid`), "
        "до сплати (`to_pay`); null — у документа немає такого реквізиту. `closed`: true — "
        "замовлення завершене/закрите в BAS, false — ні, null — база не дає це визначити. "
        "З `order_id` (UUID "
        "замовлення зі списку) — одне замовлення з товарами `items`. Необов'язковий "
        "`counterparty` (UUID) звужує до одного з дозволених контрагентів клієнта; їхні id — у "
        "полі `counterparties` списку без фільтра; чужий UUID — NOT_FOUND. Логін невідомий, не "
        "підтверджений або без дозволеного контрагента — NOT_CONFIRMED; порожній логін чи "
        "`order_id` не UUID — BAD_PARAMETER; замовлення не цього клієнта — NOT_FOUND; база без "
        "документа замовлення — NOT_CONFIGURED; немає права читання — FORBIDDEN.",
        _params(
            {"login": {"type": "string", "description": "Телефон або e-mail клієнта"},
             "order_id": {"type": "string",
                          "description": "UUID замовлення зі списку; без нього — список"},
             "counterparty": {"type": "string",
                              "description": "Необов'язково: UUID дозволеного контрагента "
                                             "клієнта (з `counterparties` списку без "
                                             "фільтра); чужий — NOT_FOUND"}},
            ["login"],
        ),
        _get_client_orders,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Sales orders of a confirmed client (read-only) under the current user's rights. "
            "`login` is the client's phone or e-mail. Without `order_id` -> up to 50 newest "
            "orders without lines: number, date, amount, currency, state, posted, `paid`, "
            "`to_pay`; null means the document has no such attribute. `closed`: true = the "
            "order is finished/closed in BAS, false = not, null = the base has no way to tell. "
            "With `order_id` (an "
            "order UUID from the list) -> one order with its `items`. Optional `counterparty` "
            "(UUID) narrows to one of the client's allowed counterparties; their ids come from "
            "the `counterparties` envelope of the unfiltered list; a foreign UUID -> NOT_FOUND. "
            "Unknown, unconfirmed "
            "login or no allowed counterparty -> NOT_CONFIRMED; blank login or non-UUID "
            "`order_id` -> BAD_PARAMETER; an order of another client -> NOT_FOUND; a base "
            "without an order document -> NOT_CONFIGURED; no read right -> FORBIDDEN."
        )),
    "get_client_bonus": ToolSpec(
        "get_client_bonus",
        "Бонусні бали підтвердженого клієнта (лише читання, лише сума) з правами поточного "
        "користувача. `login` — телефон або e-mail клієнта. Відповідь: `balance` (залишок), "
        "`accrued` (нараховано), `unit` — завжди \"points\" (бали, не гроші). null у `balance`/"
        "`accrued` — у цій базі немає даних про бонуси (немає регістра, права читання чи "
        "виміру клієнта), це не помилка; клієнт без нарахувань — 0. Необов'язковий "
        "`counterparty` (UUID) звужує до одного з дозволених контрагентів клієнта (їхні id — у "
        "полі `counterparties` get_client_orders); чужий UUID — NOT_FOUND. Логін невідомий, не "
        "підтверджений або без дозволеного контрагента — NOT_CONFIRMED; порожній логін чи "
        "`counterparty` не UUID — BAD_PARAMETER; база без довідників клієнтів — NOT_CONFIGURED.",
        _params(
            {"login": {"type": "string", "description": "Телефон або e-mail клієнта"},
             "counterparty": {"type": "string",
                              "description": "Необов'язково: UUID дозволеного контрагента "
                                             "клієнта; чужий — NOT_FOUND"}},
            ["login"],
        ),
        _get_client_bonus,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Bonus points of a confirmed client (read-only, sum only) under the current user's "
            "rights. `login` is the client's phone or e-mail. Returns `balance`, `accrued` and "
            "`unit`, which is always \"points\" (bonus points, not money). null in `balance`/"
            "`accrued` means no bonus data in this base (no register, no read right or no client "
            "dimension) and is not an error; a client with no records -> 0. Optional "
            "`counterparty` (UUID) narrows to one of the client's allowed counterparties (ids "
            "from the `counterparties` envelope of get_client_orders); a foreign UUID -> "
            "NOT_FOUND. Unknown, unconfirmed login or no allowed counterparty -> NOT_CONFIRMED; "
            "blank login or non-UUID `counterparty` -> BAD_PARAMETER; a base without the client "
            "login catalogs -> NOT_CONFIGURED."
        )),
    "exchange_enqueue": ToolSpec(
        "exchange_enqueue",
        "Крок 1 з 3 тестового обміну (exchange_enqueue -> exchange_run -> exchange_result): "
        "ставить ОДИН наявний об'єкт у чергу обміну з вузлом `base` за його правилом реєстрації "
        "«Manually». Нічого ще не відправляє. Відповідь {\"queued\": true}. BAD_PARAMETER — "
        "немає поля або `id` не UUID; NOT_FOUND — немає вузла чи об'єкта; NOT_CONFIGURED — для "
        "цього типу й вузла немає правила реєстрації Manually; CONDITION_FAILED — об'єкт не "
        "проходить умову виконання правила. " + _EXCHANGE_COMMON_UA,
        _EXCHANGE_OBJECT_PARAMS,
        _exchange_enqueue,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Step 1 of 3 of a test exchange (exchange_enqueue -> exchange_run -> "
            "exchange_result): puts ONE existing object into the exchange queue of node `base` "
            "through the node's \"Manually\" registration rule. Sends nothing yet. Returns "
            "{\"queued\": true}. BAD_PARAMETER -> a missing field or `id` not a UUID; NOT_FOUND "
            "-> no such node or object; NOT_CONFIGURED -> no Manually registration rule for "
            "this type and node; CONDITION_FAILED -> the object fails the rule's execution "
            "condition. " + _EXCHANGE_COMMON_EN
        )),
    "exchange_run": ToolSpec(
        "exchange_run",
        "Крок 2 з 3: запускає обмін з вузлом `base` зараз і відправляє дані в іншу базу. "
        "Відправляє ВСІ необроблені рядки черги цього вузла, а не лише щойно поставлений об'єкт "
        "(залежні об'єкти, поставлені раніше, теж підуть). Відповідь {\"ok\": true} означає лише "
        "те, що обмін виконано; що сталося з об'єктом — дивись exchange_result. "
        + _EXCHANGE_COMMON_UA,
        _params({"base": _EXCHANGE_BASE}, ["base"]),
        _exchange_run,
        read_only=False,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Step 2 of 3: runs the exchange with node `base` now and sends data to the other "
            "base. It flushes the node's WHOLE queue — every unprocessed row, not just the object "
            "you just enqueued (dependent objects queued earlier go too). {\"ok\": true} only "
            "means the exchange ran; check what happened to the object with exchange_result. "
            + _EXCHANGE_COMMON_EN
        )),
    "exchange_result": ToolSpec(
        "exchange_result",
        "Крок 3 з 3 (лише читання, нічого не змінює): що сталося з одним об'єктом в обміні з "
        "вузлом `base`. `queue` — рядки черги цього об'єкта й вузла, найновіші першими: "
        "`processed: true` — передано, `receiver_id` — id об'єкта в базі-одержувачі; "
        "`processed: false` з непорожнім `result` — помилка передачі (тут і помилки "
        "конвертації) — передай її текст користувачу дослівно. `exchange` — рядок "
        "MCBP_DataExchange {key, information, exchangedata}, коли об'єкт зіставлено, інакше "
        "null. NOT_FOUND — для цього об'єкта й вузла нічого не ставили в чергу й не "
        "обмінювали. " + _EXCHANGE_COMMON_UA,
        _EXCHANGE_OBJECT_PARAMS,
        _exchange_result,
        surfaces=frozenset({"mcp"}),
        description_en=(
            "Step 3 of 3 (read-only, changes nothing): what happened to one object in the "
            "exchange with node `base`. `queue` holds the queue rows of that object and node, "
            "newest first: `processed: true` -> delivered, `receiver_id` is the object's id in "
            "the receiving base; `processed: false` with a non-empty `result` -> the transfer "
            "failed and `result` is the error text (conversion errors appear here) — report it "
            "to the user verbatim. `exchange` is the MCBP_DataExchange row {key, information, "
            "exchangedata} when the object is matched, else null. NOT_FOUND -> nothing was "
            "queued or exchanged for that object and node. " + _EXCHANGE_COMMON_EN
        )),
    "list_additional_attributes": ToolSpec(
        "list_additional_attributes",
        "Додаткові реквізити БСП (ПВХ ДополнительныеРеквизитыИСведения і набори "
        "НаборыДополнительныхРеквизитовИСведений) — лише читання. Три режими: (1) список — "
        "`owner` (повне ім'я типу власника, напр. \"Catalog.Контрагенты\" або "
        "\"Справочник.Контрагенты\": реквізити його наборів), `set` (UUID набору), `q` "
        "(підрядок заголовка), `include_deleted`; рядок {id, title, name, types, set}; (2) `id` — "
        "картка одного реквізиту (types, sets, ext_values); (3) `metadata`+`type`+`object_id` — "
        "значення реквізитів одного об'єкта (`data`) і `available` — ті, що можна заповнити "
        "через set_additional_attribute_values. Додаткові ВІДОМОСТІ не підтримуються.",
        _params(
            {"owner": {"type": "string",
                       "description": "Тип власника, напр. Catalog.Контрагенты"},
             "set": {"type": "string", "description": "UUID набору"},
             "q": {"type": "string", "description": "Підрядок заголовка"},
             "include_deleted": {"type": "boolean",
                                 "description": "Показати позначені на вилучення"},
             "id": {"type": "string", "description": "UUID реквізиту — його картка"},
             "metadata": {"type": "string", "description": "Вид власника: Catalogs, Documents"},
             "type": {"type": "string", "description": "Тип власника, напр. Контрагенты"},
             "object_id": {"type": "string",
                           "description": "UUID об'єкта-власника (Ref.Data) — його значення"},
             "limit": {"type": "integer", "description": "1..1000, типово 100"},
             "cursor": {"type": "string", "description": "Курсор наступної сторінки"}},
            [],
        ),
        _list_additional_attributes,
        description_en=(
            "BSP additional attributes (chart ДополнительныеРеквизитыИСведения and the sets "
            "НаборыДополнительныхРеквизитовИСведений) — read-only. Three modes: (1) a list — "
            "`owner` (full name of the owner type, e.g. \"Catalog.Контрагенты\" or "
            "\"Справочник.Контрагенты\": the attributes of its sets), `set` (set UUID), `q` "
            "(title substring), `include_deleted`; a row is {id, title, name, types, set}; (2) "
            "`id` — the card of one attribute (types, sets, ext_values); (3) `metadata`+`type`+"
            "`object_id` — the attribute values of one object (`data`) and `available` — the ones "
            "set_additional_attribute_values can fill. Additional INFO is not supported."
        )),
    "upsert_additional_attribute": ToolSpec(
        "upsert_additional_attribute",
        "Створює (без `id`) або змінює (з `id`) додатковий реквізит БСП. Створення: `title`, "
        "`type` і `set` (UUID набору з list_additional_attributes) обов'язкові; Ім'я база "
        "генерує як БСП. `type` — масив імен типів: \"String\", \"String:100\", "
        "\"Number:15.2\", \"Date\", \"Boolean\", \"CatalogRef.Номенклатура\", "
        "\"ExtValues\" (власний список значень реквізиту; `add_values` додає до нього "
        "значення, наявне з тією ж назвою не дублюється — `values_added`); недозволений "
        "тип — 422 BAD_TYPE з переліком. Зміна: лише передані поля; тип реквізиту, що вже має значення, не "
        "змінюється (422 PROPERTY_IN_USE); `set` переносить у інший набір. Вилучення — лише "
        "`deletion_mark: true` (фізично не видаляється). Дубль заголовка в наборі — "
        "попередження `warnings`, не помилка. Підтверди результат користувачу за відповіддю.",
        _params(
            {"id": {"type": "string",
                    "description": "UUID реквізиту — змінити; без нього — створити"},
             "title": {"type": "string", "description": "Заголовок, напр. Колір"},
             "type": {"type": "array", "items": {"type": "string"},
                      "description": "Типи значення, напр. [\"String\"] або "
                                     "[\"CatalogRef.Номенклатура\"]"},
             "set": {"type": "string", "description": "UUID набору"},
             "comment": {"type": "string", "description": "Коментар"},
             "tooltip": {"type": "string", "description": "Підказка"},
             "required": {"type": "boolean", "description": "Обов'язковий для заповнення"},
             "multiline": {"type": "boolean", "description": "Багаторядкове поле"},
             "deletion_mark": {"type": "boolean",
                               "description": "Позначка вилучення (лише з `id`)"},
             "add_values": {"type": "array", "items": {"type": "string"},
                            "description": "Нові значення власного списку (лише ExtValues), "
                                           "напр. [\"Червоний\"]"}},
            [],
        ),
        _upsert_additional_attribute,
        read_only=False,
        description_en=(
            "Creates (no `id`) or changes (with `id`) a BSP additional attribute. Create: "
            "`title`, `type` and `set` (set UUID from list_additional_attributes) are required; "
            "the base generates the Name the BSP way. `type` is an array of type names: "
            "\"String\", \"String:100\", \"Number:15.2\", \"Date\", \"Boolean\", "
            "\"CatalogRef.Номенклатура\", \"ExtValues\" (the attribute's own value list; "
            "`add_values` adds names to it, an existing value with the same name is reused, "
            "not duplicated - see `values_added`); a "
            "type the chart does not allow is 422 BAD_TYPE with the list. Change: only the passed "
            "fields; the type of an attribute that already holds values cannot change (422 "
            "PROPERTY_IN_USE); `set` moves it to another set. Deletion is `deletion_mark: true` "
            "only (never physical). A duplicate title in the set is a `warnings` entry, not an "
            "error. Confirm the result to the user from the response."
        )),
    "set_additional_attribute_values": ToolSpec(
        "set_additional_attribute_values",
        "Записує значення додаткових реквізитів одного об'єкта. `set` {реквізит: значення} — "
        "реквізит це UUID, заголовок або ім'я з `available` (list_additional_attributes з "
        "object_id); значення як у patch_object: посилання — UUID, дата — YYYY-MM-DD, для "
        "ExtValues — UUID або назва значення; null/\"\" прибирає. `remove` [реквізит, ...] — "
        "прибрати значення. Реквізит не з наборів власника — 422 PROPERTY_NOT_IN_SET з "
        "переліком; кілька збігів за заголовком — 422 AMBIGUOUS (передай UUID). Не повторюй "
        "після незрозумілого результату — перевір list_additional_attributes. Відповідь: "
        "`changed` зі старим і новим значенням.",
        _params(
            {"metadata": {"type": "string", "description": "Вид: Catalogs, Documents, ..."},
             "type": {"type": "string", "description": "Тип об'єкта, напр. Контрагенты"},
             "id": {"type": "string", "description": "UUID об'єкта (Ref.Data)"},
             "set": {"type": "object",
                     "description": "{реквізит: значення}, напр. {\"Колір\": \"Червоний\"}"},
             "remove": {"type": "array", "items": {"type": "string"},
                        "description": "Реквізити, значення яких прибрати"}},
            ["metadata", "type", "id"],
        ),
        _set_additional_attribute_values,
        read_only=False,
        description_en=(
            "Writes the additional-attribute values of one object. `set` {attribute: value} — "
            "the attribute is a UUID, title or name from `available` (list_additional_attributes "
            "with object_id); values as in patch_object: a reference is a UUID, a date "
            "YYYY-MM-DD, for ExtValues a UUID or the value's name; null/\"\" removes. `remove` "
            "[attribute, ...] — clear those values. An attribute outside the owner's sets is 422 "
            "PROPERTY_NOT_IN_SET with the list; several title matches are 422 AMBIGUOUS (pass the "
            "UUID). Do not repeat after an unclear result — check with "
            "list_additional_attributes. The response lists `changed` with old and new values."
        )),
}

# Every data-changing tool (save_context only stores conversation notes) ends with the same
# confirm-before-change rule, appended here so no spec can miss it.
_CONFIRM_CHANGE_EXEMPT = frozenset({"save_context"})
_CONFIRM_CHANGE_UK = (" Змінює дані в базі: спершу скажи користувачу, що саме зміниться (об'єкт, "
                      "поле як «синонім (ім'я)», старе → нове значення), і викликай лише після "
                      "його явного підтвердження цієї зміни.")
_CONFIRM_CHANGE_EN = (" Changes data in the base: first tell the user exactly what will change "
                      "(object, field as «synonym (name)», old → new value) and call this only "
                      "after their explicit confirmation of that change.")
for _name, _spec in list(TOOLS.items()):
    if not _spec.read_only and _name not in _CONFIRM_CHANGE_EXEMPT:
        TOOLS[_name] = replace(
            _spec, description=_spec.description + _CONFIRM_CHANGE_UK,
            description_en=(_spec.description_en or "") + _CONFIRM_CHANGE_EN,
        )
del _name, _spec


def tool_specs(
    allowed: list[str] | None = None, *, surface: str | None = None, include_write: bool = False,
) -> list[ToolSpec]:
    """Registry order is preserved. `surface` filters by `spec.surfaces` membership;
    `include_write=False` (default) drops every `read_only=False` tool."""
    items = TOOLS.values() if allowed is None else (TOOLS[n] for n in allowed if n in TOOLS)
    result = []
    for spec in items:
        if surface is not None and surface not in spec.surfaces:
            continue
        if not include_write and not spec.read_only:
            continue
        result.append(spec)
    return result
