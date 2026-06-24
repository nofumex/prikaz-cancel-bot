from __future__ import annotations

from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
import json
import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

from app.enums import CaseStatus
from app.keyboards.common import admin_case_actions, admin_cases_page, admin_panel, manager_panel
from app.models import Case, CrmSyncLog, OpenAIUsage, User
from app.services.amocrm import get_amocrm_service
from app.services.app_settings import payments_enabled, toggle_payments
from app.services.payments import mark_paid_by_label
from app.texts import case_summary
from app.services.legal_data import FIELD_LABELS, normalize_order_data
from app.utils import full_name, h, safe_json_loads, username_text

router = Router(name="admin")
PAGE_SIZE = 5


class AdminAmountStates(StatesGroup):
    waiting_amount_value = State()


def _money_usd(value: float | None) -> str:
    return f"${(value or 0.0):.4f}"


async def _usage_totals(session: AsyncSession, *, case_filter=None) -> tuple[float, int, int, int, int, int]:
    stmt = select(
        func.coalesce(func.sum(OpenAIUsage.total_cost_usd), 0.0),
        func.coalesce(func.sum(OpenAIUsage.total_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.input_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.cached_input_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.output_tokens), 0),
        func.coalesce(func.sum(OpenAIUsage.reasoning_tokens), 0),
    ).select_from(OpenAIUsage)
    if case_filter is not None:
        stmt = stmt.join(Case, Case.id == OpenAIUsage.case_id).where(case_filter)
    result = await session.execute(stmt)
    row = result.one()
    return float(row[0] or 0.0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0), int(row[4] or 0), int(row[5] or 0)


