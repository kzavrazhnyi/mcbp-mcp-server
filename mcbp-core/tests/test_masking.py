"""Masker / MaskingPolicy: reference + field pseudonymization, argument unmasking,
text rehydration (including streaming), and policy overrides. Pure/sync — no BAS,
no network, matches the `mcbp-core` no-network testing convention."""
from __future__ import annotations

from mcbp_core.masking import Masker, MaskingPolicy, build_type_classifier


def _classifier(name: str) -> str | None:
    return {
        "Контрагенты": "Справочник.Контрагенты",
        "Номенклатура": "Справочник.Номенклатура",
        "ЗаказПокупателя": "Документ.ЗаказПокупателя",
        "Валюты": "Справочник.Валюты",
        "ВидыКонтрагентов": "Перечисление.ВидыКонтрагентов",
        "ТипыДенежныхСредств": "Перечисление.ТипыДенежныхСредств",
        "ДенежныеСредства": "РегистрНакопления.ДенежныеСредства",
    }.get(name)


def _ref(metadata: str, uuid: str, presentation: str) -> dict:
    return {"Presentation": presentation, "Data": uuid, "Metadata": metadata}


# --- Reference masking ---------------------------------------------------------------

def test_reference_masking_by_metadata():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref("Контрагенты", "uuid-1", "Альфа Трейд, ТОВ"),
                "Description": "Альфа Трейд, ТОВ",
                "Code": "000000002",
            }
        ],
        "cursor": None,
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Ref"]["Presentation"] != "Альфа Трейд, ТОВ"
    assert row["Ref"]["Presentation"].startswith("Контрагенты·")
    assert row["Ref"]["Data"] == "uuid-1"
    assert row["Ref"]["Metadata"] == "Контрагенты"
    assert row["Description"] != "Альфа Трейд, ТОВ"
    assert row["Code"] != "000000002"


def test_same_name_different_uuid_do_not_collide():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {"Ref": _ref("Контрагенты", "uuid-1", "Альфа Трейд, ТОВ")},
            {"Ref": _ref("Контрагенты", "uuid-2", "Альфа Трейд, ТОВ")},
        ],
        "cursor": None,
    }
    result = masker.mask(payload)
    p1 = result["data"][0]["Ref"]["Presentation"]
    p2 = result["data"][1]["Ref"]["Presentation"]
    assert p1 != p2

    # Deterministic: masking the SAME (type, uuid) again reproduces the same pseudonym.
    again = masker.mask({"data": [{"Ref": _ref("Контрагенты", "uuid-1", "Альфа Трейд, ТОВ")}]})
    assert again["data"][0]["Ref"]["Presentation"] == p1


def test_unknown_type_fallback_masks_conservatively():
    masker = Masker(type_classifier=_classifier)
    payload = {"data": [{"Ref": _ref("НевідомийТип", "uuid-9", "Щось Незрозуміле")}]}
    result = masker.mask(payload)
    assert result["data"][0]["Ref"]["Presentation"] != "Щось Незрозуміле"
    assert result["data"][0]["Ref"]["Presentation"].startswith("НевідомийТип·")


def test_reference_without_uuid_masks_presentation_by_metadata_and_text():
    masker = Masker(type_classifier=_classifier)
    ref = {"Presentation": "Хтось Незнайомий", "Data": None, "Metadata": "Контрагенты"}
    result = masker.mask({"data": [{"Ref": ref}]})
    pseudo = result["data"][0]["Ref"]["Presentation"]
    assert pseudo != "Хтось Незнайомий"
    assert pseudo.startswith("Контрагенты·")

    args = masker.unmask_arguments({"q": pseudo})
    assert args["q"] == "Хтось Незнайомий"


def test_reference_without_metadata_or_uuid_masks_conservatively():
    masker = Masker(type_classifier=_classifier)
    ref = {"Presentation": "Дещо Невідоме", "Data": None, "Metadata": None}
    result = masker.mask({"data": [{"Ref": ref}]})
    pseudo = result["data"][0]["Ref"]["Presentation"]
    assert pseudo != "Дещо Невідоме"
    assert pseudo.startswith("НевідомийТип·")

    args = masker.unmask_arguments({"q": pseudo})
    assert args["q"] == "Дещо Невідоме"


