from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.services.document_render_contract import (
    MONTHS_GENITIVE,
    date_long_text,
    dates_long_in_text,
    select_creditor_address_for_render,
)
from app.services.legal_data import clean_case_number, clean_uid, is_deadline_missed, keep_house_number_together, normalize_address_text
from app.services.name_normalizer import make_short_name

LLM_STATEMENT_TEMPLATE = """{{render_court_addressee}}
{{render_court_address}}
Судья: {{render_judge_name}}

Должник:
{{render_debtor_full_name}}
{{render_debtor_address}}

Взыскатель:
{{render_creditor_name}}
{{render_creditor_address}}

{{render_case_identifier}}

ВОЗРАЖЕНИЯ
относительно исполнения судебного приказа

{{render_order_facts_sentence}}

Копия судебного приказа получена мной {{received_date_long}}.

С судебным приказом не согласен, возражаю относительно его исполнения в полном объеме.

На основании изложенного, руководствуясь статьями 128, 129 ГПК РФ,

ПРОШУ:

Отменить судебный приказ от {{render_order_date_long}},
вынесенный {{render_court_instrumental}}
по делу/производству {{render_case_identifier}}.

Приложения:
Копия судебного приказа от {{render_order_date_long}}.

{{signature_date_long}}    _____________    /{{render_debtor_short_name}}/
"""

REQUIRED_RENDER_FIELDS = (
    "court_addressee", "court_address", "judge_name", "debtor_full_name",
    "debtor_short_name", "debtor_address", "creditor_name", "creditor_address",
    "case_identifier", "order_date_long", "court_instrumental",
    "order_facts_sentence",
)


def render_value(data: dict, name: str) -> str:
    render = data.get("render")
    if isinstance(render, dict):
        return str(render.get(name) or "").strip()
    return str(data.get(f"render_{name}") or "").strip()


def missing_render_fields(data: dict) -> list[str]:
    return [name for name in REQUIRED_RENDER_FIELDS if not render_value(data, name)]


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


def signature_date_text(document_date: date) -> str:
    return date_long_text(document_date)


def normalize_address_line(text: str) -> str:
    return normalize_address_text(text)


def normalize_court_address_line(text: str) -> str:
    return keep_house_number_together(normalize_address_text(text))


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
    first_address = re.split(r"\s*;\s*", address, maxsplit=1)[0]
    return normalize_address_line(first_address)


def build_header_lines(ctx: StatementContext) -> list[str]:
    data = ctx.data
    if not missing_render_fields(data):
        return [
            render_value(data, "court_addressee"),
            render_value(data, "court_address"),
            f"Судья: {render_value(data, 'judge_name')}",
            "",
            "Должник:",
            render_value(data, "debtor_full_name"),
            render_value(data, "debtor_address"),
            "",
            "Взыскатель:",
            render_value(data, "creditor_name"),
            render_value(data, "creditor_address"),
            "",
            render_value(data, "case_identifier"),
        ]
    debtor_full_name = _required(data, "debtor_full_name")
    court_addressee = data.get("court_addressee") or normalize_court_addressee(_required(data, "court_name"))
    lines = [court_addressee]
    court_address = _optional(data, "court_address")
    if court_address:
        lines.append(normalize_court_address_line(court_address))
    judge_name = _optional(data, "judge_name") or _optional(data, "judge")
    if judge_name:
        lines.append(f"Судья: {judge_name}")
    lines.extend(["", "Должник:", debtor_full_name])
    debtor_address = _optional(data, "debtor_address")
    if debtor_address:
        lines.append(f"адрес: {normalize_address_line(debtor_address)}")
    lines.extend(["", "Взыскатель:", _required(data, "creditor_name")])
    _, creditor_address = select_creditor_address_for_render(data)
    if creditor_address:
        lines.append(normalize_creditor_address(creditor_address))
    case_number = clean_case_number(_optional(data, "case_number"))
    uid = clean_uid(_optional(data, "uid"))
    lines.append("")
    if case_number:
        lines.append(f"Дело/производство № {case_number}")
    if uid:
        lines.append(f"УИД: {uid}")
    return lines


