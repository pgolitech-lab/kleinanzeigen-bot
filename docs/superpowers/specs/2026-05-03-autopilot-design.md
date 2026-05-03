# Autopilot mode

**Дата:** 2026-05-03
**Цель:** дать оператору кнопку «🚀 Полный автопилот» — после подтверждения бот сам ведёт переговоры с клиентом до закрытия сделки или достижения границ. Особенно полезно для долгих торгов с настырными клиентами.

## Что в скоупе

- Кнопка `🚀 Полный автопилот` на review-карточке.
- Confirmation-форма: floor по цене (preselect из `ad_briefs.key_facts.min_acceptable_eur`) + режим уведомлений (Silent / Notify).
- Sonnet auto-reply на каждое новое incoming в треде, без operator review.
- Stop conditions: 20 сообщений, ready_to_buy, wants_contact, threat, manual stop.
- Web search tool (Anthropic native) — Sonnet решает сам когда использовать; промпт ограничивает «только если специфичный вопрос и не уверен».
- Floor-enforcement: hard cap «не уступать ниже floor И ниже того что уже сказали клиенту».
- Pipeline-отображение «автопилот N/20» вместо обычного status.
- Stop-notifications в DM операторов.

## Что НЕ в скоупе

- Time-cap (24h) — отказались, только messages-cap.
- Operator-edit-during-flight — операторская правка автопилотных reply невозможна (только остановить).
- Кросс-thread автопилот — каждый тред отдельно (нельзя «всё на этого клиента в автопилот»).

---

## Пользовательский flow

1. Оператор открывает review-карточку любого треда (через pipeline или `/card N`).
2. Жмёт `🚀 Полный автопилот` (новая кнопка в keyboard).
3. Confirm-card: «введи floor €» + toggle режима. Submit → автопилот active.
4. Бот авто-отвечает на каждое следующее incoming в этом треде до stop-condition.
5. Stop → пинг операторам в DM (текст с причиной + кнопка «Открыть тред»).

## Архитектура

### Поток в `_process_incoming` (scheduler.py)

```
1. Filters (noreply, junk, classifier) — без изменений
2. Извлечь ad_url/ad_id, INSERT incoming row
3. Auto-ack (если account.auto_ack_enabled и first inquiry)
4. Playwright parse_ad
5. Brief generation
6. History/lessons fetch
7. ── НОВОЕ — Autopilot bridge ──
   ap = db.get_thread_autopilot(thread_id)
   if ap and ap.active:
       reply = claude.generate_autopilot_reply(
           ..., floor_eur=ap.floor_price_eur, with_web_search=True,
       )
   else:
       reply = claude.generate_reply(...)  # старое поведение
8. Save reply в DB (ru_client, ru_answer, de_answer, ru_translation, deal_brief_json, similar_buyers_json)
9. ── НОВОЕ — Autopilot dispatch ──
   if not ap or not ap.active:
       telegram_bot.send_for_review(msg_id)  # обычная карточка оператору
       return
   
   # ПРОВЕРКИ stop ПЕРЕД отправкой (counter инкрементим только после успешного SMTP)
   stop_reason = None
   if reply.should_stop:
       stop_reason = reply.stop_reason
   elif (ap.messages_sent + 1) > 20:  # этот reply был бы 21-м — стоп
       stop_reason = "limit"
   
   if stop_reason == "threat":
       # Отправить предупреждение клиенту + остановить
       db.update_message(msg_id, status="approved", is_autopilot_reply=1)
       result = _send_reply(get_message(msg_id))
       if result.kind == "sent" or result.kind == "skipped":  # skipped = mode=disabled
           db.increment_autopilot_messages(thread_id)
       db.stop_thread_autopilot(thread_id, "threat")
       send_autopilot_stop_notification(msg_id, "threat")
       return
   
   if stop_reason:
       # Остановить БЕЗ авто-отправки — оператор сам разрулит
       db.stop_thread_autopilot(thread_id, stop_reason)
       send_for_review(msg_id)  # обычная карточка с full keyboard
       send_autopilot_stop_notification(msg_id, stop_reason)
       return
   
   # Нормальная авто-отправка
   db.update_message(msg_id, status="approved", is_autopilot_reply=1)
   result = _send_reply(get_message(msg_id))
   if result.kind == "sent" or result.kind == "skipped":
       new_count = db.increment_autopilot_messages(thread_id)
       if ap.notify_mode == "notify":
           send_autopilot_progress(msg_id, new_count)
   else:
       # SMTP fail — counter НЕ инкрементим, status='error_send_failed' уже выставлен в _send_reply
       # автопилот остаётся active, retry на следующем inbound (если будет)
       pass
```

### Schema

