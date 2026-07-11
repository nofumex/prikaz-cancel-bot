from __future__ import annotations

from datetime import date

from app.enums import CaseStatus
from app.utils import full_name, h, money, platform_id_text, username_text


STATUS_LABELS = {
    CaseStatus.DRAFT.value: "Черновик",
    CaseStatus.WAITING_ORDER_PHOTO.value: "Ждем фото приказа",
    CaseStatus.WAITING_ENVELOPE.value: "Ждем конверт или дату",
    CaseStatus.WAITING_RECEIVED_DATE.value: "Ждем дату получения",
    CaseStatus.PROCESSING.value: "Готовим документы",
    CaseStatus.NEEDS_REVIEW.value: "Нужно уточнить данные",
    CaseStatus.PREVIEW_READY.value: "Предпросмотр готов",
    CaseStatus.PAYMENT_PENDING.value: "Ожидает оплату",
    CaseStatus.PAID.value: "Оплачено",
    CaseStatus.DELIVERED.value: "Документы выданы",
    CaseStatus.CANCELED.value: "Отменено",
}


def welcome_text(company_name: str) -> str:
    return (
        "⚖️ <b>Отмена судебного приказа</b>\n"
        f"<i>{h(company_name)}</i>\n\n"
        "Подготовлю заявление по вашим фото:\n"
        "1. пришлите судебный приказ;\n"
        "2. пришлите конверт со штампами или дату получения;\n"
        "3. после распознавания сразу получите preview PDF и ссылку на оплату;\n"
        "4. после оплаты получите полный DOCX и инструкцию текстом.\n\n"
        "<b>Срок:</b> обычно 10 дней с даты получения приказа. Если на конверте несколько штампов, берем самую позднюю дату."
    )


def help_text() -> str:
    return (
        "<b>Помощь</b>\n\n"
        "/start - главное меню\n"
        "/profile - профиль и заявки\n"
        "/new - новое заявление\n"
        "/admin - админ-панель\n"
        "/manager - панель менеджера\n"
        "/endchat - завершить чат\n"
        "/cancel - отменить текущее действие"
    )


