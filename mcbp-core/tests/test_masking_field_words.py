"""Forced field-name keys match at the words of a 1C field name, not as any substring."""

from __future__ import annotations

import pytest

from mcbp_core.masking import DEFAULT_POLICY, Masker, MaskingPolicy, is_field_forced

STILL_MASKED = (
    "ИНН", "ИННФизЛица", "КодИНН", "ИПН", "КодИПН", "ІПН", "ЄДРПОУ", "КодПоЕДРПОУ",
    "КодЕДРПОУ", "КПП", "РНОКПП", "КодРНОКПП", "ИННКПП", "СНИЛС", "ОГРН", "ОГРНИП",
    "INN", "INNCode", "Inn_Code", "ИНН2", "ИннКонтрагента", "Иннфизлица", "Кодинн", "InnKey",
    # An abbreviation run followed by a lowercase word.
    "ИННконтрагента", "ИПНпокупателя", "ІПНклієнта", "КППпокупателя", "ЕДРПОУкод", "ОГРНип",
    "РНОКППфо", "ИННфл", "INNs",
    "Телефон", "ТелефонМобильный", "НомерТелефона", "КонтактныйТелефон", "Телефон2",
    "Email", "EMail", "E-mail", "e_mail", "АдресЭлектроннойПочты", "ЭлектроннаяПочта",
    "ЕлектроннаПошта", "Адрес", "Адреса", "ЮридическийАдрес", "АдресДоставки", "Адресат",
    "ДатаРождения", "МестоРождения", "ДатаНародження", "ДеньРождения",
    "Паспорт", "ПаспортныеДанные", "НомерПаспорта",
    "Комментарий", "Коментар", "Примечание", "Примітка", "Примечаниеклиента",
    "Phone", "MobilePhone", "Telephone", "Address", "EmailAddress", "BirthDate", "Birthday",
    "DateOfBirth", "Comment", "Comments", "Note", "Notes", "Passport", "Denote", "Footnote",
    # A key at the END of a word.
    "Мобтелефон", "Радиотелефон", "Юрадрес", "Фактадрес", "Mobilephone", "Cellphone",
    "Smartphone", "Homephone", "Workphone", "Useremail", "Workemail",
    # No case step to split by: plain substring.
    "contactphone", "кодинн", "PHONE_NUMBER", "MOBILEPHONE",
)

OPENED = (
    "ТипНоменклатуры", "ВидНоменклатуры", "Длинное", "ДлинаСтроки", "ДлинноеНаименование",
    "Beginning", "Inner", "InnerText", "Dinner", "Innovation",
    "Номенклатура", "Количество", "Сумма", "Наименование",
)

# Names the old plain-substring rule masked that the word rule opens - and nothing else may
# (ВидНоменклатуры and ДлинаСтроки were never masked).
DELIBERATELY_OPENED = frozenset({
    "ТипНоменклатуры", "Длинное", "ДлинноеНаименование",
    "Beginning", "Inner", "InnerText", "Dinner", "Innovation",
})

