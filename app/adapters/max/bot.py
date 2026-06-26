from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.adapters.max import keyboards
from app.adapters.max.client import MaxBotClient
from app.adapters.max.mapper import IncomingEvent, parse_update
from app.adapters.max.state import max_state_manager
from app.config import Settings
from app.database import SessionLocal
from app.enums import CaseStatus
from app.models import Case, User
from app.services.app_settings import payments_enabled
from app.services.cases import create_case, latest_case, latest_open_case, save_photo_path, set_received_date
from app.services.crm_background import schedule_crm_sync
from app.services.document_delivery import deliver_documents_to_case_platform
from app.services.documents import create_case_documents, extraction_preview
from app.services.legal_data import FIELD_LABELS, missing_order_fields, normalize_debtor_name_fields, normalize_order_data, validate_before_generation
from app.services.llm import extract_envelope_date, extract_order_data
from app.services.payments import ensure_payment
from app.services.users import get_or_create_platform_user
from app.texts import case_summary, payment_text, profile_text, welcome_text
from app.utils import ensure_dir, h, parse_russian_date

logger = logging.getLogger(__name__)

STATE_ORDER_PHOTO = "max_waiting_order_photo"
STATE_ORDER_REPHOTO = "max_waiting_order_rephoto"
STATE_ENVELOPE = "max_waiting_envelope"
STATE_MANUAL_DATE = "max_waiting_manual_date"
STATE_FIELD_VALUE = "max_waiting_field_value"


async def _send(client: MaxBotClient, event: IncomingEvent, text: str, keyboard=None) -> None:
    await client.send_message(chat_id=event.chat_id, text=text, keyboard=keyboard)


async def _state(session, event: IncomingEvent) -> str | None:
    return await max_state_manager.get_state(session, "max", event.platform_user_id)


async def _state_data(session, event: IncomingEvent) -> dict[str, Any]:
    return await max_state_manager.get_data(session, "max", event.platform_user_id)


async def _set_state(session, event: IncomingEvent, state: str | None, data: dict[str, Any] | None = None) -> None:
    await max_state_manager.set_state(session, "max", event.platform_user_id, state, data or {})


async def _clear_state(session, event: IncomingEvent) -> None:
    await max_state_manager.clear(session, "max", event.platform_user_id)


def _missing_order_labels(missing: list[str]) -> list[str]:
    labels: list[str] = []
    for field in missing:
        if field == "case_number_or_uid":
            labels.append("номер дела или УИД")
        elif field == "state_duty_or_total_amount":
            labels.append("госпошлина или итоговая сумма")
        elif field == "received_date":
            labels.append("дата получения")
        elif field == "amount_mismatch":
            labels.append("суммы задолженности не совпали")
        else:
            labels.append(FIELD_LABELS.get(field, field))
    return labels


async def _send_order_rephoto_prompt(client: MaxBotClient, event: IncomingEvent, missing: list[str], *, attempts: int = 0, max_attempts: int = 3) -> None:
    labels = "\n".join(f"— {label}" for label in _missing_order_labels(missing))
    if attempts >= max_attempts:
        await _send(
            client,
            event,
            "Не удалось надежно прочитать приказ автоматически. Я передал заявку специалисту. Мы поможем подготовить заявление вручную.",
            keyboards.order_rephoto_menu(),
        )
        return
    await _send(
        client,
        event,
        "Не удалось надежно прочитать судебный приказ.\n\n"
        f"Не распознаны поля:\n{labels}\n\n"
        "Пожалуйста, сфотографируйте судебный приказ целиком ещё раз:\n"
        "— весь лист должен быть в кадре;\n"
        "— без бликов;\n"
        "— текст должен быть резким;\n"
        "— лучше отправить как файл без сжатия.\n\n"
        "После нового фото я снова подготовлю заявление.",
        keyboards.order_rephoto_menu(),
    )


async def _download_event_image(client: MaxBotClient, event: IncomingEvent, case_id: int, kind: str, settings: Settings) -> Path:
    ensure_dir(settings.max_download_dir)
    suffix = ".jpg"
    url = event.photo_url or event.document_url
    if event.document_name:
        suffix = Path(event.document_name).suffix or suffix
    path = Path(settings.max_download_dir) / f"case_{case_id}_{kind}{suffix}"
    if not url:
        raise RuntimeError("MAX event has no downloadable image/file URL")
    path.write_bytes(await client.download_file(url))
    return path



