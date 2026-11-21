"""Forced field-name keys: plain substring, minus an occurrence across a CamelCase step or inside
a listed word - so exactly `DELIBERATELY_OPENED` opens compared with the old substring rule."""

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
    "Email", "EMail", "E-mail", "АдресЭлектроннойПочты", "ЭлектроннаяПочта",
    "ЕлектроннаПошта", "Адрес", "Адреса", "ЮридическийАдрес", "АдресДоставки", "Адресат",
    "ДатаРождения", "МестоРождения", "ДатаНародження", "ДеньРождения",
    "Паспорт", "ПаспортныеДанные", "НомерПаспорта",
    "Комментарий", "Коментар", "Примечание", "Примітка", "Примечаниеклиента",
    "Phone", "MobilePhone", "Telephone", "Address", "EmailAddress", "BirthDate", "Birthday",
    "DateOfBirth", "Comment", "Comments", "Note", "Notes", "Passport", "Denote", "Footnote",
    # A key at the end of / inside a lowercase word.
    "Мобтелефон", "Радиотелефон", "Юрадрес", "Фактадрес", "Mobilephone", "Cellphone",
    "Smartphone", "Homephone", "Workphone", "Useremail", "Workemail",
    "Номертелефона", "Серияпаспорта", "Юрадресконтрагента", "Кодиннфизлица", "Местарождения",
    "Workphones", "Ipaddresses", "Billingaddresses", "Usercomments", "Footnotes", "INNER",
    "ВИПНалог", "ЮРЭлектроннаяПочта", "мобЭлектроннаяПочтаcode",
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
    # Review probes.
    "Номертелефона", "НомерТелефона", "Мобтелефоны", "Контактныйтелефон", "Номерателефонов",
    "Workphones", "Mobilephones", "Ipaddresses", "IPAddresses", "Emailaddresses", "Workemails",
    "Юрадресконтрагента", "Адресдоставки", "Фактадресклиента", "Номерпаспорта", "Серияпаспорта",
    "СерияПаспорта", "Данныепаспортафизлица", "Кодинн", "Кодиннфизлица", "Датарождениясотрудника",
    "Местарождения", "Месторождения", "Комментарийменеджера", "Текстпримечания",
    "Текстпримечаниякраткий", "Subnotes", "Footnotes", "Usercomments", "Електроннапоштаклієнта",
    "Mobiletelephones", "Номеркпп", "Кодкппконтрагента", "Кодедрпоуклиента", "Phonenumbers",
    "Homephonenumber", "Mailaddressline", "Deliveryaddressline", "Billingaddresses", "Пасспорт",
    "ИННКонтрагента", "КПП_Контрагента", "ВИПНалог", "INNER", "ТипНоменклатурыИПН", "NOTES",
    "ЗАМЕТКА", "SNOTE", "Notebook", "Denote", "Microphone", "Keynote", "Ринн", "Phonetic",
    "Addressee", "Commentator", "Banknote", "Notepad", "Notes", "NoteText", "Notification",
    "Notional", "Notarius", "Нотариус",
)


def _old_rule(name: str) -> bool:
    normalized = "".join(name.split()).lower()
    return any(k and k in normalized for k in DEFAULT_POLICY.forced_mask_substrings)


def test_only_the_deliberate_names_open_against_the_old_substring_rule() -> None:
    opened = {n for n in SAMPLE if _old_rule(n) and not is_field_forced(DEFAULT_POLICY, n)}
    assert opened == DELIBERATELY_OPENED


_PREFIXES = ("", "Код", "код", "Моб", "моб", "Mobile", "mobile", "IP", "Юр", "ЮР", "Work", "X",
             "Доп", "1", "_", "Основной", "ФЛ", "Контрагент")
_SUFFIXES = ("", "Контрагента", "контрагента", "Code", "code", "ФЛ", "2", "_x", "Номер", "Ы",
             "а", "ов", "s", "Physical", "physical", "ЮЛ", "Лица", "лица", "ФизЛица", "Inn")
_CAMEL_FORMS = ("ДатаРождения", "датаРождения", "ЭлектроннаяПочта", "ДеньРождения",
                "DateOfBirth", "BirthDate", "E-Mail", "АдресЭлектроннойПочты")


def _generated_names() -> set[str]:
    forms = set(_CAMEL_FORMS)
    for key in DEFAULT_POLICY.forced_mask_substrings:
        forms |= {key, key.upper(), key[:1].upper() + key[1:]}
    return {p + f + x for f in forms for p in _PREFIXES for x in _SUFFIXES}


def test_no_generated_key_name_opens_against_the_old_substring_rule() -> None:
    names = _generated_names()
    assert len(names) > 10000
    opened = sorted(n for n in names if _old_rule(n) and not is_field_forced(DEFAULT_POLICY, n))
    assert opened == []


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
    assert is_field_forced(policy, "Отключен")  # a substring, as with every key


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
        assert item["value"].startswith("Значення·"), name


CREDENTIAL_FIELDS = (
    "Пароль", "ПарольПользователя", "Password", "UserPassword", "Passwd", "APIKey", "ApiKey",
    "api_key", "API_KEY", "КлючAPI", "Token", "AccessToken", "ТокенДоступа", "Secret",
    "ClientSecret", "СекретныйКлюч", "СекретнийКлюч",
)
NOT_CREDENTIAL_FIELDS = (
    "Секретарь", "Секретар", "Secretary", "КлючевыеСлова", "Ключ", "КлючСвязи", "Description",
)


@pytest.mark.parametrize("name", CREDENTIAL_FIELDS)
def test_credential_field_is_forced(name: str) -> None:
    assert is_field_forced(DEFAULT_POLICY, name)


@pytest.mark.parametrize("name", NOT_CREDENTIAL_FIELDS)
def test_credential_words_do_not_force_lookalikes(name: str) -> None:
    assert not is_field_forced(DEFAULT_POLICY, name)


def test_credential_fields_and_additional_properties_are_masked() -> None:
    masker = _catalog_masker(CATALOG)  # an open catalog: plain strings would stay readable
    fields = {"Пароль": "p1", "Password": "p2", "APIKey": "k1", "Секретарь": "Іваненко",
              "КлючевыеСлова": "опт", "Description": "Склад"}
    props = ("Пароль від кабінету", "Ключ API", "Секретар", "Ключові слова")
    payload = {"metadata": "catalog", "type": "Контрагенты", "data": {
        **fields, "additional": [{"name": n, "value": "текст"} for n in props]}}
    data = masker.mask(payload)["data"]
    for name in ("Пароль", "Password", "APIKey"):
        assert data[name] != fields[name], name
    for name in ("Секретарь", "КлючевыеСлова", "Description"):
        assert data[name] == fields[name], name
    values = [item["value"] for item in data["additional"]]
    assert values[0].startswith("Значення·") and values[1].startswith("Значення·")
    assert values[2:] == ["текст", "текст"]
