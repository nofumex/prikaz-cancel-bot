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
        [btn('📝 Новое заявление', 'case:new')],
        [btn('📅 Изменить дату получения', 'case:manual_date')],
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
            [btn("📷 Перефотографировать конверт", "case:envelope_photo")],
            [btn("✍️ Ввести дату вручную", "case:manual_date")],
            [btn("💬 Связаться с менеджером", "chat:start")],
        ]
    )


def order_rephoto_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📷 Перефотографировать приказ", "case:rephoto_order")],
            [btn("💬 Связаться с менеджером", "chat:start")],
        ]
    )


def restore_reason_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📬 Получил приказ поздно", "case:restore_reason:late")],
            [btn("🏥 Болезнь", "case:restore_reason:illness"), btn("🚗 Командировка / отъезд", "case:restore_reason:trip")],
            [btn("🏠 Не проживал по адресу", "case:restore_reason:not_living")],
            [btn("✍️ Написать свою причину", "case:restore_reason:custom")],
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
            [btn("🏦 Взыскатель", "case:field:creditor_name"), btn("📍 Адрес взыскателя", "case:field:creditor_address")],
            [btn("📄 Номер дела", "case:field:case_number"), btn("📅 Дата приказа", "case:field:order_date")],
            [btn("🔖 УИД", "case:field:uid"), btn("🧾 Договор", "case:field:debt_contract")],
            [btn("📆 Период", "case:field:debt_period"), btn("💰 Сумма долга", "case:field:debt_amount")],
            [btn("⚖️ Госпошлина", "case:field:state_duty")],
            [btn("↩️ Назад к проверке", "case:review")],
        ]
    )


def debtor_name_fix_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Применить исправление", "case:fix_debtor_name")],
            [btn("✏️ Исправить вручную", "case:field:debtor_full_name")],
            [btn("💬 Связаться с менеджером", "chat:start")],
        ]
    )


def consultation_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("\U0001f4ac \u0421\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f \u0441 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u043e\u043c", "chat:start")],
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
            [btn('📣 Рассылки', 'admin:broadcasts')],
            [btn(payment_text, "admin:toggle_payments")],
            [btn("📋 Заявки", "admin:cases:0"), btn("⏳ Ожидают оплату", "admin:payments:0")],
            [btn("📊 Статистика", "admin:stats"), btn("📊 CRM-статистика", "admin:crm_stats")],
            [btn("⚠️ Проблемные заявки", "admin:problem_cases:0"), btn("👥 Менеджеры", "admin:managers")],
            [btn("🏠 Главное меню", "menu:main")],
        ]
    )


def paid_document_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn('❌ Данные в заявлении неверные', 'paid:correction:start')]])


def paid_edit_fields_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn('⚖️ Суд', 'paid:field:court_name'), btn('📍 Адрес суда', 'paid:field:court_address')],
        [btn('👤 Должник', 'paid:field:debtor_full_name'), btn('🏠 Адрес должника', 'paid:field:debtor_address')],
        [btn('🏦 Взыскатель', 'paid:field:creditor_name'), btn('📍 Адрес взыскателя', 'paid:field:creditor_address')],
        [btn('📄 Номер дела', 'paid:field:case_number'), btn('📅 Дата приказа', 'paid:field:order_date')],
        [btn('🔖 УИД', 'paid:field:uid'), btn('🧾 Договор', 'paid:field:debt_contract')],
        [btn('📆 Период', 'paid:field:debt_period'), btn('💰 Сумма долга', 'paid:field:debt_amount')],
        [btn('⚖️ Госпошлина', 'paid:field:state_duty')],
        [btn('↩️ Назад к проверке', 'paid:review')],
    ])


def paid_review_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn('✅ Все верно, пересоздать заявление', 'paid:regenerate')],
        [btn('✏️ Исправить еще поле', 'paid:correction:start')],
        [btn('💬 Связаться с менеджером', 'chat:start')],
    ])


def broadcast_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn('🚀 Напомнить попробовать', 'broadcast:ask:try')],
        [btn('💳 Напомнить оплатить', 'broadcast:ask:pay')],
        [btn('💬 Предложить консультацию', 'broadcast:ask:consultation')],
        [btn('⚙️ Настройки', 'broadcast:settings')],
        [btn('🔄 Обновить статистику', 'admin:broadcasts'), btn('↩️ Админка', 'admin:panel')],
    ])


def broadcast_confirm(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn('✅ Да, отправить', f'broadcast:send:{kind}'), btn('❌ Нет', 'admin:broadcasts')],
    ])


def broadcast_settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn('✏️ Текст: попробовать', 'broadcast:edit:text:try'), btn('⏱ Задержка', 'broadcast:edit:hours:try')],
        [btn('✏️ Текст: оплатить', 'broadcast:edit:text:pay'), btn('⏱ Задержка', 'broadcast:edit:hours:pay')],
        [btn('✏️ Текст: консультация', 'broadcast:edit:text:consultation'), btn('⏱ Задержка', 'broadcast:edit:hours:consultation')],
        [btn('↩️ К рассылкам', 'admin:broadcasts')],
    ])


def admin_case_actions(case_id: int, paid: bool = False, back: str = "admin:cases:0") -> InlineKeyboardMarkup:
    rows = [
        [btn("🔁 Повторно распознать суммы", f"admin:retry_amounts:{case_id}")],
        [
            btn("✏️ Исправить долг", f"admin:edit_amount:{case_id}:debt_amount"),
            btn("✏️ Исправить госпошлину", f"admin:edit_amount:{case_id}:state_duty"),
        ],
        [btn("✏️ Исправить итог", f"admin:edit_amount:{case_id}:total_amount")],
        [btn("✅ Применить предложенную сумму", f"admin:apply_suggested:{case_id}")],
        [btn("✅ Повторить QA", f"admin:rerun_qa:{case_id}"), btn("📄 Сгенерировать документы", f"admin:generate:{case_id}")],
        [btn("💬 Открыть чат", f"chat:case:{case_id}")],
        [btn("👤 Профиль клиента", f"admin:user:{case_id}")],
    ]
    if not paid:
        rows.insert(0, [btn("✅ Отметить оплату", f"admin:mark_paid:{case_id}")])
    rows.insert(1 if not paid else 0, [btn("🔄 Синхронизировать с CRM", f"admin:crm_sync:{case_id}")])
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
