from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import CaseStatus, PaymentStatus
from app.keyboards.common import case_menu, confirm_extraction, debtor_name_fix_menu, document_details_menu, documents_menu, edit_fields_menu, envelope_choice, main_menu, ocr_field_confirmation, order_rephoto_menu, phone_request_keyboard, paid_date_required_menu, paid_document_actions, paid_edit_fields_menu, paid_review_menu, restore_reason_menu
from app.models import Case, Payment, User
from app.services.amocrm import get_amocrm_service
from app.services.crm_background import schedule_crm_sync
from app.services.document_delivery import delivery_instruction_text
from app.services.cases import create_case, generated_case_for_user, generated_cases, get_or_create_active_case, latest_case, latest_open_case, save_photo_path, set_received_date
from app.services.documents import MANUAL_REVIEW_USER_TEXT, create_case_documents_reviewed, extraction_preview
from app.services.document_background import start_document_preparation, wait_started_document_preparation
from app.services.app_settings import payments_enabled
from app.services.amount_recovery import (
    AmountRecoveryResult,
    format_amount_mismatch_admin_report,
    recover_amounts_from_mismatch,
    save_amount_debug_snapshot,
)
from app.services.legal_data import (
    FIELD_LABELS,
    AmountValidationResult,
    is_deadline_missed,
    missing_order_fields,
    normalize_debtor_name_fields,
    normalize_order_data,
    suggest_nominative_full_name,
    validate_amounts,
    validate_before_generation,
)
from app.services.llm import extract_envelope_date, extract_order_amounts, extract_order_data
from app.services.order_background import start_order_extraction, wait_order_extraction
from app.services.order_confirmation import (
    apply_confirmation_answer,
    confirmation_crop,
    confirmation_text,
    next_confirmation,
    reduce_and_validate,
)
from app.services.payments import ensure_payment, refresh_yookassa_payment_for_case
from app.services.paid_correction import correction_allowed, paid_regeneration_requires_new_date, record_corrected_field, regenerate_paid_case
from app.services.received_date import received_date_prompt_text, save_received_date, validate_received_date
from app.services.uploaded_documents import normalize_order_upload
from app.services.yookassa import YooKassaError, YooKassaReceiptContactRequired
from app.texts import case_summary, manual_received_date_prompt_text, payment_text
from app.utils import ensure_dir, h, normalize_phone, normalize_receipt_contact, parse_russian_date, parse_structured_date

router = Router(name="case_flow")
logger = logging.getLogger(__name__)


class CaseStates(StatesGroup):
    waiting_order_photo = State()
    waiting_order_rephoto = State()
    waiting_envelope_choice = State()
    waiting_envelope_photo = State()
    waiting_manual_date = State()
    waiting_manual_fields = State()
    waiting_field_value = State()
    waiting_ocr_field_value = State()
    waiting_payment_contact = State()
    waiting_restore_reason = State()
    waiting_restore_reason_custom = State()
    waiting_paid_field_value = State()
    waiting_paid_received_date = State()


async def _download_photo(bot: Bot, message: Message, case_id: int, kind: str) -> Path:
    ensure_dir("storage/photos")
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    suffix = Path(file.file_path or "").suffix or ".jpg"
    path = Path("storage/photos") / f"case_{case_id}_{kind}_{message.message_id}_{photo.file_unique_id}{suffix}"
    await bot.download_file(file.file_path, destination=path)
    return path


async def _download_document_image(bot: Bot, message: Message, case_id: int, kind: str) -> Path:
    ensure_dir("storage/photos")
    doc = message.document
    file = await bot.get_file(doc.file_id)
    suffix = Path(doc.file_name or file.file_path or "").suffix or ".jpg"
    if suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.pdf', '.heic', '.heif'}:
        suffix = '.jpg'
    path = Path("storage/photos") / f"case_{case_id}_{kind}_{message.message_id}_{doc.file_unique_id}{suffix}"
    await bot.download_file(file.file_path, destination=path)
    return path


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


async def _send_order_rephoto_prompt(message: Message, missing: list[str], *, attempts: int = 0, max_attempts: int = 3) -> None:
    labels = "\n".join(f"— {label}" for label in _missing_order_labels(missing))
    text = (
        "Не удалось надежно прочитать судебный приказ.\n\n"
        f"Не распознаны поля:\n{labels}\n\n"
        "Пожалуйста, сфотографируйте судебный приказ целиком ещё раз:\n"
        "— весь лист должен быть в кадре;\n"
        "— без бликов;\n"
        "— текст должен быть резким;\n"
        "— лучше отправить как файл без сжатия.\n\n"
        "После нового фото я снова подготовлю заявление."
    )
    if attempts >= max_attempts:
        text = (
            "Не удалось надежно прочитать приказ автоматически. Я передал заявку специалисту. "
            "Мы поможем подготовить заявление вручную."
        )
        await message.answer(text, reply_markup=order_rephoto_menu())
        return
    await message.answer(text, reply_markup=order_rephoto_menu())


def _resolve_receipt_contact(current_user: User, settings: Settings) -> str | None:
    if settings.yookassa_test_customer_email:
        return settings.yookassa_test_customer_email
    normalized = normalize_receipt_contact(getattr(current_user, "email", None) or getattr(current_user, "phone", None))
    return normalized[1] if normalized else None


async def _request_payment_contact(message: Message, state: FSMContext, case: Case) -> None:
    await state.update_data(case_id=case.id)
    await state.set_state(CaseStates.waiting_payment_contact)
    await message.answer(
        '<b>Укажите свой номер телефона для связи с судом</b>\n\nНажмите кнопку \"Поделиться контактом\" снизу',
        reply_markup=phone_request_keyboard(),
    )


async def _finalize_payment(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User, case: Case) -> bool:
    try:
        payment = await ensure_payment(session, case, settings)
    except YooKassaReceiptContactRequired:
        await _request_payment_contact(message, state, case)
        return False
    except Exception:
        raise
    await state.clear()
    schedule_crm_sync(
        settings,
        case.id,
        current_user.id,
        "payment_created",
        {"note": f"Платеж: {case.payment_label}, сумма {payment.amount} руб."},
    )
    await message.answer(payment_text(case, payment.amount), reply_markup=case_menu(can_pay=True, payment_url=case.payment_url))
    return True