async def _avg_per_case(session: AsyncSession, case_filter) -> tuple[float, int]:
    usage_case = (
        select(
            OpenAIUsage.case_id.label("case_id"),
            func.sum(OpenAIUsage.total_cost_usd).label("total_cost_usd"),
            func.sum(OpenAIUsage.total_tokens).label("total_tokens"),
        )
        .group_by(OpenAIUsage.case_id)
        .subquery()
    )
    stmt = select(
        func.coalesce(func.avg(usage_case.c.total_cost_usd), 0.0),
        func.coalesce(func.avg(usage_case.c.total_tokens), 0),
    ).select_from(usage_case).join(Case, Case.id == usage_case.c.case_id).where(case_filter)
    result = await session.execute(stmt)
    row = result.one()
    return float(row[0] or 0.0), int(row[1] or 0)


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
    crm_lines = [
        "",
        "<b>CRM:</b>",
        f"Контакт: {case.amocrm_contact_id or '—'}",
        f"Сделка: {case.amocrm_lead_id or case.amo_lead_id or '—'}",
        f"Воронка: Судебный приказ (ID {case.amocrm_pipeline_id or '—'})",
        f"Этап: {case.amocrm_status_name or '—'} (ID {case.amocrm_status_id or '—'})",
        f"Последняя синхронизация: {case.amocrm_last_sync_at.strftime('%d.%m.%Y %H:%M') if case.amocrm_last_sync_at else '—'}",
    ]
    if case.amocrm_sync_error:
        crm_lines.append(f"Ошибка: {h(case.amocrm_sync_error)}")
    usage_rows = await session.execute(
        select(OpenAIUsage).where(OpenAIUsage.case_id == case.id).order_by(OpenAIUsage.created_at.asc())
    )
    usages = list(usage_rows.scalars().all())
    usage_lines = ["", "<b>OpenAI по заявке:</b>"]
    total_usage = 0.0
    for row in usages:
        usage_lines.append(
            f"{row.operation}: input {row.input_tokens}, output {row.output_tokens}, cost {_money_usd(row.total_cost_usd)}"
        )
        total_usage += row.total_cost_usd or 0.0
    if usages:
        usage_lines.append(f"Итого: {_money_usd(total_usage)}")
    text = (
        case_summary(case)
        + "\n\n"
        + f"<b>Клиент:</b> {full_name(case.user)}\n"
        + f"<b>Username:</b> {username_text(case.user)}\n"
        + f"<b>ID:</b> <code>{h(case.user.platform_user_id)}</code>"
        + "\n".join(crm_lines)
        + ("\n".join(usage_lines) if usages else "")
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
    total_cost, total_tokens, input_tokens, cached_tokens, output_tokens, reasoning_tokens = await _usage_totals(session)
    manual_cost, manual_tokens = await _avg_per_case(session, Case.envelope_photo_path.is_(None))
    envelope_cost, envelope_tokens = await _avg_per_case(session, Case.envelope_photo_path.is_not(None))
    completed_cost, completed_tokens = await _avg_per_case(session, Case.status.in_([CaseStatus.PAID.value, CaseStatus.DELIVERED.value]))
    ten_dollars = 10.0
    manual_gen = int(ten_dollars / manual_cost) if manual_cost else 0
    envelope_gen = int(ten_dollars / envelope_cost) if envelope_cost else 0
    completed_gen = int(ten_dollars / completed_cost) if completed_cost else 0
    crm_synced = int(await session.scalar(select(func.count(Case.id)).where(Case.amocrm_synced.is_(True))) or 0)
    crm_errors = int(await session.scalar(select(func.count(CrmSyncLog.id)).where(CrmSyncLog.success.is_(False))) or 0)
    crm_pending = int(
        await session.scalar(select(func.count(Case.id)).where(Case.amocrm_sync_error.is_not(None), Case.amocrm_synced.is_(False)))
        or 0
    )
    await callback.message.answer(
        "<b>Статистика</b>\n\n"
        f"Пользователей: {users_total}\n"
        f"Заявлений всего: {cases_total}\n"
        f"Ожидают оплату: {pending}\n"
        f"Оплачено/выдано: {paid}\n\n"
        "<b>CRM</b>\n"
        f"Синхронизировано сделок: {crm_synced}\n"
        f"Ошибки синхронизации: {crm_errors}\n"
        f"Ожидают повторной синхронизации: {crm_pending}\n\n"
        "<b>OpenAI API</b>\n"
        f"Всего потрачено: {_money_usd(total_cost)}\n"
        f"Всего токенов: {total_tokens}\n"
        f"Input: {input_tokens}\n"
        f"Cached input: {cached_tokens}\n"
        f"Output: {output_tokens}\n"
        f"Reasoning: {reasoning_tokens}\n\n"
        "<b>Средний расход на 1 генерацию</b>\n"
        f"- приказ + ручная дата: {_money_usd(manual_cost)}, {manual_tokens} токенов\n"
        f"- приказ + конверт: {_money_usd(envelope_cost)}, {envelope_tokens} токенов\n"
        f"- среднее по всем завершенным заявкам: {_money_usd(completed_cost)}, {completed_tokens} токенов\n\n"
        "<b>Примерно генераций на $10</b>\n"
        f"- по среднему расходу: {completed_gen}\n"
        f"- если без конверта: {manual_gen}\n"
        f"- если с конвертом: {envelope_gen}",
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


@router.callback_query(F.data.startswith("admin:crm_sync:"))
async def cb_crm_sync(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    case = await session.get(Case, int(callback.data.split(":")[-1]))
    if not case:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await session.refresh(case, ["user"])
    crm = get_amocrm_service(settings)
    await crm.sync_case_current_state(session, case, case.user)
    await callback.message.answer(f"CRM-синхронизация для заявки #{case.id} выполнена.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:retry_amounts:"))
async def cb_retry_amounts(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User, bot: Bot) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    case = await session.get(Case, int(callback.data.split(":")[-1]))
    if not case or not case.order_photo_path:
        await callback.answer("Нет фото приказа", show_alert=True)
        return
    await session.refresh(case, ["user"])
    from app.handlers.case_flow import _resolve_amount_mismatch
    from app.services.amount_recovery import format_amount_mismatch_admin_report
    data, amount_check, recovery, retry_amounts = await _resolve_amount_mismatch(
        settings, session, case, case.user, data, bot=bot, force_retry=True
    )
    if amount_check.ok:
        await callback.message.answer(f"✅ Суммы согласованы для заявки #{case.id}. Можно генерировать документы.")
    else:
        report = format_amount_mismatch_admin_report(case.id, data, retry_amounts, amount_check, recovery)
        await callback.message.answer(report)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:apply_suggested:"))
async def cb_apply_suggested_amount(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    case_id = int(callback.data.split(":")[-1])
    case = await session.get(Case, case_id)
    if not case:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    debug_path = Path("storage/debug") / f"case_{case_id}" / "amount_recovery.json"
    if not debug_path.exists():
        await callback.answer("Нет предложенной суммы", show_alert=True)
        return
    payload = json.loads(debug_path.read_text(encoding="utf-8"))
    qa = payload.get("qa_report") or {}
    new_debt = qa.get("new_debt_amount") or qa.get("debt_candidate")
    if not new_debt:
        await callback.answer("Нет предложенной суммы", show_alert=True)
        return
    data = normalize_order_data(safe_json_loads(case.extracted_json, {}))
    data["debt_amount"] = new_debt
    data = normalize_order_data(data)
    case.extracted_json = json.dumps(data, ensure_ascii=False)
    await session.commit()
    await callback.message.answer(f"✅ Применена сумма долга: {new_debt}")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:edit_amount:"))
async def cb_edit_amount(callback: CallbackQuery, state: FSMContext, session: AsyncSession, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    _, _, case_id, field = callback.data.split(":")
    case = await session.get(Case, int(case_id))
    if not case:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    label = FIELD_LABELS.get(field, field)
    await state.update_data(case_id=int(case_id), edit_amount_field=field)
    await state.set_state(AdminAmountStates.waiting_amount_value)
    await callback.message.answer(f"Введите новое значение для поля <b>{label}</b>.")
    await callback.answer()


@router.message(AdminAmountStates.waiting_amount_value)
async def admin_receive_amount_value(message: Message, state: FSMContext, session: AsyncSession) -> None:
    state_data = await state.get_data()
    case = await session.get(Case, state_data["case_id"])
    field = state_data["edit_amount_field"]
    value = (message.text or "").strip()
    if not case or not value:
        await message.answer("Значение не должно быть пустым.")
        return
    data = normalize_order_data(safe_json_loads(case.extracted_json, {}))
    data[field] = value
    data = normalize_order_data(data)
    case.extracted_json = json.dumps(data, ensure_ascii=False)
    await session.commit()
    await state.clear()
    await message.answer(f"✅ Поле {FIELD_LABELS.get(field, field)} обновлено: {data.get(field)}")


@router.callback_query(F.data.startswith("admin:rerun_qa:"))
async def cb_rerun_qa(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User, bot: Bot) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    case = await session.get(Case, int(callback.data.split(":")[-1]))
    if not case:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await session.refresh(case, ["user"])
    from app.handlers.case_flow import _generate_documents_flow

    await callback.message.answer(f"🔄 Повторная генерация для заявки #{case.id}...")
    await _generate_documents_flow(callback.message, session, settings, case.user, case, bot=bot)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:generate:"))
async def cb_admin_generate(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User, bot: Bot) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    case = await session.get(Case, int(callback.data.split(":")[-1]))
    if not case:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await session.refresh(case, ["user"])
    from app.handlers.case_flow import _generate_documents_flow

    await _generate_documents_flow(callback.message, session, settings, case.user, case, bot=bot)
    await callback.answer()


@router.callback_query(F.data == "admin:check_crm")
async def cb_check_crm(callback: CallbackQuery, session: AsyncSession, settings: Settings, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    crm = get_amocrm_service(settings)
    report = await crm.ensure_pipeline_and_statuses()
    if not report.get("pipeline"):
        await callback.message.answer("amoCRM недоступна или воронка не найдена.")
        await callback.answer()
        return
    lines = [
        "amoCRM проверена",
        "",
        f"Воронка: {report['pipeline'].get('name')}",
        f"Pipeline ID: {report['pipeline'].get('id')}",
        "",
        "Этапы:",
    ]
    for status_name in [
        "Подписался на бота",
        "Отправил приказ",
        "Ввел дату",
        "Оплатил",
        "Получил заявление",
        "Нужна проверка",
    ]:
        sid = report.get("statuses", {}).get(status_name)
        mark = "✅" if sid else "❌"
        lines.append(f"{mark} {status_name}" + (f" — id {sid}" if sid else ""))
    lines.append("")
    lines.append(f"Создано новых этапов: {report.get('created', 0)}")
    lines.append("Ошибки: " + (", ".join(report.get("errors", [])) if report.get("errors") else "none"))
    await callback.message.answer("\n".join(lines), reply_markup=admin_panel(payments_enabled()))
    await callback.answer()


@router.callback_query(F.data == "admin:crm_stats")
async def cb_crm_stats(callback: CallbackQuery, session: AsyncSession, current_user: User) -> None:
    if not await _ensure_admin_callback(callback, current_user):
        return
    synced = int(await session.scalar(select(func.count(Case.id)).where(Case.amocrm_synced.is_(True))) or 0)
    errors = int(await session.scalar(select(func.count(CrmSyncLog.id)).where(CrmSyncLog.success.is_(False))) or 0)
    pending = int(await session.scalar(select(func.count(Case.id)).where(Case.amocrm_synced.is_(False))) or 0)
    last_error = await session.scalar(
        select(CrmSyncLog.error_message).where(CrmSyncLog.success.is_(False)).order_by(CrmSyncLog.created_at.desc()).limit(1)
    )
    await callback.message.answer(
        "CRM:\n"
        f"Сделок синхронизировано: {synced}\n"
        f"Ошибки синхронизации: {errors}\n"
        f"Ожидают синхронизации: {pending}\n"
        f"Последняя ошибка: {h(last_error) if last_error else 'нет'}",
        reply_markup=admin_panel(payments_enabled()),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:mark_paid:"))
async def cb_mark_paid(callback: CallbackQuery, bot: Bot, session: AsyncSession, settings: Settings, current_user: User) -> None:
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
        if paid_case.full_pdf_path:
            await bot.send_document(paid_case.user.telegram_id, FSInputFile(paid_case.full_pdf_path), caption="Полный PDF.")
        if paid_case.instruction_path:
            await bot.send_document(paid_case.user.telegram_id, FSInputFile(paid_case.instruction_path), caption="Инструкция по отправке в суд.")
        paid_case.status = CaseStatus.DELIVERED.value
        paid_case.delivered_at = datetime.utcnow()
        crm = get_amocrm_service(settings)
        await crm.sync_case_event(session, paid_case, paid_case.user, "payment_paid", {"note": "Оплата подтверждена вручную админом."})
        await crm.sync_case_event(
            session,
            paid_case,
            paid_case.user,
            "documents_delivered",
            {"note": "Документы выданы клиенту после ручного подтверждения оплаты"},
        )
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
