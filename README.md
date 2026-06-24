# prikaz-cancel-bot

Telegram-бот для подготовки **возражений относительно исполнения судебного приказа** с оплатой, безопасным PDF-предпросмотром и синхронизацией в amoCRM.

## Что генерирует бот

Для каждой заявки создаются:

- полный DOCX;
- полный PDF (LibreOffice);
- безопасный preview PDF (PyMuPDF — каждая вторая строка скрыта);
- инструкция DOCX по подаче в суд.

До оплаты пользователь получает **только preview PDF**. После оплаты — полный DOCX, PDF и инструкцию.

## Зависимости

- Python 3.11+
- **LibreOffice** (`soffice`) — конвертация DOCX → PDF
- **PyMuPDF** (`fitz`) — preview PDF и QA
- **holidays** — расчёт процессуального срока с переносом выходных/праздников РФ

```powershell
python -m pip install -r requirements.txt
```

## Запуск

```powershell
python -m app.main
```

## Переменные окружения

Скопируйте `.env.example` в `.env`.

### OpenAI

- `VISION_MODEL`, `TEXT_MODEL` — модели OCR и нормализации ФИО
- `OPENAI_INPUT_PRICE_PER_1M`, `OPENAI_CACHED_INPUT_PRICE_PER_1M`, `OPENAI_OUTPUT_PRICE_PER_1M` — учёт расходов

### PDF preview

- `ENABLE_PDF_PREVIEW=true`
- `REQUIRE_PDF_PREVIEW_FOR_PAYMENT=true` — без preview PDF платёж не создаётся
- `ALLOW_DEV_DOCX_PREVIEW=false` — dev-only DOCX с `▒`, не для production

### Оплата

- `YOOMONEY_RECEIVER`, `YOOMONEY_NOTIFICATION_SECRET`, `PAYMENT_PUBLIC_BASE_URL`
- В админке: переключатель «Оплата ВКЛ/ВЫКЛ» для тестов

Webhook: `POST https://ваш-домен/payments/yoomoney`

### amoCRM

- `AMOCRM_ENABLED=true`
- `AMOCRM_BASE_URL`, `AMOCRM_ACCESS_TOKEN`
- `AMOCRM_PIPELINE_NAME=Судебный приказ`
- `AMOCRM_AUTO_CREATE_PIPELINE=false` — не создавать чужие воронки
- `AMOCRM_AUTO_CREATE_STATUSES=true` — создать недостающие этапы в своей воронке

Этапы воронки:

1. Подписался на бота
2. Отправил фотографию приказа
3. Сформирован предпросмотр
4. Ожидает оплату
5. Оплатил
6. Нужна проверка
7. Связался с менеджером
8. Отказ / не оплатил

## Document QA

Перед созданием платежа проверяется:

- наличие DOCX, PDF, preview PDF, инструкции;
- стоп-лист (дательный падеж ФИО, старый заголовок, `▒`, плейсхолдеры);
- заполненность полей;
- причина восстановления срока, если срок пропущен.

При ошибке QA: статус `needs_review`, админ получает уведомление.

## OpenAI usage

Расходы пишутся в таблицу `openai_usages`. В админке **📊 Статистика**:

- токены и доллары;
- средний расход на генерацию;
- оценка генераций на $10.

## Проверки

```powershell
python -m py_compile (Get-ChildItem -Recurse -Filter *.py | ForEach-Object { $_.FullName })
pytest
python scripts/smoke_test.py
```

Smoke-тест создаёт тестовый комплект документов для дела Бельского в `storage/documents/case_<id>/`.

## Production readiness

Перед включением оплаты:

1. `ENABLE_PDF_PREVIEW=true`, `REQUIRE_PDF_PREVIEW_FOR_PAYMENT=true`
2. LibreOffice и PyMuPDF установлены на сервере
3. `ALLOW_DEV_DOCX_PREVIEW=false`
4. Document QA проходит на тестовой заявке
5. amoCRM токен и воронка «Судебный приказ» настроены

## Тестовый сценарий в боте

1. `/start` → «Подготовить заявление»
2. Фото приказа → дата/конверт
3. Проверка карточки (ФИО в именительном падеже)
4. «Готовить документы» → preview PDF → оплата
5. После оплаты — полный комплект
