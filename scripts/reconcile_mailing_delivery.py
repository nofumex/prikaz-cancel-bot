from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, close_db, init_db
from app.models import CrmDealNotification, MailingJob
from app.services.automatic_mailings import resolve_uncertain_job
from app.services.crm_mailing_polling import resolve_uncertain_notification


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or reconcile uncertain mailing deliveries")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--job-id", type=int)
    target.add_argument("--notification-id", type=int)
    parser.add_argument(
        "--delivered",
        choices=("yes", "no"),
        help="yes: finalize as delivered; no: safely return to pending",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    await init_db()
    settings = get_settings()
    try:
        async with SessionLocal() as session:
            if args.job_id is not None:
                if args.delivered is None:
                    raise SystemExit("--delivered is required with --job-id")
                changed = await resolve_uncertain_job(
                    session, settings, args.job_id, delivered=args.delivered == "yes"
                )
                print("updated" if changed else "job is not uncertain or was not found")
                return 0 if changed else 1
            if args.notification_id is not None:
                if args.delivered is None:
                    raise SystemExit("--delivered is required with --notification-id")
                changed = await resolve_uncertain_notification(
                    session,
                    settings,
                    args.notification_id,
                    delivered=args.delivered == "yes",
                )
                print("updated" if changed else "notification is not uncertain or was not found")
                return 0 if changed else 1

            jobs = list(
                (await session.execute(
                    select(MailingJob).where(MailingJob.status == "uncertain").order_by(MailingJob.id)
                )).scalars()
            )
            notifications = list(
                (await session.execute(
                    select(CrmDealNotification)
                    .where(CrmDealNotification.status == "uncertain")
                    .order_by(CrmDealNotification.id)
                )).scalars()
            )
            for job in jobs:
                print(f"job id={job.id} user={job.user_id} stage={job.stage} claimed_at={job.claimed_at}")
            for item in notifications:
                print(
                    f"notification id={item.id} deal={item.amocrm_deal_id} "
                    f"type={item.notification_type} claimed_at={item.claimed_at}"
                )
            if not jobs and not notifications:
                print("no uncertain deliveries")
            return 0
    finally:
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
