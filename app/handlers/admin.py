from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CaseStatus
from app.keyboards.common import admin_case_actions, admin_cases_page, admin_panel, manager_panel
from app.models import Case, User
from app.services.app_settings import payments_enabled, toggle_payments
from app.services.payments import mark_paid_by_label
from app.texts import case_summary
from app.utils import full_name, h, username_text

router = Router(name="admin")
PAGE_SIZE = 5


async def _ensure_admin_message(message: Message, user: User) -> bool:
    if not user.is_admin:
        await message.answer("Эта команда доступна только администратору.")
        return False
    return True


async def _ensure_manager_message(message: Message, user: User) -> bool:
    if not user.is_manager:
        await message.answer("Эта команда доступна только менеджеру или администратору.")
        return False
    return True


async def _ensure_admin_callback(callback: CallbackQuery, user: User) -> bool:
    if not user.is_admin:
        await callback.answer("Недостаточно прав", show_alert=True)
        return False
    return True


@router.message(Command("admin"))
async def cmd_admin(message: Message, current_user: User) -> None:
    if await _ensure_admin_message(message, current_user):
        await message.answer("<b>⚙️ Админ-панель</b>", reply_markup=admin_panel(payments_enabled()))


@router.callback_query(F.data == "admin:panel")
async def cb_admin(callback: CallbackQuery, current_user: User) -> None:
    if await _ensure_admin_callback(callback, current_user):
        await callback.message.answer("<b>⚙️ Админ-панель</b>", reply_markup=admin_panel(payments_enabled()))
    await callback.answer()


@router.callback_query(F.data == "admin:noop")
async def cb_admin_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(Command("manager"))
async def cmd_manager(message: Message, current_user: User) -> None:
    if await _ensure_manager_message(message, current_user):
        await message.answer("<b>Панель менеджера</b>", reply_markup=manager_panel())


