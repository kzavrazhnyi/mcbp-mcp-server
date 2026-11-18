"""Response masking (pseudonymization) for BAS MCBP_AI data seen by an LLM.

The LLM must never see sensitive BAS data (counterparty/person/user names, tax codes,
phones, addresses, free-text comments) while numbers/dates/enums/documents stay usable
for analysis. Pseudonyms are deterministic (same original -> same pseudonym within one
`Masker` instance) so the model can join rows across tool calls, and reversible locally
so (a) the model can pass a pseudonym back as a filter/id argument and (b) the host
application can show the human user real values.

Design inspired by github.com/alonehobo/1c-trusted-gateway (privacy.go / type_policy.go)
— ideas only, no code copied; that repo has no license file.

Two independent classification axes:
  * REFERENCE openness — whether a `{"Presentation","Data","Metadata"}` value's
    `Presentation` is masked, decided by the REFERENCED object's own dotted type
    (`Справочник.X` / `Документ.X` / ...) via `MaskingPolicy` (prefixes + exact list).
  * ROW-OWN-STRING openness — whether a row's OWN plain string fields (e.g. a
    catalog's `Description`) are masked by default. This is narrower: only rows whose
    own type is one of the explicitly listed OPEN CATALOGS get this; documents/enums/
    charts are open only as REFERENCES (their Number/Date fields are separately open
    by exact field name), never as a blanket "every string on this row is fine".

No I/O happens inside this module — metadata type lookups are injected as plain
callables so masking stays pure, synchronous and unit-testable. `build_type_classifier`
below is a separate, optional async helper that builds such a callable from a live
`MCBPClient` (kept out of `Masker` on purpose, per the "no I/O inside masking" rule).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from mcbp_core.client import MCBPClient

log = logging.getLogger("mcbp.masking")

# A bare metadata name (e.g. "Контрагенты", "ЗаказПокупателя", as it appears in a
# `Ref.Metadata` field) -> its fully-qualified dotted type ("Справочник.Контрагенты",
# "Документ.ЗаказПокупателя"), or None when the name is unknown to the classifier.
TypeClassifier = Callable[[str], "str | None"]

# A field name -> the BSL `types` list from `describe_metadata`/schema (e.g.
# ["Рядок"], ["Число"], ["Справочник.Номенклатура", "Рядок"]), or None when unknown.
FieldTypeLookup = Callable[[str], "list[str] | None"]


# --- Default policy data -----------------------------------------------------------

_DEFAULT_OPEN_PREFIXES: tuple[str, ...] = (
    "Документ.",
    "Перечисление.",
    "ПланСчетов.",
    "ПланВидовХарактеристик.",
)

# Exact dotted catalog names open BOTH as references AND for their own row fields
# (classifier catalogs — "safe to browse" reference data, not business entities).
#
# These are PLATFORM/BSP-standard classifier names (present across most BAS/1C
# configurations built on the Библиотека стандартных подсистем, not specific to any one
# customer's configuration). Per onec-universality: a name's ABSENCE in a given
# configuration is harmless — the classify lookup simply never matches it, nothing
# breaks. A configuration-specific classifier (safe reference data unique to one
# customer's config) is opened via the JSON policy override (`open_type_exact` /
# `MCBP_ONEC_MASK_POLICY`), never added here.
_DEFAULT_OPEN_CATALOGS: tuple[str, ...] = (
    "Справочник.Номенклатура",
    "Справочник.ЕдиницыИзмерения",
    "Справочник.КлассификаторЕдиницИзмерения",
    "Справочник.Валюты",
    "Справочник.СтавкиНДС",
    "Справочник.СтраныМира",
    "Справочник.ВидыЦен",
    "Справочник.Склады",
    "Справочник.СтруктурныеЕдиницы",
    "Справочник.КатегорииНоменклатуры",
    "Справочник.ВидыКонтактнойИнформации",
)

# Exact field names always open regardless of context (unless forced_mask matches).
_DEFAULT_OPEN_FIELD_NAMES: tuple[str, ...] = (
    "Number", "Номер", "Date", "Дата", "id", "Data",
    # The platform's standard Period attribute of registers (EN / inherited RU / UA).
    "Period", "Период", "Період",
)

# Keys of a field name that force masking no matter what — identifying data and free text,
# per the goal. A key matches at a WORD of the field name (see `_has_forced_word`: "ипн" in
# "КодИПН", not in "ТипНоменклатуры"); an additional property's name (free text) still
# matches by plain substring.
_DEFAULT_FORCED_SUBSTRINGS: tuple[str, ...] = (
    "инн", "інн", "ипн", "іпн", "inn",
    "кпп", "kpp", "рнокпп",
    "едрпоу", "єдрпоу", "edrpou",
    "снилс", "snils",
    "огрн", "ogrn",
    "паспорт", "passport",
    "телефон", "phone", "telephone",
    "email", "e-mail",
    "электроннаяпочта", "електроннапошта",
    "адрес", "адреса", "address",
    "датарождения", "датанароджен", "birthdate", "birthday", "dateofbirth",
    "деньрождения", "деньнародження", "рождени", "народжен",
    "комментарий", "коментар", "comment",
    "примечание", "примітка", "note",
)

_BINARY_TYPES = frozenset({"ХранилищеЗначения", "ValueStorage", "ДвоичныеДанные", "BinaryData"})
_NUMERIC_TYPES = frozenset({"Число", "Number"})

# `kind` (singular, as used by get_object/describe_metadata: "catalog", "document", ...)
# -> dotted-type prefix. Used to build a dotted type from an explicit (type_name, kind).
_KIND_DOTTED_PREFIX: dict[str, str] = {
    "catalog": "Справочник.",
    "document": "Документ.",
    "task": "Задача.",
    "businessprocess": "БизнесПроцесс.",
    "enum": "Перечисление.",
    "chartofaccounts": "ПланСчетов.",
    "chartofcharacteristictypes": "ПланВидовХарактеристик.",
    "exchangeplan": "ПланОбмена.",
    "constant": "Константа.",
    "informationregister": "РегистрСведений.",
    "accumulationregister": "РегистрНакопления.",
    "accountingregister": "РегистрБухгалтерии.",
}

# Family `kind` hint: "the name is SOME register" (accumulation / information / accounting)
# when the caller (e.g. a register tool) knows the family but not which register kind.
# `_dotted_type` then picks the register kind that owns the bare name in the inventory,
# ignoring non-register kinds sharing the name (a chart of accounts named like its register).
REGISTER_FAMILY_KIND = "register"
_REGISTER_DOTTED_PREFIXES: tuple[str, ...] = tuple(
    prefix for kind, prefix in _KIND_DOTTED_PREFIX.items() if kind.endswith("register")
)

# `list_metadata("all")` top-level keys (plural) -> dotted-type prefix. Used by
# `build_type_classifier` to turn the whole-configuration inventory into a TypeClassifier.
_INVENTORY_KIND_PREFIX: dict[str, str] = {
    "catalogs": "Справочник.",
    "documents": "Документ.",
    "informationregisters": "РегистрСведений.",
    "accumulationregisters": "РегистрНакопления.",
    "accountingregisters": "РегистрБухгалтерии.",
    "enums": "Перечисление.",
    "tasks": "Задача.",
    "chartsofcharacteristictypes": "ПланВидовХарактеристик.",
    "chartsofaccounts": "ПланСчетов.",
    "businessprocesses": "БизнесПроцесс.",
    "exchangeplans": "ПланОбмена.",
    "constants": "Константа.",
}

# Same keys as `_INVENTORY_KIND_PREFIX`, spelled the way `GET /ai/v1/metadata/{kind}`
# expects for a SINGLE kind (per `.claude/refs/onec-tool-contract.md` §4.8). These are
# generic platform metadata-KIND names ("catalog", "chart of accounts", ...), the same
# 12 kinds on every BAS/1C configuration — not a configuration's own object names — so
# listing them here does not violate onec-universality. Some BAS service builds omit one
# or more of these kinds from the `all` inventory (observed: chartsofaccounts absent from
# `metadata/all` while `metadata/ChartsOfAccounts` answers fine); `build_type_classifier`
# falls back to a per-kind call for whichever key `all` didn't return.
_INVENTORY_KIND_ROUTE_NAME: dict[str, str] = {
    "catalogs": "Catalogs",
    "documents": "Documents",
    "informationregisters": "InformationRegisters",
    "accumulationregisters": "AccumulationRegisters",
    "accountingregisters": "AccountingRegisters",
    "enums": "Enums",
    "tasks": "Tasks",
    "chartsofcharacteristictypes": "ChartsOfCharacteristicTypes",
    "chartsofaccounts": "ChartsOfAccounts",
    "businessprocesses": "BusinessProcesses",
    "exchangeplans": "ExchangePlans",
    "constants": "Constants",
}


@dataclass(frozen=True)
class MaskingPolicy:
    """What stays plain vs gets pseudonymized. Immutable — build overrides via `merge`."""

    open_type_prefixes: tuple[str, ...] = field(default_factory=tuple)
    open_type_exact: tuple[str, ...] = field(default_factory=tuple)
    forced_mask_substrings: tuple[str, ...] = field(default_factory=tuple)
    open_field_names: tuple[str, ...] = field(default_factory=tuple)
    allow_plain_fields: tuple[str, ...] = field(default_factory=tuple)
    force_mask_fields: tuple[str, ...] = field(default_factory=tuple)
    # Exact dotted types CLOSED even when a default/override open list matches them
    # (e.g. "Справочник.Склады", or one "Документ.X" despite the "Документ." prefix).
    mask_type_exact: tuple[str, ...] = field(default_factory=tuple)
    # Per-type field rules, "<dotted type>.<field pattern>" (e.g. "Справочник.Склады.Адрес"):
    # apply only in rows whose context is exactly that dotted type.
    allow_plain_type_fields: tuple[str, ...] = field(default_factory=tuple)
    force_mask_type_fields: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def default(cls) -> MaskingPolicy:
        return cls(
            open_type_prefixes=_DEFAULT_OPEN_PREFIXES,
            open_type_exact=_DEFAULT_OPEN_CATALOGS,
            forced_mask_substrings=_DEFAULT_FORCED_SUBSTRINGS,
            open_field_names=_DEFAULT_OPEN_FIELD_NAMES,
        )

    def merge(self, overrides: dict[str, Any]) -> MaskingPolicy:
        """Returns a new policy with each list ADDITIVELY extended by `overrides`
        (deduplicated, order-preserving) — an override JSON/dict never has to repeat
        the defaults, only what it adds. Unknown keys in `overrides` are ignored."""

        def _extend(current: tuple[str, ...], key: str) -> tuple[str, ...]:
            extra = overrides.get(key) or []
            combined = list(current)
            for item in extra:
                if item not in combined:
                    combined.append(item)
            return tuple(combined)

        return MaskingPolicy(
            open_type_prefixes=_extend(self.open_type_prefixes, "open_type_prefixes"),
            open_type_exact=_extend(self.open_type_exact, "open_type_exact"),
            forced_mask_substrings=_extend(self.forced_mask_substrings, "forced_mask_substrings"),
            open_field_names=_extend(self.open_field_names, "open_field_names"),
            allow_plain_fields=_extend(self.allow_plain_fields, "allow_plain_fields"),
            force_mask_fields=_extend(self.force_mask_fields, "force_mask_fields"),
            mask_type_exact=_extend(self.mask_type_exact, "mask_type_exact"),
            allow_plain_type_fields=_extend(
                self.allow_plain_type_fields, "allow_plain_type_fields"
            ),
            force_mask_type_fields=_extend(self.force_mask_type_fields, "force_mask_type_fields"),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaskingPolicy:
        """Builds a policy = defaults + `data` (same shape as `merge`'s `overrides`)."""
        return cls.default().merge(data)

    @classmethod
    def from_json_file(cls, path: str) -> MaskingPolicy:
        """Loads a JSON file of the same shape as `from_dict` and merges it onto
        the defaults. Example file::

            {
              "open_type_exact": ["Справочник.МояБезопаснаяДовідка"],
              "allow_plain_fields": ["ПІБВодія"],
              "force_mask_fields": ["ВнутреннийКомментарий"]
            }
        """
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data)


