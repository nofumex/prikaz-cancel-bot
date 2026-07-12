from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.max.bot import _download_event_image
from app.adapters.max.mapper import IncomingEvent
from app.config import get_settings
from app.enums import CaseStatus
from app.models import Case, User
from app.services.legal_data import normalize_order_data


def _make_settings(**kwargs):
    settings = get_settings()
    return settings.__class__(**{**settings.__dict__, **kwargs})


def _case(**kwargs) -> Case:
    base = dict(
        id=1,
        user_id=1,
        platform="max",
        status=CaseStatus.PROCESSING.value,
        received_date=None,
        deadline_date=None,
        extracted_json=json.dumps(
            normalize_order_data(
                {
                    "court_name": "судебный участок №5 города Ессентуки",
                    "debtor_full_name": "Иванов Иван Иванович",
                    "creditor_name": "АО «Почта Банк»",
                    "case_number": "2-146-09-434/2021",
                    "uid": "26MS0031-01-2021-000169-72",
                    "order_date": "18.01.2021",
                    "debt_amount": "78 472 руб. 87 коп.",
                    "state_duty": "1 277 руб. 00 коп.",
                    "total_amount": "79 749 руб. 87 коп.",
                }
            ),
            ensure_ascii=False,
        ),
        order_rephoto_attempts=0,
        order_photo_path="storage/max/order.jpg",
    )
    base.update(kwargs)
    return Case(**base)


@pytest.mark.asyncio
async def test_rephoto_downloads_use_unique_paths(tmp_path) -> None:
    async def download(url, destination):
        destination.write_bytes(url.encode())
        return destination

    client = SimpleNamespace(download_external_url=download)
    settings = SimpleNamespace(max_download_dir=str(tmp_path))
    first = IncomingEvent(platform_user_id='1', chat_id='1', message_id='mid.first', photo_url='first')
    second = IncomingEvent(platform_user_id='1', chat_id='1', message_id='mid.second', photo_url='second')

    first_path = await _download_event_image(client, first, 71, 'order', settings)
    second_path = await _download_event_image(client, second, 71, 'order', settings)

    assert first_path != second_path
    assert first_path.read_bytes() == b'first'
    assert second_path.read_bytes() == b'second'


@pytest.mark.asyncio
async def test_max_order_without_received_date_prompts_for_date(monkeypatch):
    from app.adapters.max import bot as max_bot
    from app.adapters.max.mapper import IncomingEvent

    settings = _make_settings(amocrm_enabled=False)
    case = _case()
    user = User(id=1, platform="max", platform_user_id="42")
    event = IncomingEvent(platform_user_id="42", chat_id="chat-1", photo_url="https://example.test/order.jpg")
    client = SimpleNamespace(send_message=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    generate = AsyncMock()

    monkeypatch.setattr(max_bot, "_clear_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_set_state", AsyncMock())
    monkeypatch.setattr(max_bot, "_generate_documents", generate)
    monkeypatch.setattr(
        max_bot,
        "extract_order_data",
        AsyncMock(
            return_value={
                "court_name": "судебный участок №5 города Ессентуки",
                "debtor_full_name": "Иванов Иван Иванович",
                "creditor_name": "АО «Почта Банк»",
                "case_number": "2-146-09-434/2021",
                "uid": "26MS0031-01-2021-000169-72",
                "order_date": "18.01.2021",
                "debt_amount": "78 472 руб. 87 коп.",
                "state_duty": "1 277 руб. 00 коп.",
                "total_amount": "79 749 руб. 87 коп.",
            }
        ),
    )
    monkeypatch.setattr(max_bot, "schedule_crm_sync", lambda *args, **kwargs: None)

    await max_bot._extract_and_process_order(client, event, session, settings, user, case)

    assert generate.await_count == 0
    assert max_bot._set_state.await_count == 1
    assert max_bot.DATE_PROMPT in client.send_message.await_args_list[-1].kwargs["text"]
    assert max_bot.STATE_MANUAL_DATE in [call.args[2] for call in max_bot._set_state.await_args_list]
