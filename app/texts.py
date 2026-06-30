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

def manager_request_text(user) -> str:
    return (
        "<b>Пользователь хочет связаться с менеджером</b>\n\n"
        f"<b>Имя:</b> {full_name(user)}\n"
        f"<b>Username:</b> {username_text(user)}\n"
        f"<b>ID:</b> <code>{h(platform_id_text(user))}</code>"
    )