async def _recover_state_for_input(session, event: IncomingEvent, user: User, *, has_attachment: bool, is_date_text: bool) -> str | None:
    case = await latest_open_case(session, user.id)
    if not case:
        return None
    if has_attachment and (case.status in {CaseStatus.WAITING_ORDER_PHOTO.value, CaseStatus.WAITING_ORDER_REPHOTO.value} or not case.order_photo_path):
        state = STATE_ORDER_REPHOTO if case.status == CaseStatus.WAITING_ORDER_REPHOTO.value else STATE_ORDER_PHOTO
        await _set_state(session, event, state, {"case_id": case.id})
        logger.info("MAX state recovered as order image case_id=%s user_id=%s", case.id, user.id)
        return state
    if case.order_photo_path and not case.received_date and (has_attachment or is_date_text):
        state = STATE_ENVELOPE if has_attachment else STATE_MANUAL_DATE
        await _set_state(session, event, state, {"case_id": case.id})
        logger.info("MAX state recovered as %s case_id=%s user_id=%s", state, case.id, user.id)
        return state
    return None

async def handle_update(client: MaxBotClient, event: IncomingEvent, settings: Settings) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_platform_user(
            session,
            "max",
            event.platform_user_id,
            settings,
            username=event.username,
            first_name=event.first_name,
            last_name=event.last_name,
        )
        if event.callback_id:
            await client.answer_callback(event.callback_id)
        data = event.callback_data
        current_state = await _state(session, event)
        has_attachment = bool(event.photo_url or event.document_url)
        is_date_text = bool(event.text and parse_russian_date(event.text))
        if not current_state and (has_attachment or is_date_text):
            current_state = await _recover_state_for_input(session, event, user, has_attachment=has_attachment, is_date_text=is_date_text)

        if data == "menu:main" or (data is None and event.text == "/start"):
            await _clear_state(session, event)
            await _send(client, event, welcome_text(settings.company_name), keyboards.main_menu())
            return
        if data == "profile:show":
            await _send(client, event, profile_text(user, await latest_case(session, user.id)), keyboards.profile_menu())
            return
        if data == "case:my":
            case = await latest_case(session, user.id)
            if not case:
                await _send(client, event, "Заявлений пока нет. Начните с кнопки «Подготовить заявление».", keyboards.main_menu())
            else:
                await _send(client, event, case_summary(case), keyboards.case_menu(can_pay=case.status == CaseStatus.PAYMENT_PENDING.value, payment_url=case.payment_url))
            return
        if data == "case:new":
            case = await latest_open_case(session, user.id)
            is_empty_waiting_case = bool(
                case
                and case.status == CaseStatus.WAITING_ORDER_PHOTO.value
                and not case.order_photo_path
            )
            if not is_empty_waiting_case:
                case = await create_case(session, user, chat_id=event.chat_id)
                schedule_crm_sync(settings, case.id, user.id, "user_started_bot", {"note": "MAX: пользователь начал оформление"})
            await _set_state(session, event, STATE_ORDER_PHOTO, {"case_id": case.id})
            await _send(
                client,
                event,
                "📝 <b>Новое заявление</b>\n\nОтправьте фото судебного приказа целиком.\n\nПосле даты я сразу подготовлю preview PDF и ссылку на оплату.",
            )
            return
        if data == "case:rephoto_order":
            case = await latest_open_case(session, user.id)
            if not case:
                await _send(client, event, "Не нашел активное заявление. Начните заново.", keyboards.main_menu())
                return
            await _set_state(session, event, STATE_ORDER_REPHOTO, {"case_id": case.id})
            await _send(
                client,
                event,
                "Пожалуйста, отправьте фото судебного приказа ещё раз. Весь лист должен быть в кадре, без бликов.",
                keyboards.order_rephoto_menu(),
            )
            return
        if data == "chat:start":
            case = await latest_open_case(session, user.id)
            await _send(client, event, settings.manager_contact_text, keyboards.chat_end_menu())
            if case:
                schedule_crm_sync(settings, case.id, user.id, "manager_requested", {"note": "MAX: пользователь запросил менеджера"})
            return
        if data == "case:manual_date":
            case = await latest_open_case(session, user.id)
            if not case:
                await _send(client, event, "Не нашел активное заявление.", keyboards.main_menu())
                return
            await _set_state(session, event, STATE_MANUAL_DATE, {"case_id": case.id})
            await _send(client, event, "Напишите дату получения копии приказа. Пример: <code>19.06.2026</code>")
            return
        if data == "case:envelope_photo":
            case = await latest_open_case(session, user.id)
            if not case:
                await _send(client, event, "Не нашел активное заявление.", keyboards.main_menu())
                return
            await _set_state(session, event, STATE_ENVELOPE, {"case_id": case.id})
            await _send(client, event, "Отправьте фото конверта так, чтобы были видны штампы с датами.")
            return
        if data == "case:review":
            if not (user.is_admin or settings.show_user_confirmation_step):
                await _send(client, event, "Эта функция недоступна.")
                return
            await _send_review(client, event, session, user)
            return
        if data == "case:edit_fields":
            if not user.is_admin:
                await _send(client, event, "Эта функция доступна только админу.")
                return
            await _send(client, event, "✏️ Выберите поле для исправления.", keyboards.edit_fields_menu())
            return
        if data and data.startswith("case:field:"):
            if not user.is_admin:
                await _send(client, event, "Эта функция доступна только админу.")
                return
            field = data.split(":")[-1]
            case = await latest_open_case(session, user.id)
            current = "пусто"
            if case and case.extracted_json:
                current = normalize_order_data(json.loads(case.extracted_json or "{}")).get(field) or "пусто"
            await _set_state(session, event, STATE_FIELD_VALUE, {"case_id": case.id if case else None, "field": field})
            await _send(client, event, f"Введите новое значение для поля <b>{FIELD_LABELS.get(field, field)}</b>.\n\nСейчас: <code>{h(current)}</code>")
            return
        if data == "case:generate":
            if not (user.is_admin or settings.show_user_confirmation_step):
                await _send(client, event, "Эта функция недоступна.")
                return
            case = await latest_open_case(session, user.id)
            if not case:
                await _send(client, event, "Не нашел активное заявление.", keyboards.main_menu())
                return
            schedule_crm_sync(settings, case.id, user.id, "case_data_confirmed", {"note": "MAX: данные подтверждены"})
            await _generate_documents(client, event, session, settings, user, case)
            return
        if data == "payment:check":
            case = await latest_open_case(session, user.id)
            if not case or case.status != CaseStatus.PAID.value:
                await client.answer_callback(event.callback_id, "Пока не вижу оплату")
                return
            await deliver_documents_to_case_platform(case.id, settings)
            await _send(client, event, "Документы отправлены.", keyboards.case_menu())
            return

        if current_state in {STATE_ORDER_PHOTO, STATE_ORDER_REPHOTO} and (event.photo_url or event.document_url):
            await _handle_order_image(client, event, session, settings, user)
            return
        if current_state in {STATE_ORDER_PHOTO, STATE_ORDER_REPHOTO} and event.text:
            await _send(
                client,
                event,
                "Нужно фото судебного приказа. Отправьте изображение целиком или файл-картинку без сжатия.",
                keyboards.order_rephoto_menu(),
            )
            return
        if current_state == STATE_ENVELOPE and (event.photo_url or event.document_url):
            await _handle_envelope_image(client, event, session, settings, user)
            return
        if current_state in {STATE_ENVELOPE, STATE_MANUAL_DATE} and event.text:
            await _handle_manual_date(client, event, session, settings, user, event.text)
            return
        if current_state == STATE_FIELD_VALUE and event.text:
            await _handle_field_value(client, event, session, user, event.text)
            return

        await _send(client, event, welcome_text(settings.company_name), keyboards.main_menu())


