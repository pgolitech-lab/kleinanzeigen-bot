# api — новые эндпоинты Mini App (2026-10-20, ветка opt/api)

Все маршруты под `/api/ma/*` и требуют initData. Отдельные функции LLM для них не нужны.

## Отложенная отправка
- `POST /messages/{id}/send {mode?, text?, force?, delay_sec: 1..60}` → `{queued, job_id (= msg_id), send_at, status: "send_queued"}`.
  Без `delay_sec` отправка идёт сразу, как раньше.
- `POST /send/{job_id}/cancel` → `{cancelled, status}` (статус, который был до постановки в очередь). Если письмо уже ушло — 409.
- Пока строка в `send_queued`, ответ 409 «сначала Отменить» получают:
  - на этой строке: правка, перегенерация, варианты, повторная отправка;
  - в этом треде: compose, «Продано», «Пропустить».
- По таймеру — обычная отправка. Если она не прошла, письмо возвращается в черновик, а операторам уходит уведомление в Telegram.
- Очередь хранится в `settings['ma_send_queue']` (JSON) и восстанавливается при старте приложения (lifespan в `web/app.py`).
  Письма, просроченные больше чем на 10 минут, возвращаются в черновики с уведомлением.
- Строку со статусом `approved` очередь ставит как `edited`. Иначе `send_approved_replies` отправил бы её второй раз.

## Разбор
`GET /triage` → `{items: [...], count}`. Каждый элемент:

```
{thread_id, msg_id, account_id, buyer_display_name, ad_title, ad_price, ad_price_eur,
 offered_eur, floor_eur, client_message_ru, last_event_at, wait_hours, deal_summary_ru,
 draft: {msg_id, status, client_text, ru_text} | null, score, reasons[]}
```

В выборку попадают треды, где ход за нами и автопилот не активен. Сортировка по `score`:

| Сигнал | Баллы |
|---|---|
| Ожидание | до 40 |
| Горячесть | ±25 |
| Цена против объявления и минимума | до 40 |
| Есть черновик | +5 |

## Чипы цены
- `POST /messages/{id}/variants {tones?: [hold, minus5, floor, decline]}` → `{variants: [{tone, price_eur, client_text, ru_text, ru_translation, error, cached}]}`.
  - Генерация параллельная, в БД ничего не пишется.
  - Кэш по ключу (msg_id, тон, цена, версия треда).
- Минимум ищется по порядку: `ad_briefs.min_price_eur` → минимум автопилота → цена − `max_discount_percent`. Вариант «−5 %» не опускается ниже минимума.
- `POST /messages/{id}/variants/{tone}/apply` делает выбранный вариант черновиком и возвращает review payload. Если тред изменился — 409.

## Продажи и Обзор (QA «ленивый продажник»)
- `POST /threads/{id}/sold {price_eur?, note?, close_other_threads_for_ad?}` работает на любом треде.
  - Цена ищется по порядку: явная → `deal_brief` → цена объявления. Если ни одной нет — 400.
  - Если последнее письмо уже отвечено, продажа пишется отдельной строкой (`add_manual_sale`) и тред закрывается.
  - Причина: `mark_thread_sold` перевёл бы отправленный ответ в `skipped_sold`. Для db: нужна функция продажи по треду, которая не меняет статус отправленных строк.
- Обзор «сегодня» (письма, ответы, продажи) и итог дня в боте считаются через `db.berlin_day_bounds_utc` / `db.sales_summary(today_berlin)`. Для экрана «Продажи» есть `/sales?period=today`.
- `POST /detected-sales/accept-confident {min_confidence: high|medium}` → `db.accept_detected_bulk` → `{applied, skipped}`.
- В `/detected-sales` добавлены поля `evidence_ru` и `evidence_ru_failed`.

## Быстрые действия
- `POST /threads/{id}/wait {days? | until?}` → `wait_until`: до этого срока напоминаний нет (`reminder_snooze_until`). Без тела работает как раньше.
- `POST /threads/{id}/followup` → `{translated, back_ru, target_lang, ru_text}` (формат как у `/compose-preview`). Отправка — через `/compose`.
- `GET /accounts/{id}/quick-info` и `POST /accounts/{id}/quick-info {city, hours, delivery, payment}` — справка аккаунта, хранится в settings KV.
- `POST /messages/{id}/quick {kind}` → тот же формат, что у followup. В БД ничего не пишет.

## Что нужно от других модулей
- **scheduler:** джоб `telegram_bot.send_evening_summary` — cron 20:00 Europe/Berlin, `max_instances=1`.
- **db:**
  - `pipeline_threads` не учитывает `send_queued`: на время задержки тред пропадает из «Входящих».
  - Нужна продажа по треду без смены статусов (см. выше).
- **llm:** добавить `send_queued` в `claude.FINAL_STATUSES`.
- **webapp:**
  - маршрут `send_<msg_id>`; после этого кнопка «✅ Ответить» в карточке бота переключается с `review_` на `send_` (одна строка);
  - поле `telegram_admin_chat_id` в настройках;
  - показ `today.sent_external`, `related.maybe_same`, 409 с понятным текстом.