**`thread_autopilot`** — новая таблица:
```sql
CREATE TABLE IF NOT EXISTS thread_autopilot (
    gmail_thread_id TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    floor_price_eur REAL NOT NULL,
    notify_mode TEXT NOT NULL,   -- 'silent' | 'notify'
    messages_sent INTEGER NOT NULL DEFAULT 0,
    started_by TEXT,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    stop_reason TEXT             -- 'limit' | 'ready_to_buy' | 'wants_contact' | 'threat' | 'manual'
);
```

**`messages`** — новая колонка:
```sql
ALTER TABLE messages ADD COLUMN is_autopilot_reply INTEGER;  -- 1 если row отправлена автопилотом
```

### `claude.generate_autopilot_reply`

Сигнатура (в `modules/claude.py`):
```python
def generate_autopilot_reply(
    de_client_text: str,
    ad_title: str = "", ad_price: str = "", ad_description: str = "",
    seller_name: str = "",
    history: Optional[list] = None,
    brief_text: str = "",
    lessons: Optional[list] = None,
    floor_eur: float = 0,
    last_our_price_eur: Optional[float] = None,  # из deal_brief предыдущего turn-а
    max_tokens: int = 2500,
) -> dict[str, Any]:
    """Sonnet генерит auto-reply с расширенной schema."""
```

Возврат — dict со всеми полями из `generate_reply` плюс:
- `should_stop: bool`
- `stop_reason: str` ("ready_to_buy" / "wants_contact" / "threat" / "")
- `client_pressing_below_floor: bool`
- `used_web_search: bool`

System prompt дополнения (поверх `config.system_prompt()`):
```
=== РЕЖИМ АВТОПИЛОТА ===
Ты ведёшь переговоры самостоятельно. Цель — продать товар.
HARD FLOOR: НЕ ОПУСКАТЬ цену ниже {floor_eur}€. Также НЕ опускать ниже того что уже сказали клиенту в этом треде ({last_our_price_eur or "ещё не было"}).

ОБЯЗАТЕЛЬНО останавливай автопилот (should_stop=true) когда:
- клиент готов купить / просит банк-реквизиты / адрес / встречу → stop_reason="ready_to_buy" или "wants_contact"
- клиент пишет угрозы / агрессивные фразы → stop_reason="threat", в client_answer ответь «Wir betrachten das als Drohung. Bitte unterlassen Sie weitere Nachrichten dieser Art.» (или эквивалент на языке клиента)

В остальных случаях продолжай:
- Просит то чего нет → откажи: «нет в наличии, доступно X»
- Просит фото/видео → «не под рукой, могу позже / приезжайте посмотреть»
- Технический вопрос — отвечай из knowledge; web_search ТОЛЬКО если реально специфика и ты не уверен (мы платим за поиск)
- Не ври про spec-числа которых не знаешь — «уточню при встрече»
- Клиент давит ниже floor → откажи «Это окончательная»
```

Tools: `[{"type": "web_search_20250305"}]` (или актуальная версия). Anthropic-нативный инструмент.

### Telegram UI

**Новая кнопка** в review_keyboard (последний ряд перед `↩ Назад`):
```python
[{"text": "🚀 Полный автопилот", "callback_data": f"apstart:{message_id}"}]
```

**Confirmation flow** (двух-этапный):
1. Click `🚀 Полный автопилот` (callback `apstart:N`) → войти в input mode «введи floor цены €» (preselect значением из `ad_briefs.key_facts.min_acceptable_eur`). Карточка с `❌ Отменить`.
2. Оператор пишет число → `_on_text` ловит (action=`autopilot_floor`) → бот edits карточку: показывает «✅ floor: X€. Выбери режим:» + два callback-кнопки:
   - `apconfirm:silent:N` → активировать с notify_mode=silent
   - `apconfirm:notify:N` → активировать с notify_mode=notify
3. Click любой → `db.start_thread_autopilot(thread_id, floor, notify_mode, started_by=actor)` → broadcast карточка обновляется в режим «🤖 АВТОПИЛОТ АКТИВЕН» с `🛑 Остановить` кнопкой

**Карточка с активным автопилотом** (когда оператор открывает через `/card N`):
- Body имеет жёлтый header-блок:
  ```
  🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨
  🤖 АВТОПИЛОТ АКТИВЕН (N/20)
  floor: X€ · режим: 🔔/🤫
  запущен @actor в HH:MM
  🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨
  ```
- Keyboard заменён на:
  ```
  🛑 Остановить автопилот   ← apstop:N
  ↩ Назад к pipeline
  ```

**Pipeline-карточка** активного треда: status_txt = `автопилот N/20` вместо обычного `ответили/ack/draft`. Marker `🤖` рядом с цветом.

### Stop notifications

