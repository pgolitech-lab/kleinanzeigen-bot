# Бот → тонкий нотификатор; вся работа в MA/веб

**Дата:** 2026-06-23
**Статус:** design (на ревью)

## Цель и мотивация

Сделать ставку на сервер: единственное место работы оператора — Mini App / веб-морда (:8080). Telegram-бот превращается из полноценного интерактивного UI (~2400 LOC) в **тонкий исходящий нотификатор**: сигналит о событиях, у каждого уведомления — кнопка «Открыть» в нужный экран MA.

Мотивация:
- Весь класс багов с ботом за сегодня (DNS-гонка long-polling, «нет кнопок», пропажа persistent-клавиатуры, очистка чата vs лимит 48ч) порождён тем, что бот — интерактивный stateful UI поверх Telegram. Убираем интерактив → проблемы исчезают как класс.
- MA уже умеет всё (review/send/skip/sold/edit-ru/edit-de/price/instruction/compose/autopilot/suggest — `web/api_ma.py`). Дублирование логики в боте больше не нужно.

## Архитектурные решения (подтверждены с пользователем)

1. **Роль бота:** уведомление + одна inline `web_app`-кнопка «Открыть» на событие. Никаких callback'ов/клавиатур/очистки чата.
2. **Polling удаляется полностью.** Бот чисто исходящий (HTTP через urllib). Нет PTB `Application`, нет потока `tg-polling`, нет handlers.
3. **Все события** видны в боте (лента). Фильтрации нет.
4. **Чистый рерайт** `telegram_bot.py` + удаление зависимости `python-telegram-bot`.
5. Вход в MA — нативная **Menu Button** (`setChatMenuButton` с `web_app`), ставится один раз. Заменяет persistent reply-клавиатуру.

## Новая структура `modules/telegram_bot.py` (~250-350 LOC)

Только исходящие функции на `urllib` (переиспользуем существующий `_http_post` / `_http_post_single` с DM-fanout и `_truncate_html_safe`):

