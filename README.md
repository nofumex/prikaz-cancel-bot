# prikaz-cancel-bot

Telegram-бот для подготовки возражений по судебному приказу с:

- OCR/LLM извлечением данных;
- генерацией `DOCX + full PDF + preview PDF + instruction DOCX`;
- document QA перед оплатой;
- синхронизацией этапов в amoCRM.

## Что генерируется по заявке

Для каждого `Case`:

- `storage/documents/case_<id>/statement_<...>.docx`
- `storage/documents/case_<id>/statement_<...>.pdf`
- `storage/documents/case_<id>/preview_statement_<...>.pdf`
- `storage/documents/case_<id>/instruction_<id>.docx`

До оплаты клиент получает только preview PDF.
После оплаты — полный DOCX, полный PDF и инструкцию.

## Установка зависимостей

### Windows

1. Установить LibreOffice: <https://www.libreoffice.org/download/download-libreoffice/>
2. Проверить файл: `C:\Program Files\LibreOffice\program\soffice.exe`
3. Добавить в `PATH`: `C:\Program Files\LibreOffice\program`
4. Проверить:

```cmd
soffice --version
```

5. Установить Python-зависимости:

```cmd
python -m pip install -r requirements.txt
```

6. Проверить PDF-пайплайн:

```cmd
python scripts/smoke_test.py
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y libreoffice libreoffice-writer fonts-dejavu fonts-liberation
python -m pip install -r requirements.txt
python scripts/smoke_test.py
```

Если нужен Times New Roman:

```bash
sudo apt install -y ttf-mscorefonts-installer
```

## Быстрый старт

```powershell
python -m pip install -r requirements.txt
python -m app.main
```

## Проверки

```powershell
python -m py_compile (Get-ChildItem -Recurse -Filter *.py | ForEach-Object { $_.FullName })
pytest
python scripts/smoke_test.py
python scripts/check_amocrm.py
python scripts/simulate_case_crm_flow.py
```

Опционально создать тестовую сделку в CRM:

```powershell
python scripts/check_amocrm.py --create-test-lead
```

## Переменные окружения

Скопируйте `.env.example` в `.env`.

Критично для production:

- `ENABLE_PDF_PREVIEW=true`
- `REQUIRE_PDF_PREVIEW_FOR_PAYMENT=true`
- `ALLOW_DEV_DOCX_PREVIEW=false`
- `AMOCRM_ENABLED=true`
- `AMOCRM_BASE_URL`, `AMOCRM_ACCESS_TOKEN`
- `AMOCRM_PIPELINE_NAME=Судебный приказ`
- `AMOCRM_AUTO_CREATE_STATUSES=true`

## Качество документов

Обычные возражения должны помещаться на 1 страницу.
Генератор использует компактную судебную шапку, A4, Times New Roman, заполненную подпись и автоматическую проверку сумм.
Перед созданием платежа выполняется document QA и visual QA.
Если QA не пройден — платеж не создается, заявка уходит в `needs_review`.

Проверка генерации и visual QA:

```powershell
python scripts/smoke_test.py
```

Сгенерированные файлы лежат в `storage/documents/case_<id>/` и `storage/test_artifacts/belsky_case_manifest.txt`.
Откройте `statement_in_time_<id>.docx` и `.pdf` в Word/LibreOffice или любом PDF-просмотрщике.

## amoCRM синхронизация

Бот использует воронку `Судебный приказ`.
Недостающие этапы создаются (если разрешено), старые/лишние этапы не удаляются автоматически.

Этапы (6):

1. Подписался на бота
2. Отправил приказ (фото приказа прикрепляется к сделке)
3. Ввел дату
4. Оплатил
5. Получил заявление
6. Нужна проверка

После валидации можно вручную удалить старые этапы в amoCRM интерфейсе.

### TODO по кастомным полям amoCRM

Рекомендуется создать отдельные custom fields:

- Telegram ID
- Telegram username
- Источник лида

Сейчас fallback-режим добавляет эти данные в примечания.

## PDF и QA

Перед созданием платежа проверяется:

- наличие full DOCX/full PDF/preview PDF/instruction DOCX;
- стоп-лист токенов;
- обязательные поля кейса.

Если есть проблема с PDF (например, не найден LibreOffice), платеж не создается, заявка уходит в `needs_review`, админу приходит уведомление.

## YooKassa Production Setup

Production payments use the YooKassa API when `YOOKASSA_ENABLED=true`. The old `YOOMONEY_RECEIVER` and `YOOMONEY_NOTIFICATION_SECRET` variables are kept only as a legacy fallback.

- `YOOKASSA_SHOP_ID`: YooKassa shop identifier. For this project the shop id is `1391245`.
- `YOOKASSA_SECRET_KEY`: generate it in the YooKassa personal account in the API/integration settings. Never commit or log it.
- HTTP notification URL: `https://YOUR_DOMAIN/payments/yookassa`.
- Enable events: `payment.succeeded`, `payment.canceled`.
- YooKassa requires an HTTPS public webhook URL.

Safe checks:

```powershell
python scripts/check_yookassa.py
python scripts/check_yookassa.py --create-test-payment
```

Minimal production `.env` for verification:

```env
TG_BOT_TOKEN=...
RUN_TELEGRAM=true

MAX_BOT_TOKEN=...
RUN_MAX=true
MAX_API_BASE_URL=https://platform-api.max.ru
MAX_DEBUG_RAW_UPDATES=false

OPENAI_API_KEY=...

DOCUMENT_PRICE_RUB=990
PAYMENT_PUBLIC_BASE_URL=https://YOUR_DOMAIN
YOOKASSA_ENABLED=true
YOOKASSA_SHOP_ID=1391245
YOOKASSA_SECRET_KEY=...
YOOKASSA_RETURN_URL=https://YOUR_DOMAIN/payments/success
YOOKASSA_WEBHOOK_PATH=/payments/yookassa
YOOKASSA_TEST_MODE=false
YOOKASSA_RECEIPT_ENABLED=true
YOOKASSA_VAT_CODE=1
YOOKASSA_PAYMENT_SUBJECT=service
YOOKASSA_PAYMENT_MODE=full_payment
YOOKASSA_RECEIPT_DESCRIPTION=Подготовка заявления об отмене судебного приказа
YOOKASSA_TEST_CUSTOMER_EMAIL=test@example.com
YOOKASSA_TAX_SYSTEM_CODE=
PAYMENT_WEB_HOST=0.0.0.0
PAYMENT_WEB_PORT=8080

ENABLE_PDF_PREVIEW=true
REQUIRE_PDF_PREVIEW_FOR_PAYMENT=true
ALLOW_DEV_DOCX_PREVIEW=false

AMOCRM_ENABLED=true
AMOCRM_BASE_URL=...
AMOCRM_ACCESS_TOKEN=...
AMOCRM_PIPELINE_NAME=Судебный приказ
CRM_SYNC_BACKGROUND=true
```

amoCRM production stages are exactly five:

1. Подписался на бота
2. Отправил приказ
3. Указал дату
4. Оплатил
5. Получил напоминание (не оплатил)

