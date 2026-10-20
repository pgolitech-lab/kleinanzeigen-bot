# webapp — статус (2026-10-20)

Редизайн MA (гибрид A + «Разбор» из B + термометр/чипы из C) — готов на всех экранах, ветка `opt/webapp` поверх main `35b7110`.

## Что внутри
- Каркас: тема Telegram, раскладки phone/flex/split2/split3, таб-бар Входящие · Клиенты · Продажи · Склад · Ещё.
- Экраны: Входящие, тред (отправка в 3 тапа, чипы цены), Разбор (+ Flex Window), Клиенты/клиент, Продажи, Найденные продажи, Склад (по `stock.md`), Аккаунты, Обзор, Настройки, Рынок.
- Хотфиксы QA: U-01, B-01..B-04, B-09, U-02, U-03, U-04/S-03, U-06, U-12, U-14, U-19, U-20/B-05, S-01, S-04, S-05.
- Скриншоты: `docs/optimization/ui/before/` и `after/` (6 размеров, моки); концепции — `ui/concepts/index.html`.

## Проверено
- headless: сценарии `/send` (6 исходов), языковой guard (кириллица, сбой ИИ-проверки → только по галочке), Разбор, листы; 6 размеров без горизонтальной прокрутки и JS-ошибок.
- reviewer и silent-failure-hunter — по одному проходу, находки исправлены.
- pytest: 762 passed (по ревьюеру; фронт тестами не покрыт).

## Нужно от api (фронт работает и без этого, но проще)
- `/pipeline` на тред: `client_lang`, `offer_eur`, `our_last_eur`, `tags`, `is_late`, `from_app`, `maybe_same`; `summary {waiting, waiting_client, sold_today_count, sold_today_eur, detected_pending}`.
- `/threads/{id}/open`: числа сделки в review (`offer/our_last/floor/ad_price_eur`), досье клиента, `lessons`; `is_late`/`from_app` на событиях.
- `/send` и `/compose`: `kind`, `mode`, для `/compose` ещё `status`.
- `/threads/{id}/sold`: поле `qty` (продажа нескольких штук).
- `/stock/receipt/parse {text}` → позиции прихода (сейчас разбор строки на фронте).
- Отложенная отправка с отменой 5 с (для свайпа в Разборе) и `/variants` для чипов.
- `/lock/takeover` (кнопку «Забрать» убрал — сервер чужой лок не снимает).
