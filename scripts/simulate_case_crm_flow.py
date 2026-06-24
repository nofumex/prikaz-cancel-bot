from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Case, User
from app.services.amocrm import get_amocrm_service
from app.services.legal_data import legal_deadline_from_received
from sqlalchemy import select


async def main() -> int:
    settings = get_settings()
    await init_db()
    service = get_amocrm_service(settings)
    if not service.is_enabled():
        print("amoCRM disabled by env; cannot simulate live flow.")
        return 2

    async with SessionLocal() as session:
        existing = await session.execute(select(User).where(User.platform == "telegram", User.platform_user_id == "simulate-crm-flow"))
        user = existing.scalar_one_or_none()
        if not user:
            user = User(platform="telegram", platform_user_id="simulate-crm-flow", username="sim_user", telegram_username="sim_user")
            session.add(user)
            await session.flush()
        received = date(2026, 6, 19)
        case = Case(
            user_id=user.id,
            platform="telegram",
            status="processing",
            received_date=received,
            deadline_date=legal_deadline_from_received(received),
            extracted_json=json.dumps(
                {
                    "court_name": "судебный участок № 5 города Ессентуки",
                    "debtor_full_name": "Бельский Владимир Геннадьевич",
                    "creditor_name": "АО «Почта Банк»",
                    "case_number": "2-146-09-434/2021",
                    "uid": "26MS0031-01-2021-000169-72",
                    "order_date": "18.01.2021",
                    "debt_amount": "78 472 руб. 87 коп.",
                    "state_duty": "1 277 руб. 00 коп.",
                    "total_amount": "79 749 руб. 87 коп.",
                },
                ensure_ascii=False,
            ),
        )
        session.add(case)
        await session.commit()
        await session.refresh(case)

        events = [
            ("user_started_bot", {}),
            ("order_photo_uploaded", {"note": "sim order photo"}),
            ("received_date_entered", {"received_date": "19.06.2026", "deadline": "29.06.2026"}),
            ("ocr_completed", {"note": "sim ocr completed"}),
            ("case_data_confirmed", {"note": "sim confirmed"}),
            ("preview_generated", {"note": "sim preview"}),
            ("payment_created", {"payment": "sim-payment-label"}),
            ("payment_paid", {"note": "sim paid"}),
            ("documents_delivered", {"note": "sim delivered"}),
        ]
        for event, payload in events:
            await service.sync_case_event(session, case, user, event, payload)
            print(f"event={event} status={case.amocrm_status_name} status_id={case.amocrm_status_id} lead={case.amocrm_lead_id or case.amo_lead_id}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
