"""Forced field-name keys match at the words of a 1C field name, not as any substring."""

from __future__ import annotations

import pytest

from mcbp_core.masking import DEFAULT_POLICY, Masker, MaskingPolicy, is_field_forced

STILL_MASKED = (
    "ИНН", "ИННФизЛица", "КодИНН", "ИПН", "КодИПН", "ІПН", "ЄДРПОУ", "КодПоЕДРПОУ",
    "КодЕДРПОУ", "КПП", "РНОКПП", "КодРНОКПП", "ИННКПП", "СНИЛС", "ОГРН", "ОГРНИП",
    "INN", "INNCode", "Inn_Code", "ИНН2",
    "Телефон", "ТелефонМобильный", "НомерТелефона", "КонтактныйТелефон", "Телефон2",
    "Email", "EMail", "E-mail", "e_mail", "АдресЭлектроннойПочты", "ЭлектроннаяПочта",
    "ЕлектроннаПошта", "Адрес", "Адреса", "ЮридическийАдрес", "АдресДоставки", "Адресат",
    "ДатаРождения", "МестоРождения", "ДатаНародження", "ДеньРождения",
    "Паспорт", "ПаспортныеДанные", "НомерПаспорта",
    "Комментарий", "Коментар", "Примечание", "Примітка",
    "Phone", "MobilePhone", "Telephone", "Address", "EmailAddress", "BirthDate", "Birthday",
    "DateOfBirth", "Comment", "Comments", "Note", "Notes", "Passport",
    # No case step to split by: plain substring as before.
    "contactphone", "кодинн", "PHONE_NUMBER", "MOBILEPHONE",
)

OPENED = (
    "ТипНоменклатуры", "ВидНоменклатуры", "Длинное", "ДлинаСтроки", "ДлинноеНаименование",
    "Beginning", "Inner", "InnerText", "Dinner", "Innovation", "Denote", "Footnote",
    "Номенклатура", "Количество", "Сумма", "Наименование",
)


@pytest.mark.parametrize("name", STILL_MASKED)
def test_forced_word_stays_masked(name: str) -> None:
    assert is_field_forced(DEFAULT_POLICY, name)


@pytest.mark.parametrize("name", OPENED)
def test_substring_inside_a_word_is_not_forced(name: str) -> None:
    assert not is_field_forced(DEFAULT_POLICY, name)


def _open_catalog_masker(policy: MaskingPolicy = DEFAULT_POLICY) -> Masker:
    policy = policy.merge({"open_type_exact": ["Справочник.Номенклатура"]})
    return Masker(secret="t", policy=policy,
                  type_classifier=lambda n: {"Номенклатура": "Справочник.Номенклатура"}.get(n))


def test_open_catalog_shows_type_field_and_masks_forced_one() -> None:
    payload = {"metadata": "catalog", "type": "Номенклатура",
               "data": {"ТипНоменклатуры": "Товар", "КодИПН": "1234567890",
                        "Inner": "x", "Комментарий": "секрет"}}
    data = _open_catalog_masker().mask(payload)["data"]
    assert data["ТипНоменклатуры"] == "Товар"
    assert data["Inner"] == "x"
    assert data["КодИПН"] != "1234567890"
    assert data["Комментарий"] != "секрет"


def test_override_key_matches_at_a_word() -> None:
    policy = MaskingPolicy.from_dict({"forced_mask_substrings": ["Ключ"]})
    assert is_field_forced(policy, "АпиКлюч")
    assert is_field_forced(policy, "КлючДоступа")
    assert not is_field_forced(policy, "Отключен")
