from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.services.legal_data import clean_case_number, clean_uid, is_deadline_missed
from app.services.name_normalizer import make_short_name
from app.utils import parse_russian_date

MONTHS_GENITIVE = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


@dataclass(frozen=True)
class StatementContext:
    data: dict
    received_date: date
    deadline_date: date | None
    document_date: date
    restore_reason: str | None = None
    has_envelope: bool = False
    manual_date_only: bool = False


def _required(data: dict, key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise ValueError(f"Missing required document field: {key}")
    return value


def _optional(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()


def date_long_text(raw: str) -> str:
    parsed = parse_russian_date(raw)
    if not parsed:
        return raw
    return f"{parsed.day} {MONTHS_GENITIVE[parsed.month - 1]} {parsed.year} года"


def signature_date_text(document_date: date) -> str:
    return f"«{document_date.day:02d}» {MONTHS_GENITIVE[document_date.month - 1]} {document_date.year} г."


def normalize_address_line(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.;")
    text = re.sub(r"№(\d+)", r"№ \1", text)
    text = re.sub(r"\bгород\s+", "г. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bулица\s+", "ул. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bдом\s+№?\s*", "д. ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bдом\s+", "д. ", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*д\.\s*", ", д. ", text)
    text = re.sub(r"(\S+)\s+д\.\s*(\d+)", r"\1, д. \2", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r",\s*\.", ".", text)
    return text


def normalize_court_addressee(court: str) -> str:
    court = court.strip().rstrip(".")
    court = re.sub(r"№(\d+)", r"№ \1", court)
    court = re.sub(r"\bгород\s+", "г. ", court, flags=re.IGNORECASE)
    lower = court.lower()
    if lower.startswith("мировому судье"):
        return court
    if lower.startswith("мировой судья"):
        return "Мировому судье " + court[len("мировой судья") :].strip()
    if lower.startswith("судебный участок") or lower.startswith("судебного участка"):
        normalized = re.sub(r"^судебный участок", "судебного участка", court, flags=re.IGNORECASE)
        normalized = re.sub(r"№\s*(\d+)", r"№ \1", normalized)
        return "Мировому судье " + normalized
    return court


def normalize_court_instrumental(court: str) -> str:
    court = court.strip().rstrip(".")
    court = re.sub(r"№(\d+)", r"№ \1", court)
    court = re.sub(r"\bгород\s+", "г. ", court, flags=re.IGNORECASE)
    lower = court.lower()
    if lower.startswith("мировому судье"):
        rest = court[len("мировому судье") :].strip()
        return f"мировым судьей {rest}" if rest else "мировым судьей"
    if lower.startswith("мировой судья"):
        rest = court[len("мировой судья") :].strip()
        return f"мировым судьей {rest}" if rest else "мировым судьей"
    if lower.startswith("судебный участок") or lower.startswith("судебного участка"):
        normalized = re.sub(r"^судебный участок", "судебного участка", court, flags=re.IGNORECASE)
        normalized = re.sub(r"№\s*(\d+)", r"№ \1", normalized)
        return f"мировым судьей {normalized}"
    return court


def normalize_creditor_address(address: str) -> str:
    return normalize_address_line(address)


def build_header_lines(ctx: StatementContext) -> list[str]:
    data = ctx.data
    debtor_full_name = _required(data, "debtor_full_name")
    court_addressee = data.get("court_addressee") or normalize_court_addressee(_required(data, "court_name"))
    lines = [
        court_addressee,
        normalize_address_line(_required(data, "court_address")),
        "",
        "Должник:",
        debtor_full_name,
        f"адрес: {normalize_address_line(_required(data, 'debtor_address'))}",
        "",
        "Взыскатель:",
        _required(data, "creditor_name"),
        normalize_creditor_address(_required(data, "creditor_address")),
    ]
    case_number = clean_case_number(_required(data, "case_number"))
    uid = clean_uid(_optional(data, "uid"))
    lines.extend(["", f"Дело/производство № {case_number}"])
    if uid:
        lines.append(f"УИД: {uid}")
    return lines


def _case_identifier_short(data: dict) -> str:
    case_number = clean_case_number(_required(data, "case_number"))
    uid = clean_uid(_optional(data, "uid"))
    if uid:
        return f"№ {case_number}, УИД {uid}"
    return f"№ {case_number}"


def _contract_inline(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ,.;")
    lower = value.lower()
    if lower.startswith("по "):
        return value[3:].strip()
    if lower.startswith("договор"):
        return value
    if value.startswith("№"):
        return f"договор {value}"
    return value


def _period_inline(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ,.;")
    value = re.sub(r"^(за\s+период|период|за)\s+", "", value, flags=re.IGNORECASE).strip(" ,.;")
    if not value.startswith("с "):
        return f"с {value}"
    return value


def _money_body_phrase(data: dict) -> str:
    debt = _required(data, "debt_amount")
    state_duty = _optional(data, "state_duty")
    contract = _contract_inline(_required(data, "debt_contract"))
    period = _period_inline(_required(data, "debt_period"))
    base = f"задолженности по {contract} за период {period} в размере {debt}"
    if state_duty:
        return f"{base}, а также расходов по оплате государственной пошлины в размере {state_duty}"
    return base


def statement_in_time(ctx: StatementContext) -> list[str]:
    data = ctx.data
    court_body = data.get("court_instrumental") or normalize_court_instrumental(_required(data, "court_name"))
    case_identifier = _case_identifier_short(data)
    order_date_long = date_long_text(_required(data, "order_date"))
    creditor = _required(data, "creditor_name")
    received_long = date_long_text(ctx.received_date.strftime("%d.%m.%Y"))
    money_part = _money_body_phrase(data)
    return [
        f"{order_date_long} {court_body} вынесен судебный приказ по делу/производству {case_identifier}, о взыскании с меня в пользу {creditor} {money_part}.",
        f"Копия судебного приказа получена мной {received_long}.",
        "С судебным приказом не согласен, возражаю относительно его исполнения в полном объеме.",
        "Настоящие возражения подаются в установленный законом срок.",
        "На основании изложенного, руководствуясь статьями 128, 129 ГПК РФ,",
        "ПРОШУ:",
        f"1. Отменить судебный приказ от {order_date_long}, вынесенный {court_body} по делу/производству {case_identifier}.",
        "2. Направить мне копию определения об отмене судебного приказа по адресу, указанному в настоящих возражениях.",
    ]


def statement_restore_term(ctx: StatementContext) -> list[str]:
    if not ctx.restore_reason:
        raise ValueError("Для пропущенного срока нужна причина восстановления")
    data = ctx.data
    court_body = data.get("court_instrumental") or normalize_court_instrumental(_required(data, "court_name"))
    case_identifier = _case_identifier_short(data)
    order_date_long = date_long_text(_required(data, "order_date"))
    creditor = _required(data, "creditor_name")
    received_long = date_long_text(ctx.received_date.strftime("%d.%m.%Y"))
    deadline = ctx.deadline_date.strftime("%d.%m.%Y") if ctx.deadline_date else ""
    money_part = _money_body_phrase(data)
    return [
        f"{order_date_long} {court_body} вынесен судебный приказ по делу/производству {case_identifier}, о взыскании с меня в пользу {creditor} {money_part}.",
        f"Копия судебного приказа получена мной {received_long}.",
        f"Десятидневный срок подачи возражений истек {deadline}. {ctx.restore_reason}",
        "Прошу восстановить пропущенный процессуальный срок.",
        "С судебным приказом не согласен, возражаю относительно его исполнения в полном объеме.",
        "На основании изложенного, руководствуясь статьями 112, 128, 129 ГПК РФ,",
        "ПРОШУ:",
        f"1. Восстановить срок для подачи возражений относительно исполнения судебного приказа от {order_date_long}, вынесенного {court_body} по делу/производству {case_identifier}.",
        f"2. Отменить судебный приказ от {order_date_long}, вынесенный {court_body} по делу/производству {case_identifier}.",
        "3. Направить мне копию определения об отмене судебного приказа по адресу, указанному в настоящих возражениях.",
    ]


def build_statement_paragraphs(ctx: StatementContext) -> list[str]:
    restore_term = is_deadline_missed(ctx.deadline_date, ctx.document_date)
    if restore_term:
        return statement_restore_term(ctx)
    return statement_in_time(ctx)


def build_attachments(ctx: StatementContext) -> list[str]:
    data = ctx.data
    order_date = _required(data, "order_date")
    items = [f"Копия судебного приказа от {order_date}."]
    if ctx.has_envelope:
        items.append("Копия почтового конверта, подтверждающего дату получения судебного приказа.")
    elif ctx.manual_date_only:
        items.append("Документ, подтверждающий дату получения судебного приказа, — при наличии.")
    else:
        items.append("Документ, подтверждающий дату получения судебного приказа, — при наличии.")
    items.append("Копия настоящих возражений для взыскателя.")
    if is_deadline_missed(ctx.deadline_date, ctx.document_date):
        items.insert(2, "Документы, подтверждающие дату фактического получения судебного приказа и причины пропуска срока.")
    return items


def debtor_short_name(data: dict) -> str:
    short = _optional(data, "debtor_short_name")
    if short:
        return short
    return make_short_name(_required(data, "debtor_full_name"))
