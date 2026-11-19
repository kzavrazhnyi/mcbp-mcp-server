"""Masker over the MCBP_AI >= 1.0.41.6 shapes: `data.additional[{name, value}]` of get_object,
property and contact-information tabular rows, full-text-search hits and related documents,
plus the marked per-type rules `<type>.additional:<name>` / `<type>.contact:<kind>`. Pure/sync."""
from __future__ import annotations

from mcbp_core.masking import Masker, MaskingPolicy

CATALOG = "Справочник.Контрагенты"


def _classifier(name: str) -> str | None:
    return {
        "Контрагенты": CATALOG,
        "Номенклатура": "Справочник.Номенклатура",
        "ЗаказПокупателя": "Документ.ЗаказПокупателя",
        "ДополнительныеРеквизитыИСведения": "ПланВидовХарактеристик.ДополнительныеРеквизитыИСведения",
        "ВидыКонтактнойИнформации": "Справочник.ВидыКонтактнойИнформации",
    }.get(name)


def _ref(metadata: str, uuid: str, presentation: str) -> dict:
    return {"Presentation": presentation, "Data": uuid, "Metadata": metadata}


def _masker(**rules: list[str]) -> Masker:
    # The catalog is OPEN by policy, so only the marked rules / forced names mask anything.
    policy = MaskingPolicy.default().merge({"open_type_exact": [CATALOG], **rules})
    return Masker(secret="t", policy=policy, type_classifier=_classifier)


def _object(additional: list[dict], **extra: object) -> dict:
    data = {"Ref": _ref("Контрагенты", "u-1", "Альфа"), "Description": "Альфа",
            "additional": additional, **extra}
    return {"metadata": "catalog", "type": "Контрагенты", "data": data}


def _values(result: dict) -> dict:
    return {item["name"]: item["value"] for item in result["data"]["additional"]}


# --- additional -------------------------------------------------------------------------

def test_additional_masked_by_name_rule_case_insensitive():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.additional:номер картки"])
    out = _values(masker.mask(_object([{"name": "Номер Картки", "value": "AB-77"},
                                       {"name": "Хобі", "value": "риболовля"}])))
    assert out["Номер Картки"] != "AB-77" and out["Номер Картки"].startswith("Значення·")
    assert out["Хобі"] == "риболовля"  # unmatched, open type: untouched


def test_additional_unmatched_untouched_and_names_stay_readable():
    masker = _masker()
    payload = _object([{"name": "Сайт", "value": "alpha.ua"}, {"name": "Рейтинг", "value": 5}])
    result = masker.mask(payload)
    assert result["data"]["additional"] == payload["data"]["additional"]


def test_additional_numeric_value_masked_by_rule():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.additional:Ліміт"])
    out = _values(masker.mask(_object([{"name": "Ліміт", "value": 150000},
                                       {"name": "Рейтинг", "value": 5}])))
    assert isinstance(out["Ліміт"], str) and out["Ліміт"].startswith("Значення·")
    assert out["Рейтинг"] == 5
    assert masker.unmask_arguments({"x": out["Ліміт"]}) == {"x": "150000"}


def test_additional_forced_name_masked_even_on_open_type():
    # The value used to be masked by the key `value`, so an ЄДРПОУ passed on an open type.
    masker = _masker()
    out = _values(masker.mask(_object([{"name": "ЄДРПОУ", "value": "12345678"}])))
    assert out["ЄДРПОУ"] != "12345678"


def test_additional_rule_name_with_dot_and_reference_value():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.additional:Відп. менеджер"])
    manager = _ref("Номенклатура", "n-1", "Іван")  # an open type, masked by the rule anyway
    out = _values(masker.mask(_object([{"name": "Відп. менеджер", "value": manager}])))
    assert out["Відп. менеджер"]["Presentation"] != "Іван"
    assert out["Відп. менеджер"]["Data"] == "n-1"