async def _handle_order_image(client: MaxBotClient, event: IncomingEvent, session, settings: Settings, user: User) -> None:
    data = await _state_data(session, event)
    case = await session.get(Case, data["case_id"])
    path = await _download_event_image(client, event, case.id, "order", settings)
    await save_photo_path(session, case, "order", path)
    await _set_state(session, event, STATE_ENVELOPE, {"case_id": case.id})
    await _send(client, event, "✅ Приказ принят. Теперь отправьте конверт со штампами или напишите дату получения.", keyboards.envelope_choice())
    schedule_crm_sync(settings, case.id, user.id, "order_photo_uploaded", {"note": "MAX: загружен приказ", "files": [{"path": str(path), "caption": "Фото приказа"}]})


async def _handle_envelope_image(client: MaxBotClient, event: IncomingEvent, session, settings: Settings, user: User) -> None:
    data = await _state_data(session, event)
    case = await session.get(Case, data["case_id"])
    path = await _download_event_image(client, event, case.id, "envelope", settings)
    await save_photo_path(session, case, "envelope", path)
    await _send(client, event, "✅ Конверт принят. Считываю дату и приказ, это может занять минуту.")
    try:
        envelope = await extract_envelope_date(settings, session, case_id=case.id, user_id=user.id, envelope_photo_path=str(path))
        received = parse_russian_date(envelope.get("latest_date_normalized") or envelope.get("latest_date"))
    except Exception:
        logger.exception("MAX envelope extraction failed")
        received = None
    if not received:
        await _set_state(session, event, STATE_MANUAL_DATE, {"case_id": case.id})
        await _send(
            client,
            event,
            "Не удалось надежно прочитать дату на конверте.\n\nСфотографируйте конверт крупнее: должны быть видны все штампы с датами, или введите дату вручную.",
            keyboards.envelope_choice(),
        )
        return
    await set_received_date(session, case, received)
    schedule_crm_sync(settings, case.id, user.id, "envelope_photo_uploaded", {"received_date": received.strftime("%d.%m.%Y"), "files": [{"path": str(path), "caption": "Фото конверта"}]})
    await _extract_and_process_order(client, event, session, settings, user, case)