def profile_text(user, active_case=None) -> str:
    lines = [
        "👤 <b>Профиль</b>",
        "",
        f"<b>Имя:</b> {full_name(user)}",
        f"<b>Username:</b> {username_text(user)}",
        f"<b>Платформа:</b> {'MAX' if user.platform == 'max' else 'Telegram'}",
        f"<b>ID:</b> <code>{h(platform_id_text(user))}</code>",
        f"<b>Телефон:</b> {h(user.phone or 'не указан')}",
    ]
    if active_case:
        status = STATUS_LABELS.get(active_case.status, active_case.status)
        lines.extend(
            [
                "",
                f"<b>Последняя заявка:</b> #{active_case.id}",
                f"<b>Статус:</b> {h(status)}",
            ]
        )
        if active_case.deadline_date:
            lines.append(f"<b>Срок до:</b> {active_case.deadline_date.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def case_summary(case) -> str:
    status = STATUS_LABELS.get(case.status, case.status)
    lines = [
        f"<b>Заявление #{case.id}</b>",
        f"<b>Статус:</b> {h(status)}",
    ]
    if case.received_date:
        lines.append(f"<b>Дата получения:</b> {case.received_date.strftime('%d.%m.%Y')}")
    if case.deadline_date:
        lines.append(f"<b>Срок на подачу до:</b> {case.deadline_date.strftime('%d.%m.%Y')}")
    if case.payment_label:
        lines.append(f"<b>Оплата:</b> <code>{h(case.payment_label)}</code>")
    return "\n".join(lines)


def payment_text(case, price: int) -> str:
    deadline = case.deadline_date.strftime("%d.%m.%Y") if case.deadline_date else "рассчитан после проверки даты"
    return (
        "📄 <b>Предпросмотр готов.</b>\n\n"
        "Я отправил вариант, где скрыта каждая вторая строка. "
        "После оплаты вы сразу получите полный DOCX и инструкцию по отправке в суд текстом.\n\n"
        f"<b>Стоимость:</b> {money(price)} ₽\n"
        f"<b>Срок на отмену:</b> до {h(deadline)}"
    )


def manual_received_date_prompt_text() -> str:
    return (
        'Введите дату получения судебного приказа в формате ДД.ММ.ГГГГ, например: 10.07.2026\n\n'
        'Можно написать через точку, пробел, слэш, запятую или дефис: 10.07.2026, 10 07 2026, 10/07/26.'
    )


def deadline_warning(deadline: date | None, reminder_no: int) -> str:
    elapsed = {
        1: "Ваше заявление уже подготовлено.",
        2: "Ваше заявление уже подготовлено.",
        3: "Ваше заявление уже подготовлено.",
    }.get(reminder_no, "Заявление уже подготовлено.")
    tail = f"Срок на отмену судебного приказа истекает {deadline.strftime('%d.%m.%Y')}." if deadline else "Срок на отмену судебного приказа ограничен 10 днями."
    return (
        f"<b>{elapsed}</b>\n\n"
        "Ваше заявление уже подготовлено, но полный DOCX еще не оплачен.\n"
        f"{tail}\n\n"
        "Нажмите кнопку оплаты, чтобы получить полный документ и инструкцию."
    )




def no_order_deadline_reminder_text() -> str:
    return (
        "<b>\u0421\u0440\u043e\u043a\u0438 \u043d\u0430 \u043e\u0442\u043c\u0435\u043d\u0443 \u0441\u0443\u0434\u0435\u0431\u043d\u043e\u0433\u043e \u043f\u0440\u0438\u043a\u0430\u0437\u0430 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u044b.</b>\n\n"
        "\u041e\u0431\u044b\u0447\u043d\u043e \u0432\u043e\u0437\u0440\u0430\u0436\u0435\u043d\u0438\u044f \u043d\u0443\u0436\u043d\u043e \u043f\u043e\u0434\u0430\u0442\u044c \u0432 \u0442\u0435\u0447\u0435\u043d\u0438\u0435 10 \u0434\u043d\u0435\u0439 \u0441 \u0434\u0430\u0442\u044b \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u044f \u043f\u0440\u0438\u043a\u0430\u0437\u0430. "
        "\u041f\u043e\u044d\u0442\u043e\u043c\u0443 \u0441\u043e\u0432\u0435\u0442\u0443\u0435\u043c \u043f\u043e\u0442\u043e\u0440\u043e\u043f\u0438\u0442\u044c\u0441\u044f: \u0435\u0441\u043b\u0438 \u0441\u0443\u0434\u0435\u0431\u043d\u044b\u0439 \u043f\u0440\u0438\u043a\u0430\u0437 \u0443\u0436\u0435 \u043d\u0430 \u0440\u0443\u043a\u0430\u0445, \u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0435\u0433\u043e \u0432 \u0431\u043e\u0442, "
        "\u0447\u0442\u043e\u0431\u044b \u043c\u044b \u0443\u0441\u043f\u0435\u043b\u0438 \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u0442\u044c \u0437\u0430\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u0434\u043b\u044f \u043f\u043e\u0434\u0430\u0447\u0438 \u0432 \u0441\u0443\u0434."
    )


def unpaid_document_reminder_text() -> str:
    return (
        "<b>\u0412\u0430\u0448 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442 \u0443\u0436\u0435 \u0433\u043e\u0442\u043e\u0432.</b>\n\n"
        "\u0421\u0440\u043e\u043a\u0438 \u043d\u0430 \u043e\u0442\u043c\u0435\u043d\u0443 \u0441\u0443\u0434\u0435\u0431\u043d\u043e\u0433\u043e \u043f\u0440\u0438\u043a\u0430\u0437\u0430 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u044b. "
        "\u0417\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u0435 \u043e\u043f\u043b\u0430\u0442\u0443, \u0447\u0442\u043e\u0431\u044b \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u043f\u043e\u043b\u043d\u044b\u0435 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b \u0438 \u0443\u0441\u043f\u0435\u0442\u044c \u043f\u043e\u0434\u0430\u0442\u044c \u0437\u0430\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u0432 \u0441\u0443\u0434."
    )


def post_payment_court_followup_text() -> str:
    return (
        "<b>\u041f\u043e\u0434\u0441\u043a\u0430\u0436\u0438\u0442\u0435, \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043b\u0438 \u043f\u0435\u0440\u0435\u0434\u0430\u0442\u044c \u0437\u0430\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u0432 \u0441\u0443\u0434 \u0438\u043b\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0435\u0433\u043e?</b>\n\n"
        "\u0415\u0441\u043b\u0438 \u043f\u0440\u0438 \u043f\u043e\u0434\u0430\u0447\u0435 \u0432\u043e\u0437\u043d\u0438\u043a\u043b\u0438 \u0432\u043e\u043f\u0440\u043e\u0441\u044b \u0438\u043b\u0438 \u0441\u0443\u0434 \u043f\u043e\u043f\u0440\u043e\u0441\u0438\u043b \u0447\u0442\u043e-\u0442\u043e \u0443\u0442\u043e\u0447\u043d\u0438\u0442\u044c, \u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043d\u0430\u043c - \u043f\u043e\u043c\u043e\u0436\u0435\u043c \u0440\u0430\u0437\u043e\u0431\u0440\u0430\u0442\u044c\u0441\u044f."
    )


def consultation_offer_text() -> str:
    return (
        "<b>\u0425\u043e\u0442\u0435\u043b\u0438 \u0431\u044b \u043f\u0440\u043e\u043a\u043e\u043d\u0441\u0443\u043b\u044c\u0442\u0438\u0440\u043e\u0432\u0430\u0442\u044c\u0441\u044f \u043f\u043e \u0432\u0430\u0448\u0435\u0439 \u0441\u0438\u0442\u0443\u0430\u0446\u0438\u0438?</b>\n\n"
        "\u042e\u0440\u0438\u0434\u0438\u0447\u0435\u0441\u043a\u0430\u044f \u043a\u043e\u043c\u043f\u0430\u043d\u0438\u044f \u00ab\u0421\u0438\u043d\u0430\u0439\u00bb \u0437\u0430\u043d\u0438\u043c\u0430\u0435\u0442\u0441\u044f \u0432\u043e\u043f\u0440\u043e\u0441\u0430\u043c\u0438 \u0431\u0430\u043d\u043a\u0440\u043e\u0442\u0441\u0442\u0432\u0430 \u0438 \u043f\u043e\u043c\u043e\u0433\u0430\u0435\u0442 \u043e\u0446\u0435\u043d\u0438\u0442\u044c, "
        "\u0447\u0442\u043e \u043b\u0443\u0447\u0448\u0435 \u0441\u0434\u0435\u043b\u0430\u0442\u044c \u043f\u043e\u0441\u043b\u0435 \u0441\u0443\u0434\u0435\u0431\u043d\u043e\u0433\u043e \u043f\u0440\u0438\u043a\u0430\u0437\u0430."
    )


def manager_request_text(user) -> str:
    return (
        "<b>Пользователь хочет связаться с менеджером</b>\n\n"
        f"<b>Имя:</b> {full_name(user)}\n"
        f"<b>Username:</b> {username_text(user)}\n"
        f"<b>ID:</b> <code>{h(platform_id_text(user))}</code>"
    )
