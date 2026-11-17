"""Masker holes found by the mcbp-ai masking audit: per-type rules on a tabular-section field
("<type>.<section>.<field>"), rules naming a standard attribute by its metadata name
(Наименование, Код, ...), and long numeric `additional` values under a name no rule knows.
Pure/sync."""
from __future__ import annotations

from mcbp_core.masking import Masker, MaskingPolicy

CATALOG = "Справочник.Контрагенты"
OPEN_CATALOG = "Справочник.Номенклатура"
PROPERTIES = "ПланВидовХарактеристик.ДополнительныеРеквизитыИСведения"


def _classifier(name: str) -> str | None:
    return {"Контрагенты": CATALOG, "Номенклатура": OPEN_CATALOG,
            "ДополнительныеРеквизитыИСведения": PROPERTIES,
            "ТипыКонтактнойИнформации": "Перечисление.ТипыКонтактнойИнформации",
            "Партнеры": "Справочник.Партнеры"}.get(name)


def _ref(metadata: str, uuid: str, presentation: str) -> dict:
    return {"Presentation": presentation, "Data": uuid, "Metadata": metadata}


def _masker(open_types: tuple[str, ...] = (CATALOG,), **rules: list[str]) -> Masker:
    policy = MaskingPolicy.default().merge({"open_type_exact": list(open_types), **rules})
    return Masker(secret="t", policy=policy, type_classifier=_classifier)


def _object(type_: str = "Контрагенты", **data: object) -> dict:
    return {"metadata": "catalog", "type": type_, "data": data}


# --- 1. tabular-section fields --------------------------------------------------------

def _sectioned(rows: list[dict]) -> dict:
    return _object(Ref=_ref("Контрагенты", "u-1", "Альфа"), Description="Альфа",
                   tabular_sections={"КонтактнаяИнформация": rows})


def test_section_field_rule_masks_that_field_in_section_rows():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.КонтактнаяИнформация.Представление"])
    rows = [{"НомерСтроки": 1, "Вид": "Телефон", "Представление": "+380501112233"},
            {"НомерСтроки": 2, "Вид": "Сайт", "Представление": "alpha.ua"}]
    out = masker.mask(_sectioned(rows))["data"]["tabular_sections"]["КонтактнаяИнформация"]
    assert [r["Представление"].startswith("Представление·") for r in out] == [True, True]
    assert [r["Вид"] for r in out] == ["Телефон", "Сайт"]  # open type: other fields untouched
    assert [r["НомерСтроки"] for r in out] == [1, 2]
    assert masker.unmask_arguments({"x": out[1]["Представление"]}) == {"x": "alpha.ua"}


def test_section_field_rule_masks_numbers_and_references():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.Товари.Ціна",
                                             f"{CATALOG}.Товари.Номенклатура"])
    payload = _object(tabular_sections={"Товари": [
        {"Ціна": 125.5, "Номенклатура": _ref("Номенклатура", "n-1", "Цвях"), "Кількість": 3}]})
    row = masker.mask(payload)["data"]["tabular_sections"]["Товари"][0]
    assert isinstance(row["Ціна"], str) and row["Ціна"].startswith("Ціна·")
    assert row["Номенклатура"]["Presentation"] != "Цвях" and row["Номенклатура"]["Data"] == "n-1"
    assert row["Кількість"] == 3


def test_section_field_rule_does_not_reach_a_header_field_or_another_section():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.КонтактнаяИнформация.Представление"])
    payload = _object(Представление="Альфа", tabular_sections={
        "Інше": [{"Представление": "x-1"}],
        "КонтактнаяИнформация": [{"Представление": "p-1"}]})
    data = masker.mask(payload)["data"]
    assert data["Представление"] == "Альфа"
    assert data["tabular_sections"]["Інше"] == [{"Представление": "x-1"}]
    assert data["tabular_sections"]["КонтактнаяИнформация"][0]["Представление"] != "p-1"