# --- Open catalog rows -----------------------------------------------------------

def test_open_classifier_catalog_rows_stay_plain():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref("Номенклатура", "nom-1", "Стілець офісний"),
                "Description": "Стілець офісний",
                "Code": "15",
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Ref"]["Presentation"] == "Стілець офісний"
    assert row["Description"] == "Стілець офісний"
    assert row["Code"] == "15"


def test_sensitive_name_overrides_open_catalog():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref("Номенклатура", "nom-2", "Товар Х"),
                "Description": "Товар Х",
                "KodPoEDRPOU": "314159265",
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Description"] == "Товар Х"
    assert row["KodPoEDRPOU"] != "314159265"


def test_code_like_string_masked_even_in_open_catalog():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref("Номенклатура", "nom-3", "Товар Y"),
                "Артикул": "00099",
                "Серия": "1234567890123456",
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Артикул"] != "00099"
    assert row["Серия"] != "1234567890123456"


# --- Numbers / dates / booleans -------------------------------------------------------

def test_numbers_and_dates_stay_open():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref("ЗаказПокупателя", "doc-1", "ЗП-00001"),
                "Number": "ЗП-00001",
                "Date": "04.05.2026 00:00:00",
                "СуммаДокумента": 128000.5,
                "Posted": True,
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Number"] == "ЗП-00001"
    assert row["Date"] == "04.05.2026 00:00:00"
    assert row["СуммаДокумента"] == 128000.5
    assert row["Posted"] is True


# --- Aggregates --------------------------------------------------------------------

def test_aggregates_numbers_open_group_masked():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "aggregates": [
            {"count": 2, "sum_СуммаДокумента": 1234.5, "group": "Альфа Трейд, ТОВ"},
            {"count": 1, "sum_СуммаДокумента": 42.0, "group": "Бета Постач, ТОВ"},
        ],
        "groupby": "Контрагент",
    }
    result = masker.mask(payload)
    rows = result["aggregates"]
    assert rows[0]["count"] == 2
    assert rows[0]["sum_СуммаДокумента"] == 1234.5
    assert rows[0]["group"] != "Альфа Трейд, ТОВ"
    assert rows[1]["group"] != "Бета Постач, ТОВ"
    assert rows[0]["group"] != rows[1]["group"]


# --- get_object shape (list of single-key dicts) --------------------------------------

def test_get_object_list_of_single_key_dicts():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "metadata": "catalog",
        "type": "Контрагенты",
        "data": [
            {"Kod": "000000002"},
            {"Naimenovanie": "Альфа Трейд, ТОВ"},
            {"Pokupatel": True},
            {"KodPoEDRPOU": "314159265"},
        ],
    }
    result = masker.mask(payload)
    rows = {list(item.keys())[0]: list(item.values())[0] for item in result["data"]}
    assert rows["Kod"] != "000000002"
    assert rows["Naimenovanie"] != "Альфа Трейд, ТОВ"
    assert rows["Pokupatel"] is True
    assert rows["KodPoEDRPOU"] != "314159265"


# --- get_object DOCUMENTED shape (§4.10: `data` is a dict, not a list) ----------------
# The mcbp-core mock returns the list-of-single-key-dicts shape above, but the real BAS
# `GET /ai/v1/object/{metadata}/{type}/{id}` route documented in
# .claude/refs/onec-tool-contract.md §4.10 returns `data` as a flat dict of attributes
# (plus tabular sections as lists of row dicts). Both shapes must mask correctly.