def _case_identifier_short(data: dict) -> str:
    case_number = clean_case_number(_optional(data, "case_number"))
    uid = clean_uid(_optional(data, "uid"))
    if case_number and uid:
        return f"№ {case_number}, УИД {uid}"
    if case_number:
        return f"№ {case_number}"
    if uid:
        return f"УИД {uid}"
    raise ValueError("Missing required document field: case_number or uid")


def _contract_inline(value: str) -> str:
    value = dates_long_in_text(re.sub(r"\s+", " ", value).strip(" ,.;"))
    value = re.sub(r"\bзаключ[её]нный\b", "заключённому", value, flags=re.IGNORECASE)
    lower = value.lower()
    if lower.startswith("по "):
        return value[3:].strip()
    if lower.startswith("договору"):
        return value
    if lower.startswith("договор"):
        return re.sub(r"^договор", "договору", value, flags=re.IGNORECASE)
    if value.startswith("№"):
        return f"договору {value}"
    if re.fullmatch(r"\d[\d\s-]*", value):
        return f"договору № {value}"
    return value


def _structured_debt_basis_inline(data: dict) -> str:
    number = _optional(data, "debt_basis_number")
    basis_date = _optional(data, "debt_basis_date")
    basis_type = _optional(data, "debt_basis_type")
    if not number:
        return _contract_inline(_optional(data, "debt_contract"))
    if basis_type == "credit_card_agreement":
        phrase = f"договору о выпуске и использовании кредитной банковской карты № {number}"
    elif basis_type == "credit_agreement":
        phrase = f"кредитному договору № {number}"
    elif basis_type == "loan_agreement":
        phrase = f"договору займа № {number}"
    else:
        phrase = f"договору № {number}"
    if basis_date:
        phrase += f" от {date_long_text(basis_date)}"
    return phrase


def _period_inline(value: str) -> str:
    value = dates_long_in_text(re.sub(r"\s+", " ", value).strip(" ,.;"))
    value = re.sub(r"^(за\s+период|период|за)\s+", "", value, flags=re.IGNORECASE).strip(" ,.;")
    value = re.sub(r"\s+г$", " г.", value, flags=re.IGNORECASE)
    if not value.startswith("с "):
        return f"с {value}"
    return value


def _money_body_phrase(data: dict) -> str:
    state_duty_raw = _optional(data, "state_duty")
    state_duty = state_duty_raw if state_duty_raw else ""
    interest = _optional(data, "interest")
    penalty = _optional(data, "penalty")
    debt = _required(data, "debt_amount").rstrip(".") + ("." if state_duty else "")
    total = _optional(data, "total_amount").rstrip(".")
    contract = _structured_debt_basis_inline(data)
    period = _optional(data, "debt_period")
    fragments = []
    if contract:
        fragments.append(f"по { _contract_inline(contract) }")
    if period:
        fragments.append(f"за период {_period_inline(period)}")
    base = "задолженности"
    if fragments:
        base = f"{base} {' '.join(fragments)}"
    base = f"{base} в размере {debt}"
    base = re.sub(r"по договор №", "по договору №", base)
    if interest:
        base = f"{base}, процентов в размере {interest}"
    if penalty:
        base = f"{base}, неустойки в размере {penalty}"
    if state_duty:
        base = f"{base}, а также расходов по оплате государственной пошлины в размере {state_duty}"
    if total and _optional(data, "amount_render_mode") == "explicit_total":
        base = f"{base}. Общая сумма взыскания составляет {total}"
    return base