def test_section_field_open_rule_opens_a_closed_type_field_but_not_a_forced_name():
    masker = _masker(open_types=(), allow_plain_type_fields=[
        f"{CATALOG}.КонтактнаяИнформация.Вид", f"{CATALOG}.КонтактнаяИнформация.Телефон"])
    row = masker.mask(_sectioned([{"Вид": "Телефон", "Телефон": "0501112233",
                                   "Інше": "текст"}]))
    out = row["data"]["tabular_sections"]["КонтактнаяИнформация"][0]
    assert out["Вид"] == "Телефон"
    assert out["Телефон"] != "0501112233" and out["Інше"] != "текст"


def test_section_rule_in_a_list_row():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.КонтактнаяИнформация.Представление"])
    payload = {"metadata": "catalog", "type": "Контрагенты", "data": [
        {"Ref": _ref("Контрагенты", "u-1", "Альфа"),
         "КонтактнаяИнформация": [{"Представление": "p-1"}]}]}
    row = masker.mask(payload)["data"][0]
    assert row["КонтактнаяИнформация"][0]["Представление"] != "p-1"


def _property_row(name: str, value: object) -> dict:
    return {"НомерСтроки": 1, "Свойство": _ref("ДополнительныеРеквизитыИСведения", "p-1", name),
            "Значение": value}


def test_section_rule_wins_on_a_property_section_of_an_open_type():
    rule = f"{CATALOG}.ДополнительныеРеквизиты.Значение"
    payload = _object(tabular_sections={"ДополнительныеРеквизиты": [
        _property_row("Хобі", "риболовля"), _property_row("Рейтинг", 5)]})
    assert _masker().mask(payload) == payload  # open type, no rule: readable
    rows = _masker(force_mask_type_fields=[rule]).mask(payload)["data"]["tabular_sections"][
        "ДополнительныеРеквизиты"]
    assert [r["Значение"].startswith("Значение·") for r in rows] == [True, True]
    assert [r["Свойство"]["Presentation"] for r in rows] == ["Хобі", "Рейтинг"]


def test_section_open_rule_does_not_open_a_value_of_an_always_masked_property():
    masker = _masker(open_types=(), allow_plain_type_fields=[
        f"{CATALOG}.ДополнительныеРеквизиты.Значение"])
    payload = _object(tabular_sections={"ДополнительныеРеквизиты": [
        _property_row("ЄДРПОУ", "12345678"), _property_row("Хобі", "риболовля")]})
    rows = masker.mask(payload)["data"]["tabular_sections"]["ДополнительныеРеквизиты"]
    assert rows[0]["Значение"] != "12345678" and rows[1]["Значение"] == "риболовля"


def test_section_rules_win_on_a_contact_row():
    kind = {"Presentation": "Телефон", "Value": "Телефон", "Metadata": "ТипыКонтактнойИнформации"}
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.contact:Телефон",
                                             f"{CATALOG}.КонтактнаяИнформация.Порядок"],
                     allow_plain_type_fields=[f"{CATALOG}.КонтактнаяИнформация.Регіон"])
    row = masker.mask(_sectioned([{"Тип": kind, "Представление": "0501112233",
                                   "Регіон": "Київ", "Порядок": 7}]))
    out = row["data"]["tabular_sections"]["КонтактнаяИнформация"][0]
    assert out["Представление"] != "0501112233"  # the contact rule still masks the row
    assert out["Регіон"] != "Київ"  # a contact row is never opened by a section rule
    assert isinstance(out["Порядок"], str) and out["Порядок"] != 7  # explicit Mask rule wins


def test_section_open_rule_keeps_an_additional_mask_row_masked():
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.additional:Відповідальний"],
                     allow_plain_type_fields=[f"{CATALOG}.ДополнительныеРеквизиты.Значение"])
    payload = _object(tabular_sections={"ДополнительныеРеквизиты": [
        _property_row("Відповідальний", "Іваненко Петро"), _property_row("Хобі", "риболовля")]})
    rows = masker.mask(payload)["data"]["tabular_sections"]["ДополнительныеРеквизиты"]
    assert "Іваненко" not in str(rows[0]["Значение"])
    assert rows[1]["Значение"] == "риболовля"


