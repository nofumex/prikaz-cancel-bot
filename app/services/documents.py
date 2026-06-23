from __future__ import annotations

import re
import textwrap
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.models import Case, User
from app.services.legal_data import (
    FIELD_LABELS,
    clean_case_number,
    clean_uid,
    is_deadline_missed,
    normalize_order_data,
    validate_before_generation,
    validate_docx_clean,
)
from app.utils import ensure_dir, h, safe_json_loads


DOCUMENT_DIR = Path("storage/documents")


def _required(data: dict, key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise ValueError(f"Missing required document field: {key}")
    return value


def _optional(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()


def _short_name(full_name: str) -> str:
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    initials = "".join(f"{part[0]}." for part in parts[1:] if part)
    return f"{parts[0]} {initials}".strip()


def _normalize_court_for_addressee(court: str) -> str:
    court = court.strip().rstrip(".")
    lower = court.lower()
    if lower.startswith("мировому судье"):
        return court
    if lower.startswith("мировой судья"):
        return "Мировому судье " + court[len("мировой судья") :].strip()
    if lower.startswith("судебный участок"):
        normalized = re.sub(r"^судебный участок", "судебного участка", court, flags=re.IGNORECASE)
        normalized = re.sub(r"№\s*(\d+)", r"№ \1", normalized)
        return "Мировому судье " + normalized
    return court


def _normalize_court_for_body(court: str) -> str:
    court = court.strip().rstrip(".")
    lower = court.lower()
    if lower.startswith("мировому судье"):
        rest = court[len("мировому судье") :].strip()
        return f"мировым судьей {rest}" if rest else "мировым судьей"
    if lower.startswith("мировой судья"):
        rest = court[len("мировой судья") :].strip()
        return f"мировым судьей {rest}" if rest else "мировым судьей"
    if lower.startswith("судебный участок"):
        normalized = re.sub(r"^судебный участок", "судебного участка", court, flags=re.IGNORECASE)
        normalized = re.sub(r"№\s*(\d+)", r"№ \1", normalized)
        return f"мировым судьей {normalized}"
    return court


def _case_identifier(data: dict) -> str:
    case_number = clean_case_number(_required(data, "case_number"))
    uid = clean_uid(_optional(data, "uid"))
    parts = [f"№ {case_number}"]
    if uid:
        parts.append(f"УИД {uid}")
    return ", ".join(parts)


def _contract_phrase(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ,.;")
    lower = value.lower()
    if lower.startswith("по "):
        return value
    if lower.startswith(("договор", "кредит", "карта", "счет", "счёт")):
        return f"по {value}"
    if value.startswith("№"):
        return f"по договору {value}"
    return f"по договору {value}"


def _period_phrase(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ,.;")
    value = re.sub(r"^(за\s+период|период|за)\s+", "", value, flags=re.IGNORECASE).strip(" ,.;")
    return f"за период {value}"


def _money_sentence(data: dict) -> str:
    debt = _required(data, "debt_amount")
    state_duty = _optional(data, "state_duty")
    total = _optional(data, "total_amount")
    parts = [f"задолженности в размере {debt}"]
    if state_duty:
        parts.append(f"расходов по оплате государственной пошлины в размере {state_duty}")
    if total:
        parts.append(f"всего {total}")
    return ", ".join(parts)


def _set_cell_borderless(cell) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_borders = tc_pr.first_child_found_in("w:tcBorders")
    if tc_borders is None:
        tc_borders = OxmlElement("w:tcBorders")
        tc_pr.append(tc_borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = "w:" + edge
        element = tc_borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            tc_borders.append(element)
        element.set(qn("w:val"), "nil")


def _setup_styles(doc: Document) -> None:
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(1.7)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    normal.font.size = Pt(12)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15


def _paragraph(
    doc: Document,
    text: str = "",
    *,
    bold: bool = False,
    size: int | None = None,
    align=None,
    before=0,
    after=6,
    first_line=False,
):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(before)
    p.paragraph_format.space_after = Pt(after)
    p.paragraph_format.line_spacing = 1.15
    if first_line:
        p.paragraph_format.first_line_indent = Cm(1.25)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if align is not None:
        p.alignment = align
    run = p.add_run(text)
    run.bold = bold
    if size:
        run.font.size = Pt(size)
    return p


def _address_block(doc: Document, data: dict, user: User) -> None:
    table = doc.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.RIGHT
    table.autofit = False
    table.columns[0].width = Cm(7.0)
    table.columns[1].width = Cm(9.2)
    left, right = table.rows[0].cells
    _set_cell_borderless(left)
    _set_cell_borderless(right)
    right.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    lines = [
        _normalize_court_for_addressee(_required(data, "court_name")),
        _required(data, "court_address"),
        "",
        f"Должник: {_required(data, 'debtor_full_name')}",
        _optional(data, "debtor_birth_date"),
        _optional(data, "debtor_passport"),
        f"Адрес: {_required(data, 'debtor_address')}",
    ]
    if user.phone:
        lines.append(f"Тел.: {user.phone}")
    lines.extend(
        [
            "",
            f"Взыскатель: {_required(data, 'creditor_name')}",
            _optional(data, "creditor_address"),
        ]
    )
    identifiers = " ".join(
        part
        for part in [
            f"ИНН {_optional(data, 'creditor_inn')}" if _optional(data, "creditor_inn") else "",
            f"ОГРН {_optional(data, 'creditor_ogrn')}" if _optional(data, "creditor_ogrn") else "",
        ]
        if part
    )
    if identifiers:
        lines.append(identifiers)
    for line in lines:
        p = right.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        if line:
            p.add_run(line)
    doc.add_paragraph()


def _meta_line(doc: Document, data: dict) -> None:
    p = _paragraph(doc, f"Дело/производство {_case_identifier(data)}", size=11, align=WD_ALIGN_PARAGRAPH.RIGHT, after=10)
    for run in p.runs:
        run.font.color.rgb = RGBColor(90, 90, 90)


def build_statement_paragraphs(data: dict, received_date: date, deadline_date: date | None) -> list[str]:
    court_body = _normalize_court_for_body(_required(data, "court_name"))
    case_identifier = _case_identifier(data)
    order_date = _required(data, "order_date")
    creditor = _required(data, "creditor_name")
    contract = _contract_phrase(_required(data, "debt_contract"))
    period = _period_phrase(_required(data, "debt_period"))
    received = received_date.strftime("%d.%m.%Y")
    deadline = deadline_date.strftime("%d.%m.%Y") if deadline_date else ""
    money_part = _money_sentence(data)
    common = [
        f"{order_date} {court_body} вынесен судебный приказ по делу/производству {case_identifier} о взыскании с меня в пользу {creditor} {money_part} {contract} {period}.",
        f"Копия судебного приказа получена мной {received}.",
        "С судебным приказом и заявленными взыскателем требованиями я не согласен. Возражаю относительно исполнения судебного приказа в полном объеме. Считаю требования взыскателя спорными, в том числе в части наличия задолженности, ее размера, периода взыскания, расчета процентов, комиссий, неустоек, государственной пошлины и иных платежей.",
    ]
    if is_deadline_missed(deadline_date):
        common.extend(
            [
                f"Десятидневный срок для подачи возражений истек {deadline}. Прошу восстановить срок, поскольку копия судебного приказа была фактически получена мной {received}, а возможность своевременно обратиться в суд зависит от даты фактического получения судебного акта и подтверждается представленными документами.",
                "В соответствии со статьями 112, 128, 129 ГПК РФ пропущенный процессуальный срок может быть восстановлен судом при наличии уважительных причин, а при поступлении возражений должника относительно исполнения судебного приказа судья отменяет судебный приказ.",
                "На основании изложенного, руководствуясь статьями 112, 128, 129 ГПК РФ,",
                "ПРОШУ:",
                "1. Восстановить срок для подачи возражений относительно исполнения судебного приказа.",
                f"2. Отменить судебный приказ от {order_date}, вынесенный {court_body} по делу/производству {case_identifier}, о взыскании задолженности в пользу {creditor}.",
                "3. Направить мне копию определения об отмене судебного приказа по адресу, указанному в настоящем заявлении.",
            ]
        )
    else:
        common.extend(
            [
                f"Настоящие возражения подаются в установленный статьей 128 ГПК РФ десятидневный срок со дня получения копии судебного приказа. Последний день срока с учетом правил исчисления процессуальных сроков: {deadline}.",
                "В соответствии со статьей 129 ГПК РФ при поступлении в установленный срок возражений должника относительно исполнения судебного приказа судья отменяет судебный приказ. После отмены судебного приказа взыскатель вправе обратиться в суд в порядке искового производства.",
                "На основании изложенного, руководствуясь статьями 128, 129 ГПК РФ,",
                "ПРОШУ:",
                f"1. Отменить судебный приказ от {order_date}, вынесенный {court_body} по делу/производству {case_identifier}, о взыскании задолженности в пользу {creditor}.",
                "2. Направить мне копию определения об отмене судебного приказа по адресу, указанному в настоящем заявлении.",
            ]
        )
    return common


def _redacted_line(doc: Document, prefix: str = "") -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.15
    if prefix:
        p.add_run(prefix + " ")
    run = p.add_run("▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒ ▒▒▒▒▒▒▒▒▒▒▒▒▒▒ ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒")
    run.font.name = "Times New Roman"
    run.font.color.rgb = RGBColor(185, 185, 185)
    run.font.size = Pt(12)


def _add_preview_text(doc: Document, text: str, line_no: int) -> int:
    chunks = textwrap.wrap(text, width=92, break_long_words=False, break_on_hyphens=False) or [text]
    for chunk in chunks:
        match = re.match(r"^(\d+\.)\s+", chunk)
        if line_no % 2 == 0:
            _redacted_line(doc, match.group(1) if match else "")
        else:
            _paragraph(doc, chunk, first_line=False, after=2)
        line_no += 1
    return line_no


def _add_body(doc: Document, paragraphs: list[str], *, preview: bool, restore_term: bool) -> None:
    _paragraph(doc, "ЗАЯВЛЕНИЕ", bold=True, size=14, align=WD_ALIGN_PARAGRAPH.CENTER, before=4, after=2)
    subtitle = "об отмене судебного приказа и восстановлении срока" if restore_term else "об отмене судебного приказа"
    _paragraph(doc, subtitle, size=12, align=WD_ALIGN_PARAGRAPH.CENTER, after=14)
    line_no = 1
    for text in paragraphs:
        if text == "ПРОШУ:":
            _paragraph(doc, text, bold=True, align=WD_ALIGN_PARAGRAPH.LEFT, before=6, after=6)
            continue
        if preview:
            line_no = _add_preview_text(doc, text, line_no)
        else:
            _paragraph(doc, text, first_line=True, after=7)


def _add_attachments_and_signature(doc: Document, data: dict, debtor: str, *, preview: bool, restore_term: bool) -> None:
    _paragraph(doc, "Приложения:", bold=True, before=6, after=4)
    items = [
        f"Копия судебного приказа от {_required(data, 'order_date')}.",
        "Копия конверта или иной документ, подтверждающий дату получения судебного приказа.",
        "Копия настоящего заявления для взыскателя.",
    ]
    if restore_term:
        items.insert(2, "Документы, подтверждающие дату фактического получения судебного приказа и причины пропуска срока.")
    line_no = 1
    for i, text in enumerate(items, 1):
        if preview:
            line_no = _add_preview_text(doc, f"{i}. {text}", line_no)
        else:
            _paragraph(doc, f"{i}. {text}", after=3)
    doc.add_paragraph()
    table = doc.add_table(rows=1, cols=2)
    table.autofit = False
    table.columns[0].width = Cm(8.5)
    table.columns[1].width = Cm(7.0)
    for cell in table.rows[0].cells:
        _set_cell_borderless(cell)
    table.rows[0].cells[0].paragraphs[0].add_run("Дата подачи: поставить от руки")
    right = table.rows[0].cells[1].paragraphs[0]
    right.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    right.add_run("Подпись: поставить от руки")
    _paragraph(doc, _short_name(debtor), align=WD_ALIGN_PARAGRAPH.RIGHT, after=0)
    if preview:
        p = _paragraph(doc, "Предпросмотр: каждая вторая строка скрыта до оплаты.", size=10, align=WD_ALIGN_PARAGRAPH.CENTER, before=10, after=0)
        p.runs[0].font.color.rgb = RGBColor(120, 120, 120)


def create_case_documents(case: Case, user: User) -> tuple[Path, Path, Path]:
    ensure_dir(DOCUMENT_DIR)
    case_dir = ensure_dir(DOCUMENT_DIR / f"case_{case.id}")
    data = normalize_order_data(safe_json_loads(case.extracted_json, {}))
    validation = validate_before_generation(data, case.received_date)
    if not validation.ok:
        labels = [FIELD_LABELS.get(field, field) for field in validation.missing]
        raise ValueError("Нельзя сформировать заявление: " + ", ".join(labels))
    if not case.received_date:
        raise ValueError("Нельзя сформировать заявление без даты получения")

    restore_term = is_deadline_missed(case.deadline_date)
    debtor = _required(data, "debtor_full_name")
    paragraphs = build_statement_paragraphs(data, case.received_date, case.deadline_date)
    suffix = "s_vosstanovleniem_sroka" if restore_term else "ob_otmene_prikaza"
    full_path = case_dir / f"zayavlenie_{suffix}_{case.id}.docx"
    preview_path = case_dir / f"preview_zayavlenie_{suffix}_{case.id}.docx"
    instruction_path = case_dir / f"instruktsiya_po_otpravke_{case.id}.txt"

    for path, preview in ((full_path, False), (preview_path, True)):
        doc = Document()
        _setup_styles(doc)
        _address_block(doc, data, user)
        _meta_line(doc, data)
        _add_body(doc, paragraphs, preview=preview, restore_term=restore_term)
        _add_attachments_and_signature(doc, data, debtor, preview=preview, restore_term=restore_term)
        doc.save(path)
        bad_tokens = validate_docx_clean(str(path))
        if bad_tokens:
            raise ValueError(f"Документ не прошел стоп-лист: {', '.join(bad_tokens)}")

    deadline = case.deadline_date.strftime("%d.%m.%Y") if case.deadline_date else "уточняется"
    extra = (
        "Так как срок уже пропущен, заявление включает ходатайство о восстановлении срока. Приложите доказательства даты фактического получения приказа.\n"
        if restore_term
        else "Документы можно подать лично в канцелярию или отправить почтой до 24:00 последнего дня срока.\n"
    )
    instruction_path.write_text(
        "Инструкция по отправке заявления в суд\n\n"
        f"Срок на подачу: до {deadline}.\n"
        f"{extra}\n"
        "1. Распечатайте заявление в 2 экземплярах.\n"
        "2. Проверьте реквизиты суда, номер дела, ФИО, адрес и суммы.\n"
        "3. Поставьте дату и подпись от руки.\n"
        "4. Приложите копию судебного приказа и конверт/иной документ с датой получения.\n"
        "5. Передайте заявление в канцелярию мирового судьи или отправьте заказным письмом с описью вложения.\n"
        "6. Сохраните отметку канцелярии, чек и опись отправления.",
        encoding="utf-8",
    )
    return full_path, preview_path, instruction_path


def extraction_preview(data: dict, received_date: date | None, missing: list[str], deadline_date: date | None = None) -> str:
    data = normalize_order_data(data)
    lines = [
        "🔎 <b>Проверьте данные</b>",
        "",
        f"<b>Суд:</b> {h(data.get('court_name') or 'не заполнено')}",
        f"<b>Адрес суда:</b> {h(data.get('court_address') or 'не заполнено')}",
        f"<b>Должник:</b> {h(data.get('debtor_full_name') or 'не заполнено')}",
        f"<b>Адрес должника:</b> {h(data.get('debtor_address') or 'не заполнено')}",
        f"<b>Взыскатель:</b> {h(data.get('creditor_name') or 'не заполнено')}",
        f"<b>Номер дела:</b> {h(data.get('case_number') or 'не заполнено')}",
        f"<b>УИД:</b> {h(data.get('uid') or 'нет в приказе')}",
        f"<b>Дата приказа:</b> {h(data.get('order_date') or 'не заполнено')}",
        f"<b>Договор:</b> {h(data.get('debt_contract') or 'не заполнено')}",
        f"<b>Период:</b> {h(data.get('debt_period') or 'не заполнено')}",
        f"<b>Сумма долга:</b> {h(data.get('debt_amount') or 'не заполнено')}",
        f"<b>Госпошлина:</b> {h(data.get('state_duty') or 'не указана')}",
        f"<b>Дата получения:</b> {received_date.strftime('%d.%m.%Y') if received_date else 'не указана'}",
    ]
    if deadline_date:
        lines.append(f"<b>Срок до:</b> {deadline_date.strftime('%d.%m.%Y')}")
    if missing:
        labels = [FIELD_LABELS.get(field, field) for field in missing]
        lines.extend(["", "⚠️ <b>Перед генерацией нужно заполнить:</b>", ", ".join(labels)])
    else:
        lines.extend(["", "Если все верно, можно готовить документы. Если видите ошибку OCR, исправьте поле кнопкой ниже."])
    return "\n".join(lines)