Helper `send_autopilot_stop_notification(msg_id, reason)` шлёт через `_http_post` (fanout автоматический):
```
🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨
🛑 АВТОПИЛОТ ОСТАНОВЛЕН · #N
Причина: <emoji + текст>
Сообщений отправлено: M / 20
🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨
[📋 Открыть карточку треда]
```
Reason mapping:
- `limit` → 🛑 «исчерпан лимит 20 сообщений»
- `ready_to_buy` → 🎯 «клиент готов покупать, иди закрывай сделку»
- `wants_contact` → 📞 «просит контакты/реквизиты, передаёт человеку»
- `threat` → ⚠️ «угроза, бот предупредил клиента»
- `manual` → silent (оператор сам остановил)

### Progress notification (notify mode only)
Helper `send_autopilot_progress(msg_id, count)` — короткий текст:
```
🤖 #N · автопилот N/20: <первые 80 chars de_answer>
```

## Edge cases

| Случай | Поведение |
|---|---|
| Включить автопилот когда `send_mode='disabled'` | Активируется, генерит drafts, не шлёт SMTP, status='not_sent_disabled'. Counter инкремент-ится |
| SMTP fail при auto-отправке | row остаётся `error_send_failed`, автопилот active, counter НЕ инкремент. Оператор увидит в pipeline |
| Sonnet API error | Counter не инкремент. Автопилот active, retry на следующем inbound |
| 2+ inbound подряд (race) | scheduler обрабатывает sequentially, каждый = +1 counter |
| should_stop=true с самого первого reply | Стоп немедленно (corner case, корректное поведение) |
| Пытаются включить автопилот на треде где он уже active | Confirmation card покажет «уже активен N/20», предложит остановить |

## План тестирования

1. **Включение** — synthetic msg #X, кликнуть 🚀 → input floor → submit → проверить row в `thread_autopilot.active=1`
2. **Synthetic incoming** в активном треде (force через `_process_incoming`) — должен auto-reply без review-card в DM операторов; counter +1 в БД
3. **Limit 20** — forge counter=19, ещё одно incoming → 20-е отправляется, потом stop с reason=limit, пинг
4. **Stop ready_to_buy** — incoming text «I want to pick it up tomorrow, your address?» → reply.should_stop=true reason=wants_contact, не отправляется auto, оператор получает review-card + пинг
5. **Stop threat** — incoming с угрозой → reply.should_stop=true reason=threat, ОТПРАВЛЯЕТСЯ предупреждение клиенту, потом стоп, пинг
6. **Manual stop** — оператор кликает 🛑 → callback apstop:N → db.stop с reason=manual, без пинга. Карточка восстанавливается в обычный review-keyboard
7. **Floor enforcement** — incoming с торгом ниже floor → reply должен отказать (`«Цена окончательная»`), client_pressing_below_floor=true
8. **Web search** — incoming с техническим вопросом про специфичный VIN/spec → Sonnet может вызвать web_search → reply.used_web_search=true, ответ содержит точные данные
9. **send_mode='disabled'** — автопилот active, но reply сохраняется со status='not_sent_disabled', SMTP не вызывается, counter инкремент

## Затронутые файлы

| Файл | Изменения |
|---|---|
| `database.py` | CREATE TABLE thread_autopilot, ALTER messages ADD is_autopilot_reply. Helpers `get_thread_autopilot`, `start_thread_autopilot`, `increment_autopilot_messages`, `stop_thread_autopilot` |
| `modules/claude.py` | `generate_autopilot_reply` (расширенная schema, web_search tool, autopilot system-prompt) |
| `scheduler.py` | Бранч в `_process_incoming` после Sonnet (autopilot dispatch). Helpers `_send_autopilot_stop_notification` (если не делегировать в telegram_bot) |
| `modules/telegram_bot.py` | Новая кнопка в `_review_keyboard`. Callback `apstart:N` (input mode для floor + toggle), `apstop:N` (manual stop). Helpers `send_autopilot_stop_notification`, `send_autopilot_progress`, `_format_autopilot_active_card`, `_format_review_text` extension (показывает 🤖 active block если ap.active). Pipeline `_pipeline_thread_card` — новый status_txt при `autopilot.active`. New `LOCKABLE_ACTIONS` member: `apstart`, `apstop` |

## Cost оценка

- Включение: 0
- Каждый auto-reply: ~$0.02-0.05 (Sonnet ~$0.015 + web_search optional ~$0.01-0.03)
- Worst case 20 reply на тред: до **$1.00**
- Realistic 5-10 reply перед стопом: **$0.10-0.30/тред**

При 50 active autopilot threads/неделя — ориентир $5-15/неделя. Существенно дороже обычного flow (где Sonnet генерит draft но потом операторы могут не использовать). Зато освобождает оператору время.