# A wide sample of 1C field names for the old/new comparison.
SAMPLE = STILL_MASKED + OPENED + (
    "ИННКонтрагента", "КодПоДРФО", "НомерТелефонаМобильного", "ЭлектронныйАдрес",
    "ФИОРуководителя", "СерияПаспорта", "ДатаРожд", "ПочтовыйАдрес", "АдресЭП", "ТелефонДляСвязи",
    "eMail", "E_Mail", "ВнутреннийНомер", "КомментарийАвтора", "Snils", "SNILS", "ИНН_КПП",
    "ИННИП", "ЮрАдрес", "ФактАдрес", "ТелефонФакс", "Телефоны", "АдресаДоставки", "Emails",
    "EmailAddresses", "ElectronicMail", "ДопКомментарий", "ДопИнформацияКомментарий",
    "Комментарии", "Примечания", "PassportData", "PassportNo", "ДатаРожденияРебенка",
    "ГодРождения", "МесяцРождения", "ИННФизическогоЛица", "КПППолучателя", "ИННПолучателя",
    "ИННПлательщика", "КодИНН_Получателя", "IIN", "BIN", "Inn", "ИНН10", "КодЕГРПОУ", "ЕГРПОУ",
    "НалоговыйНомер", "ІПНФізОсоби", "ІндивідуальнийПодатковийНомер", "СвидетельствоНДС",
    "Note_Text", "NoteText", "UserNote", "UserComment", "CommentText", "Коментарі",
    "КоментарКлієнта", "Notes2", "Адрес_Доставки", "АДРЕС_ДОСТАВКИ", "ТЕЛЕФОН", "АдресЭлПочты",
    "ЭлПочта", "ЭлектроннаяПочтаКонтрагента", "ЕлектроннаПоштаПартнера", "ЭлектроннаяПочтаАдрес",
    "DOB", "DateBirth", "BirthPlace", "PlaceOfBirth", "ПаспортСерия", "ПаспортНомер",
    "Паспортные_данные", "NIP", "TaxID", "ПредставлениеАдреса", "ПредставлениеТелефона", "ИНН_фл",
    "КодИНNa", "СНИЛСФизЛица", "ИННКПППокупателя", "ЭлАдрес", "КодЄДРПОУ", "Телефон1С",
    "ТелефонWork", "phoneNumber", "phone_number", "PhoneNumber", "мобТелефон", "юрАдрес",
    "Description", "Code", "Ref", "DeletionMark", "Owner", "Parent", "Контрагент", "Организация",
    "Склад", "Ответственный", "ВидКонтрагента", "ТипЦен", "ЕдиницаИзмерения", "Артикул", "Цена",
    "Ставка", "СтавкаНДС", "Валюта", "Курс", "Кратность", "ДоговорКонтрагента", "СчетУчета",
    "Подразделение", "Проект", "Автор", "Статус", "Важность", "ВидОперации", "КонтактноеЛицо",
    "ОсновнойБанковскийСчет", "ВидЗапасов", "ХарактеристикаНоменклатуры", "Партия",
    "СтруктурнаяЕдиница", "Ячейка", "СерияНоменклатуры", "ДатаОтгрузки", "СрокОплаты",
    "ПодписьРуководителя",
)


def _old_rule(name: str) -> bool:
    normalized = "".join(name.split()).lower()
    return any(k and k in normalized for k in DEFAULT_POLICY.forced_mask_substrings)


def test_only_the_deliberate_names_open_against_the_old_substring_rule() -> None:
    opened = {n for n in SAMPLE if _old_rule(n) and not is_field_forced(DEFAULT_POLICY, n)}
    assert opened == DELIBERATELY_OPENED


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


CATALOG = "Справочник.Контрагенты"


def _catalog_masker(*open_types: str, **rules: list[str]) -> Masker:
    policy = DEFAULT_POLICY.merge({"open_type_exact": list(open_types), **rules})
    return Masker(secret="t", policy=policy,
                  type_classifier=lambda n: {"Контрагенты": CATALOG}.get(n))


def test_section_open_rule_opens_a_type_field_but_not_a_forced_one() -> None:
    masker = _catalog_masker(allow_plain_type_fields=[
        f"{CATALOG}.Товари.ТипНоменклатуры", f"{CATALOG}.Товари.ИННконтрагента"])
    payload = {"metadata": "catalog", "type": "Контрагенты", "data": {"tabular_sections": {
        "Товари": [{"ТипНоменклатуры": "Товар", "ИННконтрагента": "1234567890"}]}}}
    row = masker.mask(payload)["data"]["tabular_sections"]["Товари"][0]
    assert row["ТипНоменклатуры"] == "Товар"
    assert row["ИННконтрагента"] != "1234567890"


def test_additional_property_name_is_matched_by_plain_substring() -> None:
    names = ("Податковий номер ИННклієнта", "Тип номенклатуры")  # the 2nd: "ипн" across words
    masker = _catalog_masker(CATALOG, allow_plain_type_fields=[
        f"{CATALOG}.additional:{n}" for n in names])
    payload = {"metadata": "catalog", "type": "Контрагенты",
               "data": {"additional": [{"name": n, "value": "текст"} for n in names]}}
    out = masker.mask(payload)["data"]["additional"]
    for name, item in zip(names, out):
        assert item["value"].startswith(f"{name}·"), name