def test_additional_rule_of_another_type_does_not_apply():
    masker = _masker(force_mask_type_fields=["Справочник.Номенклатура.additional:Хобі"])
    assert _values(masker.mask(_object([{"name": "Хобі", "value": "x"}])))["Хобі"] == "x"


def test_additional_open_rule_opens_a_closed_type_value_but_not_a_forced_name():
    policy = MaskingPolicy.default().merge({"allow_plain_type_fields": [
        f"{CATALOG}.additional:Хобі", f"{CATALOG}.additional:Телефон"]})
    masker = Masker(secret="t", policy=policy, type_classifier=_classifier)
    out = _values(masker.mask(_object([{"name": "Хобі", "value": "x"},
                                       {"name": "Телефон", "value": "+380501112233"}])))
    assert out["Хобі"] == "x"
    assert out["Телефон"] != "+380501112233"


def test_additional_open_field_name_does_not_open_the_value():
    out = _values(Masker(secret="t", type_classifier=_classifier).mask(
        _object([{"name": "Номер", "value": "AA123456"}])))
    assert out["Номер"] != "AA123456"


def test_additional_item_with_an_extra_key_is_still_masked_and_keeps_the_key():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.additional:Ліміт"])
    items = [{"name": "Ліміт", "value": "AB-77", "kind": "info"},
             {"name": "Хобі", "value": "шахи", "kind": "info"}]
    result = masker.mask(_object(items))["data"]["additional"]
    assert [list(i) for i in result] == [["name", "value", "kind"]] * 2
    assert result[0]["value"] != "AB-77" and result[0]["kind"] == "info"
    assert result[1] == items[1]


# --- property / contact tabular rows ---------------------------------------------------

def test_property_tabular_row_masked_by_rule_found_by_shape():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.additional:Ліміт"])
    prop = _ref("ДополнительныеРеквизитыИСведения", "p-1", "Ліміт")
    other = _ref("ДополнительныеРеквизитыИСведения", "p-2", "Хобі")
    rows = [{"LineNumber": 1, "Свойство": prop, "Значение": 9000},
            {"LineNumber": 2, "Свойство": other, "Значение": "шахи"}]
    result = masker.mask(_object([], ДопРеквизиты=rows))["data"]["ДопРеквизиты"]
    assert result[0]["Свойство"] == prop and result[0]["Значение"] != 9000
    assert result[1]["Значение"] == "шахи"


def test_property_row_with_a_text_copy_column_masks_every_value_column():
    # BSP shape: Свойство + Значение + ТекстоваяСтрока (a long string kept in the text copy only).
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.additional:Ліміт"])
    prop = _ref("ДополнительныеРеквизитыИСведения", "p-1", "Ліміт")
    other = _ref("ДополнительныеРеквизитыИСведения", "p-2", "Хобі")
    rows = [{"НомерСтроки": 1, "Свойство": prop, "Значение": "", "ТекстоваяСтрока": "AB-77 секрет"},
            {"НомерСтроки": 2, "Свойство": prop, "Значение": "AB-77", "ТекстоваяСтрока": "AB-77"},
            {"НомерСтроки": 3, "Свойство": other, "Значение": "шахи", "ТекстоваяСтрока": "шахи"}]
    result = masker.mask(_object([], ДополнительныеРеквизиты=rows))["data"]["ДополнительныеРеквизиты"]
    assert result[0]["Свойство"] == prop and result[0]["НомерСтроки"] == 1
    assert result[0]["Значение"] == "" and "AB-77" not in result[0]["ТекстоваяСтрока"]
    assert "AB-77" not in result[1]["Значение"] and "AB-77" not in result[1]["ТекстоваяСтрока"]
    assert result[2] == rows[2]


