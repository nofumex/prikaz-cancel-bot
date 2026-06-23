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
        [btn("Подготовить заявление", "case:new")],
        [btn("Профиль", "profile:show")],
        [btn("Связаться с менеджером", "chat:start")],
    ]


def envelope_choice() -> MaxKeyboard:
    return [[btn("Отправить фото конверта", "case:envelope_photo")], [btn("Указать дату вручную", "case:manual_date")]]


def pay_menu(payment_url: str | None = None) -> MaxKeyboard:
    rows: MaxKeyboard = []
    if payment_url:
        rows.append([btn("Оплатить", url=payment_url)])
    rows.append([btn("Я оплатил", "payment:check")])
    rows.append([btn("Главное меню", "menu:main")])
    return rows


def chat_end_menu() -> MaxKeyboard:
    return [[btn("Завершить чат", "chat:end")]]