def test_section_open_rule_keeps_a_contact_mask_row_masked():
    kind = {"Presentation": "Телефон", "Value": "Телефон", "Metadata": "ТипыКонтактнойИнформации"}
    masker = _masker(force_mask_type_fields=[f"{CATALOG}.contact:Телефон"],
                     allow_plain_type_fields=[f"{CATALOG}.КонтактнаяИнформация.Представление"])
    out = masker.mask(_sectioned([{"Тип": kind, "Представление": "+380501112233"}]))
    row = out["data"]["tabular_sections"]["КонтактнаяИнформация"][0]
    assert "380501112233" not in str(row["Представление"])


def test_section_open_rule_does_not_open_a_long_number_property_value():
    section_rule = f"{CATALOG}.ДополнительныеРеквизиты.Значение"
    payload = _object(tabular_sections={"ДополнительныеРеквизиты": [
        _property_row("Податковий номер", "31234567"), _property_row("Рейтинг", 5)]})
    rows = _masker(allow_plain_type_fields=[section_rule]).mask(payload)["data"][
        "tabular_sections"]["ДополнительныеРеквизиты"]
    assert rows[0]["Значение"] != "31234567" and rows[1]["Значение"] == 5
    # only an explicit open additional:<name> row opens it
    opened = _masker(allow_plain_type_fields=[
        section_rule, f"{CATALOG}.additional:Податковий номер"]).mask(payload)["data"][
        "tabular_sections"]["ДополнительныеРеквизиты"]
    assert opened[0]["Значение"] == "31234567"


def test_section_open_rule_keeps_a_closed_type_reference_masked():
    masker = _masker(allow_plain_type_fields=[f"{CATALOG}.Товари.Партнер",
                                              f"{CATALOG}.Товари.Номенклатура"])
    payload = _object(tabular_sections={"Товари": [
        {"Партнер": _ref("Партнеры", "p-9", "Світанок, ТОВ"),
         "Номенклатура": _ref("Номенклатура", "n-1", "Цвях")}]})
    row = masker.mask(payload)["data"]["tabular_sections"]["Товари"][0]
    assert row["Партнер"]["Presentation"] != "Світанок, ТОВ" and row["Партнер"]["Data"] == "p-9"
    assert row["Номенклатура"]["Presentation"] == "Цвях"


# --- 2. standard attributes named by metadata name ----------------------------------------

def test_rule_on_metadata_name_masks_the_canonical_key_and_reference_presentations():
    masker = _masker(open_types=(OPEN_CATALOG,),
                     force_mask_type_fields=[f"{OPEN_CATALOG}.Наименование", f"{OPEN_CATALOG}.Код"])
    payload = _object("Номенклатура", Ref=_ref("Номенклатура", "n-1", "Цвях"),
                      Description="Цвях", Code="0001", Артикул="A-1")
    data = masker.mask(payload)["data"]
    assert data["Description"] != "Цвях" and data["Code"] != "0001"
    assert data["Ref"]["Presentation"] != "Цвях" and data["Ref"]["Data"] == "n-1"
    assert data["Артикул"] == "A-1"
    # A reference to that open type elsewhere hides the name too.
    other = masker.mask({"data": [{"Товар": _ref("Номенклатура", "n-2", "Шуруп")}]})
    assert other["data"][0]["Товар"]["Presentation"] != "Шуруп"


def test_rule_on_canonical_key_and_ua_metadata_name_both_match():
    for rule in ("Description", "Найменування"):
        masker = _masker(open_types=(OPEN_CATALOG,), force_mask_type_fields=[f"{OPEN_CATALOG}.{rule}"])
        data = masker.mask(_object("Номенклатура", Description="Цвях"))["data"]
        assert data["Description"] != "Цвях", rule