def test_contact_kind_rule_masks_every_string_of_the_row():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.contact:Сайт"])
    kind = _ref("ВидыКонтактнойИнформации", "k-1", "Сайт")
    rows = [
        {"LineNumber": 1, "Вид": kind, "Представление": "alpha.ua", "ЗначенияПолей": "{...}"},
        {"LineNumber": 2, "Вид": _ref("ВидыКонтактнойИнформации", "k-2", "Інше"),
         "Представление": "відкрито"},
    ]
    result = masker.mask(_object([], КонтактнаяИнформация=rows))["data"]["КонтактнаяИнформация"]
    assert result[0]["Вид"] == kind and result[0]["LineNumber"] == 1
    assert result[0]["Представление"] != "alpha.ua"
    assert result[0]["ЗначенияПолей"] != "{...}"
    assert result[1]["Представление"] == "відкрито"


# --- full-text search -------------------------------------------------------------------

def _fts(type_: str, metadata: str, presentation: str, snippet: str) -> dict:
    return {"query": "Альфа", "index_up_to_date": True, "index_updated": "2026-11-01T00:00:00",
            "metadata_filter_applied": None, "total": 1, "count": 1, "truncated": False,
            "data": [{"type": type_, "ref": _ref(metadata, "u-9", presentation),
                      "presentation": presentation, "description": snippet}]}


def test_fts_closed_type_snippet_masked_whole_presentation_joins_ref():
    masker = Masker(secret="t", type_classifier=_classifier)
    result = masker.mask(_fts("Catalog.Контрагенты", "Контрагенты", "Альфа Трейд",
                              "Альфа Трейд код 12345678"))
    row = result["data"][0]
    assert row["type"] == "Catalog.Контрагенты"
    assert row["presentation"] == row["ref"]["Presentation"] != "Альфа Трейд"
    assert "12345678" not in row["description"] and "Альфа" not in row["description"]
    assert result["index_updated"] == "2026-11-01T00:00:00" and result["total"] == 1


def test_fts_open_type_snippet_masked_whole_in_a_fresh_session():
    # A fresh masker has never seen the supplier: a snippet kept readable would leak its name.
    masker = Masker(secret="t", type_classifier=_classifier)
    snippet = "Цвях 50 мм, Постачальник: Світанок, ТОВ, a@b.ua, тел +38 050 111 22 33"
    result = masker.mask(_fts("Catalog.Номенклатура", "Номенклатура", "Цвях 50 мм", snippet))
    row = result["data"][0]
    assert row["presentation"] == "Цвях 50 мм"  # an open type's own name stays readable
    assert row["ref"]["Presentation"] == "Цвях 50 мм" and row["type"] == "Catalog.Номенклатура"
    assert "Світанок" not in row["description"] and "111 22 33" not in row["description"]
    assert masker.rehydrate_text(row["description"]) == snippet


def test_fts_open_type_with_type_rules_snippet_masked_whole():
    policy = MaskingPolicy.default().merge(
        {"force_mask_type_fields": ["Справочник.Номенклатура.Артикул"]})
    masker = Masker(secret="t", policy=policy, type_classifier=_classifier)
    row = masker.mask(_fts("Catalog.Номенклатура", "Номенклатура", "Цвях", "Цвях A-1"))["data"][0]
    assert row["presentation"] == "Цвях" and row["description"] != "Цвях A-1"


def test_fts_open_type_with_only_a_marked_rule_snippet_masked_whole():
    for rule in (f"{CATALOG}.contact:Телефон", f"{CATALOG}.additional:Ліміт"):
        masker = _masker(force_mask_type_fields=[rule])
        row = masker.mask(_fts("Catalog.Контрагенты", "Контрагенты", "Альфа",
                               "Альфа тел 050 111"))["data"][0]
        assert row["presentation"] == "Альфа", rule
        assert "050 111" not in row["description"] and "Альфа" not in row["description"], rule


def test_fts_query_masks_values_known_as_pseudonyms():
    masker = Masker(secret="t", type_classifier=_classifier)
    result = masker.mask(_fts("Catalog.Контрагенты", "Контрагенты", "Альфа", "знайдено: Альфа"))
    assert result["query"] == result["data"][0]["ref"]["Presentation"]