def test_get_object_documented_dict_shape_with_tabular_section():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "metadata": "catalog",
        "type": "Контрагенты",
        "data": {
            "Код": "000000002",
            "Наименование": "Альфа Трейд, ТОВ",
            "КодПоЕДРПОУ": "314159265",
            "СуммаЛимита": 50000.0,
            "ДоговораКонтрагентов": [
                {
                    "Ref": _ref("Договоры", "dogovor-1", "Договір №1"),
                    "Телефон": "+380501234567",
                    "СуммаДоговора": 12345.0,
                },
            ],
        },
    }
    result = masker.mask(payload)
    data = result["data"]
    assert data["Код"] != "000000002"
    assert data["Наименование"] != "Альфа Трейд, ТОВ"
    assert data["КодПоЕДРПОУ"] != "314159265"
    assert data["СуммаЛимита"] == 50000.0

    row = data["ДоговораКонтрагентов"][0]
    assert row["Ref"]["Presentation"] != "Договір №1"
    assert row["Телефон"] != "+380501234567"
    assert row["СуммаДоговора"] == 12345.0

    assert result["metadata"] == "catalog"
    assert result["type"] == "Контрагенты"


# --- Structural envelope keys stay plain (self-correction hints) ----------------------

def test_envelope_structural_keys_stay_unmasked():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "metadata": "document",
        "type": "ЗаказПокупателя",
        "data": [{"Ref": _ref("ЗаказПокупателя", "doc-9", "ЗП-00009")}],
        "cursor": "e3f1d9b6-0009-4d6d-9d6d-0000000000",
        "truncated": False,
        "unknown_fields": ["Комментарий", "Покупатель"],
        "available_names": ["Комментарий", "Покупатель", "Дата"],
    }
    result = masker.mask(payload)
    assert result["metadata"] == "document"
    assert result["type"] == "ЗаказПокупателя"
    assert result["cursor"] == payload["cursor"]
    assert result["truncated"] is False
    assert result["unknown_fields"] == ["Комментарий", "Покупатель"]
    assert result["available_names"] == ["Комментарий", "Покупатель", "Дата"]


# --- Argument unmasking --------------------------------------------------------------

def test_unmask_arguments_reference_roundtrip():
    masker = Masker(type_classifier=_classifier)
    masked = masker.mask({"data": [{"Ref": _ref("Контрагенты", "uuid-42", "Альфа Трейд, ТОВ")}]})
    pseudo = masked["data"][0]["Ref"]["Presentation"]

    args = masker.unmask_arguments({"filters": {"Контрагент": pseudo}, "type": "ЗаказПокупателя"})
    assert args["filters"]["Контрагент"] == "uuid-42"
    assert args["type"] == "ЗаказПокупателя"


def test_unmask_arguments_field_roundtrip():
    masker = Masker(type_classifier=_classifier)
    masked = masker.mask(
        {"metadata": "catalog", "type": "Контрагенты", "data": [{"Naimenovanie": "Альфа Трейд, ТОВ"}]}
    )
    pseudo = masked["data"][0]["Naimenovanie"]

    args = masker.unmask_arguments({"q": pseudo})
    assert args["q"] == "Альфа Трейд, ТОВ"


# --- Text rehydration ---------------------------------------------------------------

def test_rehydrate_text_replaces_longest_pseudonym_first():
    masker = Masker(type_classifier=_classifier)
    # White-box: two pseudonyms where the shorter is a literal prefix of the longer,
    # to prove rehydrate_text does not let the short one swallow part of the long one.
    masker._pseudo_to_original["ABC·X"] = "SHORT"
    masker._pseudo_to_original["ABC·XY"] = "LONG"

    text = masker.rehydrate_text("see ABC·XY here")
    assert text == "see LONG here"


def test_split_chunk_streaming_rehydration():
    masker = Masker(type_classifier=_classifier)
    masked = masker.mask({"data": [{"Ref": _ref("Контрагенты", "uuid-7", "Гамма Опт, ТОВ")}]})
    pseudo = masked["data"][0]["Ref"]["Presentation"]

    sr = masker.stream_rehydrator()
    out1 = sr.feed("Клієнт: ")
    assert out1 == "Клієнт: "

    split = len(pseudo) // 2
    out2 = sr.feed(pseudo[:split])
    assert out2 == ""  # held back — might still be forming a known pseudonym

    out3 = sr.feed(pseudo[split:] + " дякую")
    assert "Гамма Опт, ТОВ" in out3
    assert pseudo not in out3

    assert sr.flush() == ""


# --- Value/enum reference masking (`{"Presentation","Value","Metadata"}`, no `Data`) --
# Real shape BAS returns for e.g. `ВидКонтрагента`, contact-info `Тип` — captured live
# against basmbdemo (get_object on a Контрагенты row), trimmed and anonymized here.

