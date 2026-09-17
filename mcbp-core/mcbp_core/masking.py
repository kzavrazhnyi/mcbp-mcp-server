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
import hmac
import json
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
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
_DEFAULT_OPEN_FIELD_NAMES: tuple[str, ...] = ("Number", "Номер", "Date", "Дата", "id", "Data")

# Case-insensitive substrings (whitespace-stripped, lowercased) of a field name that
# force masking no matter what — identifying data and free text, per the goal.
_DEFAULT_FORCED_SUBSTRINGS: tuple[str, ...] = (
    "инн", "інн", "ипн", "іпн", "inn",
    "кпп", "kpp",
    "едрпоу", "єдрпоу", "edrpou",
    "снилс", "snils",
    "огрн", "ogrn",
    "паспорт", "passport",
    "телефон", "phone",
    "email", "e-mail",
    "электроннаяпочта", "електроннапошта",
    "адрес", "адреса", "address",
    "датарождения", "датанароджен", "birthdate", "birthday", "dateofbirth",
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
}

# `list_metadata("all")` top-level keys (plural) -> dotted-type prefix. Used by
# `build_type_classifier` to turn the whole-configuration inventory into a TypeClassifier.
_INVENTORY_KIND_PREFIX: dict[str, str] = {
    "catalogs": "Справочник.",
    "documents": "Документ.",
    "informationregisters": "РегистрСведений.",
    "accumulationregisters": "РегистрНакопления.",
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
# 11 kinds on every BAS/1C configuration — not a configuration's own object names — so
# listing them here does not violate onec-universality. Some BAS service builds omit one
# or more of these kinds from the `all` inventory (observed: chartsofaccounts absent from
# `metadata/all` while `metadata/ChartsOfAccounts` answers fine); `build_type_classifier`
# falls back to a per-kind call for whichever key `all` didn't return.
_INVENTORY_KIND_ROUTE_NAME: dict[str, str] = {
    "catalogs": "Catalogs",
    "documents": "Documents",
    "informationregisters": "InformationRegisters",
    "accumulationregisters": "AccumulationRegisters",
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
_ENVELOPE_STRUCTURAL_KEYS = frozenset(
    {"unknown_fields", "available_names", "cursor", "truncated", "metadata", "type"}
)
_CODE_LIKE_RE = re.compile(r"^\d+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _normalize_field_name(name: str) -> str:
    return "".join(name.split()).lower()


def _contains_any(normalized: str, substrings: tuple[str, ...]) -> bool:
    return any(s and s in normalized for s in substrings)


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


def _looks_like_aggregate_envelope(value: dict[str, Any]) -> bool:
    return "aggregates" in value


def _is_open_type(dotted: str | None, policy: MaskingPolicy) -> bool:
    if dotted is None:
        return False
    if dotted in policy.open_type_exact:
        return True
    return any(dotted.startswith(prefix) for prefix in policy.open_type_prefixes)


def _b32(digest: bytes, length: int) -> str:
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")
    return encoded[:length] if length > 0 else encoded


# --- Masker ----------------------------------------------------------------------

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
        self._classify = type_classifier or (lambda _name: None)
        self._field_type_lookup = field_type_lookup
        self._code_length = code_length

        self._cache: dict[tuple[str, str], str] = {}
        self._pseudo_to_original: dict[str, str] = {}
        self._pseudo_to_uuid: dict[str, str] = {}
        self._original_to_pseudo: dict[str, str] = {}

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

    def unmask_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Replaces any known pseudonym found inside tool-call arguments — including
        values nested in `filters`/`f.*` dicts and plain `id` strings — with what the
        BAS route actually expects: the reference UUID for a masked reference
        pseudonym, the original text for a masked field pseudonym."""
        return {k: self._unmask_value(v) for k, v in arguments.items()}

    def rehydrate_text(self, text: str) -> str:
        """Replaces pseudonyms in free text with their REAL display value (for
        showing the human user), longest pseudonym first so one is never partially
        swallowed by a shorter one that happens to be its prefix."""
        if not text or "·" not in text:
            return text
        pairs = [(p, o) for p, o in self._pseudo_to_original.items() if p in text]
        if not pairs:
            return text
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        for pseudo, original in pairs:
            text = text.replace(pseudo, original)
        return text

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
            prefix = _KIND_DOTTED_PREFIX.get(kind.lower())
            if prefix:
                return f"{prefix}{type_name}"
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
        if isinstance(value, dict):
            if _looks_like_reference(value):
                return self._mask_reference(value)
            if _looks_like_value_reference(value):
                return self._mask_value_reference(value)
            if _looks_like_change_entry(value):
                return self._mask_change_entry(value, context)
            if _looks_like_aggregate_envelope(value):
                return self._mask_aggregate_envelope(value, context)
            if _looks_like_envelope(value):
                return self._mask_envelope(value, context)
            return {k: self._walk_value(k, v, context) for k, v in value.items()}
        if isinstance(value, list):
            if value and all(isinstance(item, dict) for item in value):
                return [
                    self._walk_value(None, item, self._row_context(item, context))
                    for item in value
                ]
            return [self._walk_value(key, item, context) for item in value]
        return self._mask_scalar(key, value, context)

    def _mask_envelope(self, value: dict[str, Any], fallback: str | None) -> dict[str, Any]:
        new_context = self._dotted_type(value.get("type"), value.get("metadata"))
        context = new_context or fallback
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "data":
                out[k] = self._mask_data(v, context)
            elif k in _ENVELOPE_STRUCTURAL_KEYS:
                out[k] = v
            else:
                out[k] = self._walk_value(k, v, fallback)
        return out

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

    def _mask_aggregate_envelope(
        self, value: dict[str, Any], context: str | None
    ) -> dict[str, Any]:
        groupby_field = value.get("groupby")
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
                else:
                    masked_row[k] = self._walk_value(k, v, context)
            masked_rows.append(masked_row)
        out["aggregates"] = masked_rows
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

    def _mask_scalar(self, key: str | None, value: Any, context: str | None) -> Any:
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
            or _contains_any(normalized, self._policy.forced_mask_substrings)
        )
        if forced:
            return self._pseudonym_for_field(key, value)

        if key and _matches_pattern(normalized, self._policy.allow_plain_fields):
            return value

        if key in self._policy.open_field_names:
            return value

        if isinstance(value, (int, float)):
            return value

        if not isinstance(value, str):
            return value

        if types and any(t in _NUMERIC_TYPES for t in types):
            return value

        if _UUID_RE.match(value):
            return value

        row_open = context is not None and context in self._policy.open_type_exact
        if row_open and not _looks_like_code(value):
            return value

        return self._pseudonym_for_field(key, value)

    def _mask_reference(self, ref: dict[str, Any]) -> dict[str, Any]:
        metadata_name = ref.get("Metadata")
        uuid = ref.get("Data")
        dotted = self._classify(metadata_name) if metadata_name else None
        if _is_open_type(dotted, self._policy):
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

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        hold = self._pending_prefix_len(self._buffer)
        if hold == 0:
            out = self._masker.rehydrate_text(self._buffer)
            self._buffer = ""
            return out
        safe, self._buffer = self._buffer[:-hold], self._buffer[-hold:]
        return self._masker.rehydrate_text(safe) if safe else ""

    def flush(self) -> str:
        remaining, self._buffer = self._buffer, ""
        return self._masker.rehydrate_text(remaining) if remaining else ""

    def _pending_prefix_len(self, text: str) -> int:
        known = self._masker.known_pseudonyms()
        if not known:
            return 0
        max_len = self._masker.max_known_pseudonym_length()
        limit = min(len(text), max_len - 1) if max_len > 1 else 0
        for length in range(limit, 0, -1):
            suffix = text[-length:]
            if any(p != suffix and p.startswith(suffix) for p in known):
                return length
        return 0


# --- Optional async helper: build a TypeClassifier from a live MCBPClient ---------

def _merge_inventory_items(mapping: dict[str, str], prefix: str, items: Any) -> None:
    for item in items or ():
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            mapping[name] = f"{prefix}{name}"


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
    mapping: dict[str, str] = {}
    missing_keys: list[str] = list(_INVENTORY_KIND_PREFIX)
    if isinstance(payload, dict):
        missing_keys = []
        for inventory_key, prefix in _INVENTORY_KIND_PREFIX.items():
            if inventory_key not in payload:
                missing_keys.append(inventory_key)
                continue
            _merge_inventory_items(mapping, prefix, payload.get(inventory_key))

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
            _merge_inventory_items(mapping, _INVENTORY_KIND_PREFIX[inventory_key], items)

    def classify(name: str) -> str | None:
        return mapping.get(name)

    return classify