# --- related documents ------------------------------------------------------------------

def _related(number: str) -> dict:
    return {"metadata": "catalog", "type": "Контрагенты", "id": "u-1", "sources": "all",
            "data": [{"type": "ЗаказПокупателя",
                      "ref": _ref("ЗаказПокупателя", "d-1", f"Замовлення {number}"),
                      "number": number, "date": "2026-05-04T00:00:00", "deleted": False,
                      "found_in": "Контрагент"}],
            "truncated": False, "scanned_sources": 12, "failed_sources": ["Document.X"]}


def test_related_rows_keep_structure_and_open_document_fields():
    payload = _related("ЗП-00001")
    assert Masker(secret="t", type_classifier=_classifier).mask(payload) == payload


def test_related_rows_of_a_closed_document_type_are_masked():
    policy = MaskingPolicy.default().merge({"mask_type_exact": ["Документ.ЗаказПокупателя"]})
    masker = Masker(secret="t", policy=policy, type_classifier=_classifier)
    result = masker.mask(_related("ЗП-00001"))
    row = result["data"][0]
    assert row["ref"]["Presentation"] != "Замовлення ЗП-00001"
    assert row["number"] != "ЗП-00001"
    assert row["type"] == "ЗаказПокупателя" and row["found_in"] == "Контрагент"
    assert row["date"] == "2026-05-04T00:00:00"
    assert result["failed_sources"] == ["Document.X"] and result["sources"] == "all"


def test_additional_pseudonym_does_not_repeat_its_name_and_round_trips():
    """MCBP_Bugs #15: `{"name": "ID організації", "value": "ID організації·CODE"}` read as the
    label repeated; the value pseudonym takes a neutral prefix and still restores."""
    real = {"ID організації": "5f1c9b2e-org-0001", "ЄДРПОУ / ДРФО": "12345678",
            "Номер картки": "AB-77", "Податковий номер": 87654321}
    masker = Masker(secret="t", policy=MaskingPolicy.default().merge(
        {"open_type_exact": [CATALOG], "forced_mask_substrings": ["id"],
         "force_mask_type_fields": [f"{CATALOG}.additional:Номер картки"]}),
        type_classifier=_classifier)
    out = _values(masker.mask(_object([{"name": n, "value": v} for n, v in real.items()])))
    for name, value in real.items():
        masked = out[name]
        assert isinstance(masked, str) and masked != str(value), name
        assert not masked.startswith(name), masked
        assert masked.startswith("Значення·"), masked
        assert masker.unmask_arguments({"x": masked}) == {"x": str(value)}, name
        assert masker.rehydrate_text(f"{name}: {masked}") == f"{name}: {value}", name


def test_nested_additional_values_do_not_repeat_the_attribute_name_and_round_trip():
    """A list of scalars, a list mixing a dict, and a dict: no masked string starts with the
    attribute's name; each restores through unmask and rehydrate."""
    masker = Masker(secret="t", policy=MaskingPolicy.default(), type_classifier=_classifier)
    items = [{"name": "Коди", "value": ["XK-1", "YK-2"]},
             {"name": "Змішане", "value": ["MX-3", {"Інфо": "abc-4"}]},
             {"name": "Склад", "value": {"Інфо": "abc-5"}}]
    out = _values(masker.mask(_object(items)))

    def strings(value: object) -> list[str]:
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return [value] if isinstance(value, str) else []

    originals = {"Коди": ["XK-1", "YK-2"], "Змішане": ["MX-3", "abc-4"], "Склад": ["abc-5"]}
    for name, real in originals.items():
        masked = strings(out[name])
        assert len(masked) == len(real) and not set(masked) & set(real), name
        assert not any(s.startswith(name) for s in masked), masked
        assert masker.unmask_arguments({"x": masked}) == {"x": real}, name
        assert masker.rehydrate_text(" ".join(masked)) == " ".join(real), name
    assert out["Коди"][0].startswith("Значення·")