def test_enum_value_reference_open_type_stays_plain():
    masker = Masker(type_classifier=_classifier)
    ref = {"Presentation": "Юридична особа", "Value": "ЮридическоеЛицо", "Metadata": "ВидыКонтрагентов"}
    result = masker.mask({"data": [{"ВидКонтрагента": ref}]})
    assert result["data"][0]["ВидКонтрагента"] == ref


def test_non_open_value_reference_masks_presentation_keeps_metadata_open():
    masker = Masker(type_classifier=_classifier)
    # Metadata unresolvable by the classifier -> conservative non-open, same as
    # `_mask_reference`'s handling of an unknown `Metadata`.
    ref = {"Presentation": "Телефон", "Value": "Телефон", "Metadata": "НевідомийТипЗначення"}
    result = masker.mask({"data": [{"Тип": ref}]})
    row = result["data"][0]["Тип"]
    assert row["Metadata"] == "НевідомийТипЗначення"
    assert row["Presentation"] != "Телефон"
    assert row["Presentation"].startswith("НевідомийТипЗначення·")
    # `Value` is a bare enum-literal token (no whitespace) -- not human-readable free text,
    # nothing extra to leak beyond what `Presentation` already carried -> left open.
    assert row["Value"] == "Телефон"


def test_non_open_value_reference_masks_human_readable_value():
    masker = Masker(type_classifier=_classifier)
    ref = {
        "Presentation": "Приватний підприємець",
        "Value": "Приватний підприємець з обмеженою відповідальністю",
        "Metadata": "НевідомийТипЗначення",
    }
    result = masker.mask({"data": [{"Тип": ref}]})
    row = result["data"][0]["Тип"]
    assert row["Metadata"] == "НевідомийТипЗначення"
    assert row["Presentation"] != ref["Presentation"]
    assert row["Value"] != ref["Value"]


def test_value_reference_unmask_roundtrip():
    masker = Masker(type_classifier=_classifier)
    ref = {
        "Presentation": "Юридична особа",
        "Value": "внутрішнє значення з пробілом",
        "Metadata": "НевідомийТипЗначення",
    }
    masked = masker.mask({"data": [{"Тип": ref}]})
    row = masked["data"][0]["Тип"]
    assert row["Presentation"] != ref["Presentation"]
    assert row["Value"] != ref["Value"]

    args = masker.unmask_arguments({"filters": {"Тип": row["Value"]}, "q": row["Presentation"]})
    assert args["filters"]["Тип"] == ref["Value"]
    assert args["q"] == ref["Presentation"]


# --- Register balance envelope without a `metadata` key (`{"type","data"}`) ----------
# Real shape captured live from `GET /ai/v1/registers/{type}/balance` — trimmed to one
# row, org/bank names replaced with fakes; field/type names kept exact.

def test_register_balance_envelope_type_open_dimensions_masked_numbers_open():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "type": "ДенежныеСредства",
        "data": [
            {
                "Организация": _ref("Организации", "org-1", "Тестова Компанія, ТОВ"),
                "ТипДенежныхСредств": {
                    "Presentation": "Безготівкові",
                    "Value": "Безналичные",
                    "Metadata": "ТипыДенежныхСредств",
                },
                "БанковскийСчетКасса": _ref(
                    "БанковскиеСчета", "acc-1", "260000000000000, у тестовому банку"
                ),
                "Валюта": _ref("Валюты", "cur-1", "грн."),
                "СуммаОстаток": 73384,
                "СуммаВалОстаток": 73384,
            },
        ],
    }
    result = masker.mask(payload)
    assert result["type"] == "ДенежныеСредства"

    row = result["data"][0]
    assert row["Организация"]["Presentation"] != "Тестова Компанія, ТОВ"
    assert row["Организация"]["Metadata"] == "Организации"
    assert row["ТипДенежныхСредств"] == payload["data"][0]["ТипДенежныхСредств"]
    assert row["БанковскийСчетКасса"]["Presentation"] != "260000000000000, у тестовому банку"
    assert row["Валюта"]["Presentation"] == "грн."  # Справочник.Валюты is an open catalog
    assert row["СуммаОстаток"] == 73384
    assert row["СуммаВалОстаток"] == 73384