- `_http_post(method, payload)` — отправка + DM-fanout по `telegram_operator_dm_ids` (как сейчас). **Без** трекинга сообщений (`_track_msg`/`_CHAT_TRACKED_MSGS`/persist удаляются — лента не чистится).
- `notify(text, open_target=None)` — единый хелпер: шлёт сообщение + (если задан target) inline `web_app`-кнопку «Открыть», deep-link через `_ma_deep_link(target)`.
- `set_menu_button()` — `setChatMenuButton` (web_app → MA pipeline) для каждого operator-DM. Вызывается при старте сервиса (из `main.py`/`scheduler.start`).
- Тонкие нотификаторы поверх `notify(...)`, сохраняют сигнатуры (чтобы caller'ы менялись минимально):
  - `send_for_review(msg_id)` → «🆕 Новое обращение …» + Открыть → `review_<msg_id>`
  - `send_autopilot_start_notification` / `send_autopilot_progress` / `send_autopilot_stop_notification` → текст + Открыть → `thread_<id>`
  - `send_reminder_offer(...)` → «⏰ Пора напомнить …» + Открыть → `thread_<id>`
  - НОВОЕ: уведомление «клиент ответил в активном треде» (заменяет `broadcast_thread_state`) → Открыть → `thread_<id>`
  - Дневная сводка / ошибки-дайджест → текст + Открыть → дашборд (web `/` или MA).

Удаляются: `_format_pipeline_messages`, `_pipeline_thread_card`, `_review_keyboard`, `_on_callback`, `_on_text`, `_on_pipeline`, `_on_menu`, `_on_card`, `_on_*` команды, `build_application`, `run_polling*`, `refresh_pipeline_for_active_chats`, `broadcast_after_external_action`, `broadcast_thread_state`, `_CHAT_TRACKED_MSGS`/`_track_msg`/`_delete_range`/`_remember_pipeline_msgs`/`_save_tracking`/`_load_tracking`, `_persistent_menu_keyboard`, autopilot/edit/compose input-flow.

## Локи / concurrency → `modules/operator_lock.py`

Сейчас `scheduler`/`api_ma` зовут у бота: `_acquire_lock`/`_release_lock`/`_check_lock`, `thread_is_busy`/`mark_thread_busy`/`clear_thread_busy`. Это про concurrency (защита от одновременной работы), теперь чисто MA-side.

**Решение:** перенести эту логику в `modules/operator_lock.py` (законный дом — его уже использует MA), экспортировать те же функции. Обновить вызовы в `scheduler.py`/`web/api_ma.py` с `telegram_bot.X` → `operator_lock.X`. Бот к локам больше не причастен.

## Обновление вызывающих (`scheduler.py`, `web/api_ma.py`)

- Удалить все вызовы `refresh_pipeline_for_active_chats` (×18), `broadcast_after_external_action` (×10), `broadcast_thread_state` (×2) — синхронизировать нечего.
- Лок-вызовы → `operator_lock.*`.
- Нотификаторы (`send_for_review`, `send_autopilot_*`, `send_reminder_offer`, `_http_post` для сводки/ошибок) остаются, сигнатуры те же.
- `monitor_errors_job`: убрать polling-health (`_check_polling_health`) — polling больше нет. Ошибки-мониторинг оставить (шлёт через `notify`).

## main.py

Убрать `telegram_bot.run_polling_in_thread()`. Добавить однократный `telegram_bot.set_menu_button()` при старте (после bootstrap). Бот теперь не держит фонового процесса — только вызовы-нотификаторы из scheduler-задач и uvicorn (MA backend) живут как раньше.

## Зависимости

Убрать `python-telegram-bot` (и при отсутствии др. потребителей — `httpx`) из `requirements.txt`. Удалить с сервера: `pip uninstall` не обязателен, но из requirements убрать. Проверить, что ничего больше не импортит `telegram`/`httpx`.

## MA / веб

Изменений по сути нет — действия уже реализованы в `api_ma.py`. Проверить, что deep-link каждого типа уведомления (`review_<id>`, `thread_<id>`, дашборд) открывает корректный экран. Если нет отдельного «дашборда» в MA — Menu Button и общие уведомления ведут на pipeline; веб-дашборд `/` доступен в браузере.

## Бэкап и откат

- git tag `pre-bot-notifier-2026-06-23` + снапшот БД в `/home/pg/backups/` до начала.
- Откат: `git revert`/checkout тега + `systemctl restart`.

## Тестирование

- Unit: существующие 111 тестов — починить/удалить те, что про удаляемые handler'ы; добавить тест на `notify()` (формирует корректный payload с web_app-кнопкой) и `_ma_deep_link`.
- Ручная проверка (через scp-скрипты, как в этой сессии): каждый нотификатор реально доставляется в оба DM (API `ok`+`message_id`); Menu Button установлена (`getChatMenuButton`); deep-link открывает нужный экран MA.
- Регрессия: scheduler-задачи (poll_gmail → новое обращение → уведомление; autopilot-стоп; reminder; daily_summary; ошибки) шлют уведомления без ошибок.

## Риски

- **Скрытые потребители удаляемых функций.** Митигировать grep'ом перед удалением (sched/api_ma/web/templates).
- **Лента уведомлений копится** (не чистится). Это сознательно (пользователь хочет видеть все уведки). При желании — ручная «Очистить историю» оператором; либо опционально позже добавить retention-чистку <48ч.
- **Потеря возможностей, которых нет в MA.** Проверка: всё, что делал бот (edit/price/instruction/compose/autopilot-старт), уже есть в `api_ma.py` — потерь нет.

## Вне scope

- Новые фичи MA/веб (дашборд-виджеты и т.п.) — отдельно.
- Retention-чистка ленты бота — опционально, потом.
