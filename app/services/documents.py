from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.models import Case, User
from app.utils import ensure_dir, safe_json_loads


DOCUMENT_DIR = Path("storage/documents")
MISSING = "____________________________"


def _value(data: dict, key: str, fallback: str = MISSING) -> str:
    value = str(data.get(key) or "").strip()
    return value or fallback


def _optional(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()


def _short_name(full_name: str) -> str:
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    initials = "".join(f"{p[0]}." for p in parts[1:] if p)
    return f"{parts[0]} {initials}".strip()


def _normalize_court_for_addressee(court: str) -> str:
    court = court.strip().rstrip(".")
    if not court or court == MISSING:
        return MISSING
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
    if not court or court == MISSING:
        return "мировым судьей"
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


def _clean_number(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_uid(value: str) -> str:
    value = _clean_number(value)
    value = re.sub(r"^(уид|uid)\s*[:№-]?\s*", "", value, flags=re.IGNORECASE)
    return value.strip(" ,.;")


def _case_identifier(case_number: str, uid: str) -> str:
    parts = [f"№ {_clean_number(case_number)}"]
    clean_uid = _clean_uid(uid)
    if clean_uid:
        parts.append(f"УИД {clean_uid}")
    return ", ".join(parts)


def _contract_phrase(value: str) -> str:
    value = _clean_number(value).strip(" ,.;")
    if not value:
        return "по обязательству, указанному в судебном приказе"
    lower = value.lower()
    if lower.startswith(("по ", "по договор", "по кредит", "по карте")):
        return value
    if lower.startswith(("договор", "кредит", "карта", "счет", "счёт")):
        return f"по {value}"
    if value.startswith("№"):
        return f"по договору {value}"
    return f"по договору {value}"


def _period_phrase(value: str) -> str:
    value = _clean_number(value).strip(" ,.;")
    if not value:
        return "за период, указанный в судебном приказе"
    value = re.sub(r"^(за\s+период|период|за)\s+", "", value, flags=re.IGNORECASE).strip(" ,.;")
    return f"за период {value}" if value else "за период, указанный в судебном приказе"


def _set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


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

    for name in ("Heading 1", "Heading 2"):
        style = doc.styles[name]
        style.font.name = "Times New Roman"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
        style.font.color.rgb = RGBColor(0, 0, 0)


def _paragraph(doc: Document, text: str = "", *, bold: bool = False, size: int | None = None, align=None, before=0, after=6, first_line=False):
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
    table.columns[0].width = Cm(7.2)
    table.columns[1].width = Cm(9.0)
    left, right = table.rows[0].cells
    _set_cell_borderless(left)
    _set_cell_borderless(right)
    right.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    lines = [
        _normalize_court_for_addressee(_value(data, "court_name")),
        _optional(data, "court_address"),
        "",
        f"Должник: {_value(data, 'debtor_full_name')}",
        _optional(data, "debtor_birth_date"),
        _optional(data, "debtor_passport"),
        f"Адрес: {_value(data, 'debtor_address')}",
    ]
    if user.phone:
        lines.append(f"Тел.: {user.phone}")
    creditor_lines = [
        "",
        f"Взыскатель: {_value(data, 'creditor_name')}",
        _optional(data, "creditor_address"),
    ]
    identifiers = " ".join(part for part in [f"ИНН {_optional(data, 'creditor_inn')}" if _optional(data, "creditor_inn") else "", f"ОГРН {_optional(data, 'creditor_ogrn')}" if _optional(data, "creditor_ogrn") else ""] if part)
    if identifiers:
        creditor_lines.append(identifiers)
    lines.extend(creditor_lines)
    for line in lines:
        p = right.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_after = Pt(2)
        if line:
            p.add_run(line)
    doc.add_paragraph()


def _meta_line(doc: Document, data: dict) -> None:
    parts = [f"Дело/производство {_case_identifier(_value(data, 'case_number'), _optional(data, 'uid'))}"]
    p = _paragraph(doc, "    ".join(parts), size=11, align=WD_ALIGN_PARAGRAPH.RIGHT, after=10)
    for run in p.runs:
        run.font.color.rgb = RGBColor(90, 90, 90)


def build_statement_paragraphs(data: dict, received_date: date | None) -> list[str]:
    court_body = _normalize_court_for_body(_value(data, "court_name"))
    case_number = _clean_number(_value(data, "case_number"))
    uid = _clean_uid(_optional(data, "uid"))
    case_identifier = _case_identifier(case_number, uid)
    order_date = _value(data, "order_date")
    creditor = _value(data, "creditor_name")
    contract = _contract_phrase(_optional(data, "debt_contract"))
    period = _period_phrase(_optional(data, "debt_period"))
    debt_amount = _optional(data, "debt_amount")
    received = received_date.strftime("%d.%m.%Y") if received_date else "____.__.20__"
    amount_part = f" в размере {debt_amount}" if debt_amount else ""
    return [
        f"{order_date} {court_body} вынесен судебный приказ по делу/производству {case_identifier} о взыскании с меня в пользу {creditor} задолженности{amount_part} {contract} {period}, а также судебных расходов.",
        f"Копия судебного приказа получена мной {received}. Настоящие возражения подаются в установленный статьей 128 ГПК РФ десятидневный срок со дня получения копии судебного приказа.",
        "С судебным приказом и заявленными взыскателем требованиями я не согласен. Возражаю относительно исполнения судебного приказа в полном объеме. Считаю требования взыскателя спорными, в том числе в части наличия задолженности, ее размера, периода взыскания, расчета процентов, комиссий, неустоек и иных платежей.",
        "В соответствии со статьей 129 ГПК РФ при поступлении в установленный срок возражений должника относительно исполнения судебного приказа судья отменяет судебный приказ. После отмены судебного приказа взыскатель вправе обратиться в суд в порядке искового производства.",
        "На основании изложенного, руководствуясь статьями 128, 129 ГПК РФ,",
        "ПРОШУ:",
        f"1. Отменить судебный приказ от {order_date}, вынесенный {court_body} по делу/производству {case_identifier}, о взыскании задолженности в пользу {creditor}.",
        "2. Направить мне копию определения об отмене судебного приказа по адресу, указанному в настоящем заявлении.",
    ]


def _redacted_paragraph(doc: Document, width: int = 58, prefix: str = "") -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.15
    if prefix:
        p.add_run(prefix + " ")
    run = p.add_run(" ".join(["▒" * 18, "▒" * 14, "▒" * 20])[:width])
    run.font.name = "Times New Roman"
    run.font.color.rgb = RGBColor(185, 185, 185)
    run.font.size = Pt(12)


def _add_body(doc: Document, paragraphs: list[str], *, preview: bool) -> None:
    _paragraph(doc, "ЗАЯВЛЕНИЕ", bold=True, size=14, align=WD_ALIGN_PARAGRAPH.CENTER, before=4, after=2)
    _paragraph(doc, "об отмене судебного приказа", size=12, align=WD_ALIGN_PARAGRAPH.CENTER, after=14)
    for index, text in enumerate(paragraphs):
        if text == "ПРОШУ:":
            _paragraph(doc, text, bold=True, align=WD_ALIGN_PARAGRAPH.LEFT, before=6, after=6)
            continue
        if preview and index % 2 == 1:
            match = re.match(r"^(\d+\.)\s+", text)
            _redacted_paragraph(doc, prefix=match.group(1) if match else "")
            continue
        _paragraph(doc, text, first_line=text not in {"ПРОШУ:"}, after=7)


def _add_attachments_and_signature(doc: Document, data: dict, debtor: str, *, preview: bool) -> None:
    _paragraph(doc, "Приложения:", bold=True, before=6, after=4)
    items = [
        f"Копия судебного приказа от {_value(data, 'order_date')}.",
        "Копия конверта или иной документ, подтверждающий дату получения судебного приказа, при наличии.",
        "Копия настоящего заявления для взыскателя.",
    ]
    for i, text in enumerate(items, 1):
        if preview and i == 2:
            _redacted_paragraph(doc, 42, prefix=f"{i}.")
        else:
            _paragraph(doc, f"{i}. {text}", after=3)
    doc.add_paragraph()
    table = doc.add_table(rows=1, cols=2)
    table.autofit = False
    table.columns[0].width = Cm(8.5)
    table.columns[1].width = Cm(7.0)
    for cell in table.rows[0].cells:
        _set_cell_borderless(cell)
    table.rows[0].cells[0].paragraphs[0].add_run("«___» __________ 20__ г.")
    right = table.rows[0].cells[1].paragraphs[0]
    right.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    right.add_run("_____________________")
    _paragraph(doc, _short_name(debtor), align=WD_ALIGN_PARAGRAPH.RIGHT, after=0)
    if preview:
        p = _paragraph(doc, "Предпросмотр: часть строк скрыта до оплаты.", size=10, align=WD_ALIGN_PARAGRAPH.CENTER, before=10, after=0)
        p.runs[0].font.color.rgb = RGBColor(120, 120, 120)


def create_case_documents(case: Case, user: User) -> tuple[Path, Path, Path]:
    ensure_dir(DOCUMENT_DIR)
    case_dir = ensure_dir(DOCUMENT_DIR / f"case_{case.id}")
    data = safe_json_loads(case.extracted_json, {})
    debtor = _value(data, "debtor_full_name", "Должник")

    paragraphs = build_statement_paragraphs(data, case.received_date)
    full_path = case_dir / f"zayavlenie_ob_otmene_prikaza_{case.id}.docx"
    preview_path = case_dir / f"preview_zayavlenie_ob_otmene_prikaza_{case.id}.docx"
    instruction_path = case_dir / f"instruktsiya_po_otpravke_{case.id}.txt"

    for path, preview in ((full_path, False), (preview_path, True)):
        doc = Document()
        _setup_styles(doc)
        _address_block(doc, data, user)
        _meta_line(doc, data)
        _add_body(doc, paragraphs, preview=preview)
        _add_attachments_and_signature(doc, data, debtor, preview=preview)
        doc.save(path)

    instruction_path.write_text(
        "Инструкция по отправке заявления в суд\n\n"
        "1. Распечатайте заявление в 2 экземплярах.\n"
        "2. Проверьте реквизиты суда, номер дела, ФИО и адрес.\n"
        "3. Поставьте дату и подпись от руки.\n"
        "4. Приложите копию судебного приказа и конверт/иной документ с датой получения.\n"
        "5. Передайте заявление в канцелярию мирового судьи или отправьте заказным письмом с описью вложения.\n"
        "6. Сохраните отметку канцелярии, чек и опись отправления.\n\n"
        "Если 10-дневный срок уже прошел, дополнительно стоит подготовить ходатайство о восстановлении срока.",
        encoding="utf-8",
    )
    return full_path, preview_path, instruction_path


def extraction_preview(data: dict, received_date: date | None, missing: list[str]) -> str:
    lines = [
        "<b>Проверьте найденные данные</b>",
        "",
        f"<b>Суд:</b> {data.get('court_name') or 'не найдено'}",
        f"<b>Должник:</b> {data.get('debtor_full_name') or 'не найдено'}",
        f"<b>Взыскатель:</b> {data.get('creditor_name') or 'не найдено'}",
        f"<b>Номер дела:</b> {data.get('case_number') or 'не найдено'}",
        f"<b>Дата приказа:</b> {data.get('order_date') or 'не найдено'}",
        f"<b>Дата получения:</b> {received_date.strftime('%d.%m.%Y') if received_date else 'не указана'}",
    ]
    if missing:
        lines.extend(["", "<b>Нужно проверить вручную:</b>", ", ".join(missing)])
    return "\n".join(lines)
