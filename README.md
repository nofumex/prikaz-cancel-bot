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

## amoCRM синхронизация

Бот использует воронку `Судебный приказ`.
Недостающие этапы создаются (если разрешено), старые/лишние этапы не удаляются автоматически.

Этапы:

1. Подписался на бота
2. Отправил фотографию приказа
3. Ввел дату получения
4. Данные распознаны
5. Сформирован предпросмотр
6. Ожидает оплату
7. Оплатил
8. Получил документы
9. Нужна проверка
10. Связался с менеджером
11. Отказ / не оплатил

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