def test_data_row_with_type_and_data_keys_is_not_mistaken_for_an_envelope():
    """A row that happens to carry its own `type`/`data` fields, ALONGSIDE other fields,
    must still be masked — the loosened metadata-less envelope detection only fires when
    the dict's keys are exactly the envelope's own."""
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref("Контрагенты", "uuid-77", "Дельта Логістик, ТОВ"),
                "type": "some literal value",
                "data": "additional literal value",
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Ref"]["Presentation"] != "Дельта Логістик, ТОВ"
    assert row["type"] != "some literal value"
    assert row["data"] != "additional literal value"


# --- Policy overrides -----------------------------------------------------------------

def test_allow_plain_field_pattern_overrides_default_masking():
    default_masker = Masker(type_classifier=_classifier)
    default_payload = {"data": [{"Ref": _ref("Контрагенты", "uuid-5", "X"), "ВнутреннийКод": "abc"}]}
    default_result = default_masker.mask(default_payload)
    assert default_result["data"][0]["ВнутреннийКод"] != "abc"

    policy = MaskingPolicy.from_dict({"allow_plain_fields": ["*Код"]})
    open_masker = Masker(type_classifier=_classifier, policy=policy)
    open_result = open_masker.mask(default_payload)
    assert open_result["data"][0]["ВнутреннийКод"] == "abc"


def test_force_mask_fields_override_adds_to_defaults():
    policy = MaskingPolicy.from_dict({"force_mask_fields": ["СпеціальнеПоле"]})
    masker = Masker(type_classifier=_classifier, policy=policy)
    payload = {"data": [{"Ref": _ref("Номенклатура", "nom-9", "Товар"), "СпеціальнеПоле": "42"}]}
    result = masker.mask(payload)
    # Номенклатура is an open catalog (row strings default open) — the custom
    # force_mask entry must still win.
    assert result["data"][0]["Ref"]["Presentation"] == "Товар"
    assert result["data"][0]["СпеціальнеПоле"] != "42"


# --- Empty/blank values carry nothing to hide, in every branch ------------------------

def test_empty_string_in_forced_field_stays_unmasked():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "УдалитьАдресЭП": "",
                "Телефон": "",
                "КодПоЕДРПОУ": "",
            }
        ],
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["УдалитьАдресЭП"] == ""
    assert row["Телефон"] == ""
    assert row["КодПоЕДРПОУ"] == ""


def test_whitespace_only_string_in_forced_field_stays_unmasked():
    masker = Masker(type_classifier=_classifier)
    payload = {"data": [{"Телефон": "   "}]}
    result = masker.mask(payload)
    assert result["data"][0]["Телефон"] == "   "


def test_non_empty_forced_field_still_masked():
    masker = Masker(type_classifier=_classifier)
    payload = {"data": [{"Телефон": "+380501234567"}]}
    result = masker.mask(payload)
    assert result["data"][0]["Телефон"] != "+380501234567"


def test_none_value_in_forced_field_stays_none():
    masker = Masker(type_classifier=_classifier)
    payload = {"data": [{"Телефон": None}]}
    result = masker.mask(payload)
    assert result["data"][0]["Телефон"] is None


def test_empty_presentation_reference_stays_unchanged():
    masker = Masker(type_classifier=_classifier)
    payload = {"data": [{"Ref": _ref("Контрагенты", "uuid-empty", "")}]}
    result = masker.mask(payload)
    assert result["data"][0]["Ref"]["Presentation"] == ""


def test_blank_presentation_value_reference_stays_unchanged():
    masker = Masker(type_classifier=_classifier)
    ref = {"Presentation": "   ", "Value": "", "Metadata": "НевідомийТипЗначення"}
    result = masker.mask({"data": [{"ВидКонтрагента": ref}]})
    row = result["data"][0]["ВидКонтрагента"]
    assert row["Presentation"] == "   "
    assert row["Value"] == ""


def test_mask_text_empty_string_returns_empty():
    masker = Masker(type_classifier=_classifier)
    assert masker.mask_text("") == ""


