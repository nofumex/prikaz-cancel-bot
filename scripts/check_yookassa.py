from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.models import Case, Payment
from app.services.yookassa import YooKassaClient, YooKassaError


async def main() -> int:
    parser = argparse.ArgumentParser(description="Check YooKassa settings and optional test payment creation.")
    parser.add_argument("--create-test-payment", action="store_true", help="Create a minimal YooKassa test payment via API.")
    parser.add_argument("--strict-env", action="store_true", help="Return non-zero when YooKassa env is incomplete.")
    args = parser.parse_args()

    settings = get_settings()
    missing = []
    if not settings.yookassa_enabled:
        missing.append("YOOKASSA_ENABLED=true")
    if not settings.yookassa_shop_id:
        missing.append("YOOKASSA_SHOP_ID")
    if not settings.yookassa_secret_key:
        missing.append("YOOKASSA_SECRET_KEY")
    if not (settings.yookassa_return_url or settings.payment_public_base_url):
        missing.append("YOOKASSA_RETURN_URL or PAYMENT_PUBLIC_BASE_URL")
    if settings.yookassa_receipt_enabled and args.create_test_payment and not settings.yookassa_test_customer_email:
        missing.append("YOOKASSA_TEST_CUSTOMER_EMAIL")
    if missing:
        print("YooKassa env is incomplete:")
        for item in missing:
            print(f"  - {item}")
        if args.create_test_payment or args.strict_env:
            return 1
        print("Dry run only: env check reported missing values, but no live payment was attempted.")
        return 0

    print("YooKassa env OK")
    print(f"  shop_id: {settings.yookassa_shop_id}")
    print(f"  webhook_path: {settings.yookassa_webhook_path}")
    print(f"  test_mode: {settings.yookassa_test_mode}")
    print(f"  receipt_enabled: {settings.yookassa_receipt_enabled}")
    print("  secret_key: ***")

    if not args.create_test_payment:
        print("Dry run only: no payment was created. Use --create-test-payment for a live API check.")
        return 0

    client = YooKassaClient(settings)
    case = Case(id=0, platform="script", platform_user_id="check_yookassa")
    payment = Payment(case_id=0, label="check-yookassa-dry-run", amount=max(1, int(settings.document_price_rub or 1)), provider="yookassa")
    try:
        response = await client.create_payment(case, payment)
    except YooKassaError as exc:
        print(f"YooKassa API check failed: {exc}")
        return 1
    confirmation = response.get("confirmation") if isinstance(response.get("confirmation"), dict) else {}
    print("YooKassa API check OK")
    print(f"  payment_id: {response.get('id')}")
    print(f"  status: {response.get('status')}")
    print(f"  confirmation_url: {confirmation.get('confirmation_url')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