DEFAULT_POLICY = MaskingPolicy.default()


# --- Small pure helpers --------------------------------------------------------------

_REF_KEYS = frozenset({"Presentation", "Data", "Metadata"})
# The enum/simple-value reference shape BAS returns for e.g. `ВидКонтрагента`, contact-info
# `Тип`: no `Data` UUID (there is no underlying object to point at), `Value` carries the
# internal enum literal name instead. Classified the same way as `_REF_KEYS` (by `Metadata`),
# but masked differently — see `_mask_value_reference`.
_VALUE_REF_KEYS = frozenset({"Presentation", "Value", "Metadata"})
_CHANGE_ENTRY_KEYS = frozenset({"field", "old", "new"})
# Envelope keys that carry STRUCTURE (real field/type names, pagination state) rather than
# business data — the model's self-correction depends on reading these as-is (see
# ai_orchestrator.SYSTEM_PROMPT: `unknown_fields`/`available_names` steer it to a real field
# name). Values inside `data`/`aggregates` rows are unaffected and stay masked as before.
# The accounting balance envelope (get_register_balance on an AccountingRegister) adds its echo
# of the request (`table`, `periodicity`, `from`/`to`/`on`, `groupby`, `resources`) and the
# `ext_dimension_types` legend: account codes -> extdimension type names, structure too.
_ENVELOPE_STRUCTURAL_KEYS = frozenset(
    {"unknown_fields", "available_names", "cursor", "truncated", "metadata", "type",
     "table", "periodicity", "from", "to", "on", "groupby", "resources",
     "ext_dimension_types"}
)
_CODE_LIKE_RE = re.compile(r"^\d+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _normalize_field_name(name: str) -> str:
    return "".join(name.split()).lower()


def _contains_any(normalized: str, substrings: tuple[str, ...]) -> bool:
    return any(s and s in normalized for s in substrings)


# Forced keys that open English words when they START one ("Inner", "Innovation", "Beginning"):
# as a word of a field name they must END a word ("INN", "InnCode", "КлиентInn").
_FORCED_WORD_ENDS_ONLY = frozenset({"inn"})


def _name_words(name: str) -> list[str]:
    """A 1C field name split into its words: at CamelCase steps (an acronym run ends before
    its last capital when a lowercase letter follows: "ИННФизЛица" -> ИНН|Физ|Лица), at
    letter/digit changes, and at any non-alphanumeric character (space, "_", "-")."""
    words: list[str] = []
    cur = ""
    for ch in name:
        if not ch.isalnum():
            if cur:
                words.append(cur)
            cur = ""
            continue
        if cur:
            prev = cur[-1]
            if ch.isdigit() != prev.isdigit() or (ch.isupper() and prev.islower()):
                words.append(cur)
                cur = ""
            elif ch.islower() and prev.isupper() and len(cur) > 1 and cur[-2].isupper():
                words.append(cur[:-1])
                cur = prev
        cur += ch
    if cur:
        words.append(cur)
    return words


def _forced_key(raw: str) -> str:
    return "".join(ch for ch in raw.lower() if ch.isalnum())


def _capital_runs(name: str) -> list[str]:
    """Every run of 2+ capitals in `name`, whole ("ИНН" in "ИННконтрагента", "ИННФ" in
    "ИННФизЛица"): an acronym run has no case step inside it to split by."""
    runs: list[str] = []
    cur = ""
    for ch in name:
        if ch.isupper():
            cur += ch
            continue
        if len(cur) > 1:
            runs.append(cur.lower())
        cur = ""
    if len(cur) > 1:
        runs.append(cur.lower())
    return runs


def _has_forced_word(name: str, keys: tuple[str, ...]) -> bool:
    """True when a forced key starts or ends at a word boundary of `name` (it may span words:
    "датарождения" in "ДатаРождения", "phone" in "Mobilephone"); a key of
    `_FORCED_WORD_ENDS_ONLY` must end at a word end. A run of capitals ("РНОКПП",
    "ИННконтрагента") and a name with no capital at all ("contactphone") are matched by plain
    substring. A key only inside a word or across a word step stays unmatched: "ипн" in
    "ТипНоменклатуры", "инн" in "Длинное"."""
    words = _name_words(name)
    if not words:
        return False
    joined = "".join(w.lower() for w in words)
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    for w in words:
        starts.append(pos)
        pos += len(w)
        ends.append(pos)
    caseless = [joined] if not any(ch.isupper() for ch in name) else _capital_runs(name)
    for raw in keys:
        key = _forced_key(raw)
        if not key:
            continue
        if any(key in run for run in caseless):
            return True
        if any(joined.endswith(key, 0, end) for end in ends):
            return True
        if key not in _FORCED_WORD_ENDS_ONLY and any(
            joined.startswith(key, start) for start in starts
        ):
            return True
    return False


def _matches_pattern(normalized: str, patterns: tuple[str, ...]) -> bool:
    """Exact match, or `*suffix` / `prefix*` glob-lite match, against a normalized
    (lowercased, whitespace-stripped) field name."""
    for raw in patterns:
        pattern = _normalize_field_name(raw)
        if not pattern:
            continue
        if pattern == normalized:
            return True
        if len(pattern) > 1 and pattern[0] == "*" and normalized.endswith(pattern[1:]):
            return True
        if len(pattern) > 1 and pattern[-1] == "*" and normalized.startswith(pattern[:-1]):
            return True
    return False


def _looks_like_code(value: str) -> bool:
    """A numeric-looking STRING that reads as an identifier, not a real number:
    a leading zero (excluding the bare digit "0") or more than 15 digits."""
    s = value.strip()
    if not _CODE_LIKE_RE.match(s):
        return False
    if len(s) > 15:
        return True
    return len(s) > 1 and s[0] == "0"


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?$")
_DOTTED_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}(?: \d{1,2}:\d{2}(?::\d{2})?)?$")