async def _handle_manual_date(client: MaxBotClient, event: IncomingEvent, session, settings: Settings, user: User, text: str) -> None:
    received = parse_russian_date(text)
    if not received:
        await _send(client, event, "Не смог распознать дату. Напишите в формате <code>ДД.ММ.ГГГГ</code>.", keyboards.envelope_choice())
        return
    data = await _state_data(session, event)
    case = await session.get(Case, data["case_id"])
    await set_received_date(session, case, received)
    schedule_crm_sync(settings, case.id, user.id, "received_date_entered", {"received_date": received.strftime("%d.%m.%Y")})
    await _extract_and_process_order(client, event, session, settings, user, case)


async def _extract_and_process_order(client: MaxBotClient, event: IncomingEvent, session, settings: Settings, user: User, case: Case) -> None:
    await _clear_state(session, event)
    await _send(client, event, "🔎 Считываю приказ и собираю данные для заявления.")
    try:
        extracted = await extract_order_data(settings, session, case_id=case.id, user_id=user.id, order_photo_path=case.order_photo_path)
    except Exception:
        logger.exception("MAX order extraction failed")
        extracted = {}
    extracted = normalize_order_data(extracted)
    extracted, _ = normalize_debtor_name_fields(extracted)
    missing = missing_order_fields(extracted, case.received_date)
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    case.missing_fields = json.dumps(missing, ensure_ascii=False)
    if missing:
        case.order_rephoto_attempts = (case.order_rephoto_attempts or 0) + 1
        case.status = CaseStatus.WAITING_ORDER_REPHOTO.value
    else:
        case.order_rephoto_attempts = 0
        case.status = CaseStatus.PROCESSING.value
    await session.commit()
    schedule_crm_sync(settings, case.id, user.id, "ocr_completed", {"note": "MAX: OCR завершен"})
    if missing:
        await _send_order_rephoto_prompt(client, event, missing, attempts=case.order_rephoto_attempts)
        schedule_crm_sync(settings, case.id, user.id, "document_qa_failed", {"note": "MAX: не удалось прочитать обязательные поля приказа"})
        if case.order_rephoto_attempts >= 3:
            case.status = CaseStatus.NEEDS_REVIEW.value
            await session.commit()
            for admin_id in settings.max_admin_ids or settings.admin_ids:
                try:
                    await client.send_message(
                        user_id=admin_id,
                        text=f"⚠️ Заявка #{case.id}: приказ не распознан после 3 попыток. Нужна ручная обработка.",
                    )
                except Exception:
                    logger.exception("Failed to notify MAX admin %s about repeated rephoto", admin_id)
        return
    if settings.show_user_confirmation_step:
        await _send(client, event, extraction_preview(extracted, case.received_date, missing, case.deadline_date), keyboards.confirm_extraction())
        return
    await _generate_documents(client, event, session, settings, user, case)


async def _send_review(client: MaxBotClient, event: IncomingEvent, session, user: User) -> None:
    case = await latest_open_case(session, user.id)
    if not case:
        await _send(client, event, "Не нашел активное заявление.", keyboards.main_menu())
        return
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    missing = missing_order_fields(data, case.received_date)
    await _send(client, event, extraction_preview(data, case.received_date, missing, case.deadline_date), keyboards.confirm_extraction())


