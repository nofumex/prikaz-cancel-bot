#!/usr/bin/env python3
"""One-command, idempotent local installer for useful prikaz-cancel-bot mailings.

Run from the root of a checkout of nofumex/prikaz-cancel-bot:
    python install_useful_mailings.py

Requires only the Python standard library. Does not send messages or access the network.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime

MARKER = "USEFUL_MAILING_TEXTS = {"
TARGETS = (
    "app/services/automatic_mailings.py",
    "app/keyboards/common.py",
    "app/adapters/max/keyboards.py",
    "tests/test_automatic_mailings.py",
    "README.md",
)


def once(source: str, old: str, new: str, label: str) -> str:
    matches = source.count(old)
    if matches != 1:
        raise ValueError(f"{label}: нужен ровно один фрагмент, найдено {matches}. Файлы не изменены.")
    return source.replace(old, new, 1)


def patch_mailings(src: str) -> str:
    src = once(src,
        "from app.keyboards.common import automatic_mailing_menu",
        "from app.keyboards.common import automatic_mailing_menu, useful_mailing_menu",
        "import keyboard")

    src = once(src, "LAST_STAGE = 12", '''# Separate durable jobs. Negative stages never advance the consultation campaign.
# One helpful message per user; the dates are offset from joining or backfill.
USEFUL_MAILING_TEXTS = {
    -1: (
        "📩 <b>Получили извещение из суда?</b>\\n\\n"
        "Судебное письмо хранится на почте всего <b>7 дней</b>. "
        "Заберите его вовремя — иначе можно пропустить срок отмены приказа."
    ),
    -2: (
        "📮 <b>Отправили возражения в суд?</b>\\n\\n"
        "<b>Сохраните почтовую квитанцию и трек-номер.</b> "
        "Они помогут доказать, что вы отправили документы вовремя."
    ),
    -3: (
        "⏳ <b>Получили судебный приказ?</b>\\n\\n"
        "У вас есть <b>10 дней с момента получения</b>, чтобы подать возражения.\\n\\n"
        "Не откладывайте — иначе отменить приказ будет сложнее."
    ),
}
USEFUL_MAILING_DAYS = {-1: 3, -2: 10, -3: 17}

LAST_STAGE = 12''', "texts and schedule")

    src = once(src,
        "    if stage > LAST_STAGE or state.reminders_disabled or state.excluded_sales or not state.participating:\n"
        "        return None",
        "    if (\n"
        "        (stage < 1 and stage not in USEFUL_MAILING_TEXTS)\n"
        "        or stage > LAST_STAGE\n"
        "        or state.reminders_disabled\n"
        "        or state.excluded_sales\n"
        "        or not state.participating\n"
        "    ):\n"
        "        return None", "stage guard")

    src = once(src,
        "    return job\n\n\nasync def ensure_mailing_started(",
        '''    return job


async def _schedule_useful_jobs(
    session: AsyncSession, state: MailingState, anchor: datetime
) -> None:
    for stage, days in USEFUL_MAILING_DAYS.items():
        await _schedule_job(session, state, stage, anchor + timedelta(days=days))


async def ensure_mailing_started(''', "useful job scheduling helper")

    src = once(src,
        "    await _schedule_job(session, state, 1, due_for_stage(1, started_at))\n"
        "    await session.commit()",
        "    await _schedule_job(session, state, 1, due_for_stage(1, started_at))\n"
        "    await _schedule_useful_jobs(session, state, started_at)\n"
        "    await session.commit()", "new subscribers")

    src = once(src,
        "    return state\n\n\nasync def cancel_future_jobs(",
        '''    return state


async def backfill_useful_mailings(session: AsyncSession, *, limit: int = 100) -> int:
    """Schedule tips for older subscribers without releasing old overdue messages.

    Existing non-eligible users and anyone with the first tip already recorded
    are excluded. A bounded batch is processed each worker cycle.
    """
    has_first_tip = select(MailingJob.id).where(
        MailingJob.user_id == MailingState.user_id,
        MailingJob.stage == -1,
    ).exists()
    states = list((await session.execute(
        select(MailingState)
        .where(
            MailingState.participating.is_(True),
            MailingState.reminders_disabled.is_(False),
            MailingState.excluded_sales.is_(False),
            MailingState.consultation_completed.is_(False),
            ~has_first_tip,
        )
        .order_by(MailingState.id)
        .limit(limit)
    )).scalars())
    if not states:
        return 0
    anchor = datetime.utcnow()
    for state in states:
        await _schedule_useful_jobs(session, state, anchor)
    await session.commit()
    logger.info("Scheduled useful mailings for %s existing subscribers", len(states))
    return len(states)


async def cancel_future_jobs(''', "existing subscriber backfill")

    src = once(src,
        "        telegram_markup = automatic_mailing_menu(allow_disable=job.stage >= 2)\n"
        "        max_markup = max_keyboards.automatic_mailing_menu(allow_disable=job.stage >= 2)\n"
        "        sent = await _send_user_message(\n"
        "            settings,\n"
        "            bot,\n"
        "            user,\n"
        "            stage_text(job.stage),\n",
        "        if job.stage in USEFUL_MAILING_TEXTS:\n"
        "            telegram_markup = useful_mailing_menu()\n"
        "            max_markup = max_keyboards.useful_mailing_menu()\n"
        "            message_text = USEFUL_MAILING_TEXTS[job.stage]\n"
        "        else:\n"
        "            telegram_markup = automatic_mailing_menu(allow_disable=job.stage >= 2)\n"
        "            max_markup = max_keyboards.automatic_mailing_menu(allow_disable=job.stage >= 2)\n"
        "            message_text = stage_text(job.stage)\n"
        "        sent = await _send_user_message(\n"
        "            settings,\n"
        "            bot,\n"
        "            user,\n"
        "            message_text,\n", "delivery format")

    src = once(src,
        '''    state.last_sent_stage = job.stage
    state.last_sent_at = sent_at
    state.next_stage = job.stage + 1
    await _schedule_job(session, state, state.next_stage, due_for_stage(state.next_stage, sent_at))
    await session.commit()
    case = await _case_for_user(session, user)
    await record_action(
        session,
        settings,
        user,
        case,
        f"message:{job.stage}",
        "mailing_message_sent",
        f"Клиенту отправлено сообщение регулярной рассылки №{job.stage}.\\n\\n"
        f"Текст сообщения:\\n{plain_message_text(stage_text(job.stage))}",
        execute_immediately=False,
    )
    logger.info("Система рассылок: отправлено сообщение №%s user_id=%s", job.stage, user.id)''',
        '''    is_useful = job.stage in USEFUL_MAILING_TEXTS
    if not is_useful:
        # Tip jobs must never alter the saved consultation campaign position.
        state.last_sent_stage = job.stage
        state.last_sent_at = sent_at
        state.next_stage = job.stage + 1
        await _schedule_job(session, state, state.next_stage, due_for_stage(state.next_stage, sent_at))
    await session.commit()
    case = await _case_for_user(session, user)
    action_key = f"useful-message:{-job.stage}" if is_useful else f"message:{job.stage}"
    message_text = USEFUL_MAILING_TEXTS[job.stage] if is_useful else stage_text(job.stage)
    description = (
        f"полезной рассылки №{-job.stage}" if is_useful
        else f"регулярной рассылки №{job.stage}"
    )
    await record_action(
        session,
        settings,
        user,
        case,
        action_key,
        "mailing_message_sent",
        f"Клиенту отправлено сообщение {description}.\\n\\n"
        f"Текст сообщения:\\n{plain_message_text(message_text)}",
        execute_immediately=False,
    )
    logger.info("Система рассылок: отправлено сообщение %s user_id=%s", action_key, user.id)''', "delivery completion and CRM note")

    src = once(src,
        "            async with SessionLocal() as session:\n"
        "                await recover_uncertain_jobs(session)\n",
        "            async with SessionLocal() as session:\n"
        "                await backfill_useful_mailings(session)\n"
        "                await recover_uncertain_jobs(session)\n", "worker backfill")
    return src


def patch_telegram_keyboard(src: str) -> str:
    return once(src,
        "    return InlineKeyboardMarkup(inline_keyboard=rows)\n\n\ndef main_menu()",
        '''    return InlineKeyboardMarkup(inline_keyboard=rows)


def useful_mailing_menu() -> InlineKeyboardMarkup:
    # No consultation CTA in informational messages; allow unsubscribe.
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Не присылать напоминания", "mailing:disable")],
    ])


def main_menu()''', "Telegram useful keyboard")


def patch_max_keyboard(src: str) -> str:
    return once(src,
        "    return rows\n\n\ndef main_menu()",
        '''    return rows


def useful_mailing_menu() -> MaxKeyboard:
    return [[btn("Не присылать напоминания", "mailing:disable")]]


def main_menu()''', "MAX useful keyboard")


def patch_tests(src: str) -> str:
    src = once(src,
        '''    assert len(jobs) == 1
    assert jobs[0].stage == 1
    assert jobs[0].due_at == started + timedelta(days=7)''',
        '''    assert {job.stage for job in jobs} == {-3, -2, -1, 1}
    due = {job.stage: job.due_at for job in jobs}
    assert due[1] == started + timedelta(days=7)
    assert due[-1] == started + timedelta(days=3)
    assert due[-2] == started + timedelta(days=10)
    assert due[-3] == started + timedelta(days=17)''', "existing idempotent start test")
    src = once(src,
        "select(MailingJob).order_by(MailingJob.stage)",
        "select(MailingJob).where(MailingJob.stage > 0).order_by(MailingJob.stage)", "existing consultation stage test")

    src += '''

@pytest.mark.asyncio
async def test_useful_delivery_preserves_consultation_schedule(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    sender = AsyncMock(return_value=True)
    note = AsyncMock(return_value=True)
    monkeypatch.setattr(mailings, "_send_user_message", sender)
    monkeypatch.setattr(mailings, "record_action", note)
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(
            session, user, started_at=datetime.utcnow() - timedelta(days=4)
        )
        tip_job_id = await session.scalar(select(MailingJob.id).where(MailingJob.stage == -1))

    await mailings._deliver_job(settings, None, tip_job_id)

    async with mailing_db() as session:
        state = await session.scalar(select(MailingState))
        tip = await session.get(MailingJob, tip_job_id)
        stages = set((await session.execute(select(MailingJob.stage))).scalars())
    assert tip.status == "sent"
    assert state.next_stage == 1
    assert state.last_sent_stage is None
    assert state.last_sent_at is None
    assert stages == {-3, -2, -1, 1}
    assert sender.await_args.args[3] == mailings.USEFUL_MAILING_TEXTS[-1]
    buttons = sender.await_args.kwargs["telegram_markup"].inline_keyboard
    assert [button.callback_data for row in buttons for button in row] == ["mailing:disable"]
    assert "Получить консультацию" not in sender.await_args.args[3]
    assert note.await_args.args[4] == "useful-message:1"
    assert "Клиенту отправлено сообщение полезной рассылки №1" in note.await_args.args[6]


@pytest.mark.asyncio
async def test_backfill_is_delayed_once_and_skips_disabled_users(mailing_db) -> None:
    async with mailing_db() as session:
        active = await _user(session, 1)
        disabled = await _user(session, 2)
        old = datetime.utcnow() - timedelta(days=100)
        session.add(MailingState(user_id=active.id, started_at=old, next_stage=3))
        session.add(MailingState(
            user_id=disabled.id, started_at=old, participating=False,
            reminders_disabled=True,
        ))
        await session.commit()
        now_before = datetime.utcnow()
        assert await mailings.backfill_useful_mailings(session) == 1
        assert await mailings.backfill_useful_mailings(session) == 0
        jobs = list((await session.execute(select(MailingJob))).scalars())
    assert len(jobs) == 3
    assert {job.stage for job in jobs} == {-1, -2, -3}
    assert {job.user_id for job in jobs} == {active.id}
    assert all(job.status == "pending" for job in jobs)
    assert min(job.due_at for job in jobs) >= now_before + timedelta(days=3)


@pytest.mark.asyncio
async def test_confirmed_uncertain_tip_does_not_advance_campaign(mailing_db, monkeypatch) -> None:
    settings = replace(get_settings(), amocrm_enabled=False)
    monkeypatch.setattr(mailings, "record_action", AsyncMock(return_value=True))
    async with mailing_db() as session:
        user = await _user(session)
        await mailings.ensure_mailing_started(session, user)
        job = await session.scalar(select(MailingJob).where(MailingJob.stage == -2))
        job.status = "uncertain"
        await session.commit()
        assert await mailings.resolve_uncertain_job(
            session, settings, job.id, delivered=True
        ) is True
        state = await session.scalar(select(MailingState))
        assert job.status == "sent"
        assert state.next_stage == 1
        assert state.last_sent_stage is None
'''
    return src


def patch_readme(src: str) -> str:
    return once(src, "## amoCRM синхронизация", '''## Полезные автоматические сообщения

В существующую очередь `mailing_jobs` добавляются три самостоятельных сообщения:
через **3, 10 и 17 дней** после первого `/start`. Они не содержат предложения
консультации, только кнопку «Не присылать напоминания», и доставляются через
Telegram/MAX. Каждое сообщение отправляется пользователю не более одного раза.

Для действующих подписчиков задания создаются автоматически при запуске
worker, с отсчётом **от момента установки** (первое через 3 дня); никто не
получит все старые сообщения сразу. Отключившиеся пользователи и исключённые
сделки не включаются. Проверка amoCRM, защита от дублей, uncertain/reconciliation,
запись заметки в amoCRM и отмена будущих заданий работают как для обычной рассылки.
План консультаций из 12 этапов остаётся неизменным.

Тексты и интервалы находятся в `app/services/automatic_mailings.py`:
`USEFUL_MAILING_TEXTS` / `USEFUL_MAILING_DAYS` (этапы −1, −2, −3).

## amoCRM синхронизация''', "README feature docs")


PATCHES = {
    "app/services/automatic_mailings.py": patch_mailings,
    "app/keyboards/common.py": patch_telegram_keyboard,
    "app/adapters/max/keyboards.py": patch_max_keyboard,
    "tests/test_automatic_mailings.py": patch_tests,
    "README.md": patch_readme,
}


def load_source(path: Path):
    raw = path.read_bytes()
    # Only UTF-8 text; preserve original CRLF vs LF when writing back.
    text = raw.decode("utf-8-sig")
    bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return text.replace("\r\n", "\n"), newline, bom


def main() -> int:
    parser = argparse.ArgumentParser(description="Install useful mailing jobs into the local bot checkout")
    parser.add_argument("--check", action="store_true", help="Only check whether the patch applies")
    args = parser.parse_args()
    root = Path.cwd()
    for relative in TARGETS:
        if not (root / relative).is_file():
            print(f"ОШИБКА: файл {relative} не найден. Запустите команду из корня prikaz-cancel-bot.", file=sys.stderr)
            return 2

    originals = {}
    outputs = {}
    try:
        for relative, patcher in PATCHES.items():
            path = root / relative
            source, newline, bom = load_source(path)
            originals[relative] = path.read_bytes()
            if relative == "app/services/automatic_mailings.py" and MARKER in source:
                required = {
                    "app/services/automatic_mailings.py": (
                        "async def backfill_useful_mailings(", "useful_mailing_menu()",
                        "await backfill_useful_mailings(session)",
                    ),
                    "app/keyboards/common.py": ("def useful_mailing_menu()",),
                    "app/adapters/max/keyboards.py": ("def useful_mailing_menu()",),
                    "tests/test_automatic_mailings.py": (
                        "test_useful_delivery_preserves_consultation_schedule",),
                    "README.md": ("## Полезные автоматические сообщения",),
                }
                if all(all(token in load_source(root / filename)[0] for token in tokens)
                       for filename, tokens in required.items()):
                    print("Полезные рассылки уже интегрированы. Повторное применение не требуется.")
                    return 0
                raise ValueError("найдена частично установленная версия; восстановите файлы из резервной копии")
            patched = patcher(source)
            output = patched.replace("\n", newline).encode("utf-8")
            outputs[relative] = (b"\xef\xbb\xbf" if bom else b"") + output
        # Python syntax check before touching any file.
        for relative in outputs:
            if relative.endswith(".py"):
                compile(outputs[relative].decode("utf-8-sig"), relative, "exec")
    except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        print("Ни один файл не изменён. Возможно, проект уже отличается от проверенной версии GitHub.", file=sys.stderr)
        return 1

    if args.check:
        print("Проверено: все изменения подходят текущей версии, Python-синтаксис корректен.")
        return 0

    suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_parent = root / ".git" if (root / ".git").is_dir() else root
    backup_root = backup_parent / "useful-mailings-backup" / suffix
    try:
        for relative, raw in originals.items():
            dest = backup_root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        updated = []
        for relative, raw in outputs.items():
            dest = root / relative
            with tempfile.NamedTemporaryFile(mode="wb", dir=str(dest.parent), delete=False) as temp:
                temp.write(raw)
                temp_path = Path(temp.name)
            try:
                os.replace(temp_path, dest)
            finally:
                temp_path.unlink(missing_ok=True)
            updated.append(relative)
    except Exception as exc:
        for relative in locals().get("updated", []):
            (root / relative).write_bytes(originals[relative])
        print(f"ОШИБКА записи: {exc}. Изменённые файлы восстановлены из копии.", file=sys.stderr)
        return 1

    print("УСПЕШНО: полезные рассылки интегрированы. Изменено файлов:", len(outputs))
    for relative in outputs:
        print("  ", relative)
    print("Резервная копия:", backup_root)
    print("Новые клиенты: советы на 3-й, 10-й и 17-й день после /start.")
    print("Существующие активные клиенты: на 3-й, 10-й и 17-й день после запуска обновлённого бота.")
    print("Существующий график консультаций не изменён.")
    print("Проверка тестов: python -m pytest tests/test_automatic_mailings.py -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
