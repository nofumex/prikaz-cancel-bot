from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import CaseStatus
from app.keyboards.common import case_menu, confirm_extraction, edit_fields_menu, envelope_choice, main_menu
from app.models import Case, User
from app.services.cases import create_case, latest_case, latest_open_case, save_photo_path, set_received_date
from app.services.documents import create_case_documents, extraction_preview
from app.services.app_settings import payments_enabled
from app.services.legal_data import FIELD_LABELS, missing_order_fields, normalize_order_data, validate_before_generation
from app.services.llm import extract_envelope_date, extract_order_data
from app.services.payments import ensure_payment
from app.texts import case_summary, payment_text
from app.utils import ensure_dir, h, parse_russian_date

router = Router(name="case_flow")
logger = logging.getLogger(__name__)


class CaseStates(StatesGroup):
    waiting_order_photo = State()
    waiting_envelope_choice = State()
    waiting_envelope_photo = State()
    waiting_manual_date = State()
    waiting_manual_fields = State()
    waiting_field_value = State()


async def _download_photo(bot: Bot, message: Message, case_id: int, kind: str) -> Path:
    ensure_dir("storage/photos")
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    suffix = Path(file.file_path or "").suffix or ".jpg"
    path = Path("storage/photos") / f"case_{case_id}_{kind}_{photo.file_unique_id}{suffix}"
    await bot.download_file(file.file_path, destination=path)
    return path


