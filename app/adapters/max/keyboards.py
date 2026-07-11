from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MaxButton:
    text: str
    callback_data: str | None = None
    url: str | None = None


MaxKeyboard = list[list[MaxButton]]


def btn(text: str, callback_data: str | None = None, url: str | None = None) -> MaxButton:
    return MaxButton(text=text, callback_data=callback_data, url=url)


def to_attachments(keyboard: MaxKeyboard | None) -> list[dict] | None:
    if not keyboard:
        return None
    rows = []
    for row in keyboard:
        buttons = []
        for button in row:
            if button.url:
                buttons.append({"type": "link", "text": button.text, "url": button.url})
            elif button.callback_data:
                buttons.append({"type": "callback", "text": button.text, "payload": button.callback_data})
            else:
                buttons.append({"type": "message", "text": button.text})
        rows.append(buttons)
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def main_menu() -> MaxKeyboard:
    return [
        [btn("📝 Подготовить заявление", "case:new")],
        [btn("👤 Профиль", "profile:show"), btn("💬 Менеджер", "chat:start")],
    ]


def profile_menu() -> MaxKeyboard:
    return [
        [btn("📄 Мои документы", "case:my")],
        [btn("💬 Связаться с менеджером", "chat:start")],
        [btn("🏠 Главное меню", "menu:main")],
    ]


def case_menu(can_pay: bool = False, payment_url: str | None = None) -> MaxKeyboard:
    rows: MaxKeyboard = [
        [btn('📝 Новое заявление', 'case:new')],
        [btn('📅 Изменить дату получения', 'case:manual_date')],
        [btn("👤 Профиль", "profile:show"), btn("💬 Менеджер", "chat:start")],
        [btn("🏠 Главное меню", "menu:main")],
    ]
    if can_pay and payment_url:
        rows.insert(0, [btn("💳 Оплатить и получить документ", url=payment_url)])
        rows.insert(1, [btn("✅ Я оплатил", "payment:check")])
    return rows


def envelope_choice() -> MaxKeyboard:
    return [
        [btn("📷 Перефотографировать конверт", "case:envelope_photo")],
        [btn("✍️ Ввести дату вручную", "case:manual_date")],
        [btn("💬 Связаться с менеджером", "chat:start")],
    ]


def order_rephoto_menu() -> MaxKeyboard:
    return [
        [btn("📷 Перефотографировать приказ", "case:rephoto_order")],
        [btn("💬 Связаться с менеджером", "chat:start")],
    ]


def restore_reason_menu() -> MaxKeyboard:
    return [
        [btn("📬 Получил приказ поздно", "case:restore_reason:late")],
        [btn("🏥 Болезнь", "case:restore_reason:illness"), btn("🚗 Командировка / отъезд", "case:restore_reason:trip")],
        [btn("🏠 Не проживал по адресу", "case:restore_reason:not_living")],
        [btn("✍️ Написать свою причину", "case:restore_reason:custom")],
        [btn("💬 Связаться с менеджером", "chat:start")],
    ]


def confirm_extraction() -> MaxKeyboard:
    return [
        [btn("✅ Все верно, готовить документы", "case:generate")],
        [btn("✏️ Исправить поле", "case:edit_fields")],
        [btn("📷 Загрузить приказ заново", "case:new")],
        [btn("💬 Связаться с менеджером", "chat:start")],
    ]


def edit_fields_menu() -> MaxKeyboard:
    return [
        [btn("⚖️ Суд", "case:field:court_name"), btn("📍 Адрес суда", "case:field:court_address")],
        [btn("👤 Должник", "case:field:debtor_full_name"), btn("🏠 Адрес должника", "case:field:debtor_address")],
        [btn("🏦 Взыскатель", "case:field:creditor_name"), btn("📍 Адрес взыскателя", "case:field:creditor_address")],
        [btn("📄 Номер дела", "case:field:case_number"), btn("📅 Дата приказа", "case:field:order_date")],
        [btn("🔖 УИД", "case:field:uid"), btn("🧾 Договор", "case:field:debt_contract")],
        [btn("📆 Период", "case:field:debt_period"), btn("💰 Сумма долга", "case:field:debt_amount")],
        [btn("⚖️ Госпошлина", "case:field:state_duty")],
        [btn("↩️ Назад к проверке", "case:review")],
    ]


def debtor_name_fix_menu() -> MaxKeyboard:
    return [
        [btn("✅ Применить исправление", "case:fix_debtor_name")],
        [btn("✏️ Исправить вручную", "case:field:debtor_full_name")],
        [btn("💬 Связаться с менеджером", "chat:start")],
    ]


def consultation_menu() -> MaxKeyboard:
    return [
        [btn("\U0001f4ac \u0421\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f \u0441 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u043e\u043c", "chat:start")],
    ]


def chat_end_menu() -> MaxKeyboard:
    return [[btn("✅ Завершить чат", "chat:end")]]


def connect_chat_keyboard(session_id: int) -> MaxKeyboard:
    return [[btn("Подключиться в чат", f"chat:session:{session_id}")]]


def admin_panel(payments_enabled: bool = True) -> MaxKeyboard:
    payment_text = "💳 Оплата: ВКЛ" if payments_enabled else "🧪 Оплата: ВЫКЛ"
    return [
        [btn(payment_text, "admin:toggle_payments")],
        [btn("📋 Заявки", "admin:cases:0"), btn("⏳ Ожидают оплату", "admin:payments:0")],
        [btn("📊 Статистика", "admin:stats"), btn("📊 CRM-статистика", "admin:crm_stats")],
        [btn("⚠️ Проблемные заявки", "admin:problem_cases:0"), btn("👥 Менеджеры", "admin:managers")],
        [btn("🏠 Главное меню", "menu:main")],
    ]


def admin_case_actions(case_id: int, paid: bool = False, back: str = "admin:cases:0") -> MaxKeyboard:
    rows: MaxKeyboard = [
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
    return rows


def admin_cases_page(items: list[tuple[int, str]], page: int, total_pages: int, prefix: str) -> MaxKeyboard:
    section = prefix.split(":")[-1]
    rows: MaxKeyboard = [[btn(label, f"admin:case:{case_id}:{section}:{page}")] for case_id, label in items]
    nav: list[MaxButton] = []
    if page > 0:
        nav.append(btn("◀️", f"{prefix}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{max(total_pages, 1)}", "admin:noop"))
    if page + 1 < total_pages:
        nav.append(btn("▶️", f"{prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("⚙️ Админка", "admin:panel")])
    return rows


def manager_panel() -> MaxKeyboard:
    return [
        [btn("📋 Новые заявки", "manager:cases")],
        [btn("💬 Активный чат", "manager:active_chat")],
        [btn("✅ Завершить чат", "chat:end")],
        [btn("🏠 Главное меню", "menu:main")],
    ]


def pay_menu(payment_url: str | None = None) -> MaxKeyboard:
    rows: MaxKeyboard = []
    if payment_url:
        rows.append([btn("💳 Оплатить", url=payment_url)])
    rows.append([btn("✅ Я оплатил", "payment:check")])
    rows.append([btn("🏠 Главное меню", "menu:main")])
    return rows
