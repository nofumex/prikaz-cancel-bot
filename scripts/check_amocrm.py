from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Case, User
from app.services.amocrm import PIPELINE_STATUSES, get_amocrm_service


async def _check_file_upload(service, lead_id: int | None = None) -> bool:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "amocrm_upload_check.txt"
        path.write_text("amoCRM file upload check", encoding="utf-8")
        file_uuid, error = await service.upload_file_to_drive(path)
        if error or not file_uuid:
            print("Token cannot upload files to amoCRM")
            print(f"Files API error: {error or 'empty file uuid'}")
            return False
        print(f"Files API upload OK: file_uuid={file_uuid}")
        if lead_id:
            linked, link_error = await service.link_file_to_lead(lead_id, file_uuid)
            if not linked:
                print("Token cannot upload files to amoCRM")
                print(f"Files API link error: {link_error or 'unknown link error'}")
                return False
            print(f"Files API attach OK: lead_id={lead_id}, file_uuid={file_uuid}")
        else:
            print("Files API attach skipped: use --create-test-lead or --attach-test-file-to-lead LEAD_ID")
        return True


async def main(create_test_lead: bool, check_file_upload: bool, attach_test_file_to_lead: int | None, list_lead_files: int | None, attach_local_file_to_lead: list[str] | None) -> int:
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

    test_lead_id = attach_test_file_to_lead

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
            test_lead_id = case.amocrm_lead_id or case.amo_lead_id
            print(f"Test lead created: case_id={case.id}, lead_id={test_lead_id}")

    if check_file_upload:
        upload_ok = await _check_file_upload(service, int(test_lead_id) if test_lead_id else None)
        if not upload_ok:
            return 4

    if list_lead_files:
        files, error = await service.list_lead_files(int(list_lead_files))
        if error:
            print(f"Lead files ERROR: {error}")
            return 5
        print(f"Lead files for lead_id={list_lead_files}: {len(files)}")
        for item in files:
            file_uuid = item.get("file_uuid") or item.get("uuid") or item.get("id") or ""
            name = item.get("name") or item.get("file_name") or item.get("filename") or ""
            print(f"- uuid={file_uuid} name={name} raw={item}")

    if attach_local_file_to_lead:
        lead_id = int(attach_local_file_to_lead[0])
        file_path = Path(attach_local_file_to_lead[1])
        caption = attach_local_file_to_lead[2]
        case = Case(id=0, user_id=0, amocrm_lead_id=lead_id, amo_lead_id=lead_id)
        ok = await service.attach_file_to_lead(case, file_path, caption)
        if not ok:
            print(f"Attach local file ERROR: lead_id={lead_id}, path={file_path}")
            return 6
        files, error = await service.list_lead_files(lead_id)
        print(f"Attach local file OK: lead_id={lead_id}, files_now={len(files) if not error else 'unknown'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-test-lead", action="store_true")
    parser.add_argument("--skip-file-upload-check", action="store_true")
    parser.add_argument("--attach-test-file-to-lead", type=int, default=None)
    parser.add_argument("--list-lead-files", type=int, default=None, metavar="LEAD_ID")
    parser.add_argument("--attach-local-file-to-lead", nargs=3, metavar=("LEAD_ID", "PATH", "CAPTION"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.create_test_lead, not args.skip_file_upload_check, args.attach_test_file_to_lead, args.list_lead_files, args.attach_local_file_to_lead)))
