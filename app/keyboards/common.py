from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def btn(text: str, callback_data: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    kwargs = {"text": text}
    if callback_data:
        kwargs["callback_data"] = callback_data
    if url:
        kwargs["url"] = url
    return InlineKeyboardButton(**kwargs)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📝 Подготовить заявление", "case:new")],
            [btn("👤 Профиль", "profile:show"), btn("💬 Менеджер", "chat:start")],
        ]
    )


def profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📄 Мои документы", "case:my")],
            [btn("💬 Связаться с менеджером", "chat:start")],
            [btn("🏠 Главное меню", "menu:main")],
        ]
    )


def case_menu(can_pay: bool = False, payment_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [btn("📝 Новое заявление", "case:new")],
        [btn("👤 Профиль", "profile:show"), btn("💬 Менеджер", "chat:start")],
        [btn("🏠 Главное меню", "menu:main")],
    ]
    if can_pay and payment_url:
        rows.insert(0, [btn("💳 Оплатить и получить документ", url=payment_url)])
        rows.insert(1, [btn("✅ Я оплатил", "payment:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def envelope_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📸 Отправить фото конверта", "case:envelope_photo")],
            [btn("✍️ Указать дату вручную", "case:manual_date")],
            [btn("💬 Связаться с менеджером", "chat:start")],
        ]
    )


def confirm_extraction() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Все верно, готовить документы", "case:generate")],
            [btn("✏️ Исправить поле", "case:edit_fields")],
            [btn("📷 Загрузить приказ заново", "case:new")],
            [btn("💬 Связаться с менеджером", "chat:start")],
        ]
    )


def edit_fields_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("⚖️ Суд", "case:field:court_name"), btn("📍 Адрес суда", "case:field:court_address")],
            [btn("👤 Должник", "case:field:debtor_full_name"), btn("🏠 Адрес должника", "case:field:debtor_address")],
            [btn("🏦 Взыскатель", "case:field:creditor_name"), btn("📄 Номер дела", "case:field:case_number")],
            [btn("📅 Дата приказа", "case:field:order_date"), btn("🔖 УИД", "case:field:uid")],
            [btn("🧾 Договор", "case:field:debt_contract"), btn("📆 Период", "case:field:debt_period")],
            [btn("💰 Сумма долга", "case:field:debt_amount"), btn("⚖️ Госпошлина", "case:field:state_duty")],
            [btn("↩️ Назад к проверке", "case:review")],
        ]
    )


def chat_end_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("Завершить чат", "chat:end")]])


def connect_chat_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("Подключиться в чат", f"chat:session:{session_id}")]])


def admin_panel(payments_enabled: bool = True) -> InlineKeyboardMarkup:
    payment_text = "💳 Оплата: ВКЛ" if payments_enabled else "🧪 Оплата: ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(payment_text, "admin:toggle_payments")],
            [btn("📋 Заявки", "admin:cases:0"), btn("⏳ Ожидают оплату", "admin:payments:0")],
            [btn("📊 Статистика", "admin:stats"), btn("👥 Менеджеры", "admin:managers")],
            [btn("🏠 Главное меню", "menu:main")],
        ]
    )


def admin_case_actions(case_id: int, paid: bool = False, back: str = "admin:cases:0") -> InlineKeyboardMarkup:
    rows = [
        [btn("💬 Открыть чат", f"chat:case:{case_id}")],
        [btn("👤 Профиль клиента", f"admin:user:{case_id}")],
    ]
    if not paid:
        rows.insert(0, [btn("✅ Отметить оплату", f"admin:mark_paid:{case_id}")])
    rows.append([btn("↩️ Назад", back), btn("⚙️ Админка", "admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cases_page(items: list[tuple[int, str]], page: int, total_pages: int, prefix: str) -> InlineKeyboardMarkup:
    section = prefix.split(":")[-1]
    rows = [[btn(label, f"admin:case:{case_id}:{section}:{page}")] for case_id, label in items]
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"{prefix}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{max(total_pages, 1)}", "admin:noop"))
    if page + 1 < total_pages:
        nav.append(btn("▶️", f"{prefix}:{page + 1}"))
    rows.append(nav)
    rows.append([btn("⚙️ Админка", "admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def manager_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📋 Новые заявки", "manager:cases")],
            [btn("💬 Активный чат", "manager:active_chat")],
            [btn("✅ Завершить чат", "chat:end")],
            [btn("🏠 Главное меню", "menu:main")],
        ]
    )