@router.callback_query(F.data == "case:new")
@router.message(F.text == "/new")
async def start_case(event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User, settings: Settings) -> None:
    start = time.monotonic()
    target = event.message if isinstance(event, CallbackQuery) else event
    chat_id = str(target.chat.id) if getattr(target, "chat", None) else current_user.platform_user_id
    previous = await latest_open_case(session, current_user.id)
    case = await get_or_create_active_case(session, current_user, chat_id=chat_id, force_new=True)
    is_new_case = previous is None or previous.id != case.id
    await state.update_data(case_id=case.id)
    await state.set_state(CaseStates.waiting_order_photo)
    await target.answer(
        "📝 <b>Новое заявление</b>\n\n"
        "Отправьте фото судебного приказа целиком.\n\n"
        "Лучше сфотографировать ровно сверху, без обрезанных краев, чтобы были видны суд, номер дела, должник и взыскатель.\n\n"
        "После даты я сразу подготовлю preview PDF и ссылку на оплату."
    )
    logger.info("handler case:new answered_to_user duration_ms=%s", int((time.monotonic() - start) * 1000))
    if is_new_case:
        schedule_crm_sync(settings, case.id, current_user.id, "user_started_bot", {"note": "Пользователь запустил бот"})
    if isinstance(event, CallbackQuery):
        await event.answer()


async def _edit_or_answer(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        await callback.message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == 'case:my:noop')
async def my_documents_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "case:my")
@router.callback_query(F.data.startswith('case:my:'))
async def my_case(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    cases = await generated_cases(session, current_user.id)
    page_size = 5
    requested_page = int(callback.data.rsplit(':', 1)[-1]) if callback.data.count(':') == 2 else 0
    total_pages = max(1, math.ceil(len(cases) / page_size))
    page = max(0, min(requested_page, total_pages - 1))
    page_cases = cases[page * page_size:(page + 1) * page_size]
    if not cases:
        await _edit_or_answer(callback, "Сгенерированных заявлений пока нет. Начните с кнопки «Подготовить заявление».", main_menu())
    else:
        await _edit_or_answer(
            callback,
            '<b>📄 Мои документы</b>\n\nВыберите заявление:',
            documents_menu(page_cases, page, total_pages, len(cases) - page * page_size, descending=True),
        )
    await callback.answer()


@router.callback_query(F.data.startswith('case:document:'))
async def my_document(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    case_id = int(callback.data.rsplit(':', 1)[-1])
    case = await generated_case_for_user(session, current_user.id, case_id)
    if not case or not case.full_doc_path or not Path(case.full_doc_path).exists():
        await callback.answer('Документ не найден.', show_alert=True)
        return
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.message.answer_document(FSInputFile(case.full_doc_path), caption='📄 Ваше заявление')
    extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
    await callback.message.answer(
        extraction_preview(extracted, case.received_date, [], case.deadline_date, title='📄 <b>Данные в заявлении:</b>'),
        reply_markup=document_details_menu(case.id),
    )
    await callback.answer()


async def _wait_for_background_order(
    session: AsyncSession, settings: Settings, case: Case, current_user: User
) -> list[str]:
    if case.extracted_json:
        return json.loads(case.missing_fields or "[]")
    result = await wait_order_extraction(settings, case.id, current_user.id)
    await session.refresh(case)
    return result.missing


async def _continue_after_received_date(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    current_user: User,
    case: Case,
) -> None:
    start_document_preparation(settings, case.id, current_user.id)
    if not normalize_phone(current_user.phone):
        await _request_payment_contact(message, state, case)
        return
    await message.answer("<b>🔄 Заявление составляется, нужно немного подождать...</b>")
    preparation = await wait_started_document_preparation(case.id)
    if preparation is not None:
        await session.refresh(case)
        missing = json.loads(case.missing_fields or "[]")
    else:
        missing = await _wait_for_background_order(session, settings, case, current_user)
    if missing:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_order_rephoto)
        await _send_order_rephoto_prompt(message, missing, attempts=case.order_rephoto_attempts)
        return
    _, date_error = validate_received_date(case, case.received_date.strftime("%d.%m.%Y") if case.received_date else None)
    if date_error:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer(date_error + "\n\n" + manual_received_date_prompt_text())
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or "{}"))
    if await _send_pending_ocr_confirmation(message, state, case, extracted):
        return
    if settings.show_user_confirmation_step:
        await state.clear()
        await message.answer(extraction_preview(extracted, case.received_date, [], case.deadline_date), reply_markup=confirm_extraction())
        return
    if preparation is not None and preparation.ok and (case.preview_pdf_path or case.preview_doc_path):
        await _deliver_prepared_preview(message, state, session, settings, current_user, case)
        return
    await _generate_documents_flow(message, session, settings, current_user, case, state=state, bot=message.bot)


async def _deliver_prepared_preview(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User, case: Case
) -> bool:
    if not payments_enabled():
        return await _generate_documents_flow(message, session, settings, current_user, case, state=state, bot=message.bot)
    preview_file = case.preview_pdf_path or case.preview_doc_path
    if preview_file:
        await message.answer_document(FSInputFile(preview_file), reply_markup=ReplyKeyboardRemove(), caption="Скрытый предпросмотр заявления.")
    return await _finalize_payment(message, state, session, settings, current_user, case)