def test_open_rule_on_metadata_name_opens_the_canonical_key_of_a_closed_type():
    masker = _masker(open_types=(), allow_plain_type_fields=[f"{CATALOG}.Наименование"])
    data = masker.mask(_object(Description="Альфа", Інше="текст"))["data"]
    assert data["Description"] == "Альфа" and data["Інше"] != "текст"


def test_open_type_without_name_rule_keeps_reference_presentation():
    masker = _masker(open_types=(OPEN_CATALOG,), force_mask_type_fields=[f"{OPEN_CATALOG}.Артикул"])
    out = masker.mask({"data": [{"Товар": _ref("Номенклатура", "n-2", "Шуруп")}]})
    assert out["data"][0]["Товар"]["Presentation"] == "Шуруп"


# --- 3. long numeric additional values ------------------------------------------------------

def _additional(*items: tuple[str, object]) -> dict:
    payload = _object(additional=[{"name": n, "value": v} for n, v in items])
    return {i["name"]: i["value"] for i in Masker(
        secret="t", policy=MaskingPolicy.default().merge({"open_type_exact": [CATALOG]}),
        type_classifier=_classifier).mask(payload)["data"]["additional"]}


def test_additional_long_number_under_a_plain_name_is_masked():
    out = _additional(("Податковий номер", 87654321), ("Код платника", "1234567890"),
                      ("Мобільний", "+38 (050) 111-22-33"), ("Сума", 12345678.0),
                      ("Робочий", "(050) 111-22-33"), ("Домашній", "050.111.22.33"))
    for name in ("Податковий номер", "Код платника", "Мобільний", "Сума", "Робочий",
                 "Домашній"):
        assert isinstance(out[name], str) and out[name].startswith(f"{name}·"), name


def test_additional_short_numbers_dates_and_amounts_stay():
    out = _additional(("Рейтинг", 5), ("Ліміт", 1500000), ("Курс", 12345678.25),
                      ("Дата угоди", "2026-05-04"), ("Код", "1234567"), ("Активний", True))
    assert out == {"Рейтинг": 5, "Ліміт": 1500000, "Курс": 12345678.25,
                   "Дата угоди": "2026-05-04", "Код": "1234567", "Активний": True}


def test_additional_long_number_opened_by_an_explicit_open_rule():
    masker = _masker(allow_plain_type_fields=[f"{CATALOG}.additional:Податковий номер"])
    payload = _object(additional=[{"name": "Податковий номер", "value": 87654321}])
    assert masker.mask(payload)["data"]["additional"][0]["value"] == 87654321


def test_public_rule_predicates_match_the_masker():
    from mcbp_core.masking import (
        is_field_forced,
        is_field_opened,
        is_open_type,
        normalize_field_name,
    )

    policy = MaskingPolicy.default().merge({
        "open_type_exact": [CATALOG],
        "force_mask_type_fields": [f"{CATALOG}.Наименование", f"{CATALOG}.Контакты.Телефон2"],
        "allow_plain_type_fields": [f"{CATALOG}.Артикул"],
    })
    assert normalize_field_name("Код ЄДРПОУ") == "кодєдрпоу"
    assert is_open_type(CATALOG, policy) and not is_open_type("Справочник.Партнеры", policy)
    assert is_field_forced(policy, "КодЕДРПОУ")  # forced substring, any context
    assert is_field_forced(policy, "Description", CATALOG)  # metadata name of a standard field
    assert not is_field_forced(policy, "Description", OPEN_CATALOG)
    assert is_field_forced(policy, "Телефон2", f"{CATALOG}.Контакты")
    assert not is_field_forced(policy, "")
    assert is_field_opened(policy, "Артикул", CATALOG)
    assert not is_field_opened(policy, "Артикул", OPEN_CATALOG)