def test_empty_original_never_registered_in_pseudonym_maps():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Телефон": "",
                "Ref": _ref("Контрагенты", "uuid-blank", ""),
            }
        ],
    }
    masker.mask(payload)
    assert "" not in masker._original_to_pseudo
    assert all(original != "" for original in masker._pseudo_to_original.values())


# --- build_type_classifier: kinds absent from `all` ----------------------------------

class _FakeClient:
    """Minimal stand-in for MCBPClient.list_metadata, keyed by the exact `kind` arg."""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def list_metadata(self, kind: str) -> dict:
        self.calls.append(kind)
        result = self._responses[kind]
        if isinstance(result, BaseException):
            raise result
        return result


_ALL_WITHOUT_CHARTSOFACCOUNTS = {
    "catalogs": [{"name": "Контрагенты", "synonym": "Контрагенты"}],
    "documents": [{"name": "ЗаказПокупателя", "synonym": "Заказ покупателя"}],
}


async def test_missing_kind_in_all_falls_back_to_per_kind_call_and_opens_reference():
    client = _FakeClient(
        {
            "all": dict(_ALL_WITHOUT_CHARTSOFACCOUNTS),
            "ChartsOfAccounts": {
                "metadata": "chartofaccounts",
                "count": 1,
                "items": [{"name": "Управленческий", "synonym": "Управленческий"}],
            },
        }
    )
    classify = await build_type_classifier(client)
    assert classify("Управленческий") == "ПланСчетов.Управленческий"
    assert classify("Контрагенты") == "Справочник.Контрагенты"

    masker = Masker(type_classifier=classify)
    result = masker.mask(_ref("Управленческий", "acct-1", "Управленческий"))
    assert result["Presentation"] == "Управленческий"


async def test_missing_kind_per_kind_call_failure_does_not_break_classifier():
    client = _FakeClient(
        {
            "all": dict(_ALL_WITHOUT_CHARTSOFACCOUNTS),
            "ChartsOfAccounts": RuntimeError("404 unsupported on this service build"),
        }
    )
    classify = await build_type_classifier(client)
    assert classify("Управленческий") is None
    assert classify("Контрагенты") == "Справочник.Контрагенты"


async def test_kind_already_present_in_all_skips_extra_call():
    all_payload = dict(_ALL_WITHOUT_CHARTSOFACCOUNTS)
    all_payload["chartsofaccounts"] = [{"name": "Управленческий", "synonym": "Управленческий"}]
    client = _FakeClient({"all": all_payload})
    classify = await build_type_classifier(client)
    assert classify("Управленческий") == "ПланСчетов.Управленческий"
    assert "ChartsOfAccounts" not in client.calls


def _ci_kind_classifier(name: str) -> str | None:
    return {
        "ВидыКонтактнойИнформации": "Справочник.ВидыКонтактнойИнформации",
    }.get(name)


def test_contact_information_kind_catalog_is_open_by_default():
    masker = Masker(type_classifier=_ci_kind_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref(
                    "ВидыКонтактнойИнформации", "cikind-1", "Телефон контрагента"
                ),
                "Description": "Телефон контрагента",
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Ref"]["Presentation"] == "Телефон контрагента"
    assert row["Description"] == "Телефон контрагента"


def test_contact_information_kind_row_own_forced_field_still_masked():
    masker = Masker(type_classifier=_ci_kind_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref(
                    "ВидыКонтактнойИнформации", "cikind-2", "Email контрагента"
                ),
                "Комментарий": "внутренняя заметка",
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Комментарий"] != "внутренняя заметка"


def test_unrelated_catalog_still_masked_alongside_contact_information_default():
    masker = Masker(type_classifier=_classifier)
    payload = {
        "data": [
            {
                "Ref": _ref("Контрагенты", "uuid-9", "Бета Логістик, ТОВ"),
                "Description": "Бета Логістик, ТОВ",
            }
        ]
    }
    result = masker.mask(payload)
    row = result["data"][0]
    assert row["Ref"]["Presentation"] != "Бета Логістик, ТОВ"
    assert row["Description"] != "Бета Логістик, ТОВ"