async def _handle_field_value(client: MaxBotClient, event: IncomingEvent, session, user: User, value: str) -> None:
    state_data = await _state_data(session, event)
    case = await session.get(Case, state_data["case_id"])
    field = state_data["field"]
    extracted = normalize_order_data(json.loads(case.extracted_json or "{}"))
    extracted[field] = value.strip()
    extracted = normalize_order_data(extracted)
    missing = missing_order_fields(extracted, case.received_date)
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    case.missing_fields = json.dumps(missing, ensure_ascii=False)
    case.status = CaseStatus.NEEDS_REVIEW.value if missing else CaseStatus.PROCESSING.value
    await session.commit()
    await _clear_state(session, event)
    await _send(client, event, "✅ Поле обновлено.")
    await _send(client, event, extraction_preview(extracted, case.received_date, missing, case.deadline_date), keyboards.confirm_extraction())


async def _generate_documents(client: MaxBotClient, event: IncomingEvent, session, settings: Settings, user: User, case: Case) -> None:
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    validation = validate_before_generation(data, case.received_date)
    if not validation.ok:
        case.status = CaseStatus.NEEDS_REVIEW.value
        case.missing_fields = json.dumps(validation.missing, ensure_ascii=False)
        await session.commit()
        await _set_state(session, event, STATE_ORDER_REPHOTO, {"case_id": case.id})
        await _send_order_rephoto_prompt(client, event, validation.missing, attempts=case.order_rephoto_attempts)
        return
    await _send(client, event, "📄 Готовлю документы и preview PDF.")
    try:
        full_docx, full_pdf, preview_pdf, preview_docx, instruction = create_case_documents(case, user, settings)
    except Exception as exc:
        logger.exception("MAX document generation failed")
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        schedule_crm_sync(settings, case.id, user.id, "document_qa_failed", {"note": str(exc)})
        await _set_state(session, event, STATE_ORDER_REPHOTO, {"case_id": case.id})
        await _send(
            client,
            event,
            f"⚠️ {h(exc)}\n\nПожалуйста, сфотографируйте судебный приказ целиком ещё раз.",
            keyboards.order_rephoto_menu(),
        )
        return
    case.full_doc_path = str(full_docx)
    case.full_pdf_path = str(full_pdf) if full_pdf else None
    case.preview_pdf_path = str(preview_pdf) if preview_pdf else None
    case.preview_doc_path = str(preview_docx) if preview_docx else None
    case.instruction_path = str(instruction)
    case.status = CaseStatus.PREVIEW_READY.value
    await session.commit()
    schedule_crm_sync(settings, case.id, user.id, "preview_generated", {"note": "MAX: preview сформирован"})
    preview_file = preview_pdf or preview_docx
    if not payments_enabled():
        if preview_file:
            await client.send_file(event.chat_id, preview_file, caption="Предпросмотр заявления.")
        await _send(client, event, "🧪 Оплата выключена. Отправляю полный комплект.")
        await deliver_documents_to_case_platform(case.id, settings)
        return
    if settings.require_pdf_preview_for_payment and not preview_pdf:
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        await _send(client, event, "⚠️ Preview PDF не создан, платеж не сформирован. Нужен LibreOffice/PyMuPDF.")
        return
    payment = await ensure_payment(session, case, settings)
    schedule_crm_sync(settings, case.id, user.id, "payment_created", {"note": f"MAX: платеж {case.payment_label}"})
    if preview_file:
        await client.send_file(event.chat_id, preview_file, caption="Скрытый предпросмотр заявления.")
    await _send(client, event, payment_text(case, payment.amount), keyboards.case_menu(can_pay=True, payment_url=case.payment_url))


async def run_max_bot(settings: Settings) -> None:
    marker: int | None = None
    async with MaxBotClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        upload_retry_attempts=settings.max_upload_retry_attempts,
        upload_retry_base_seconds=settings.max_upload_retry_base_seconds,
    ) as client:
        logger.info("MAX polling started")
        while True:
            try:
                payload = await client.get_updates(marker=marker, timeout=settings.max_longpoll_timeout_seconds)
                marker = payload.get("marker", marker)
                for raw in payload.get("updates", []):
                    event = parse_update(raw)
                    if event:
                        await handle_update(client, event, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MAX polling error")
                await asyncio.sleep(3)