@router.callback_query(F.data == "case:new")
@router.message(F.text == "/new")
async def start_case(event: Message | CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    target = event.message if isinstance(event, CallbackQuery) else event
    case = await create_case(session, current_user)
    await state.update_data(case_id=case.id)
    await state.set_state(CaseStates.waiting_order_photo)
    await target.answer(
        "📝 <b>Новое заявление</b>\n\n"
        "Отправьте фото судебного приказа целиком.\n\n"
        "Лучше сфотографировать ровно сверху, без обрезанных краев, чтобы были видны суд, номер дела, должник и взыскатель."
    )
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "case:my")
async def my_case(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    case = await latest_case(session, current_user.id)
    if not case:
        await callback.message.answer("Заявлений пока нет. Начните с кнопки «Подготовить заявление».", reply_markup=main_menu())
    else:
        await callback.message.answer(
            case_summary(case),
            reply_markup=case_menu(can_pay=case.status == CaseStatus.PAYMENT_PENDING.value, payment_url=case.payment_url),
        )
    await callback.answer()


@router.message(CaseStates.waiting_order_photo, F.photo)
async def receive_order_photo(message: Message, bot: Bot, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    path = await _download_photo(bot, message, case.id, "order")
    await save_photo_path(session, case, "order", path)
    await state.set_state(CaseStates.waiting_envelope_choice)
    await message.answer(
        "✅ Фото приказа принято.\n\nТеперь отправьте фото конверта со штампами или сразу напишите дату получения в формате <code>ДД.ММ.ГГГГ</code>.",
        reply_markup=envelope_choice(),
    )


@router.message(CaseStates.waiting_order_photo)
async def receive_order_photo_wrong(message: Message) -> None:
    await message.answer("Нужно именно фото судебного приказа. Отправьте изображение одним сообщением.")


@router.message(CaseStates.waiting_envelope_choice, F.photo)
async def receive_envelope_photo_direct(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    await receive_envelope_photo(message, bot, state, session, settings)


@router.message(CaseStates.waiting_envelope_choice, F.text)
async def receive_date_direct(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    received = parse_russian_date(message.text)
    if not received:
        await message.answer(
            "Отправьте фото конверта или напишите дату получения в формате <code>ДД.ММ.ГГГГ</code>, например <code>19.06.2026</code>.",
            reply_markup=envelope_choice(),
        )
        return
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    await set_received_date(session, case, received)
    await _extract_and_confirm(message, state, session, settings, case)


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
    await callback.message.answer("Напишите дату получения копии приказа. Пример: <code>19.06.2026</code>")
    await callback.answer()


@router.message(CaseStates.waiting_manual_date)
async def receive_manual_date(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, current_user: User) -> None:
    received = parse_russian_date(message.text)
    if not received:
        await message.answer("Не смог распознать дату. Напишите в формате <code>ДД.ММ.ГГГГ</code>, например <code>19.06.2026</code>.")
        return
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    await set_received_date(session, case, received)
    await _extract_and_confirm(message, state, session, settings, case)


@router.message(CaseStates.waiting_envelope_photo, F.photo)
async def receive_envelope_photo(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    data = await state.get_data()
    case = await session.get(Case, data["case_id"])
    path = await _download_photo(bot, message, case.id, "envelope")
    await save_photo_path(session, case, "envelope", path)
    await message.answer("✅ Конверт принят. Считываю самую позднюю дату штампа и данные приказа, это может занять минуту.")
    try:
        envelope = await extract_envelope_date(settings, str(path))
        received = parse_russian_date(envelope.get("latest_date_normalized") or envelope.get("latest_date"))
        if not received:
            await state.set_state(CaseStates.waiting_manual_date)
            await message.answer("Не смог уверенно прочитать дату на конверте. Напишите дату вручную в формате <code>ДД.ММ.ГГГГ</code>.")
            return
        await set_received_date(session, case, received)
    except Exception:
        logger.exception("Envelope extraction failed")
        await state.set_state(CaseStates.waiting_manual_date)
        await message.answer("Не получилось автоматически прочитать конверт. Напишите дату вручную в формате <code>ДД.ММ.ГГГГ</code>.")
        return
    await _extract_and_confirm(message, state, session, settings, case)


async def _extract_and_confirm(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, case: Case) -> None:
    await state.clear()
    await message.answer("🔎 Считываю судебный приказ и собираю данные для заявления.")
    try:
        extracted = await extract_order_data(settings, case.order_photo_path)
    except Exception:
        logger.exception("Order extraction failed")
        extracted = {}
        await message.answer(
            "Нейросеть не смогла прочитать приказ. Можно ввести ключевые данные вручную или связаться с менеджером.",
            reply_markup=confirm_extraction(),
        )
    extracted = normalize_order_data(extracted)
    missing = missing_order_fields(extracted, case.received_date)
    case.extracted_json = json.dumps(extracted, ensure_ascii=False)
    case.missing_fields = json.dumps(missing, ensure_ascii=False)
    case.status = CaseStatus.NEEDS_REVIEW.value if missing else CaseStatus.PROCESSING.value
    await session.commit()
    await message.answer(extraction_preview(extracted, case.received_date, missing, case.deadline_date), reply_markup=confirm_extraction())


@router.callback_query(F.data == "case:manual_fields")
@router.callback_query(F.data == "case:edit_fields")
async def edit_fields(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
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


@router.callback_query(F.data.startswith("case:field:"))
async def choose_field(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
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
async def process_field_value(message: Message, state: FSMContext, session: AsyncSession) -> None:
    state_data = await state.get_data()
    case = await session.get(Case, state_data["case_id"])
    field = state_data["edit_field"]
    value = (message.text or "").strip()
    if not value:
        await message.answer("Значение не должно быть пустым. Напишите новое значение текстом.")
        return
    extracted = normalize_order_data(json.loads(case.extracted_json or "{}"))
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
async def process_manual_fields(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await message.answer("Теперь данные исправляются по одному полю через кнопки.", reply_markup=edit_fields_menu())


@router.callback_query(F.data == "case:generate")
async def generate_documents(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case:
        await callback.message.answer("Не нашел активное заявление.", reply_markup=main_menu())
        await callback.answer()
        return
    data = normalize_order_data(json.loads(case.extracted_json or "{}"))
    validation = validate_before_generation(data, case.received_date)
    if not validation.ok:
        case.missing_fields = json.dumps(validation.missing, ensure_ascii=False)
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        await callback.message.answer(
            "⚠️ Документ пока нельзя готовить: есть пустые или технические поля.\n\n"
            + extraction_preview(data, case.received_date, validation.missing, case.deadline_date),
            reply_markup=confirm_extraction(),
        )
        await callback.answer()
        return
    await callback.message.answer("📄 Готовлю полный и скрытый варианты заявления.")
    try:
        full_path, preview_path, instruction_path = create_case_documents(case, current_user)
    except ValueError as exc:
        case.status = CaseStatus.NEEDS_REVIEW.value
        await session.commit()
        await callback.message.answer(f"⚠️ {exc}", reply_markup=edit_fields_menu())
        await callback.answer()
        return
    case.full_doc_path = str(full_path)
    case.preview_doc_path = str(preview_path)
    case.instruction_path = str(instruction_path)
    if not payments_enabled():
        await callback.message.answer_document(FSInputFile(preview_path), caption="Предпросмотр заявления.")
        await callback.message.answer("🧪 Режим оплаты выключен. Сразу отправляю полный комплект для теста.")
        await deliver_full_documents(callback.message, session, case)
        await callback.answer()
        return
    payment = await ensure_payment(session, case, settings)
    await callback.message.answer_document(FSInputFile(preview_path), caption="Скрытый предпросмотр заявления.")
    await callback.message.answer(payment_text(case, payment.amount), reply_markup=case_menu(can_pay=True, payment_url=case.payment_url))
    await callback.answer()


@router.callback_query(F.data == "payment:check")
async def payment_check(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    case = await latest_open_case(session, current_user.id)
    if not case or case.status != CaseStatus.PAID.value:
        await callback.answer("Пока не вижу оплату. Если оплатили недавно, подождите уведомление ЮMoney или напишите менеджеру.", show_alert=True)
        return
    await deliver_full_documents(callback.message, session, case)
    await callback.answer()


async def deliver_full_documents(message: Message, session: AsyncSession, case: Case) -> None:
    if case.full_doc_path:
        await message.answer_document(FSInputFile(case.full_doc_path), caption="Полный вариант заявления.")
    if case.instruction_path:
        await message.answer_document(FSInputFile(case.instruction_path), caption="Инструкция по отправке в суд.")
    case.status = CaseStatus.DELIVERED.value
    case.delivered_at = datetime.utcnow()
    await session.commit()
    await message.answer("Готово. Документы выданы. Не забудьте поставить дату и подпись перед отправкой.", reply_markup=case_menu())