def _looks_like_date(value: str) -> bool:
    """A calendar date/datetime as BAS emits it — ISO `YYYY-MM-DD[THH:MM[:SS]]` or the 1C
    `DD.MM.YYYY[ H:MM[:SS]]` form — that parses as a real date. A date is not personal data:
    it stays open unless a forced rule (birth date, policy, per-type Mask) masks the field."""
    s = value.strip()
    if _ISO_DATE_RE.match(s):
        try:
            datetime.fromisoformat(s)
        except ValueError:
            return False
        return True
    if _DOTTED_DATE_RE.match(s):
        time_part = s.partition(" ")[2]
        fmt = "%d.%m.%Y" + {0: "", 1: " %H:%M", 2: " %H:%M:%S"}[time_part.count(":")]
        try:
            datetime.strptime(s, fmt)
        except ValueError:
            return False
        return True
    return False


def _looks_like_reference(value: dict[str, Any]) -> bool:
    return _REF_KEYS <= value.keys()


def _looks_like_value_reference(value: dict[str, Any]) -> bool:
    return "Data" not in value and _VALUE_REF_KEYS <= value.keys()


def _looks_like_identifier_value(value: str) -> bool:
    """True for a bare enum-literal token (`"ЮридическоеЛицо"`, `"Безналичные"`) as opposed
    to human-readable free text — no whitespace, nothing to leak as a business fact."""
    stripped = value.strip()
    return bool(stripped) and not any(ch.isspace() for ch in stripped)


def _looks_like_change_entry(value: dict[str, Any]) -> bool:
    return _CHANGE_ENTRY_KEYS <= value.keys()


_BALANCE_ENVELOPE_KEYS = frozenset({"type", "data"})
_BALANCE_ENVELOPE_OPTIONAL_KEYS = frozenset({"on", "cursor", "truncated"})


def _period_bucket_keys(groupby: Any) -> set[str]:
    """Row keys of `Period:<granularity>` groups in a `groupby` list (e.g. {"Period"}).

    Such a bucket is a calendar date, not data about anyone — left open, or a by-month breakdown
    would come back as unreadable pseudonyms. Shared by the register aggregate envelope and the
    accounting balance envelope (whose rows carry the bucket under its own field name)."""
    if not isinstance(groupby, list):
        return set()
    return {str(item).split(":", 1)[0] for item in groupby if ":" in str(item)}


def _looks_like_envelope(value: dict[str, Any]) -> bool:
    """`get_object`/`describe_metadata`-style `{"metadata","type","data",...}`, or the
    `metadata`-less variant `get_register_balance`/records return (`{"type","data"}`, plus
    `on` when a balance date was requested). The metadata-less variant is recognized only
    when its keys are exactly the envelope's own plus `on`/`cursor`/`truncated` AND `type`/
    `data` have the envelope's own shapes — an ordinary data row that happens to carry extra
    fields alongside a `type`/`data` key never matches, so it keeps being masked normally."""
    if "type" not in value or "data" not in value:
        return False
    if "metadata" in value:
        return True
    extra = value.keys() - _BALANCE_ENVELOPE_KEYS - _BALANCE_ENVELOPE_OPTIONAL_KEYS
    if extra:
        return False
    return isinstance(value["type"], str) and isinstance(value["data"], (list, dict))


_EVENT_LOG_ENVELOPE_KEYS = frozenset({"data", "truncated", "from", "to"})
_EVENT_LOG_ROW_KEYS = frozenset({
    "date", "level", "event", "user", "metadata", "data_presentation", "comment", "session",
    "application", "computer",
})
# Platform values of an event-log row (timestamps, level/event names, metadata full names,
# session numbers, client application ids) — never personal data, so they stay readable.
_EVENT_LOG_OPEN_KEYS = frozenset({"date", "level", "event", "metadata", "session", "application"})


def _looks_like_event_log_envelope(value: dict[str, Any]) -> bool:
    """`get_event_log`: exactly `{data, truncated, from, to}` whose rows carry exactly the
    ten event-log fields — anything less specific keeps the default masking."""
    if value.keys() != _EVENT_LOG_ENVELOPE_KEYS or not isinstance(value["data"], list):
        return False
    return all(isinstance(r, dict) and r.keys() == _EVENT_LOG_ROW_KEYS for r in value["data"])


def _looks_like_aggregate_envelope(value: dict[str, Any]) -> bool:
    return "aggregates" in value


# --- Marked per-type rules: additional properties and contact-information kinds -------------

# A per-type policy entry "<dotted type>.<marker>:<name pattern>" (from an MCBP_SensitiveData
# register row whose ObjectAttribute is "<marker>:<name>") targets not an attribute but:
#   additional:<name> - one additional attribute / property, by its presentation, in
#       `data.additional[{name, value}]` and in a property tabular section (rows of a
#       chart-of-characteristic-types reference + one value column, found by shape);
#   contact:<name> - the contact-information rows of one kind: a tabular-section row holding a
#       reference whose presentation is that kind; every string of such a row is masked.
ADDITIONAL_MARKER = "additional"
CONTACT_MARKER = "contact"
_ADDITIONAL_KEY = "additional"
_ADDITIONAL_ITEM_KEYS = frozenset({"name", "value"})
_MARKER_RE: dict[str, re.Pattern[str]] = {
    marker: re.compile(rf"\.{marker}:", re.IGNORECASE)
    for marker in (ADDITIONAL_MARKER, CONTACT_MARKER)
}
# The platform's standard line-number attribute of a tabular-section row (EN / inherited RU / UA).
_LINE_NUMBER_KEYS = frozenset({"LineNumber", "НомерСтроки", "НомерРядка"})
_PROPERTY_TYPE_PREFIX = "ПланВидовХарактеристик."


def _looks_like_additional(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and _ADDITIONAL_ITEM_KEYS <= item.keys()
        and isinstance(item["name"], str)
        for item in value
    )


def _matches_marked_rule(
    context: str | None, normalized_name: str, entries: tuple[str, ...], marker: str
) -> bool:
    """True when an entry "<dotted type>.<marker>:<name pattern>" has its type equal to
    `context` and its name pattern matching the normalized name (case- and whitespace-
    insensitive, same glob-lite as `_matches_pattern`). The name may hold dots, so the entry
    is split at the marker, not at the last dot."""
    if context is None or not normalized_name:
        return False
    pattern = _MARKER_RE[marker]
    for entry in entries:
        found = pattern.search(entry)
        if found and entry[:found.start()] == context and _matches_pattern(
            normalized_name, (entry[found.end():],)
        ):
            return True
    return False


def _has_marked_rules(context: str | None, entries: tuple[str, ...], marker: str) -> bool:
    if context is None:
        return False
    pattern = _MARKER_RE[marker]
    return any((m := pattern.search(e)) and e[:m.start()] == context for e in entries)


# Full-text search (`GET /metadata/all?fts=`): `{query, index_up_to_date, ..., data: [{type,
# ref, presentation, description}]}`. `type` is the platform full name ("Catalog.X"); its kind
# prefix (a generic platform kind name, the same on every configuration) gives the dotted type
# when the row's `ref` carries no classifiable `Metadata`.
_FTS_ENVELOPE_KEYS = frozenset({"query", "index_up_to_date", "data"})
_FULL_NAME_DOTTED_PREFIX: dict[str, str] = {
    "catalog": "Справочник.",
    "document": "Документ.",
    "task": "Задача.",
    "businessprocess": "БизнесПроцесс.",
    "chartofaccounts": "ПланСчетов.",
    "chartofcharacteristictypes": "ПланВидовХарактеристик.",
    "exchangeplan": "ПланОбмена.",
}
# A run of digits (tax codes, phone numbers, account numbers), separators inside a run allowed:
# an `additional` value of `_DIGIT_RUN_MIN`+ digits is masked whatever its name.
_DIGIT_RUN_RE = re.compile(r"\+?\(?\d[\d \t().\-]*\d")
_DIGIT_RUN_MIN = 8


def _looks_like_fts_envelope(value: dict[str, Any]) -> bool:
    return _FTS_ENVELOPE_KEYS <= value.keys() and isinstance(value["data"], list)


def _dotted_from_full_name(full_name: Any) -> str | None:
    if not isinstance(full_name, str):
        return None
    kind, sep, name = full_name.partition(".")
    prefix = _FULL_NAME_DOTTED_PREFIX.get(kind.lower())
    return f"{prefix}{name}" if sep and name and prefix else None


# Related documents (`GET /related/{kind}/{type}/{id}`): `{metadata, type, id, sources, data:
# [{type, ref, number, date, deleted, found_in}], truncated, scanned_sources[, failed_sources]}`.
# `type`/`found_in` of a row and every envelope key but `data` are structure (metadata names,
# counters, scanned query tables).
_RELATED_ROW_KEYS = frozenset({"type", "ref", "found_in"})
_RELATED_STRUCTURAL_KEYS = frozenset(
    {"metadata", "type", "id", "sources", "truncated", "scanned_sources", "failed_sources"}
)
_RELATED_FIELD_ALIASES = {"number": "Number", "date": "Date"}