@router.callback_query(F.data == "manager:cases")
@router.callback_query(F.data.startswith("admin:cases"))
async def cb_cases(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_manager:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    page = int(callback.data.split(":")[-1]) if callback.data.startswith("admin:cases:") else 0
    total = int(await session.scalar(select(func.count(Case.id))) or 0)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    result = await session.execute(select(Case).order_by(Case.created_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE))
    cases = list(result.scalars().all())
    if not cases:
        await callback.message.answer("Заявок пока нет.", reply_markup=admin_panel(payments_enabled()) if current_user.is_admin else manager_panel())
        await callback.answer()
        return
    items = []
    for case in cases:
        await session.refresh(case, ["user"])
        name = full_name(case.user).replace("<", "").replace(">", "")
        date = case.created_at.strftime("%d.%m") if case.created_at else ""
        items.append((case.id, f"#{case.id} • {date} • {name}"))
    await callback.message.answer(
        f"<b>📋 Заявки</b>\n\nПоказано по {PAGE_SIZE} на странице. Выберите заявку:",
        reply_markup=admin_cases_page(items, page, total_pages, "admin:cases"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:payments"))
async def cb_payments(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    page = int(callback.data.split(":")[-1]) if callback.data.startswith("admin:payments:") else 0
    count_stmt = select(func.count(Case.id)).where(Case.status == CaseStatus.PAYMENT_PENDING.value)
    total = int(await session.scalar(count_stmt) or 0)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    result = await session.execute(
        select(Case)
        .where(Case.status == CaseStatus.PAYMENT_PENDING.value)
        .order_by(Case.created_at.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    cases = list(result.scalars().all())
    if not cases:
        await callback.message.answer("Неоплаченных предпросмотров пока нет.", reply_markup=admin_panel(payments_enabled()))
        await callback.answer()
        return
    items = []
    for case in cases:
        await session.refresh(case, ["user"])
        name = full_name(case.user).replace("<", "").replace(">", "")
        date = case.created_at.strftime("%d.%m") if case.created_at else ""
        items.append((case.id, f"#{case.id} • {date} • {name}"))
    await callback.message.answer(
        "<b>⏳ Ожидают оплату</b>\n\nВыберите заявку:",
        reply_markup=admin_cases_page(items, page, total_pages, "admin:payments"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:case:"))
async def cb_case_detail(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not current_user.is_manager:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, _, case_id, prefix, page = callback.data.split(":")
    case = await session.get(Case, int(case_id))
    if not case:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await session.refresh(case, ["user"])
    text = (
        case_summary(case)
        + "\n\n"
        + f"<b>Клиент:</b> {full_name(case.user)}\n"
        + f"<b>Username:</b> {username_text(case.user)}\n"
        + f"<b>ID:</b> <code>{h(case.user.platform_user_id)}</code>"
    )
    paid = case.status in {CaseStatus.PAID.value, CaseStatus.DELIVERED.value}
    await callback.message.answer(text, reply_markup=admin_case_actions(case.id, paid=paid, back=f"admin:{prefix}:{page}"))
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_stats(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    users_total = int(await session.scalar(select(func.count(User.id))) or 0)
    cases_total = int(await session.scalar(select(func.count(Case.id))) or 0)
    pending = int(await session.scalar(select(func.count(Case.id)).where(Case.status == CaseStatus.PAYMENT_PENDING.value)) or 0)
    paid = int(await session.scalar(select(func.count(Case.id)).where(Case.status.in_([CaseStatus.PAID.value, CaseStatus.DELIVERED.value]))) or 0)
    await callback.message.answer(
        "<b>Статистика</b>\n\n"
        f"Пользователей: {users_total}\n"
        f"Заявлений всего: {cases_total}\n"
        f"Ожидают оплату: {pending}\n"
        f"Оплачено/выдано: {paid}",
        reply_markup=admin_panel(payments_enabled()),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:toggle_payments")
async def cb_toggle_payments(callback: CallbackQuery, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    enabled = toggle_payments()
    await callback.message.answer(
        f"Режим оплаты {'включен' if enabled else 'выключен для тестов'}.",
        reply_markup=admin_panel(enabled),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:mark_paid:"))
async def cb_mark_paid(callback: CallbackQuery, bot: Bot, session: AsyncSession, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    case = await session.get(Case, int(callback.data.split(":")[-1]))
    if not case or not case.payment_label:
        await callback.answer("Заявка без платежа", show_alert=True)
        return
    paid_case = await mark_paid_by_label(session, case.payment_label, {"manual_admin_id": current_user.id})
    await session.refresh(paid_case, ["user"])
    await callback.message.answer(f"Оплата по заявлению #{paid_case.id} отмечена.")
    if paid_case.user.telegram_id:
        from app.handlers.case_flow import deliver_full_documents

        await bot.send_message(paid_case.user.telegram_id, "Оплата подтверждена. Отправляю полный комплект документов.")
        # Use a lightweight stand-in object with answer_document methods unavailable is not safe here;
        # send directly to keep admin confirmation deterministic.
        from aiogram.types import FSInputFile

        if paid_case.full_doc_path:
            await bot.send_document(paid_case.user.telegram_id, FSInputFile(paid_case.full_doc_path), caption="Полный вариант заявления.")
        if paid_case.instruction_path:
            await bot.send_document(paid_case.user.telegram_id, FSInputFile(paid_case.instruction_path), caption="Инструкция по отправке в суд.")
        paid_case.status = CaseStatus.DELIVERED.value
        await session.commit()
    await callback.answer()


@router.callback_query(F.data == "admin:managers")
async def cb_managers(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    result = await session.execute(select(User).where(User.is_manager.is_(True)).order_by(User.created_at.desc()))
    managers = list(result.scalars().all())
    if not managers:
        await callback.message.answer("Менеджеров пока нет. Добавьте MANAGER_IDS в .env.", reply_markup=admin_panel(payments_enabled()))
    else:
        await callback.message.answer(
            "<b>Менеджеры</b>\n\n"
            + "\n".join(f"{full_name(user)} | {username_text(user)} | <code>{h(user.platform_user_id)}</code>" for user in managers),
            reply_markup=admin_panel(payments_enabled()),
        )
    await callback.answer()