def statement_in_time(ctx: StatementContext) -> list[str]:
    data = ctx.data
    court_body = data.get("court_instrumental") or normalize_court_instrumental(_required(data, "court_name"))
    case_identifier = _case_identifier_short(data)
    order_date_long = date_long_text(_required(data, "order_date"))
    creditor = _required(data, "creditor_name")
    creditor_genitive = _optional(data, "creditor_name_genitive")
    creditor_phrase = (
        f"в пользу {creditor_genitive}"
        if creditor_genitive
        else f"по заявлению взыскателя — {creditor}"
    )
    received_long = date_long_text(ctx.received_date)
    money_part = _money_body_phrase(data)
    return [
        f"{order_date_long} {court_body} вынесен судебный приказ по делу/производству {case_identifier}, о взыскании с меня {creditor_phrase} {money_part}.",
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
    creditor_genitive = _optional(data, "creditor_name_genitive")
    creditor_phrase = (
        f"в пользу {creditor_genitive}"
        if creditor_genitive
        else f"по заявлению взыскателя — {creditor}"
    )
    received_long = date_long_text(ctx.received_date)
    deadline = date_long_text(ctx.deadline_date) if ctx.deadline_date else ""
    money_part = _money_body_phrase(data)
    return [
        f"{order_date_long} {court_body} вынесен судебный приказ по делу/производству {case_identifier}, о взыскании с меня {creditor_phrase} {money_part}.",
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
    if not missing_render_fields(ctx.data):
        data = ctx.data
        if restore_term:
            order_date_long = render_value(data, "order_date_long")
            court_instrumental = render_value(data, "court_instrumental")
            case_identifier = render_value(data, "case_identifier")
            deadline_long = date_long_text(ctx.deadline_date)
            return [
                render_value(data, "order_facts_sentence"),
                f"Копия судебного приказа получена мной {date_long_text(ctx.received_date)}.",
                f"Десятидневный срок подачи возражений истек {deadline_long}. {ctx.restore_reason}",
                "Прошу восстановить пропущенный процессуальный срок.",
                "С судебным приказом не согласен, возражаю относительно его исполнения в полном объеме.",
                "На основании изложенного, руководствуясь статьями 112, 128, 129 ГПК РФ,",
                "ПРОШУ:",
                (
                    f"1. Восстановить срок для подачи возражений относительно исполнения судебного приказа "
                    f"от {order_date_long}, вынесенного {court_instrumental} "
                    f"по делу/производству {case_identifier}."
                ),
                (
                    f"2. Отменить судебный приказ от {order_date_long}, "
                    f"вынесенный {court_instrumental} "
                    f"по делу/производству {case_identifier}."
                ),
                "3. Направить мне копию определения об отмене судебного приказа по адресу, указанному в настоящих возражениях.",
            ]
        return [
            render_value(data, "order_facts_sentence"),
            f"Копия судебного приказа получена мной {date_long_text(ctx.received_date)}.",
            "С судебным приказом не согласен, возражаю относительно его исполнения в полном объеме.",
            "На основании изложенного, руководствуясь статьями 128, 129 ГПК РФ,",
            "ПРОШУ:",
            (
                f"1. Отменить судебный приказ от {render_value(data, 'order_date_long')}, "
                f"вынесенный {render_value(data, 'court_instrumental')} "
                f"по делу/производству {render_value(data, 'case_identifier')}."
            ),
            "2. Направить мне копию определения об отмене судебного приказа по адресу, указанному в настоящих возражениях.",
        ]
    if restore_term:
        return statement_restore_term(ctx)
    return statement_in_time(ctx)


def build_attachments(ctx: StatementContext) -> list[str]:
    data = ctx.data
    order_date = render_value(data, "order_date_long") or date_long_text(_required(data, "order_date"))
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
    rendered = render_value(data, "debtor_short_name")
    if rendered:
        return rendered
    short = _optional(data, "debtor_short_name")
    if short:
        return short
    return make_short_name(_required(data, "debtor_full_name"))