def _looks_like_related_envelope(value: dict[str, Any]) -> bool:
    data = value.get("data")
    return "scanned_sources" in value and isinstance(data, list) and all(
        isinstance(row, dict) and _RELATED_ROW_KEYS <= row.keys() for row in data
    )


def _is_open_type(dotted: str | None, policy: MaskingPolicy) -> bool:
    if dotted is None:
        return False
    if dotted in policy.mask_type_exact:
        return False
    if dotted in policy.open_type_exact:
        return True
    return any(dotted.startswith(prefix) for prefix in policy.open_type_prefixes)


# Standard attributes: the JSON key is canonical (Description, Code, ...), a register row written
# by BSL names the metadata attribute (Наименование / Найменування, Код, ...). Normalized names of
# one group are the same field for per-type rules, both ways.
_STANDARD_NAME_GROUPS: tuple[tuple[str, ...], ...] = (
    ("description", "наименование", "найменування"),
    ("code", "код"),
    ("number", "номер"),
    ("date", "дата"),
    ("owner", "владелец", "власник"),
    ("parent", "родитель", "батько"),
    ("deletionmark", "пометкаудаления", "позначкавидалення"),
    ("posted", "проведен", "проведений"),
    ("isfolder", "этогруппа", "цегрупа"),
    ("valuetype", "типзначения", "типзначення"),
    ("ref", "ссылка", "посилання"),
    ("linenumber", "номерстроки", "номеррядка"),
)
_STANDARD_NAME_VARIANTS: dict[str, tuple[str, ...]] = {
    name: group for group in _STANDARD_NAME_GROUPS for name in group
}


def _name_variants(normalized: str) -> tuple[str, ...]:
    return _STANDARD_NAME_VARIANTS.get(normalized, (normalized,))


def _matches_type_field(context: str | None, normalized: str, entries: tuple[str, ...]) -> bool:
    """True when an entry "<dotted type>.<field pattern>" has its type equal to `context`
    and its field pattern matching the normalized field name (same glob-lite as
    `_matches_pattern`) or one of its standard-attribute names (`_STANDARD_NAME_GROUPS`).
    The field part is after the LAST dot — field names have none. A tabular-section field
    is matched with `context` = "<dotted type>.<section>" (entry "<type>.<section>.<field>")."""
    if context is None or not normalized:
        return False
    variants = _name_variants(normalized)
    for entry in entries:
        type_part, sep, field_part = entry.rpartition(".")
        if sep and type_part == context and any(
            _matches_pattern(name, (field_part,)) for name in variants
        ):
            return True
    return False


@dataclass(frozen=True)
class _PolicyIndex:
    """Lookups built once per policy, so a per-row / per-reference check is O(1) whatever the
    number of per-type rules: `rule_contexts` - every "<type>[.<section>]" part of a per-type
    field rule (a tabular-section context has rules iff it is here); `name_masked_types` - the
    dotted types with a Mask rule on Description under any of its names (Наименование, ...)."""

    rule_contexts: frozenset[str]
    name_masked_types: frozenset[str]

    @classmethod
    def build(cls, policy: MaskingPolicy) -> _PolicyIndex:
        contexts: set[str] = set()
        name_masked: set[str] = set()
        description = _name_variants("description")
        for entries, forced in ((policy.force_mask_type_fields, True),
                                (policy.allow_plain_type_fields, False)):
            for entry in entries:
                type_part, sep, field_part = entry.rpartition(".")
                if not sep:
                    continue
                contexts.add(type_part)
                if forced and any(_matches_pattern(n, (field_part,)) for n in description):
                    name_masked.add(type_part)
        return cls(frozenset(contexts), frozenset(name_masked))