@router.message(CaseStates.waiting_order_photo, F.photo)
@router.message(CaseStates.waiting_order_rephoto, F.photo)
async def receive_order_photo(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    if case and (case.preview_pdf_path or case.preview_doc_path or case.full_doc_path or case.full_pdf_path):
        case = await get_or_create_active_case(
            session, current_user, chat_id=str(message.chat.id), force_new=True
        )
        await state.update_data(case_id=case.id)
    path = await _download_photo(bot, message, case.id, 'order')
    await save_photo_path(session, case, 'order', path)
    schedule_crm_sync(settings, case.id, current_user.id, 'order_photo_uploaded', {
        'note': 'Пользователь отправил фото судебного приказа',
        'files': [{'path': str(path), 'caption': 'Фото приказа'}],
    })
    start_order_extraction(settings, case.id, current_user.id)
    if not case.received_date:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer(received_date_prompt_text().replace("Приказ распознан", "Приказ принят"))
        return
    await _continue_after_received_date(message, state, session, settings, current_user, case)
    return


@router.message(CaseStates.waiting_order_photo, F.document)
@router.message(CaseStates.waiting_order_rephoto, F.document)
async def receive_order_document(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    suffix = Path(message.document.file_name or '').suffix.lower() if message.document else ''
    allowed = {'.jpg', '.jpeg', '.png', '.webp', '.pdf', '.heic', '.heif'}
    if not message.document or suffix not in allowed:
        await message.answer('Отправьте приказ в формате JPG, PNG, WEBP, PDF, HEIC или HEIF.')
        return
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    if case and (case.preview_pdf_path or case.preview_doc_path or case.full_doc_path or case.full_pdf_path):
        case = await get_or_create_active_case(
            session, current_user, chat_id=str(message.chat.id), force_new=True
        )
        await state.update_data(case_id=case.id)
    path = normalize_order_upload(await _download_document_image(bot, message, case.id, 'order'))
    await save_photo_path(session, case, 'order', path)
    schedule_crm_sync(settings, case.id, current_user.id, 'order_photo_uploaded', {
        'note': 'Пользователь отправил приказ как файл',
        'files': [{'path': str(path), 'caption': 'Приказ (файл)'}],
    })
    start_order_extraction(settings, case.id, current_user.id)
    if not case.received_date:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer(received_date_prompt_text().replace("Приказ распознан", "Приказ принят"))
        return
    await _continue_after_received_date(message, state, session, settings, current_user, case)
    return


async def _ensure_case_for_unscoped_order_upload(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    current_user: User,
) -> Case:
    case = await latest_open_case(session, current_user.id)
    has_generated_documents = bool(
        case
        and (case.preview_pdf_path or case.preview_doc_path or case.full_doc_path or case.full_pdf_path)
    )
    must_start_new = bool(
        case
        and case.order_photo_path
        and (
            case.status != CaseStatus.WAITING_ORDER_REPHOTO.value
            or has_generated_documents
        )
    )
    if case is None or must_start_new:
        if must_start_new:
            case = await get_or_create_active_case(
                session, current_user, chat_id=str(message.chat.id), force_new=True
            )
        else:
            case = await get_or_create_active_case(
                session, current_user, chat_id=str(message.chat.id)
            )
        schedule_crm_sync(
            settings,
            case.id,
            current_user.id,
            "user_started_bot",
            {"note": "Telegram: заявка автоматически создана по входящему фото приказа"},
        )
        logger.info(
            "Telegram auto-created case for order upload case_id=%s user_id=%s replaced_existing=%s",
            case.id,
            current_user.id,
            must_start_new,
        )
    await state.update_data(case_id=case.id)
    target_state = (
        CaseStates.waiting_order_rephoto
        if case.status == CaseStatus.WAITING_ORDER_REPHOTO.value
        else CaseStates.waiting_order_photo
    )
    await state.set_state(target_state)
    return case


@router.message(StateFilter(None), F.photo)
async def receive_unscoped_order_photo(
    message: Message,
    bot: Bot,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    current_user: User,
) -> None:
    case = await latest_open_case(session, current_user.id)
    if case and case.order_photo_path and not case.received_date:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_envelope_photo)
        await receive_envelope_photo(message, bot, state, session, settings, current_user)
        return
    await _ensure_case_for_unscoped_order_upload(message, state, session, settings, current_user)
    await receive_order_photo(message, bot, state, session, settings, current_user)


@router.message(StateFilter(None), F.document)
async def receive_unscoped_order_document(
    message: Message,
    bot: Bot,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    current_user: User,
) -> None:
    await _ensure_case_for_unscoped_order_upload(message, state, session, settings, current_user)
    await receive_order_document(message, bot, state, session, settings, current_user)


@router.message(CaseStates.waiting_order_photo)
@router.message(CaseStates.waiting_order_rephoto)
async def receive_order_photo_wrong(message: Message) -> None:
    await message.answer(
        "Нужно именно фото судебного приказа. Отправьте изображение одним сообщением "
        "или прикрепите файл-картинку без сжатия.",
        reply_markup=order_rephoto_menu(),
    )


@router.message(CaseStates.waiting_envelope_choice, F.photo)
async def receive_envelope_photo_direct(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    await receive_envelope_photo(message, bot, state, session, settings, current_user)


@router.message(CaseStates.waiting_envelope_choice, F.text)
async def receive_date_direct(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    received = parse_russian_date(message.text)
    if not received:
        await message.answer(
            "Отправьте фото конверта или напишите дату получения в формате <code>ДД.ММ.ГГГГ</code>, например <code>19.06.2026</code>.",
            reply_markup=envelope_choice(),
        )
        return
    start = time.monotonic()
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    await set_received_date(session, case, received)
    logger.info("handler received_date_entered answered_to_user duration_ms=%s", int((time.monotonic() - start) * 1000))
    schedule_crm_sync(
        settings,
        case.id,
        current_user.id,
        "received_date_entered",
        {"received_date": received.strftime("%d.%m.%Y"), "deadline": case.deadline_date.strftime("%d.%m.%Y") if case.deadline_date else ""},
    )
    await _extract_and_process_order(message, state, session, settings, case, current_user)


@router.callback_query(F.data == "case:envelope_photo")
async def choose_envelope_photo(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление. Начните заново.", reply_markup=main_menu())
        await callback.answer()
        return
    await state.update_data(case_id=case.id)
    await state.set_state(CaseStates.waiting_envelope_photo)
    await callback.message.answer("Отправьте фото конверта так, чтобы были видны все почтовые штампы с датами.")
    await callback.answer()


@router.callback_query(F.data == "case:manual_date")
async def choose_manual_date(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление. Начните заново.", reply_markup=main_menu())
        await callback.answer()
        return
    await state.update_data(case_id=case.id)
    await state.set_state(CaseStates.waiting_manual_date)
    await callback.message.answer(manual_received_date_prompt_text())
    await callback.answer()


@router.message(CaseStates.waiting_manual_date, F.text, ~F.text.startswith('/'))
async def receive_manual_date(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    state_data = await state.get_data()
    case = await session.get(Case, state_data['case_id'])
    received, error = validate_received_date(case, message.text)
    if error:
        await message.answer(error)
        return
    await save_received_date(session, settings, case, current_user, received)
    await _continue_after_received_date(message, state, session, settings, current_user, case)
    return


@router.message(CaseStates.waiting_payment_contact, F.contact)
@router.message(CaseStates.waiting_payment_contact, F.text)
async def receive_payment_contact(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    raw_phone = message.contact.phone_number if message.contact else message.text
    phone = normalize_phone(raw_phone)
    if not phone:
        await message.answer("Укажите корректный номер телефона.", reply_markup=phone_request_keyboard())
        return
    current_user.phone = phone
    await session.commit()
    await message.answer(
        "<b>🔄 Заявление составляется, нужно немного подождать...</b>",
        reply_markup=ReplyKeyboardRemove(),
    )
    schedule_crm_sync(settings, case.id, current_user.id, "phone_provided", {"note": "Пользователь указал номер телефона"})
    preparation = await wait_started_document_preparation(case.id)
    if preparation is not None:
        await session.refresh(case)
        missing = json.loads(case.missing_fields or "[]")
    else:
        missing = await _wait_for_background_order(session, settings, case, current_user)
    if missing:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_order_rephoto)
        await _send_order_rephoto_prompt(message, missing, attempts=case.order_rephoto_attempts)
        return
    _, date_error = validate_received_date(case, case.received_date.strftime("%d.%m.%Y") if case.received_date else None)
    if date_error:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer(date_error + "\n\n" + manual_received_date_prompt_text())
        return
    if preparation is not None and preparation.ok and (case.preview_pdf_path or case.preview_doc_path):
        await _deliver_prepared_preview(message, state, session, settings, current_user, case)
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
    if await _send_pending_ocr_confirmation(message, state, case, extracted):
        return
    if settings.show_user_confirmation_step:
        await state.clear()
        await message.answer(extraction_preview(extracted, case.received_date, [], case.deadline_date), reply_markup=confirm_extraction())
        return
    await _generate_documents_flow(
        message,
        session,
        settings,
        current_user,
        case,
        state=state,
        bot=message.bot,
        remove_phone_keyboard=True,
    )


@router.message(CaseStates.waiting_envelope_photo, F.photo)
async def receive_envelope_photo(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    path = await _download_photo(bot, message, case.id, "envelope")
    await save_photo_path(session, case, "envelope", path)
    await message.answer("✅ Конверт принят. Считываю самую позднюю дату штампа и данные приказа, это может занять минуту.")
    try:
        envelope = await extract_envelope_date(settings, session, case_id=case.id, user_id=current_user.id, envelope_photo_path=str(path))
        received = parse_structured_date(envelope.get("latest_date_normalized") or envelope.get("latest_date"))
        if not received:
            await state.set_state(CaseStates.waiting_manual_date)
            await message.answer(
                "Не удалось надежно прочитать дату на конверте.\n\n"
                "Пожалуйста, сфотографируйте конверт крупнее: должны быть видны все почтовые штампы с датами.\n\n"
                "Или нажмите «Ввести дату вручную».",
                reply_markup=envelope_choice(),
            )
            return
        await set_received_date(session, case, received)
        schedule_crm_sync(
            settings,
            case.id,
            current_user.id,
            "envelope_photo_uploaded",
            {
                "received_date": received.strftime("%d.%m.%Y"),
                "deadline": case.deadline_date.strftime("%d.%m.%Y") if case.deadline_date else "",
                "note": "Дата получена с конверта",
                "files": [{"path": str(path), "caption": "Фото конверта"}],
            },
        )
    except Exception:
        logger.exception("Envelope extraction failed")
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer(
            "Не удалось надежно прочитать дату на конверте.\n\n"
            "Пожалуйста, сфотографируйте конверт крупнее: должны быть видны все почтовые штампы с датами.\n\n"
            "Или нажмите «Ввести дату вручную».",
            reply_markup=envelope_choice(),
        )
        return
    await _extract_and_process_order(message, state, session, settings, case, current_user)


async def _send_pending_ocr_confirmation(message: Message, state: FSMContext, case: Case, data: dict) -> bool:
    step = next_confirmation(data)
    if step is None:
        return False
    crop = confirmation_crop(case.order_photo_path, step, case_id=case.id) if case.order_photo_path else None
    keyboard = ocr_field_confirmation(step.field_name)
    text = confirmation_text(step)
    if crop and crop.exists():
        await message.answer_photo(FSInputFile(crop), caption=text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)
    await state.update_data(case_id=case.id, ocr_field=step.field_name)
    return True


async def _continue_after_ocr_confirmation(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, user: User, case: Case,
) -> None:
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    outcome = reduce_and_validate(data, case.received_date)
    case.extracted_json = json.dumps(outcome.data, ensure_ascii=False)
    case.missing_fields = json.dumps(list(outcome.missing_fields), ensure_ascii=False)
    case.status = CaseStatus.PROCESSING.value if outcome.ready or outcome.next_step else CaseStatus.WAITING_ORDER_REPHOTO.value
    await session.commit()
    if outcome.next_step and await _send_pending_ocr_confirmation(message, state, case, outcome.data):
        return
    if not outcome.ready:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_order_rephoto)
        await _send_order_rephoto_prompt(message, list(outcome.missing_fields), attempts=case.order_rephoto_attempts)
        return
    await state.clear()
    if not case.received_date:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer(received_date_prompt_text())
    elif normalize_phone(user.phone):
        await _generate_documents_flow(message, session, settings, user, case, state=state, bot=message.bot)
    else:
        await _request_payment_contact(message, state, case)

async def _extract_and_process_order(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    case: Case,
    current_user: User,
) -> None:
    await state.clear()
    await message.answer("🔎 Считываю судебный приказ и собираю данные для заявления.")
    try:
        extracted = await extract_order_data(settings, session, case_id=case.id, user_id=current_user.id, order_photo_path=case.order_photo_path)
    except Exception:
        logger.exception("Order extraction failed")
        extracted = {}
    extracted = normalize_order_data(extracted)
    extracted, name_result = normalize_debtor_name_fields(extracted)
    if name_result and name_result.confidence >= 0.85 and name_result.normalized:
        extracted["debtor_full_name"] = name_result.normalized

    pending = next_confirmation(extracted)
    if extracted.get("_pipeline_status") == "technical_fail":
        missing = [item for item in (extracted.get("_simple_validation_errors") or ["technical_ocr_fail"]) if item not in {"missing:debt_period", "format:order_date"}]
        case.extracted_json = json.dumps(extracted, ensure_ascii=False)
        case.missing_fields = json.dumps(missing, ensure_ascii=False)
        case.status = CaseStatus.WAITING_ORDER_REPHOTO.value
        await session.commit()
        await state.set_state(CaseStates.waiting_order_rephoto)
        await _send_order_rephoto_prompt(message, missing, attempts=case.order_rephoto_attempts)
        return
    if pending:
        outcome = reduce_and_validate(extracted, case.received_date)
        extracted = outcome.data
        case.extracted_json = json.dumps(extracted, ensure_ascii=False)
        case.missing_fields = json.dumps(list(outcome.missing_fields), ensure_ascii=False)
        case.status = CaseStatus.PROCESSING.value
        await session.commit()
        await _send_pending_ocr_confirmation(message, state, case, extracted)
        return
    missing = [field for field in missing_order_fields(extracted, case.received_date) if field != 'received_date']
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    case.missing_fields = json.dumps(missing, ensure_ascii=False)
    if missing:
        case.order_rephoto_attempts = (case.order_rephoto_attempts or 0) + 1
        case.status = CaseStatus.WAITING_ORDER_REPHOTO.value
    else:
        case.order_rephoto_attempts = 0
        case.status = CaseStatus.PROCESSING.value
    await session.commit()
    crm = get_amocrm_service(settings)
    schedule_crm_sync(settings, case.id, current_user.id, "ocr_completed", {"note": await crm.build_ocr_note(case)})

    if missing:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_order_rephoto)
        await _send_order_rephoto_prompt(message, missing, attempts=case.order_rephoto_attempts)
        schedule_crm_sync(settings, case.id, current_user.id, "document_qa_failed", {"note": "Не удалось прочитать обязательные поля приказа"})
        if case.order_rephoto_attempts >= 3:
            case.status = CaseStatus.NEEDS_REVIEW.value
            await session.commit()
            for admin_id in settings.admin_ids:
                try:
                    await message.bot.send_message(
                        admin_id,
                        f"⚠️ Заявка #{case.id}: приказ не распознан после 3 попыток. Нужна ручная обработка.",
                    )
                except Exception:
                    logger.exception("Failed to notify admin %s about repeated rephoto", admin_id)
        return

    if not case.received_date:
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer(received_date_prompt_text())
        return

    if normalize_phone(current_user.phone):
        if settings.show_user_confirmation_step:
            await state.clear()
            await message.answer(extraction_preview(extracted, case.received_date, [], case.deadline_date), reply_markup=confirm_extraction())
            return
        await _generate_documents_flow(message, session, settings, current_user, case, state=state, bot=message.bot)
        return
    await _request_payment_contact(message, state, case)


@router.callback_query(F.data.startswith("case:ocr_confirm:"))
async def confirm_ocr_field(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.answer("Заявка не найдена")
        return
    field_name = callback.data.rsplit(":", 1)[-1]
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    record = (data.get("_field_provenance") or {}).get(field_name)
    if not isinstance(record, dict) or record.get("status") != "disputed":
        await callback.answer("Поле уже подтверждено")
        return
    value = record.get("extracted_value") or record.get("raw_ocr_value") or ""
    outcome = apply_confirmation_answer(data, field_name, value, case.received_date)
    case.extracted_json = json.dumps(outcome.data, ensure_ascii=False)
    case.missing_fields = json.dumps(list(outcome.missing_fields), ensure_ascii=False)
    await session.commit()
    await callback.answer("Подтверждено")
    await _continue_after_ocr_confirmation(callback.message, state, session, settings, current_user, case)


@router.callback_query(F.data.startswith("case:ocr_edit:"))
async def edit_ocr_field(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.answer("Заявка не найдена")
        return
    field_name = callback.data.rsplit(":", 1)[-1]
    await state.update_data(case_id=case.id, ocr_field=field_name)
    await state.set_state(CaseStates.waiting_ocr_field_value)
    await callback.message.answer(f"Введите исправленное значение для поля «{FIELD_LABELS.get(field_name, field_name)}».")
    await callback.answer()


@router.message(CaseStates.waiting_ocr_field_value)
async def save_ocr_field_value(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    state_data = await state.get_data()
    case = await session.get(Case, state_data.get("case_id"))
    value = (message.text or "").strip()
    if case is None or not value:
        await message.answer("Введите непустое значение текстом.")
        return
    field_name = state_data["ocr_field"]
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    outcome = apply_confirmation_answer(data, field_name, value, case.received_date)
    case.extracted_json = json.dumps(outcome.data, ensure_ascii=False)
    case.missing_fields = json.dumps(list(outcome.missing_fields), ensure_ascii=False)
    await session.commit()
    await _continue_after_ocr_confirmation(message, state, session, settings, current_user, case)

@router.callback_query(F.data == "case:rephoto_order")
async def choose_rephoto_order(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление. Начните заново.", reply_markup=main_menu())
        await callback.answer()
        return
    await state.update_data(case_id=case.id)
    await state.set_state(CaseStates.waiting_order_rephoto)
    await callback.message.answer(
        "Пожалуйста, отправьте фото судебного приказа ещё раз. Весь лист должен быть в кадре, без бликов.",
    )
    await callback.answer()


async def _prompt_debtor_name_fix(message: Message, debtor_full_name: str) -> None:
    suggested = suggest_nominative_full_name(debtor_full_name)
    if not suggested:
        return
    await message.answer(
        "Похоже, ФИО должника пришло в дательном падеже.\n"
        f"Я предлагаю заменить на: <b>{h(suggested)}</b>\n\n"
        "Подтвердите исправление кнопкой ниже, после этого я продолжу генерацию.",
        reply_markup=debtor_name_fix_menu(),
    )


@router.callback_query(F.data == "case:manual_fields")
@router.callback_query(F.data == "case:edit_fields")
async def edit_fields(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_admin:
        await callback.answer("Эта функция доступна только админу.")
        return
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление.", reply_markup=main_menu())
        await callback.answer()
        return
    await state.update_data(case_id=case.id)
    await callback.message.answer(
        "✏️ <b>Что нужно исправить?</b>\n\nВыберите поле, отправьте новое значение, и я снова покажу карточку проверки.",
        reply_markup=edit_fields_menu(),
    )
    await callback.answer()

@router.callback_query(F.data == "case:review")
async def review_case(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_admin:
        await callback.answer("Эта функция доступна только админу.")
        return
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление.", reply_markup=main_menu())
        await callback.answer()
        return
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    missing = missing_order_fields(data, case.received_date)
    case.missing_fields = json.dumps(missing, ensure_ascii=False)
    case.status = CaseStatus.NEEDS_REVIEW.value if missing else CaseStatus.PROCESSING.value
    await session.commit()
    await callback.message.answer(extraction_preview(data, case.received_date, missing, case.deadline_date), reply_markup=confirm_extraction())
    await callback.answer()


@router.callback_query(F.data == "case:fix_debtor_name")
async def fix_debtor_name(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_admin:
        await callback.answer("Эта функция доступна только админу.")
        return
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.answer("Не нашел активное заявление")
        return
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    suggested = suggest_nominative_full_name(data.get("debtor_full_name"))
    if not suggested:
        await callback.answer("Не смог предложить исправление")
        return
    data["debtor_full_name"] = suggested
    case.extracted_json = json.dumps(data, ensure_ascii=False)
    case.missing_fields = json.dumps([], ensure_ascii=False)
    case.status = CaseStatus.PROCESSING.value
    await session.commit()
    await callback.message.answer(f"✅ Исправил ФИО должника на <b>{h(suggested)}</b>.")
    await callback.message.answer(extraction_preview(data, case.received_date, [], case.deadline_date), reply_markup=confirm_extraction())
    await callback.answer()


@router.callback_query(F.data.startswith("case:field:"))
async def choose_field(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_admin:
        await callback.answer("Эта функция доступна только админу.")
        return
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление.", reply_markup=main_menu())
        await callback.answer()
        return
    field = callback.data.split(":")[-1]
    label = FIELD_LABELS.get(field, field)
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    current = data.get(field) or "пусто"
    await state.update_data(case_id=case.id, edit_field=field)
    await state.set_state(CaseStates.waiting_field_value)
    await callback.message.answer(f"Введите новое значение для поля <b>{label}</b>.\n\nСейчас: <code>{h(current)}</code>")
    await callback.answer()


@router.message(CaseStates.waiting_field_value)
async def process_field_value(message: Message, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_admin:
        await state.clear()
        await message.answer("Эта функция доступна только администратору.")
        return
    state_data = await state.get_data()
    case = await session.get(Case, state_data["case_id"])
    field = state_data["edit_field"]
    value = (message.text or "").strip()
    if not value:
        await message.answer("Значение не должно быть пустым. Напишите новое значение текстом.")
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or "{}"))
    if field in (extracted.get("_field_provenance") or {}):
        outcome = apply_confirmation_answer(extracted, field, value, case.received_date)
        extracted = outcome.data
        missing = list(outcome.missing_fields)
    else:
        extracted[field] = value
        extracted = normalize_order_data(extracted)
        missing = missing_order_fields(extracted, case.received_date)
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    case.missing_fields = json.dumps(missing, ensure_ascii=False)
    case.status = CaseStatus.NEEDS_REVIEW.value if missing else CaseStatus.PROCESSING.value
    await session.commit()
    await state.clear()
    await message.answer("✅ Поле обновлено.")
    await message.answer(extraction_preview(extracted, case.received_date, missing, case.deadline_date), reply_markup=confirm_extraction())


@router.message(CaseStates.waiting_manual_fields)
async def process_manual_fields(message: Message, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_admin:
        await state.clear()
        await message.answer("Эта функция доступна только администратору.")
        return
    await state.clear()
    await message.answer("Эта функция доступна только администратору.")


async def _notify_admin_qa_failure(bot: Bot, settings: Settings, case: Case, reason: str) -> None:
    if not settings.admin_debug_to_telegram:
        logger.warning("Admin QA/debug Telegram report suppressed case_id=%s reason=%s", case.id, reason[:500])
        return
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, reason)
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)


async def _notify_admin_amount_warning(bot: Bot, settings: Settings, case: Case, recovery: AmountRecoveryResult) -> None:
    text = (
        f"⚠️ Заявка #{case.id}: суммы автоматически восстановлены ({recovery.recovery_method}).\n"
        f"Было: {recovery.old_debt_amount}\n"
        f"Стало: {recovery.new_debt_amount}"
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Failed to notify admin %s about amount recovery", admin_id)


async def _resolve_amount_mismatch(
    settings: Settings,
    session: AsyncSession,
    case: Case,
    current_user: User,
    data: dict,
    *,
    bot: Bot | None = None,
    force_retry: bool = False,
) -> tuple[dict, AmountValidationResult, AmountRecoveryResult | None, dict | None]:
    amount_check = validate_amounts(data)
    if amount_check.ok and not force_retry:
        return data, amount_check, None, None

    retry_amounts: dict | None = None
    recovery: AmountRecoveryResult | None = None

    if settings.amount_retry_on_mismatch and case.order_photo_path and ("amount_mismatch" in amount_check.errors or force_retry):
        try:
            retry_amounts = await extract_order_amounts(
                settings,
                session,
                case_id=case.id,
                user_id=current_user.id,
                order_photo_path=case.order_photo_path,
            )
        except Exception:
            logger.exception("Targeted amount OCR failed for case %s", case.id)

    if settings.auto_recover_amount_mismatch:
        recovery = recover_amounts_from_mismatch(
            data,
            retry_amounts,
            min_confidence=settings.auto_recover_amount_min_confidence,
            auto_recover=True,
        )
        if recovery.applied:
            data = recovery.order_data
            case.extracted_json = json.dumps(data, ensure_ascii=False)
            if session is not None:
                await session.commit()
            amount_check = validate_amounts(data)
            save_amount_debug_snapshot(
                case.id,
                {
                    "amount_recovery_applied": True,
                    "recovery_method": recovery.recovery_method,
                    "qa_report": recovery.qa_report,
                },
            )
            if bot:
                await _notify_admin_amount_warning(bot, settings, case, recovery)
            logger.warning(
                "Amount recovery applied for case %s method=%s old=%s new=%s",
                case.id,
                recovery.recovery_method,
                recovery.old_debt_amount,
                recovery.new_debt_amount,
            )

    return data, amount_check, recovery, retry_amounts


async def _generate_documents_flow(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    current_user: User,
    case: Case,
    *,
    state: FSMContext | None = None,
    restore_reason: str | None = None,
    bot: Bot | None = None,
    remove_phone_keyboard: bool = False,
) -> bool:
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    validation = validate_before_generation(data, case.received_date)
    if not validation.ok:
        missing = list(validation.missing)
        case.missing_fields = json.dumps(missing, ensure_ascii=False)
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        if missing:
            if state is not None:
                await state.update_data(case_id=case.id)
                await state.set_state(CaseStates.waiting_order_rephoto)
            await _send_order_rephoto_prompt(message, missing, attempts=case.order_rephoto_attempts)
        else:
            await message.answer(
                "Документ не прошёл автоматическую QA-проверку: "
                + ", ".join(validation.bad_tokens)
            )
        return False
    stored_reason = restore_reason or data.get("restore_reason") or ""
    if is_deadline_missed(case.deadline_date) and not stored_reason:
        await message.answer(
            "Срок подачи уже пропущен. Выберите причину, чтобы подготовить возражения с ходатайством о восстановлении срока.",
            reply_markup=restore_reason_menu(),
        )
        return False

    data, amount_check, recovery, retry_amounts = await _resolve_amount_mismatch(
        settings, session, case, current_user, data, bot=bot
    )
    if not amount_check.ok:
        admin_report = format_amount_mismatch_admin_report(
            case.id,
            normalize_order_data(json.loads(case.extracted_json or "{}")),
            retry_amounts,
            amount_check,
            recovery,
        )
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        schedule_crm_sync(settings, case.id, current_user.id, "document_qa_failed", {"note": admin_report[:65000]})
        if bot:
            await _notify_admin_qa_failure(bot, settings, case, admin_report)
        if state is not None:
            await state.update_data(case_id=case.id)
            await state.set_state(CaseStates.waiting_order_rephoto)
        await message.answer(
            "Не удалось автоматически согласовать суммы по приказу.\n\n"
            "Пожалуйста, сфотографируйте судебный приказ целиком ещё раз.",
            reply_markup=order_rephoto_menu(),
        )
        return False

    try:
        review_outcome = await create_case_documents_reviewed(
            case,
            current_user,
            settings,
            session,
            restore_reason=stored_reason or None,
        )
    except ValueError as exc:
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        schedule_crm_sync(settings, case.id, current_user.id, "document_qa_failed", {"note": str(exc)})
        if bot:
            await _notify_admin_qa_failure(bot, settings, case, f"⚠️ QA не пройден по заявке #{case.id}: {exc}")
        if state is not None:
            await state.update_data(case_id=case.id)
            await state.set_state(CaseStates.waiting_order_rephoto)
        await message.answer(MANUAL_REVIEW_USER_TEXT)
        return False
    if not review_outcome.ok or review_outcome.artifacts is None:
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        report = review_outcome.admin_report or "AI document review failed"
        schedule_crm_sync(settings, case.id, current_user.id, "document_qa_failed", {"note": report[:65000]})
        if bot:
            await _notify_admin_qa_failure(bot, settings, case, report)
        await message.answer(MANUAL_REVIEW_USER_TEXT)
        return False
    full_docx = review_outcome.artifacts.full_docx_path
    full_pdf = review_outcome.artifacts.full_pdf_path
    preview_pdf = review_outcome.artifacts.preview_pdf_path
    preview_docx = None
    instruction_path = review_outcome.artifacts.instruction_docx_path
    case.full_doc_path = str(full_docx)
    case.full_pdf_path = str(full_pdf) if full_pdf else None
    case.preview_pdf_path = str(preview_pdf) if preview_pdf else None
    case.preview_doc_path = str(preview_docx) if preview_docx else None
    case.instruction_path = str(instruction_path)
    case.status = CaseStatus.PREVIEW_READY.value
    await session.commit()
    schedule_crm_sync(
        settings,
        case.id,
        current_user.id,
        "preview_generated",
        {
            "note": "Preview сформирован. Document QA: passed",
            "files": [
                {"path": case.full_doc_path or "", "caption": "Полный DOCX"},
                {"path": case.preview_pdf_path or case.preview_doc_path or "", "caption": "Preview PDF"},
            ],
        },
    )
    if payments_enabled():
        if not full_pdf:
            await message.answer("⚠️ Не удалось собрать preview PDF. Для оплаты нужен LibreOffice/soffice и PyMuPDF.")
            case.status = CaseStatus.NEEDS_REVIEW.value
            await session.commit()
            return False
        if settings.require_pdf_preview_for_payment and not preview_pdf:
            await message.answer("⚠️ Не удалось собрать preview PDF. Платеж не создан.")
            case.status = CaseStatus.NEEDS_REVIEW.value
            await session.commit()
            if bot:
                await _notify_admin_qa_failure(bot, settings, case, "нет preview PDF")
            return False
    preview_file = preview_pdf or preview_docx
    preview_reply_markup = ReplyKeyboardRemove() if remove_phone_keyboard else None
    if not payments_enabled():
        if state is not None:
            await state.clear()
        if preview_file:
            await message.answer_document(
                FSInputFile(preview_file),
                reply_markup=preview_reply_markup,
                caption="Предпросмотр заявления." if preview_pdf else "Предпросмотр заявления (dev-only DOCX).",
        )
        await message.answer("🧪 Режим оплаты выключен. Сразу отправляю полный DOCX для теста.")
        await deliver_full_documents(message, session, case, settings, current_user)
        return True
    if preview_file:
        await message.answer_document(
            FSInputFile(preview_file),
            reply_markup=preview_reply_markup,
            caption="Скрытый предпросмотр заявления." if preview_pdf else "Скрытый предпросмотр заявления (dev-only DOCX).",
        )
    return await _finalize_payment(message, state, session, settings, current_user, case)


@router.callback_query(F.data == "case:generate")
async def generate_documents(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User, state: FSMContext, bot: Bot) -> None:
    if not (current_user.is_admin or settings.show_user_confirmation_step):
        await callback.answer("Эта функция недоступна.")
        return
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление.", reply_markup=main_menu())
        await callback.answer()
        return
    await state.clear()
    schedule_crm_sync(settings, case.id, current_user.id, "case_data_confirmed", {"note": "Пользователь подтвердил распознанные данные"})
    await _generate_documents_flow(callback.message, session, settings, current_user, case, state=state, bot=bot)
    await callback.answer()


@router.callback_query(F.data.startswith("case:restore_reason:"))
async def choose_restore_reason(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User, settings: Settings) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление.", reply_markup=main_menu())
        await callback.answer()
        return
    code = callback.data.split(":")[-1]
    if code == "custom":
        await state.update_data(case_id=case.id)
        await state.set_state(CaseStates.waiting_restore_reason_custom)
        await callback.message.answer("Напишите свою причину пропуска срока одним сообщением.")
        await callback.answer()
        return
    reasons = {
        "late": "Причина пропуска срока: копия судебного приказа получена поздно.",
        "illness": "Причина пропуска срока: болезнь.",
        "trip": "Причина пропуска срока: командировка / отъезд.",
        "not_living": "Причина пропуска срока: по адресу регистрации фактически не проживал.",
    }
    reason_text = reasons.get(code)
    if not reason_text:
        await callback.answer("Причина не распознана")
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or "{}"))
    extracted["restore_reason"] = reason_text
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    await session.commit()
    await state.clear()
    await _generate_documents_flow(callback.message, session, settings, current_user, case, state=state, restore_reason=reason_text)
    await callback.answer()


@router.message(CaseStates.waiting_restore_reason_custom)
async def receive_restore_reason_custom(message: Message, state: FSMContext, session: AsyncSession, current_user: User, settings: Settings) -> None:
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    reason_text = (message.text or "").strip()
    if not reason_text:
        await message.answer("Причина не должна быть пустой. Напишите ее одним сообщением.")
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or "{}"))
    extracted["restore_reason"] = f"Причина пропуска срока: {reason_text}"
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    await session.commit()
    await state.clear()
    await _generate_documents_flow(message, session, settings, current_user, case, state=state, restore_reason=extracted["restore_reason"])


@router.callback_query(F.data.startswith('paid:correction:start'))
async def paid_correction_start(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User) -> None:
    parts = callback.data.split(':')
    case = await generated_case_for_user(session, current_user.id, int(parts[3])) if len(parts) > 3 else await latest_case(session, current_user.id)
    if not case or case.status not in {CaseStatus.PAID.value, CaseStatus.DELIVERED.value}:
        await callback.answer('Оплаченное заявление не найдено.', show_alert=True)
        return
    schedule_crm_sync(settings, case.id, current_user.id, 'paid_document_correction_started', {'note': 'Пользователь сообщил: данные в заявлении неверные'})
    await _edit_or_answer(callback, '<b>✏️ Что нужно исправить?</b>\n\nВыберите поле и отправьте новое значение.', paid_edit_fields_menu(case.id))
    await callback.answer()


@router.callback_query(F.data.startswith('paid:field:'))
async def paid_field_selected(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    parts = callback.data.split(':')
    case = await generated_case_for_user(session, current_user.id, int(parts[2])) if len(parts) > 3 else await latest_case(session, current_user.id)
    if not case or case.status not in {CaseStatus.PAID.value, CaseStatus.DELIVERED.value}:
        await callback.answer('Оплаченное заявление не найдено.', show_alert=True)
        return
    field = callback.data.split(':')[-1]
    if not correction_allowed(case, field):
        await callback.answer('Нельзя изменить абсолютно все поля одного заявления. Оставьте хотя бы одно исходное поле.', show_alert=True)
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
    await state.update_data(case_id=case.id, paid_field=field)
    await state.set_state(CaseStates.waiting_paid_field_value)
    schedule_crm_sync(settings, case.id, current_user.id, 'paid_document_field_selected', {'note': f'Выбрано поле: {FIELD_LABELS.get(field, field)}'})
    current = extracted.get(field) or 'не указано'
    await _edit_or_answer(callback, f'Текущее значение поля <b>{FIELD_LABELS.get(field, field)}</b>:\n<code>{h(current)}</code>\n\nНапишите верное значение.')
    await callback.answer()


@router.message(CaseStates.waiting_paid_field_value)
async def paid_field_value(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    state_data = await state.get_data()
    case = await session.get(Case, state_data['case_id'])
    field = state_data['paid_field']
    value = (message.text or '').strip()
    if not value:
        await message.answer('Значение не должно быть пустым.')
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
    extracted[field] = value
    extracted = normalize_order_data(extracted)
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    record_corrected_field(case, field)
    await session.commit()
    await state.clear()
    schedule_crm_sync(settings, case.id, current_user.id, 'paid_document_field_corrected', {'note': f'Исправил поле: {FIELD_LABELS.get(field, field)}'})
    await message.answer(extraction_preview(extracted, case.received_date, [], case.deadline_date), reply_markup=paid_review_menu(case.id))


@router.callback_query(F.data.startswith('paid:review'))
async def paid_review(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    parts = callback.data.split(':')
    case = await generated_case_for_user(session, current_user.id, int(parts[2])) if len(parts) > 2 else await latest_case(session, current_user.id)
    if not case:
        await callback.answer('Заявление не найдено.', show_alert=True)
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
    text = extraction_preview(extracted, case.received_date, [], case.deadline_date)
    await _edit_or_answer(callback, text, paid_review_menu(case.id))
    await callback.answer()


@router.callback_query(F.data.startswith('paid:regenerate'))
async def paid_regenerate(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User) -> None:
    parts = callback.data.split(':')
    case = await generated_case_for_user(session, current_user.id, int(parts[2])) if len(parts) > 2 else await latest_case(session, current_user.id)
    if not case or case.status not in {CaseStatus.PAID.value, CaseStatus.DELIVERED.value}:
        await callback.answer('Оплаченное заявление не найдено.', show_alert=True)
        return
    if paid_regeneration_requires_new_date(case):
        await _edit_or_answer(
            callback,
            'Срок подачи по указанной дате уже истёк. Пересоздание невозможно, пока не будет указана актуальная дата получения.',
            paid_date_required_menu(case.id),
        )
        await callback.answer()
        return
    await _edit_or_answer(callback, '🔄 Перегенерирую заявление с исправленными данными...')
    try:
        artifacts = await regenerate_paid_case(session, settings, case, current_user)
    except ValueError as exc:
        logger.warning('Paid document regeneration rejected case_id=%s: %s', case.id, exc)
        await _edit_or_answer(
            callback,
            'Не удалось пересоздать заявление: проверьте дату получения и исправленные данные.',
            paid_review_menu(case.id),
        )
        await callback.answer()
        return
    await callback.message.answer_document(FSInputFile(artifacts.full_docx_path), caption='✅ Исправленное заявление готово.', reply_markup=paid_document_actions(case.id))
    await callback.answer()


@router.callback_query(F.data.startswith('paid:date:'))
async def paid_date_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    case = await generated_case_for_user(session, current_user.id, int(callback.data.rsplit(':', 1)[-1]))
    if not case:
        await callback.answer('Заявление не найдено.', show_alert=True)
        return
    await state.set_state(CaseStates.waiting_paid_received_date)
    await state.update_data(case_id=case.id)
    await _edit_or_answer(callback, manual_received_date_prompt_text())
    await callback.answer()


@router.message(CaseStates.waiting_paid_received_date, F.text, ~F.text.startswith('/'))
async def paid_date_value(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    state_data = await state.get_data()
    case = await session.get(Case, state_data['case_id'])
    received, error = validate_received_date(case, message.text)
    if error:
        await message.answer(error)
        return
    document_state = (
        case.status, case.full_doc_path, case.full_pdf_path, case.preview_pdf_path,
        case.preview_doc_path, case.instruction_path,
    )
    await save_received_date(session, settings, case, current_user, received)
    (
        case.status, case.full_doc_path, case.full_pdf_path, case.preview_pdf_path,
        case.preview_doc_path, case.instruction_path,
    ) = document_state
    await session.commit()
    await state.clear()
    extracted = normalize_order_data(json.loads(case.extracted_json or '{}'))
    await message.answer(extraction_preview(extracted, case.received_date, [], case.deadline_date), reply_markup=paid_review_menu(case.id))


@router.callback_query(F.data == 'payment:check')
async def payment_check(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        case = await latest_case(session, current_user.id)

    payment: Payment | None = None
    if case:
        result = await session.execute(
            select(Payment)
            .where(Payment.case_id == case.id, Payment.provider == "yookassa")
            .order_by(Payment.id.desc())
            .limit(1)
        )
        payment = result.scalar_one_or_none()

    if case and settings.yookassa_enabled:
        try:
            refreshed = await refresh_yookassa_payment_for_case(session, case, settings)
        except YooKassaError:
            await callback.answer()
            await callback.message.answer("Не удалось проверить оплату. Попробуйте ещё раз через минуту или напишите менеджеру.")
            return
        if refreshed:
            case = refreshed
            result = await session.execute(
                select(Payment)
                .where(Payment.case_id == case.id, Payment.provider == "yookassa")
                .order_by(Payment.id.desc())
                .limit(1)
            )
            payment = result.scalar_one_or_none()

    if case and (case.status == CaseStatus.CANCELED.value or (payment and payment.status == PaymentStatus.CANCELED.value)):
        await callback.answer()
        await callback.message.answer("Платеж отменен или не завершен. Попробуйте оплатить снова.")
        return

    if not case or case.status != CaseStatus.PAID.value:
        await callback.answer("Платеж пока не найден. Попробуйте через 10–20 секунд.", show_alert=False)
        return
    if case.delivered_at:
        await callback.answer("Документы уже отправлены.")
        return
    await callback.answer()
    await callback.message.answer("Оплата найдена. Отправляю документы.")
    await deliver_full_documents(callback.message, session, case, settings, current_user)


async def deliver_full_documents(message: Message, session: AsyncSession, case: Case, settings: Settings | None = None, user: User | None = None) -> None:
    if not case.full_doc_path or not Path(case.full_doc_path).exists():
        raise RuntimeError("Full DOCX file not found")
    await message.answer_document(
        FSInputFile(case.full_doc_path),
        caption=delivery_instruction_text(case),
        reply_markup=paid_document_actions(case.id),
    )
    case.status = CaseStatus.DELIVERED.value
    case.delivered_at = datetime.utcnow()
    await session.commit()
    if settings and user:
        schedule_crm_sync(settings, case.id, user.id, "payment_paid", {"note": "Оплата подтверждена"})
        schedule_crm_sync(
            settings,
            case.id,
            user.id,
            "documents_delivered",
            {
                "note": "Полные документы: DOCX и инструкция выданы",
                "files": [
                    {"path": case.full_doc_path or "", "caption": "Полный DOCX"},
                    {"path": case.full_pdf_path or "", "caption": "Полный PDF"},
                ],
            },
        )
