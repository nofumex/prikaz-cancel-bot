from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Case, User
from app.services.amocrm import PIPELINE_STATUSES, get_amocrm_service


async def main(create_test_lead: bool) -> int:
    settings = get_settings()
    required = {
        "AMOCRM_ENABLED": settings.amocrm_enabled,
        "AMOCRM_BASE_URL": bool(settings.amocrm_base_url),
        "AMOCRM_ACCESS_TOKEN": bool(settings.amocrm_access_token),
    }
    if not all(required.values()):
        print("amoCRM config is incomplete:")
        for key, ok in required.items():
            print(f"- {key}: {'set' if ok else 'missing/disabled'}")
        return 2

    await init_db()
    service = get_amocrm_service(settings)
    report = await service.ensure_pipeline_and_statuses()
    if not report.get("pipeline"):
        print("amoCRM ERROR: pipeline not found")
        return 3

    pipeline = report["pipeline"]
    print("amoCRM OK")
    print(f"Pipeline: {pipeline.get('name')}, id={pipeline.get('id')}")
    print("Statuses:")
    for status_name in PIPELINE_STATUSES:
        sid = report.get("statuses", {}).get(status_name)
        print(f"{'[OK]' if sid else '[MISS]'} {status_name}" + (f" id={sid}" if sid else ""))
    print(f"Missing created: {report.get('created', 0)}")
    print("Errors: " + ("none" if not report.get("errors") else ", ".join(report["errors"])))

    if create_test_lead:
        async with SessionLocal() as session:
            user = User(
                platform="telegram",
                platform_user_id=f"crm-check-{int(asyncio.get_running_loop().time())}",
                telegram_id=None,
                username="crm_check",
                telegram_username="crm_check",
                first_name="CRM",
                last_name="Check",
            )
            session.add(user)
            await session.flush()
            case = Case(user_id=user.id, platform="telegram", status="draft")
            session.add(case)
            await session.commit()
            await session.refresh(case)
            await service.sync_case_event(session, case, user, "user_started_bot", {"note": "Test lead from scripts/check_amocrm.py"})
            print(f"Test lead created: case_id={case.id}, lead_id={case.amocrm_lead_id or case.amo_lead_id}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-test-lead", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.create_test_lead)))