def _looks_like_long_number(value: Any) -> bool:
    """A number of `_DIGIT_RUN_MIN`+ digits, as an int, an integral float or a string of digits
    (spaces, brackets, dots, dashes and a leading + / "(" allowed: a phone): ЄДРПОУ (8), ІПН (10),
    phone numbers, account numbers. A date is not one.

    Trade-off, kept on purpose: an `additional` value is masked by this whatever its name, so a
    whole amount of 10 000 000+ (or such a number written as a string, "12345678.25" too) is
    masked as well; a float with a fraction, 7 digits or fewer and dates stay readable. A wrong
    mask costs the model one readable number; a wrong open leaks a tax code or a phone."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        if not value.is_integer():
            return False
        value = int(value)
    if isinstance(value, int):
        return len(str(abs(value))) >= _DIGIT_RUN_MIN
    if not isinstance(value, str):
        return False
    text = value.strip()
    return (
        bool(_DIGIT_RUN_RE.fullmatch(text))
        and not _looks_like_date(text)
        and sum(ch.isdigit() for ch in text) >= _DIGIT_RUN_MIN
    )


# --- Public predicates: the policy's field rules, for callers that check masking (audits) ----

def normalize_field_name(name: str) -> str:
    """A field name as the rules compare it: lowercased, whitespace removed."""
    return _normalize_field_name(name)


def is_open_type(dotted: str | None, policy: MaskingPolicy) -> bool:
    """True when the dotted type ("Справочник.X") is open under `policy` (its references and,
    for an exact open catalog, its own fields stay readable)."""
    return _is_open_type(dotted, policy)


def is_field_forced(policy: MaskingPolicy, field_name: str, context: str | None = None) -> bool:
    """True when `field_name` is always masked: a forced substring (ІПН, phone, ...), a
    `force_mask_fields` pattern, or a per-type Mask rule of `context` ("<type>" or, for a
    tabular-section field, "<type>.<section>"; standard attributes under any of their names)."""
    normalized = _normalize_field_name(field_name)
    return bool(normalized) and (
        _has_forced_word(field_name, policy.forced_mask_substrings)
        or _matches_pattern(normalized, policy.force_mask_fields)
        or _matches_type_field(context, normalized, policy.force_mask_type_fields)
    )


def is_field_opened(policy: MaskingPolicy, field_name: str, context: str | None = None) -> bool:
    """True when a rule opens `field_name` (`allow_plain_fields` or a per-type open rule of
    `context`). A forced name stays masked whatever this says - check `is_field_forced` first."""
    normalized = _normalize_field_name(field_name)
    return bool(normalized) and (
        _matches_pattern(normalized, policy.allow_plain_fields)
        or _matches_type_field(context, normalized, policy.allow_plain_type_fields)
    )


def _b32(digest: bytes, length: int) -> str:
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")
    return encoded[:length] if length > 0 else encoded


# --- Masker ----------------------------------------------------------------------

# Group keys of the RecordType enum in register aggregates (canonical + RU/UA attribute names).
_RECORD_TYPE_KEYS = frozenset({"recordtype", "виддвижения", "видруху"})


def _is_record_type_value(value: Any) -> bool:
    """The AccumulationRecordType shape BSL emits: a bare string or a presentation-only
    `{"Presentation": ...}` (platform enum, no UUID/Metadata). A reference dict - anything
    carrying `Data`/`Metadata`/`Value` - is a real dimension value and stays masked."""
    if isinstance(value, str):
        return True
    return (isinstance(value, dict) and value.keys() == {"Presentation"}
            and isinstance(value["Presentation"], str))


class Masker:
    """Session-scoped pseudonymizer. Pure, synchronous, no I/O.

    Deterministic within one instance: the same (type, id) or (field, value) always
    produces the same pseudonym, letting the model join rows across tool calls. The
    secret is per-instance (random unless given) — pseudonyms from different sessions
    never collide in meaning, and nothing here persists across instances.
    """

    def __init__(
        self,
        *,
        secret: bytes | str | None = None,
        policy: MaskingPolicy | None = None,
        type_classifier: TypeClassifier | None = None,
        field_type_lookup: FieldTypeLookup | None = None,
        code_length: int = 6,
    ) -> None:
        if secret is None:
            secret = secrets.token_bytes(32)
        elif isinstance(secret, str):
            secret = secret.encode("utf-8")
        self._secret = secret
        self._policy = policy or DEFAULT_POLICY
        self._index = _PolicyIndex.build(self._policy)
        self._classify = type_classifier or (lambda _name: None)
        self._field_type_lookup = field_type_lookup
        self._code_length = code_length

        self._cache: dict[tuple[str, str], str] = {}
        self._pseudo_to_original: dict[str, str] = {}
        self._pseudo_to_uuid: dict[str, str] = {}
        self._original_to_pseudo: dict[str, str] = {}
        # rehydrate_text index, rebuilt when the pseudonym map grows (it never shrinks).
        self._rehydrate_size = -1
        self._rehydrate_pattern: re.Pattern[str] | None = None
        self._rehydrate_table: dict[str, str] = {}
        self._bare_codes: frozenset[str] = frozenset()

    def _rehydrate_index(self) -> tuple[re.Pattern[str] | None, dict[str, str]]:
        """(pattern, match -> original) over issued pseudonyms and their unambiguous codes."""
        if self._rehydrate_size == len(self._pseudo_to_original):
            return self._rehydrate_pattern, self._rehydrate_table
        table = dict(self._pseudo_to_original)
        owners: dict[str, list[str]] = {}
        for pseudo in table:
            _, sep, code = pseudo.rpartition("·")
            if sep and code:
                owners.setdefault(code, []).append(pseudo)
        # An all-digit code would rewrite ordinary numbers in an answer: never matched bare.
        bare = {
            code: table[pseudos[0]] for code, pseudos in owners.items()
            if len(pseudos) == 1 and not code.isdigit()
        }
        pattern: re.Pattern[str] | None = None
        if table:
            full = "|".join(re.escape(p) for p in sorted(table, key=len, reverse=True))
            regex = f"(?:{full})"
            if bare:
                codes = "|".join(re.escape(c) for c in sorted(bare, key=len, reverse=True))
                regex += rf"|(?<![\w·])(?:{codes})(?!\w)"
            pattern = re.compile(regex)
        self._rehydrate_table = {**bare, **table}
        self._rehydrate_pattern = pattern
        self._bare_codes = frozenset(bare)
        self._rehydrate_size = len(table)
        return pattern, self._rehydrate_table

    # --- Public API ---------------------------------------------------------------

    def mask(self, result: Any, *, type_name: str | None = None, kind: str | None = None) -> Any:
        """Returns a deep-masked copy of a tool result. `type_name`/`kind` are an
        OPTIONAL fallback context (e.g. `type_name="MCBP_Debt", kind="accumulationregister"`
        for a balance/records call) used only where the payload doesn't self-describe
        its type (register balance/records rows, aggregates without embedded metadata).
        List/get_object/describe_metadata-style payloads that carry their own
        `Ref.Metadata` or top-level `{"metadata","type"}` need no hint."""
        context = self._dotted_type(type_name, kind)
        return self._walk_value(None, result, context)

    def set_policy(self, policy: MaskingPolicy) -> None:
        """Replaces the masking rules only. The secret, the cache and the pseudonym maps are
        kept, so every pseudonym already issued stays identical and still rehydrates/unmasks."""
        self._policy = policy
        self._index = _PolicyIndex.build(policy)

    def unmask_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Replaces any known pseudonym found inside tool-call arguments — including
        values nested in `filters`/`f.*` dicts and plain `id` strings — with what the
        BAS route actually expects: the reference UUID for a masked reference
        pseudonym, the original text for a masked field pseudonym."""
        return {k: self._unmask_value(v) for k, v in arguments.items()}

    def rehydrate_text(self, text: str, *, before: str = "", after: str = "") -> str:
        """Replaces pseudonyms in free text with their REAL display value (for
        showing the human user), longest pseudonym first so one is never partially
        swallowed by a shorter one that happens to be its prefix.

        A model sometimes writes only the code of a pseudonym ("сценарій JYUOI2" for
        `Kind·JYUOI2`). A BARE code is replaced too, but only one this masker issued, only
        as a whole word (not inside a longer alphanumeric token, not after `·`), and only
        when no two issued pseudonyms share it. Full `Kind·CODE` matches win. One pass, so
        an inserted original is never rewritten again. `before`/`after` are the raw text
        around `text` (a streaming caller's last emitted char / first held-back char), used
        only for the word-boundary checks; a match running into `after` is left as written."""
        if not text:
            return text
        pattern, table = self._rehydrate_index()
        if pattern is None:
            return text
        source = before + text + after
        end = len(before) + len(text)
        out: list[str] = []
        last = len(before)
        for match in pattern.finditer(source, len(before)):
            if match.start() >= end:
                break
            if match.end() > end:
                continue
            out.append(source[last:match.start()])
            out.append(table[match.group()])
            last = match.end()
        if not out:
            return text
        out.append(source[last:end])
        return "".join(out)

    def bare_codes(self) -> frozenset[str]:
        """Codes of issued pseudonyms that `rehydrate_text` also replaces when written bare."""
        self._rehydrate_index()
        return self._bare_codes

    def mask_text(self, text: str) -> str:
        """Masks a free string (e.g. an upstream error message) by replacing any
        ALREADY-SEEN original value with its pseudonym, longest original first.
        Best-effort: only values this `Masker` has already pseudonymized once (via
        `mask()`) can be found here — it cannot mask something it has never seen."""
        if not text:
            return text
        pairs = [(o, p) for o, p in self._original_to_pseudo.items() if o and o in text]
        if not pairs:
            return text
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        for original, pseudo in pairs:
            text = text.replace(original, pseudo)
        return text

    def known_pseudonyms(self) -> frozenset[str]:
        return frozenset(self._pseudo_to_original.keys())

    def max_known_pseudonym_length(self) -> int:
        return max((len(p) for p in self._pseudo_to_original), default=0)

    def stream_rehydrator(self) -> StreamRehydrator:
        """A stateful helper for rehydrating SSE text deltas, where a pseudonym can
        be split across chunk boundaries."""
        return StreamRehydrator(self)

    # --- Internals: dotted-type resolution -----------------------------------------

    def _dotted_type(self, type_name: str | None, kind: str | None) -> str | None:
        if not type_name:
            return None
        if kind:
            if kind.lower() == REGISTER_FAMILY_KIND:
                return self._register_type(type_name)
            prefix = _KIND_DOTTED_PREFIX.get(kind.lower())
            if prefix:
                return f"{prefix}{type_name}"
        return self._classify(type_name)

    def _register_type(self, type_name: str) -> str | None:
        # An inventory classifier knows every kind a name has; a plain callable only its one
        # answer. Without a register candidate, fall back to the plain classification.
        all_types = getattr(self._classify, "all_types", None)
        candidates = all_types(type_name) if callable(all_types) else (self._classify(type_name),)
        for dotted in candidates:
            if dotted and dotted.startswith(_REGISTER_DOTTED_PREFIXES):
                return dotted
        return self._classify(type_name)

    def _row_context(self, row: Any, fallback: str | None) -> str | None:
        if isinstance(row, dict):
            ref = row.get("Ref")
            if isinstance(ref, dict) and ref.get("Metadata"):
                classified = self._classify(ref["Metadata"])
                if classified:
                    return classified
        return fallback

    # --- Internals: recursive walk ---------------------------------------------

    def _walk_value(self, key: str | None, value: Any, context: str | None) -> Any:
        if key == _ADDITIONAL_KEY and _looks_like_additional(value):
            return self._mask_additional(value, context)
        if isinstance(value, dict):
            if _looks_like_reference(value):
                return self._mask_reference(value)
            if _looks_like_value_reference(value):
                return self._mask_value_reference(value)
            if _looks_like_change_entry(value):
                return self._mask_change_entry(value, context)
            if _looks_like_aggregate_envelope(value):
                return self._mask_aggregate_envelope(value, context)
            if _looks_like_event_log_envelope(value):
                return self._mask_event_log_envelope(value)
            if _looks_like_fts_envelope(value):
                return self._mask_fts_envelope(value)
            if _looks_like_related_envelope(value):
                return self._mask_related_envelope(value)
            if _looks_like_envelope(value):
                return self._mask_envelope(value, context)
            return {k: self._walk_value(k, v, context) for k, v in value.items()}
        if isinstance(value, list):
            if value and all(isinstance(item, dict) for item in value):
                return [self._mask_row(item, context, key) for item in value]
            return [self._walk_value(key, item, context) for item in value]
        return self._mask_scalar(key, value, context)

    def _mask_envelope(self, value: dict[str, Any], fallback: str | None) -> dict[str, Any]:
        new_context = self._dotted_type(value.get("type"), value.get("metadata"))
        context = new_context or fallback
        out: dict[str, Any] = {}
        open_keys = _period_bucket_keys(value.get("groupby"))
        for k, v in value.items():
            if k == "data" and open_keys and isinstance(v, list) and all(
                isinstance(row, dict) for row in v
            ):
                out[k] = [self._mask_row_keeping_open(row, context, open_keys) for row in v]
            elif k == "data":
                out[k] = self._mask_data(v, context)
            elif k in _ENVELOPE_STRUCTURAL_KEYS:
                out[k] = v
            else:
                out[k] = self._walk_value(k, v, fallback)
        return out

    def _mask_row_keeping_open(
        self, row: dict[str, Any], context: str | None, open_keys: set[str]
    ) -> dict[str, Any]:
        rest = {k: v for k, v in row.items() if k not in open_keys}
        masked = self._walk_value(None, rest, self._row_context(row, context)) if rest else {}
        return {k: row[k] if k in open_keys else masked[k] for k in row}

    def _mask_data(self, data: Any, context: str | None) -> Any:
        if isinstance(data, list):
            if data and all(isinstance(item, dict) and len(item) == 1 for item in data):
                # get_object shape: a list of single-key {field: value} dicts.
                out = []
                for item in data:
                    ((k, v),) = item.items()
                    out.append({k: self._walk_value(k, v, context)})
                return out
            return [
                self._walk_value(None, row, self._row_context(row, context)) for row in data
            ]
        return self._walk_value(None, data, self._row_context(data, context))

    def _mask_change_entry(self, value: dict[str, Any], context: str | None) -> dict[str, Any]:
        field_name = value.get("field")
        out = dict(value)
        out["old"] = self._mask_scalar(field_name, value.get("old"), context)
        out["new"] = self._mask_scalar(field_name, value.get("new"), context)
        return out

    def _mask_event_log_envelope(self, value: dict[str, Any]) -> dict[str, Any]:
        rows = [
            {k: v if k in _EVENT_LOG_OPEN_KEYS else self._mask_scalar(k, v, None)
             for k, v in row.items()}
            for row in value["data"]
        ]
        return {**value, "data": rows}

    def _mask_aggregate_envelope(
        self, value: dict[str, Any], context: str | None
    ) -> dict[str, Any]:
        groupby_field = value.get("groupby")
        # Register aggregates: `groupby` is a list and each group value sits under its own field
        # name. A `Period:<granularity>` bucket stays open (see _period_bucket_keys).
        # RecordType (receipt/expense) is a platform enum, not PII: masked, the split rows would be
        # indistinguishable. Open ONLY when BSL marked the split (`record_type_split: true`, set
        # for balance accumulation registers alone) - an information register may have its own
        # dimension named ВидДвижения, which is ordinary data and masked like any other.
        open_keys: set[str] = set()
        record_type_open = False
        if isinstance(groupby_field, list):
            open_keys = _period_bucket_keys(groupby_field)
            record_type_open = value.get("record_type_split") is True
            groupby_field = None
        context = self._dotted_type(value.get("type"), value.get("metadata")) or context
        out = dict(value)
        rows = value.get("aggregates") or []
        masked_rows = []
        for row in rows:
            if not isinstance(row, dict):
                masked_rows.append(row)
                continue
            masked_row: dict[str, Any] = {}
            for k, v in row.items():
                if k == "group" and groupby_field:
                    masked_row[k] = self._mask_scalar(groupby_field, v, context)
                elif k in open_keys or (
                    record_type_open and k.casefold() in _RECORD_TYPE_KEYS
                    and _is_record_type_value(v)
                ):
                    masked_row[k] = v
                else:
                    masked_row[k] = self._walk_value(k, v, context)
            masked_rows.append(masked_row)
        out["aggregates"] = masked_rows
        return out

    def _mask_row(
        self, row: dict[str, Any], context: str | None, section: str | None = None
    ) -> Any:
        """One dict of a list: a property row or a contact-kind row of a tabular section of
        `context` gets its own rules, any other row is walked as before. On top of that, an
        explicit rule "<type>.<section>.<field>" of the row's section `section` wins for its
        field, in a property or contact row too (`_apply_section_rules`)."""
        if "Ref" in row:
            return self._walk_value(None, row, self._row_context(row, context))
        prop = self._property_row(row)
        can_open = True
        prop_name: str | None = None
        if prop is not None:
            masked = self._mask_property_row(row, prop, context)
            name = row[prop].get("Presentation")
            prop_name = name if isinstance(name, str) else ""
            # A value of an always-masked property (ЄДРПОУ, phone, ...) or of one with an
            # explicit "<type>.additional:<name>" Mask row is never opened by a section rule.
            can_open = not self._property_always_masked(prop_name, context)
        elif self._is_contact_row(row, context):
            masked = self._mask_contact_row(row, context)
            # A "<type>.contact:<kind>" Mask row masks the whole row; a section rule never opens it.
            can_open = False
        else:
            masked = self._walk_value(None, row, context)
        section_context = f"{context}.{section}" if context and section else None
        if section_context in self._index.rule_contexts:
            return self._apply_section_rules(row, masked, section_context, can_open,
                                             prop_name, context)
        return masked

    def _property_always_masked(self, name: str, context: str | None) -> bool:
        normalized = _normalize_field_name(name)
        # A property name is free text with no CamelCase to split by: plain substring match.
        forced = _matches_pattern(normalized, self._policy.force_mask_fields) or _contains_any(
            normalized, self._policy.forced_mask_substrings)
        return (bool(normalized) and forced) or _matches_marked_rule(
            context, normalized, self._policy.force_mask_type_fields, ADDITIONAL_MARKER)

    def _apply_section_rules(
        self, row: dict[str, Any], masked: dict[str, Any], section_context: str,
        can_open: bool, prop_name: str | None = None, context: str | None = None,
    ) -> dict[str, Any]:
        """A tabular-section row after its usual masking (`masked`): a field with a Mask rule is
        masked whatever its type (a reference's presentation, a number); one with an open rule
        is readable unless its name is always masked or `can_open` is false (a contact row, a
        property with a forced name or an `additional:` Mask row). In a property row a long
        number opens only through an explicit open `additional:<name>` row, as in the
        `additional` block. An opened reference / enum value keeps its usual masking -
        `_mask_reference` by its own type, so the presentation of a closed type stays masked;
        only scalars are opened. Other fields keep their usual masking."""
        out = dict(masked)
        explicit_open = prop_name is not None and _matches_marked_rule(
            context, _normalize_field_name(prop_name), self._policy.allow_plain_type_fields,
            ADDITIONAL_MARKER)
        for k, v in row.items():
            normalized = _normalize_field_name(k)
            if _matches_type_field(section_context, normalized,
                                   self._policy.force_mask_type_fields):
                out[k] = self._force_mask(k, v)
            elif (can_open and not isinstance(v, (dict, list))
                  and not self._is_forced_name(normalized, k)
                  and not (prop_name is not None and not explicit_open
                           and _looks_like_long_number(v))
                  and _matches_type_field(section_context, normalized,
                                          self._policy.allow_plain_type_fields)):
                out[k] = v
        return out

    def _is_forced_name(self, normalized: str, name: str) -> bool:
        return bool(normalized) and (
            _matches_pattern(normalized, self._policy.force_mask_fields)
            or _has_forced_word(name, self._policy.forced_mask_substrings)
        )

    def _property_row(self, row: dict[str, Any]) -> str | None:
        """The property column of a property tabular-section row: a column holding a reference
        to a chart of characteristic types (whatever the other columns - value, text copy,
        ...). None when the row has no such column."""
        for key, ref in row.items():
            if key in _LINE_NUMBER_KEYS:
                continue
            if isinstance(ref, dict) and _looks_like_reference(ref) and ref.get("Metadata"):
                dotted = self._classify(ref["Metadata"])
                if dotted and dotted.startswith(_PROPERTY_TYPE_PREFIX):
                    return key
        return None

    def _mask_property_row(
        self, row: dict[str, Any], prop: str, context: str | None
    ) -> dict[str, Any]:
        """The property reference is walked as usual; every other column (the value, its text
        copy, ...) is masked by the property's rules, like the `additional` block."""
        ref = row[prop]
        name = ref.get("Presentation") if isinstance(ref.get("Presentation"), str) else ""
        out: dict[str, Any] = {}
        for k, v in row.items():
            if k in _LINE_NUMBER_KEYS:
                out[k] = v
            elif k == prop:
                out[k] = self._walk_value(k, v, context)
            else:
                out[k] = self._mask_property_value(name or k, v, context)
        return out

    def _is_contact_row(self, row: dict[str, Any], context: str | None) -> bool:
        if not _has_marked_rules(context, self._policy.force_mask_type_fields, CONTACT_MARKER):
            return False
        return any(
            isinstance(v, dict) and isinstance(v.get("Presentation"), str)
            and _matches_marked_rule(context, _normalize_field_name(v["Presentation"]),
                                     self._policy.force_mask_type_fields, CONTACT_MARKER)
            for v in row.values()
        )

    def _mask_contact_row(self, row: dict[str, Any], context: str | None) -> dict[str, Any]:
        """Every string of the row (presentation, field values, e-mail, phone, ...) masked; the
        kind and type references stay as the ordinary rules leave them."""
        out: dict[str, Any] = {}
        for k, v in row.items():
            if k in _LINE_NUMBER_KEYS:
                out[k] = v
            elif isinstance(v, str):
                out[k] = self._force_mask(k, v)
            else:
                out[k] = self._walk_value(k, v, context)
        return out

    def _mask_additional(self, items: list[dict[str, Any]], context: str | None) -> list:
        """`[{name, value}]`: `name` (the property's presentation - configuration data) stays
        readable, `value` is masked by `_mask_property_value`."""
        out = []
        for item in items:
            masked = {
                k: self._walk_value(k, v, context)
                for k, v in item.items() if k not in _ADDITIONAL_ITEM_KEYS
            }
            masked["name"] = item["name"]
            masked["value"] = self._mask_property_value(item["name"], item["value"], context)
            out.append({k: masked[k] for k in item})
        return out

    def _mask_property_value(self, name: str, value: Any, context: str | None) -> Any:
        """The value of an additional property, masked as if `name` were its field name: the
        forced substrings (ЄДРПОУ, phone, ...) and a per-type rule "<type>.additional:<name>"
        mask it whatever its type, numbers included; an open rule opens it unless the name is
        forced. An exact open field name (Number, Date, ...) does not open a value here: a
        property is not that platform attribute."""
        normalized = _normalize_field_name(name)
        forced = _matches_pattern(normalized, self._policy.force_mask_fields) or _contains_any(
            normalized, self._policy.forced_mask_substrings
        )
        if forced or _matches_marked_rule(
            context, normalized, self._policy.force_mask_type_fields, ADDITIONAL_MARKER
        ):
            return self._force_mask(name, value)
        if _matches_marked_rule(
            context, normalized, self._policy.allow_plain_type_fields, ADDITIONAL_MARKER
        ):
            return value
        if _looks_like_long_number(value):
            # A code under a name no rule knows ("Податковий номер": 87654321): numbers and
            # open types would leave it plain. Only an explicit open rule (above) opens it.
            return self._force_mask(name, value)
        if isinstance(value, (dict, list)):
            return self._walk_value(name, value, context)
        return self._mask_scalar(name, value, context, open_names=False)

    def _force_mask(self, key: str, value: Any) -> Any:
        """Masks a value whatever its type's openness: numbers and a reference's
        Presentation too. None, booleans and blank strings carry nothing and stay."""
        if isinstance(value, dict) and _looks_like_reference(value):
            return self._mask_reference(value, force=True)
        if isinstance(value, dict) and _looks_like_value_reference(value):
            out = dict(value)
            for part in ("Presentation", "Value"):
                text = value.get(part)
                if isinstance(text, str) and text.strip():
                    out[part] = self._pseudonym_for_field(key, text)
            return out
        if value is None or isinstance(value, bool) or (
            isinstance(value, str) and not value.strip()
        ):
            return value
        if isinstance(value, dict):
            return {k: self._force_mask(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._force_mask(key, v) for v in value]
        return self._pseudonym_for_field(key, value)

    def _mask_fts_envelope(self, value: dict[str, Any]) -> dict[str, Any]:
        rows = [self._mask_fts_row(row) if isinstance(row, dict) else row
                for row in value["data"]]
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "data":
                out[k] = rows
            elif k == "query" and isinstance(v, str):
                # The model's own search text; values it knows only as pseudonyms (unmasked for
                # the call) go back as pseudonyms. After the rows, so a value first seen in this
                # very answer is known too.
                out[k] = self.mask_text(v)
            elif v is None or isinstance(v, (bool, int, float)) or k == "index_updated":
                out[k] = v
            else:
                out[k] = self._walk_value(k, v, None)
        return out

    def _mask_fts_row(self, row: dict[str, Any]) -> dict[str, Any]:
        ref = row.get("ref")
        metadata_name = ref.get("Metadata") if isinstance(ref, dict) else None
        dotted = (self._classify(metadata_name) if metadata_name else None) or (
            _dotted_from_full_name(row.get("type"))
        )
        masked_ref = self._walk_value("ref", ref, dotted) if isinstance(ref, dict) else ref
        ref_masked = isinstance(ref, dict) and masked_ref != ref
        # The snippet is masked whole, of an open type too: it quotes any attribute of the hit
        # (a closed counterparty's name, a contact), which the row does not say and the masker
        # cannot know. The type, the ref and an open type's presentation stay readable.
        ref_open = not ref_masked and _is_open_type(dotted, self._policy)
        out: dict[str, Any] = {}
        for k, v in row.items():
            if k == "type":
                out[k] = v
            elif k == "ref":
                out[k] = masked_ref
            elif not isinstance(v, str) or not v.strip():
                out[k] = self._walk_value(k, v, dotted)
            elif k != "description" and ref_open:
                out[k] = v
            elif k == "presentation" and ref_masked and v == ref.get("Presentation"):
                out[k] = masked_ref["Presentation"]  # the ref's own pseudonym: joinable
            else:
                out[k] = self._pseudonym_for_field(
                    metadata_name if k == "presentation" and metadata_name else k, v
                )
        return out

    def _mask_related_envelope(self, value: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "data":
                out[k] = [self._mask_related_row(row) for row in v]
            elif k in _RELATED_STRUCTURAL_KEYS:
                out[k] = v
            else:
                out[k] = self._walk_value(k, v, None)
        return out

    def _mask_related_row(self, row: dict[str, Any]) -> dict[str, Any]:
        ref = row.get("ref")
        metadata_name = ref.get("Metadata") if isinstance(ref, dict) else None
        doc_type = row.get("type")
        dotted = (self._classify(metadata_name) if metadata_name else None) or (
            self._dotted_type(doc_type, "document") if isinstance(doc_type, str) else None
        )
        closed = dotted in self._policy.mask_type_exact
        out: dict[str, Any] = {}
        for k, v in row.items():
            if k in ("type", "found_in", "deleted"):
                out[k] = v
            elif k == "number" and closed:
                out[k] = self._force_mask("Number", v)  # a closed document type: number too
            elif k in _RELATED_FIELD_ALIASES:
                out[k] = self._mask_scalar(_RELATED_FIELD_ALIASES[k], v, dotted)
            else:
                out[k] = self._walk_value(k, v, dotted)
        return out

    # --- Internals: scalar / reference masking -----------------------------------

    def _field_types(self, key: str) -> list[str] | None:
        if not self._field_type_lookup:
            return None
        try:
            return self._field_type_lookup(key)
        except Exception:
            log.debug("field_type_lookup failed for %r", key, exc_info=True)
            return None

    def _mask_scalar(
        self, key: str | None, value: Any, context: str | None, *, open_names: bool = True
    ) -> Any:
        if value is None or isinstance(value, bool):
            return value

        if isinstance(value, str) and not value.strip():
            # Empty/blank — nothing to hide, in every branch (binary, forced, open).
            return value

        types = self._field_types(key) if key else None
        if types and any(t in _BINARY_TYPES for t in types):
            return self._pseudonym_for_field(key, value)

        normalized = _normalize_field_name(key) if key else ""
        forced = bool(key) and (
            _matches_pattern(normalized, self._policy.force_mask_fields)
            or _has_forced_word(key, self._policy.forced_mask_substrings)
        )
        if forced:
            return self._pseudonym_for_field(key, value)

        # Per-type rules come AFTER the global forced check: a per-type allow can never
        # open a forced-substring field (ІПН, phone, address, comment, ...).
        if _matches_type_field(context, normalized, self._policy.force_mask_type_fields):
            return self._pseudonym_for_field(key, value)

        if _matches_type_field(context, normalized, self._policy.allow_plain_type_fields):
            return value

        if key and _matches_pattern(normalized, self._policy.allow_plain_fields):
            return value

        if open_names and key in self._policy.open_field_names:
            return value

        if isinstance(value, (int, float)):
            return value

        if not isinstance(value, str):
            return value

        if types and any(t in _NUMERIC_TYPES for t in types):
            return value

        if _UUID_RE.match(value):
            return value

        # A whole type closed by mask_type_exact masks every value of its rows, dates included.
        if _looks_like_date(value) and context not in self._policy.mask_type_exact:
            return value

        row_open = (
            context is not None
            and context in self._policy.open_type_exact
            and context not in self._policy.mask_type_exact
        )
        if row_open and not _looks_like_code(value):
            return value

        return self._pseudonym_for_field(key, value)

    def _mask_reference(self, ref: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        metadata_name = ref.get("Metadata")
        uuid = ref.get("Data")
        dotted = self._classify(metadata_name) if metadata_name else None
        # A per-type Mask rule on Description (Наименование) of an open type: the presentation
        # of its references is that very name.
        if (not force and dotted not in self._index.name_masked_types
                and _is_open_type(dotted, self._policy)):
            return ref

        presentation = ref.get("Presentation")
        if not presentation or (isinstance(presentation, str) and not presentation.strip()):
            # Nothing to leak — an empty/blank/missing Presentation carries no value either way.
            return ref

        if metadata_name and uuid is not None:
            key = f"{metadata_name}\x1f{uuid}"
            cache_key = ("ref", key)
            pseudo = self._cache.get(cache_key)
            if pseudo is None:
                pseudo = self._new_pseudonym(metadata_name, key)
                self._cache[cache_key] = pseudo
                self._pseudo_to_uuid[pseudo] = str(uuid)
                self._pseudo_to_original[pseudo] = presentation
                self._original_to_pseudo[presentation] = pseudo
        else:
            # No UUID to key on (or no metadata at all -> unknown type, mask conservatively):
            # pseudonymize the Presentation TEXT itself, so `unmask_arguments` maps the
            # pseudonym back to that text rather than to a UUID BAS never gave us.
            prefix = metadata_name or "НевідомийТип"
            key = f"{prefix}\x1f{presentation}"
            cache_key = ("ref-presentation", key)
            pseudo = self._cache.get(cache_key)
            if pseudo is None:
                pseudo = self._new_pseudonym(prefix, key)
                self._cache[cache_key] = pseudo
                self._pseudo_to_original[pseudo] = presentation
                self._original_to_pseudo[presentation] = pseudo

        out = dict(ref)
        out["Presentation"] = pseudo
        # An account ref carries Code/Description beside its Presentation; a ref that is masked
        # must not leak the same text through them (open account refs never get here).
        if isinstance(out.get("Description"), str) and out["Description"].strip():
            out["Description"] = pseudo
        if isinstance(out.get("Code"), str) and out["Code"].strip():
            out["Code"] = self._pseudonym_for_field("Code", out["Code"])
        return out

    def _mask_value_reference(self, ref: dict[str, Any]) -> dict[str, Any]:
        metadata_name = ref.get("Metadata")
        dotted = self._classify(metadata_name) if metadata_name else None
        if _is_open_type(dotted, self._policy):
            return ref

        out = dict(ref)
        presentation = ref.get("Presentation")
        if presentation and (not isinstance(presentation, str) or presentation.strip()):
            out["Presentation"] = self._pseudonym_for_field(
                metadata_name or "Presentation", presentation
            )

        value = ref.get("Value")
        if isinstance(value, str) and value.strip() and not _looks_like_identifier_value(value):
            out["Value"] = self._pseudonym_for_field(metadata_name or "Value", value)

        return out

    def _pseudonym_for_field(self, field_name: str | None, value: Any) -> str:
        text = value if isinstance(value, str) else str(value)
        name = field_name or "Поле"
        key = f"{name}\x1f{text}"
        cache_key = ("field", key)
        pseudo = self._cache.get(cache_key)
        if pseudo is None:
            pseudo = self._new_pseudonym(name, key)
            self._cache[cache_key] = pseudo
            self._pseudo_to_original[pseudo] = text
            self._original_to_pseudo[text] = pseudo
        return pseudo

    def _new_pseudonym(self, prefix: str, key: str) -> str:
        digest = hmac.new(self._secret, key.encode("utf-8"), hashlib.sha256).digest()
        code = _b32(digest, self._code_length)
        return f"{prefix}·{code}"

    # --- Internals: argument unmasking ---------------------------------------------

    def _unmask_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._unmask_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._unmask_value(v) for v in value]
        if isinstance(value, str):
            return self._unmask_string(value)
        return value

    def _unmask_string(self, text: str) -> str:
        if "·" not in text:
            return text
        replacements: list[tuple[str, str]] = []
        for pseudo, uuid in self._pseudo_to_uuid.items():
            if pseudo in text:
                replacements.append((pseudo, uuid))
        for pseudo, original in self._pseudo_to_original.items():
            if pseudo not in self._pseudo_to_uuid and pseudo in text:
                replacements.append((pseudo, original))
        if not replacements:
            return text
        replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
        for pseudo, repl in replacements:
            text = text.replace(pseudo, repl)
        return text


_WORD_OR_DOT_RE = re.compile(r"[\w·]")


class StreamRehydrator:
    """Stateful, streaming-safe wrapper around `Masker.rehydrate_text` for SSE text
    deltas: a pseudonym can be split across two `feed()` calls, so this buffers the
    trailing text that might still be an in-progress pseudonym instead of emitting it
    raw. `known_pseudonyms()` is fixed at each call (grows only as `Masker.mask()`
    generates new ones), so "might still be forming" is decided exactly, not guessed —
    a suffix is held back only while it is a genuine strict prefix of some pseudonym
    this masker has actually produced."""

    def __init__(self, masker: Masker) -> None:
        self._masker = masker
        self._buffer = ""
        # Last raw char already emitted: the word-boundary context of the next chunk.
        self._prev = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        hold = self._pending_prefix_len(self._buffer)
        if hold == 0:
            safe, self._buffer = self._buffer, ""
        else:
            safe, self._buffer = self._buffer[:-hold], self._buffer[-hold:]
        # The held text follows `safe` in the final output: a bare code glued to it is not a
        # whole word, so it is passed as lookahead context.
        return self._emit(safe, self._buffer[:1])

    def flush(self) -> str:
        remaining, self._buffer = self._buffer, ""
        return self._emit(remaining)

    def _emit(self, raw: str, after: str = "") -> str:
        if not raw:
            return ""
        out = self._masker.rehydrate_text(raw, before=self._prev, after=after)
        self._prev = raw[-1]
        return out

    def _pending_prefix_len(self, text: str) -> int:
        known = self._masker.known_pseudonyms()
        if not known:
            return 0
        held = 0
        max_len = self._masker.max_known_pseudonym_length()
        limit = min(len(text), max_len - 1) if max_len > 1 else 0
        for length in range(limit, 0, -1):
            suffix = text[-length:]
            if any(p != suffix and p.startswith(suffix) for p in known):
                held = length
                break
        # A trailing word that is (a prefix of) a bare code is held too, even when complete:
        # the next chunk may extend it into a longer token that must stay as written.
        codes = self._masker.bare_codes()
        max_code = max((len(c) for c in codes), default=0)
        for length in range(min(len(text), max_code), held, -1):
            start = len(text) - length
            before = text[start - 1] if start else self._prev
            if before and _WORD_OR_DOT_RE.match(before):
                continue
            word = text[start:]
            if any(c.startswith(word) for c in codes):
                return length
        return held


# --- Optional async helper: build a TypeClassifier from a live MCBPClient ---------

def _merge_inventory_items(mapping: dict[str, list[str]], prefix: str, items: Any) -> None:
    # Keep every kind a bare name has, in merge order: the first entry is the kind that claims
    # the name for a plain lookup (see _INVENTORY_MERGE_ORDER); the rest serve kind hints.
    for item in items or ():
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            dotted = f"{prefix}{name}"
            kinds = mapping.setdefault(name, [])
            if dotted not in kinds:
                kinds.append(dotted)


class _InventoryClassifier:
    """`TypeClassifier` over the metadata inventory: called with a bare name it answers the
    name's first-claiming kind; `all_types(name)` lists every kind the name has."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping

    def __call__(self, name: str) -> str | None:
        kinds = self._mapping.get(name)
        return kinds[0] if kinds else None

    def all_types(self, name: str) -> tuple[str, ...]:
        return tuple(self._mapping.get(name, ()))


# The classifier maps BARE names, and a register often shares its name with a reference type —
# a chart of accounts and its accounting register both called e.g. Хозрасчетный. A reference
# `Metadata` is what row values carry, so reference-bearing kinds claim a name first and
# registers/constants (never a row value's `Metadata`) only take what is left. Without this an
# account ref would classify as a register and come out masked.
_INVENTORY_MERGE_ORDER: tuple[str, ...] = tuple(
    sorted(_INVENTORY_KIND_PREFIX,
           key=lambda key: key.endswith("registers") or key == "constants")
)


async def build_type_classifier(client: MCBPClient) -> TypeClassifier:
    """Builds a `TypeClassifier` (bare metadata name -> dotted type) from
    `MCBPClient.list_metadata("all")` — the same whole-configuration inventory call
    `MCBPClient.prefetch_metadata` already warms into its own cache, so this is cheap
    to call once per connection. Kept OUT of `Masker` itself: masking stays pure/sync,
    this is the one place that talks to BAS.

    Some BAS service builds don't include every kind in the `all` payload (observed:
    `chartsofaccounts` absent even though `GET /ai/v1/metadata/ChartsOfAccounts` answers
    fine) — for each kind `all` didn't return, this issues one extra per-kind
    `list_metadata` call and merges its `items`. A kind that call still can't produce
    (404/422, or a service build that plain doesn't support it) is skipped with a debug
    log rather than failing classifier construction — the classifier just won't
    recognize that kind's names, same as if it truly doesn't exist on this
    configuration. This is a one-time startup cost, run concurrently."""
    payload = await client.list_metadata("all")
    mapping: dict[str, list[str]] = {}
    items_by_key: dict[str, Any] = {}
    missing_keys: list[str] = list(_INVENTORY_KIND_PREFIX)
    if isinstance(payload, dict):
        missing_keys = []
        for inventory_key in _INVENTORY_KIND_PREFIX:
            if inventory_key not in payload:
                missing_keys.append(inventory_key)
                continue
            items_by_key[inventory_key] = payload.get(inventory_key)

    if missing_keys:

        async def _fetch_one(inventory_key: str) -> tuple[str, dict | BaseException]:
            route_name = _INVENTORY_KIND_ROUTE_NAME.get(inventory_key, inventory_key)
            try:
                return inventory_key, await client.list_metadata(route_name)
            except Exception as exc:  # noqa: BLE001 - tolerate any per-kind failure
                return inventory_key, exc

        results = await asyncio.gather(*(_fetch_one(k) for k in missing_keys))
        for inventory_key, result in results:
            if isinstance(result, BaseException):
                log.debug(
                    "list_metadata(%r) unavailable for this configuration/service build; "
                    "classifier will not recognize this kind's names",
                    _INVENTORY_KIND_ROUTE_NAME.get(inventory_key, inventory_key),
                    exc_info=result,
                )
                continue
            items = result.get("items") if isinstance(result, dict) else None
            items_by_key[inventory_key] = items

    for inventory_key in _INVENTORY_MERGE_ORDER:
        if inventory_key in items_by_key:
            _merge_inventory_items(mapping, _INVENTORY_KIND_PREFIX[inventory_key],
                                   items_by_key[inventory_key])

    return _InventoryClassifier(mapping)
