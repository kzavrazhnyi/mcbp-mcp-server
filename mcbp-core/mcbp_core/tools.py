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

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from mcbp_core.client import MCBPClient

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


def _params(properties: dict, required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# --- Executors (kwargs only — the client's parameter order is NOT a contract) ---
async def _search_catalog(c: MCBPClient, a: dict) -> Any:
    return await c.list_catalog(
        type_=a["type"], cursor=a.get("cursor"), limit=a.get("limit", 50),
        q=a.get("q"), fields=a.get("fields"),
    )


async def _get_documents(c: MCBPClient, a: dict) -> Any:
    return await c.list_documents(
        type_=a["type"], frm=a.get("from"), to=a.get("to"),
        cursor=a.get("cursor"), limit=a.get("limit", 100),
        filters=a.get("filters"), fields=a.get("fields"),
        orderby=a.get("orderby"), desc=bool(a.get("desc", False)),
        agg=a.get("agg"), groupby=a.get("groupby"),
    )


async def _get_schema(c: MCBPClient, a: dict) -> Any:
    return await c.document_schema(type_=a["type"])


async def _register_balance(c: MCBPClient, a: dict) -> Any:
    # Accept dimensions either nested under "filters" or as top-level keys (besides "type").
    filters = dict(a.get("filters") or {})
    for k, v in a.items():
        if k not in ("type", "filters"):
            filters[k] = v
    return await c.register_balance(type_=a["type"], filters=filters)


async def _register_records(c: MCBPClient, a: dict) -> Any:
    return await c.register_records(
        type_=a["type"], date_from=a.get("from"), date_to=a.get("to"),
        filters=a.get("filters"), orderby=a.get("orderby"), desc=bool(a.get("desc", False)),
        limit=a.get("limit", 100),
    )


async def _filter_catalog(c: MCBPClient, a: dict) -> Any:
    return await c.filter_catalog(
        type_=a["type"], filters=a.get("filters") or {}, orderby=a.get("orderby"),
        desc=bool(a.get("desc", False)), exclude_groups=bool(a.get("exclude_groups", False)),
        limit=a.get("limit", 100), cursor=a.get("cursor"), fields=a.get("fields"),
        agg=a.get("agg"), groupby=a.get("groupby"),
    )


async def _list_metadata(c: MCBPClient, a: dict) -> Any:
    return await c.list_metadata(kind=a["metadata"])


async def _describe_metadata(c: MCBPClient, a: dict) -> Any:
    return await c.describe_metadata(
        kind=a["metadata"], type_=a["type"], tabular_section=a.get("tabular_section"),
        q=a.get("q"),
    )


async def _get_object(c: MCBPClient, a: dict) -> Any:
    return await c.get_object(kind=a["metadata"], type_=a["type"], object_id=a["id"])


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


# --- Registry ---
# ORDER MATTERS: MockProvider calls TOOLS[0], so search_catalog must stay first.
# Introspection tools were appended at the end; the "structure first" priority is set in
# SYSTEM_PROMPT instead. health/write_object/save_context were appended at the END too (stage 5)
# — so as not to shift TOOLS[0].
TOOLS: dict[str, ToolSpec] = {
    "search_catalog": ToolSpec(
        "search_catalog",
        "Шукає елементи довідника BAS (контрагенти, товари, користувачі тощо). "
        "Кожен рядок містить ЛИШЕ стандартні поля (Ref, Code, Description, IsFolder, Parent, "
        "DeletionMark) — БЕЗ значень інших реквізитів. `fields` лише ДОДАЄ вказані реквізити до "
        "цього набору (не звужує вибірку); невідоме ім'я поля потрапляє в `unknown_fields` "
        "відповіді, а не в помилку. Порожнє поле у списку НЕ означає, що реквізит порожній у "
        "базі — щоб побачити реальні значення реквізитів одного запису, виклич get_object з "
        "його Ref.",
        _params(
            {"type": {"type": "string", "description": "Тип довідника, напр. Контрагенты, Номенклатура"},
             "q": {"type": "string", "description": "Текст пошуку за назвою/кодом"},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Додаткові реквізити у рядках, напр. [\"Покупатель\",\"КодПоЕДРПОУ\"]"},
             "limit": {"type": "integer", "default": 50},
             "cursor": {"type": "string"}},
            ["type"],
        ),
        _search_catalog,
        description_en=(
            "Searches items of a BAS catalog (counterparties, items, users, etc.). Each row "
            "carries ONLY the standard fields (Ref, Code, Description, IsFolder, Parent, "
            "DeletionMark) — WITHOUT the values of other attributes. `fields` only ADDS the "
            "named attributes to that set (it never narrows the selection); an unknown field "
            "name lands in the response's `unknown_fields`, not in an error. An empty field in "
            "a list row does NOT mean the attribute is empty in the base — to see the real "
            "attribute values of one record, call get_object with its Ref."
        )),
    "get_documents": ToolSpec(
        "get_documents",
        "Повертає документи BAS заданого типу за період. Дати у форматі YYYY-MM-DD. "
        "`from` ОБОВ'ЯЗКОВИЙ — без нього сервіс відповідає 422. Якщо користувач не назвав "
        "період, бери ЗАВІДОМО ШИРОКИЙ (from=\"2000-01-01\"), а не останній рік: вузьке вікно "
        "мовчки відрізає документи, і «скільки всього» виходить неправильним. "
        "ВАЖЛИВО: за замовчуванням рядки містять лише стандартні поля (Number/Date/Posted/...) — "
        "БЕЗ контрагента й сум і БЕЗ значень інших реквізитів. Щоб відібрати документи "
        "КОНКРЕТНОГО контрагента (чи за іншим посилальним полем), спершу знайди його ref через "
        "search_catalog/filter_catalog, потім передай `filters`, напр. {\"Контрагент\": "
        "\"<uuid>\"} (значення — UUID з Ref.Data; для складеного типу — \"ІмяТипу:UUID\"). Імена "
        "полів бери з describe_metadata, не вгадуй. `fields` лише ДОДАЄ вказані реквізити шапки "
        "до стандартного набору (напр. [\"Контрагент\", \"СуммаДокумента\"]) — не звужує "
        "вибірку; невідоме ім'я поля потрапляє в `unknown_fields` відповіді, а не в помилку. "
        "Порожнє поле у списку НЕ означає, що реквізит порожній у базі. Для повних даних і "
        "табличних частин (номенклатура) — get_object за Ref.Data рядка. БЕЗ `orderby` порядок "
        "рядків ДОВІЛЬНИЙ (за внутрішнім ідентифікатором), а НЕ хронологічний — визначати "
        "«останній»/«перший» документ без `orderby` НЕ МОЖНА. Щоб отримати останній документ: "
        "orderby=\"Дата\", desc=true, limit=1 — \"Дата\" є типовим внутрішнім ім'ям поля дати в "
        "успадкованих російськомовних конфігураціях, АЛЕ не гарантією (в іншій конфігурації, "
        "напр. англомовній, поле може зватись інакше). Якщо здогад хибний, сервіс відповідає "
        "422 BAD_PARAMETER з РЕАЛЬНИМ ім'ям поля в тексті помилки (напр. \"did you mean "
        "'Дата'?\") — візьми ім'я звідти й повтори виклик, НЕ йди спершу за повним деревом "
        "метаданих. Якщо помилка не підказала поле дати (напр. вибірка порожня чи інша "
        "структура) — використай describe_metadata з q=\"дата\" (чи відповідним синонімом), щоб "
        "знайти точне ім'я, замість перебору навмання. При заданому `orderby` курсорна пагінація "
        "(`cursor`) не діє. Якщо питання — «скільки»/«на яку суму»/«середнє»/«максимум»/"
        "«мінімум» — використовуй `agg` ЗАМІСТЬ вивантаження рядків і ручного підрахунку: "
        "напр. agg=[\"count\"] або agg=[\"sum:СуммаДокумента\"]. Відповідь тоді має форму "
        "{\"aggregates\":[...]} замість {\"data\":[...]}, і `cursor` з `agg` разом не працюють.",
        _params(
            {"type": {"type": "string", "description": "Тип документа, напр. ЗаказПокупателя"},
             "from": {"type": "string", "description": "Початок періоду YYYY-MM-DD"},
             "to": {"type": "string",
                    "description": "Кінець періоду YYYY-MM-DD; без нього — до кінця часу"},
             "filters": {"type": "object",
                         "description": "{ім'я_поля: значення}; для контрагента — {\"Контрагент\": \"<uuid>\"}"},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Додаткові реквізити шапки у рядках, напр. [\"Контрагент\",\"СуммаДокумента\"]"},
             "orderby": {"type": "string", "description": "Поле сортування, напр. Дата"},
             "desc": {"type": "boolean", "default": False},
             "limit": {"type": "integer", "default": 100},
             "cursor": {"type": "string"},
             "agg": {"type": "array", "items": {"type": "string"},
                     "description": "Агрегатні функції замість рядків, напр. [\"count\"], "
                                    "[\"sum:СуммаДокумента\"], [\"sum:Сумма\",\"avg:Сумма\"]. "
                                    "Функції: count, sum, avg, min, max (крім count — поле "
                                    "ОБОВ'ЯЗКОВЕ і числове). Не рахуй суму/кількість руками по "
                                    "списку рядків — використовуй agg. Несумісне з `cursor`."},
             "groupby": {"type": "string",
                         "description": "Поле групування для agg, напр. Контрагент — по одній "
                                        "групі на значення поля (напр. «сума по кожному "
                                        "контрагенту»/«топ-N за сумою» з orderby=псевдонім "
                                        "агрегата). Без agg ігнорується."}},
            ["type", "from"],
        ),
        _get_documents,
        description_en=(
            "Returns BAS documents of the given type over a period. Dates in YYYY-MM-DD format. "
            "`from` is REQUIRED — without it the service answers 422. If the user did not name "
            "a period, use a DELIBERATELY WIDE one (from=\"2000-01-01\"), not the last year: a "
            "narrow window silently cuts off documents, and a 'how many total' answer comes out "
            "wrong. IMPORTANT: by default rows carry only the standard fields "
            "(Number/Date/Posted/...) — WITHOUT the counterparty, sums, or the values of other "
            "attributes. To filter documents for a SPECIFIC counterparty (or another reference "
            "field), first find its ref via search_catalog/filter_catalog, then pass `filters`, "
            "e.g. {\"Контрагент\": \"<uuid>\"} (the value is the UUID from Ref.Data; for a "
            "composite type — \"TypeName:UUID\"). Take field names from describe_metadata, "
            "don't guess. `fields` only ADDS the named header attributes to the standard set "
            "(e.g. [\"Контрагент\", \"СуммаДокумента\"]) — it never narrows the selection; an "
            "unknown field name lands in the response's `unknown_fields`, not in an error. An "
            "empty field in a list row does NOT mean the attribute is empty in the base. For "
            "full data and tabular sections (line items) — get_object using the row's Ref.Data. "
            "WITHOUT `orderby` row order is ARBITRARY (by internal identifier), NOT "
            "chronological — you CANNOT determine the 'latest'/'first' document without "
            "`orderby`. To get the latest document: orderby=\"Дата\", desc=true, limit=1 — "
            "\"Дата\" is the TYPICAL internal date field name in legacy Russian-named "
            "configurations, BUT not a guarantee (in another configuration, e.g. an "
            "English-named one, the field may be called something else). If the guess is wrong, "
            "the service answers 422 BAD_PARAMETER with the REAL field name in the error text "
            "(e.g. \"did you mean 'Дата'?\") — take the name from there and retry, do NOT go "
            "through the full metadata tree first. If the error didn't hint the date field "
            "(e.g. the selection is empty or the structure differs) — use describe_metadata "
            "with q=\"дата\" (or the matching synonym) to find the exact name, instead of "
            "guessing blindly. With `orderby` set, cursor pagination (`cursor`) does not apply. "
            "If the question is 'how many'/'for what total sum'/'average'/'max'/'min' — use "
            "`agg` INSTEAD OF pulling rows and counting by hand: e.g. agg=[\"count\"] or "
            "agg=[\"sum:СуммаДокумента\"]. The response then takes the shape "
            "{\"aggregates\":[...]} instead of {\"data\":[...]}, and `cursor` is incompatible "
            "with `agg`."
        )),
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
        "Повертає залишок по регістру накопичення (борг/взаєморозрахунки/залишки складу тощо) "
        "з фільтрами за ВИМІРАМИ регістру. Точні імена вимірів бери з describe_metadata "
        "(вид AccumulationRegisters) — не вгадуй (напр. вимір може зватися Контрагент, а не "
        "counterparty). Значення-посилання (контрагент, організація, номенклатура) передавай "
        "як UUID з Ref.Data. Приклад: {\"type\":\"...\",\"filters\":{\"Контрагент\":\"<uuid>\"}}.",
        _params(
            {"type": {"type": "string", "description": "Ім'я регістру накопичення (з list_metadata)"},
             "filters": {"type": "object",
                         "description": "{ім'я_виміру: значення}; посилання — UUID, напр. {\"Контрагент\": \"<uuid>\"}"}},
            ["type"],
        ),
        _register_balance,
        description_en=(
            "Returns the balance of an accumulation register (debt/mutual settlements/warehouse "
            "stock, etc.) filtered by the register's DIMENSIONS. Take exact dimension names from "
            "describe_metadata (kind AccumulationRegisters) — don't guess (e.g. a dimension may "
            "be called Контрагент, not counterparty). Reference values (counterparty, "
            "organization, item) are passed as UUIDs from Ref.Data. Example: "
            "{\"type\":\"...\",\"filters\":{\"Контрагент\":\"<uuid>\"}}."
        )),
    "list_metadata": ToolSpec(
        "list_metadata",
        "Структура конфігурації BAS. metadata='all' → перелік об'єктів усіх видів одразу; "
        "metadata=Catalogs|Documents|InformationRegisters|AccumulationRegisters|Enums|Tasks|... → "
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
            "Enums|Tasks|... → list of objects of that kind (name + synonym, WITHOUT attributes "
            "and WITHOUT values). Works for any configuration. Call this to learn the real type "
            "names instead of guessing them."
        )),
    "describe_metadata": ToolSpec(
        "describe_metadata",
        "Детальний опис одного об'єкта метаданих у вигляді дерева: реквізити з типами "
        "(для регістрів — виміри/ресурси/реквізити; для перелічень — значення). Це "
        "СТРУКТУРА, БЕЗ жодних значень конкретного запису — наявність поля тут не означає, "
        "що воно заповнене чи порожнє в базі. Використовуй, щоб дізнатися ТОЧНІ імена полів "
        "(напр. чи є прапорець Pokupatel/Postavshchik) ПЕРЕД фільтрацією чи читанням даних. Не "
        "вигадуй імена полів — спершу подивись опис. "
        "ТАБЛИЧНІ ЧАСТИНИ за замовчуванням повертаються ЛИШЕ переліком (ім'я, синонім, "
        "кількість реквізитів) — БЕЗ вкладених реквізитів; це НЕ означає, що в них немає "
        "реквізитів. Щоб отримати повний перелік реквізитів КОНКРЕТНОЇ табличної частини, "
        "виклич ще раз із `tabular_section=\"<ім'я з переліку>\"`. "
        "Без `q` відповідь містить ВСІ реквізити шапки (для документа їх може бути 100+, це "
        "великий обсяг) — якщо потрібне лише одне-два поля (напр. статус, дата, контрагент), "
        "передай `q` з підрядком: він шукає і за внутрішнім іменем, і за українським синонімом "
        "(можна писати «статус», навіть не знаючи, що поле зветься СостояниеЗаказа). Якщо `q` "
        "нічого не знайшов — повертається не порожнеча і не повне дерево, а компактний список "
        "усіх наявних імен (`available_names`), щоб уточнити запит.",
        _params(
            {"metadata": {"type": "string",
                          "description": "Вид: Catalogs, Documents, InformationRegisters, ..."},
             "type": {"type": "string",
                      "description": "Ім'я об'єкта, напр. Контрагенты, ЗаказПокупателя, MCBP_StatusFunctions"},
             "tabular_section": {"type": "string",
                                  "description": "Ім'я однієї табличної частини (з попереднього "
                                                 "виклику без цього параметра) — повертає ЛИШЕ "
                                                 "її повний перелік реквізитів"},
             "q": {"type": "string",
                   "description": "Підрядок для звуження реквізитів ШАПКИ (не табличних частин) "
                                  "— шукає за внутрішнім іменем І за українським синонімом, без "
                                  "урахування регістру. Без цього параметра повертається ВЕСЬ "
                                  "(часто великий) перелік реквізитів."}},
            ["metadata", "type"],
        ),
        _describe_metadata,
        description_en=(
            "Detailed description of a single metadata object as a tree: attributes with types "
            "(for registers — dimensions/resources/attributes; for enums — values). This is "
            "STRUCTURE, WITHOUT any specific record's values — a field being present here does "
            "not mean it is filled or empty in the base. Use it to learn the EXACT field names "
            "(e.g. whether there is a flag Pokupatel/Postavshchik) BEFORE filtering or reading "
            "data. Don't guess field names — look at the description first. "
            "TABULAR SECTIONS are returned by default as a LIST ONLY (name, synonym, attribute "
            "count) — WITHOUT the nested attributes; this does NOT mean they have no "
            "attributes. To get the full attribute list of a SPECIFIC tabular section, call "
            "again with `tabular_section=\"<name from the list>\"`. "
            "Without `q` the response contains ALL header attributes (a document can have 100+, "
            "a large payload) — if only one or two fields are needed (e.g. status, date, "
            "counterparty), pass `q` with a substring: it searches both the internal name and "
            "the Ukrainian synonym (you can write 'статус' without knowing the field is called "
            "СостояниеЗаказа). If `q` finds nothing — the response is neither empty nor the full "
            "tree, but a compact list of all available names (`available_names`) to refine the "
            "query."
        )),
    "filter_catalog": ToolSpec(
        "filter_catalog",
        "Серверна вибірка елементів довідника з УНІВЕРСАЛЬНИМ фільтром за будь-якими полями "
        "та сортуванням (фільтрація і типізація — на боці BAS, надійно). Кожен рядок містить "
        "ЛИШЕ стандартні поля + те, що явно додано через `fields` — БЕЗ значень інших "
        "реквізитів; порожнє поле у списку НЕ означає, що реквізит порожній у базі. "
        "`filters` — словник {ім'я_поля: значення}, поля бери з `describe_metadata` (реальні "
        "імена, напр. Покупатель, Постачальник, Наименование). Рівність; для рядка зі знаком "
        "% — пошук LIKE. `orderby` — поле сортування (напр. Наименование), `desc` — за спаданням. "
        "`exclude_groups=true` виключає групи-папки. `fields` лише ДОДАЄ реквізити до "
        "стандартного набору (не звужує вибірку); невідоме ім'я поля йде в `unknown_fields` "
        "відповіді, а не в помилку. Щоб побачити реальні значення реквізитів одного запису — "
        "get_object за його Ref. Працює для будь-якої конфігурації. Використовуй це (а не "
        "search_catalog), коли треба відфільтрувати/відсортувати за полем. Якщо питання — "
        "«скільки»/«на яку суму»/«середнє»/«максимум»/«мінімум» — використовуй `agg` ЗАМІСТЬ "
        "вивантаження рядків і ручного підрахунку: напр. agg=[\"count\"] або "
        "agg=[\"sum:Сумма\"]. Відповідь тоді має форму {\"aggregates\":[...]} замість "
        "{\"data\":[...]}.",
        _params(
            {"type": {"type": "string", "description": "Тип довідника, напр. Контрагенты"},
             "filters": {"type": "object",
                         "description": "{ім'я_поля: значення}, напр. {\"Покупатель\": true}"},
             "orderby": {"type": "string", "description": "Поле сортування, напр. Наименование"},
             "desc": {"type": "boolean", "default": False},
             "exclude_groups": {"type": "boolean", "default": False},
             "fields": {"type": "array", "items": {"type": "string"},
                        "description": "Додаткові реквізити у рядках, напр. [\"Покупатель\",\"КодПоЕДРПОУ\"]"},
             "limit": {"type": "integer", "default": 100},
             "agg": {"type": "array", "items": {"type": "string"},
                     "description": "Агрегатні функції замість рядків, напр. [\"count\"], "
                                    "[\"sum:Поле\"], [\"sum:Поле\",\"avg:Поле\"]. Функції: count, "
                                    "sum, avg, min, max (крім count — поле ОБОВ'ЯЗКОВЕ і числове). "
                                    "Не рахуй суму/кількість руками по списку рядків — "
                                    "використовуй agg. Несумісне з `cursor`."},
             "groupby": {"type": "string",
                         "description": "Поле групування для agg — по одній групі на значення "
                                        "поля (напр. «скільки контрагентів у кожній групі», "
                                        "«топ-N за сумою» з orderby=псевдонім агрегата). Без agg "
                                        "ігнорується."}},
            ["type"],
        ),
        _filter_catalog,
        description_en=(
            "Server-side selection of catalog items with a UNIVERSAL filter over any fields and "
            "sorting (filtering and typing happen on the BAS side — reliable). Each row carries "
            "ONLY the standard fields plus whatever `fields` explicitly added — WITHOUT the "
            "values of other attributes; an empty field in a list row does NOT mean the "
            "attribute is empty in the base. `filters` — a dict {field_name: value}, take field "
            "names from `describe_metadata` (real names, e.g. Покупатель, Постачальник, "
            "Наименование). Equality; for a string with a % sign — LIKE search. `orderby` — the "
            "sort field (e.g. Наименование), `desc` — descending. `exclude_groups=true` excludes "
            "folder groups. `fields` only ADDS attributes to the standard set (never narrows the "
            "selection); an unknown field name goes into the response's `unknown_fields`, not "
            "into an error. To see the real attribute values of one record — get_object using "
            "its Ref. Works for any configuration. Use this (rather than search_catalog) when "
            "you need to filter/sort by a field. If the question is 'how many'/'for what total "
            "sum'/'average'/'max'/'min' — use `agg` INSTEAD OF pulling rows and counting by "
            "hand: e.g. agg=[\"count\"] or agg=[\"sum:Сумма\"]. The response then takes the "
            "shape {\"aggregates\":[...]} instead of {\"data\":[...]}."
        )),
    "get_object": ToolSpec(
        "get_object",
        "ЄДИНИЙ спосіб отримати РЕАЛЬНІ ЗНАЧЕННЯ реквізитів одного конкретного об'єкта (усі "
        "реквізити + табличні частини). Списки (search_catalog/filter_catalog/get_documents) і "
        "структурні інструменти (describe_metadata/get_schema/list_metadata) НЕ містять значень "
        "реквізитів — не роби висновок «поле порожнє» доти, доки не перевірив саме тут. "
        "Виклич після того, як список знайшов потрібний запис — передай його id (UUID з поля "
        "Ref.Data у рядку списку).",
        _params(
            {"metadata": {"type": "string", "description": "Вид: Catalogs, Documents, Tasks, ..."},
             "type": {"type": "string", "description": "Тип об'єкта, напр. Контрагенты, ЗаказПокупателя"},
             "id": {"type": "string", "description": "UUID об'єкта (Ref.Data зі списку)"}},
            ["metadata", "type", "id"],
        ),
        _get_object,
        description_en=(
            "The ONLY way to get the REAL VALUES of a single specific object's attributes (all "
            "attributes + tabular sections). Lists (search_catalog/filter_catalog/get_documents) "
            "and structure tools (describe_metadata/get_schema/list_metadata) do NOT contain "
            "attribute values — never conclude 'the field is empty' until you've checked here. "
            "Call it after a list has found the record you need — pass its id (the UUID from "
            "the list row's Ref.Data field)."
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
        "(`InformationBase` — застарілий синонім `base`, не використовуй).",
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
            "deprecated synonym of `base` — don't use it)."
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
        "Змінює ЛИШЕ названі реквізити шапки ІСНУЮЧОГО об'єкта (напр. статус замовлення) — БЕЗ "
        "MCBP Plus і БЕЗ правила конвертації (на відміну від write_object, який завжди йде через "
        "Plus і без налаштованого правила для типу дає 501 PLUS_REQUIRED/422 "
        "CONVERSION_NOT_CONFIGURED). Усе, що НЕ назване в `fields`, лишається без змін — головна "
        "відмінність від write_object. Спершу знайди об'єкт (search_catalog/get_documents) і "
        "візьми його `id` з Ref.Data. Значення посилального поля — UUID цільового об'єкта; для "
        "СКЛАДЕНОГО посилального поля (кілька можливих типів, напр. СостояниеЗаказа — довідники "
        "СостоянияЗаказовПокупателей/СостоянияЗаказНарядов) голий UUID — ПОМИЛКА: завжди передавай "
        "\"ІмяТипу:UUID\" (типи з describe_metadata). `Ref`/`Ссылка` і `Проведен`/`Posted` "
        "змінити не можна — стан проведення документа зберігається автоматично (був проведений — "
        "лишиться проведеним; не був — записується непроведеним), зміна статусу НЕ впливає на "
        "облік. Табличні частини не підтримуються в цій версії (BAD_PARAMETER, якщо назвати "
        "поле-табчастину). Відповідь показує старе й нове значення КОЖНОГО зміненого поля і "
        "поточний стан проведення — підтверди результат користувачу саме за цими даними, не "
        "вигадуй.",
        _params(
            {"metadata": {"type": "string", "description": "Вид: Catalogs, Documents, Tasks, ..."},
             "type": {"type": "string", "description": "Тип об'єкта, напр. ЗаказПокупателя"},
             "id": {"type": "string", "description": "UUID об'єкта (Ref.Data зі списку/get_object)"},
             "fields": {"type": "object",
                        "description": "{ім'я_поля: нове_значення}, напр. {\"СостояниеЗаказа\": "
                                       "\"СостоянияЗаказовПокупателей:<uuid>\"}. Лише названі поля "
                                       "змінюються."}},
            ["metadata", "type", "id", "fields"],
        ),
        _patch_object,
        read_only=False,
        description_en=(
            "Changes ONLY the named header attributes of an EXISTING object (e.g. an order's "
            "status) — WITHOUT MCBP Plus and WITHOUT a conversion rule (unlike write_object, "
            "which always goes through Plus and gives 501 PLUS_REQUIRED/422 "
            "CONVERSION_NOT_CONFIGURED without a configured rule for the type). Everything NOT "
            "named in `fields` stays unchanged — the main difference from write_object. First "
            "find the object (search_catalog/get_documents) and take its `id` from Ref.Data. A "
            "reference field's value is the UUID of the target object; for a COMPOSITE "
            "reference field (several possible types, e.g. СостояниеЗаказа — catalogs "
            "СостоянияЗаказовПокупателей/СостоянияЗаказНарядов) a bare UUID is an ERROR — always "
            "pass \"TypeName:UUID\" (types from describe_metadata). `Ref`/`Ссылка` and "
            "`Проведен`/`Posted` cannot be changed — a document's posting state is preserved "
            "automatically (posted stays posted, unposted is written unposted; changing the "
            "status does NOT affect the ledger). Tabular sections are not supported in this "
            "version (BAD_PARAMETER if you name a tabular-section field). The response shows "
            "the old and new value of EACH changed field and the current posting state — "
            "confirm the result to the user based on that data, don't make it up."
        )),
    "get_register_records": ToolSpec(
        "get_register_records",
        "ЄДИНИЙ спосіб прочитати РЯДКИ регістру відомостей (InformationRegisters) — на відміну "
        "від get_register_balance (даного регістру НАКОПИЧЕННЯ і завжди ОБЧИСЛЕНОГО залишку), "
        "регістр відомостей не має Ref, тож search_catalog/filter_catalog/get_documents/"
        "get_object і курсорна пагінація до нього НЕ дістають — тільки цей інструмент. Працює "
        "і для регістру накопичення — тоді повертає СИРІ рухи (кожен запис руху), а НЕ "
        "обчислений залишок (для залишку — get_register_balance). "
        "`from`/`to` (YYYY-MM-DD) — ОБИДВА необов'язкові: відсутній `from` — відкрита нижня "
        "межа, відсутній `to` — відкрита верхня; якщо регістр НЕперіодичний, а `from`/`to` "
        "все ж передані — 422 BAD_PARAMETER. `filters` — {ім'я_поля: значення} за будь-яким "
        "виміром/ресурсом/реквізитом (і стандартними: приймається як внутрішня назва "
        "конфігурації, так і англійський аліас — Period/Recorder/Active/LineNumber/RecordType; "
        "українських назв НЕ існує, синоніми лише для показу); значення-"
        "посилання — UUID з Ref.Data; рядок зі знаком % — LIKE. `orderby`/`desc` — сортування; "
        "невідоме поле у filters чи orderby — 422 BAD_PARAMETER з переліком реальних полів. "
        "БЕЗ `orderby` періодичний регістр сервер сортує сам за Period СПАДНО (найновіші "
        "першими) — щоб отримати ОСТАННІЙ запис, досить limit=1 (або явно orderby=\"Period\", "
        "desc=true, limit=1). Курсорної пагінації НЕМАЄ (немає Ref, немає стабільного ключа) "
        "— `truncated: true` у відповіді означає «звузь фільтр», а НЕ «візьми наступну "
        "сторінку». Спершу list_metadata/describe_metadata, щоб дізнатись точні імена полів.",
        _params(
            {"type": {"type": "string",
                      "description": "Ім'я регістру відомостей або накопичення (з list_metadata)"},
             "from": {"type": "string", "description": "Початок періоду YYYY-MM-DD (необов'язково)"},
             "to": {"type": "string", "description": "Кінець періоду YYYY-MM-DD (необов'язково)"},
             "filters": {"type": "object",
                         "description": "{ім'я_поля: значення}; посилання — UUID, напр. {\"Контрагент\": \"<uuid>\"}"},
             "orderby": {"type": "string", "description": "Поле сортування, напр. Period"},
             "desc": {"type": "boolean", "default": False},
             "limit": {"type": "integer", "default": 100}},
            ["type"],
        ),
        _register_records,
        description_en=(
            "The ONLY way to read the ROWS of an information register (InformationRegisters) — "
            "unlike get_register_balance (which is for the ACCUMULATION register and always the "
            "COMPUTED balance), an information register has no Ref, so "
            "search_catalog/filter_catalog/get_documents/get_object and cursor pagination "
            "cannot reach it — only this tool can. Also works for accumulation registers — then "
            "it returns RAW movements (each movement record), NOT the computed balance (for the "
            "balance — get_register_balance). "
            "`from`/`to` (YYYY-MM-DD) are BOTH optional: no `from` means an open lower bound, no "
            "`to` means an open upper bound; if the register is NOT periodic and `from`/`to` are "
            "passed anyway — 422 BAD_PARAMETER. `filters` — {field_name: value} over any "
            "dimension/resource/attribute (including standard ones: accepts either the internal "
            "configuration name or the English alias — Period/Recorder/Active/LineNumber/"
            "RecordType; there are NO Ukrainian names for these, synonyms are display-only); a "
            "reference value is a UUID from Ref.Data; a string with a % sign is LIKE. "
            "`orderby`/`desc` — sorting; an unknown field in filters or orderby — 422 "
            "BAD_PARAMETER with a list of the real field names. WITHOUT `orderby` a periodic "
            "register is sorted by the server itself by Period DESCENDING (newest first) — to "
            "get the LATEST record, limit=1 is enough (or explicitly orderby=\"Period\", "
            "desc=true, limit=1). There is NO cursor pagination (no Ref, no stable key) — "
            "`truncated: true` in the response means 'narrow the filter', NOT 'fetch the next "
            "page'. Call list_metadata/describe_metadata first to learn the exact field names."
        )),
}


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
