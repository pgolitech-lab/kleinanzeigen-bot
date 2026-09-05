// Экран треда (редизайн 2026-07-15): чат + черновик одной карточкой с вкладками
// (DE клиенту / RU идея / RU перевод) + плавающий док действий внизу.
// Второстепенные действия — в «⋯»-меню (bottom-sheet), подтверждения — confirmSheet.
// Логика локов, send-пайплайна (edit-ru → edit-de → send) и deep-link сохранена.
import { api, LLM_TIMEOUT_MS } from "../api.js?v=20260905-212258";
import { el, berlinTime, setLoading, setError, accountColor } from "../utils.js?v=20260905-212258";
import { setTitle } from "../components/backbar.js?v=20260905-212258";
import { openSheet, closeSheet, menuSheet, confirmSheet } from "../components/sheet.js?v=20260905-212258";
import { buildEditForm } from "../components/edit-form.js?v=20260905-212258";
import { buildComposeForm } from "../components/compose-form.js?v=20260905-212258";
import { buildAutopilotForm } from "../components/autopilot-form.js?v=20260905-212258";
import { initData } from "../tg.js?v=20260905-212258";

const PENDING_STATUSES = new Set(["pending", "new", "edited", "approved"]);

// Module-state для lock release at unmount.
let _heldMsgId = null;

function findLatestPending(events) {
  let candidate = null;
  for (const ev of events) {
    if (ev.kind === "in" && ev.status && PENDING_STATUSES.has(ev.status) && ev.msg_id) {
      candidate = ev.msg_id;
    }
  }
  return candidate;
}

function threadHeader(header, lock, ourActor) {
  const card = el(`
    <div class="tcard" style="cursor:default">
      <div class="tc-m">
        <div class="tc-t">
          <span class="acct acct-name"></span>
          <b class="ad-title"></b>
          <span class="price ad-price"></span>
        </div>
        <div class="tc-sub buyer-line" style="white-space:normal"></div>
        <div class="d-flex gap-1 flex-wrap mt-1 chips-line"></div>
      </div>
      <div class="tc-side">
        <a class="ad-link" target="_blank" rel="noopener">↗</a>
      </div>
    </div>
  `);
  card.querySelector(".ad-title").textContent = header.ad_title ?? "(без названия)";
  card.querySelector(".ad-price").textContent = header.ad_price ?? "";

  const acctEl = card.querySelector(".acct-name");
  acctEl.textContent = header.account_name ?? "?";
  acctEl.style.backgroundColor = header.account_id != null
    ? accountColor(header.account_id) : "var(--mut)";
  acctEl.title = header.account_email ?? "";

  card.querySelector(".buyer-line").textContent =
    `${header.buyer_display_name ?? "?"}${header.buyer_email ? ` · ${header.buyer_email}` : ""}`;

  const chips = card.querySelector(".chips-line");
  if (header.is_autopilot) chips.appendChild(el(`<span class="chip c-amb">автопилот</span>`));
  if (lock?.holder && lock.holder !== ourActor) {
    const b = el(`<span class="chip c-red"></span>`);
    b.textContent = `в работе у ${lock.holder} · ${lock.remaining_min} мин`;
    chips.appendChild(b);
  }
  if (!chips.children.length) chips.remove();

  if (header.ad_url) {
    card.querySelector(".ad-link").href = header.ad_url;
  } else {
    card.querySelector(".ad-link").remove();
  }
  return card;
}

function eventBubble(event, header, latestPendingMsgId) {
  const isIn = event.kind === "in";
  const isPending = isIn && event.msg_id === latestPendingMsgId &&
                    PENDING_STATUSES.has(event.status);
  const bubble = el(`
    <div class="bub">
      <div class="bw"><span class="who"></span><time class="when"></time></div>
      <div class="text"></div>
      <div class="ru"></div>
    </div>
  `);
  if (!isIn) bubble.classList.add("out");
  let who = isIn
    ? (header.buyer_display_name ?? "клиент")
    : `${header.account_name ?? "мы"}${event.is_auto_ack ? " · авто-привет" : ""}`;
  if (isPending) who += " · 📝";
  bubble.querySelector(".who").textContent = who;
  bubble.querySelector(".when").textContent = berlinTime(event.ts);
  bubble.querySelector(".text").textContent = event.text ?? "";
  if (event.ru_text && event.ru_text !== event.text) {
    bubble.querySelector(".ru").textContent = event.ru_text;
  } else {
    bubble.querySelector(".ru").remove();
  }
  return bubble;
}

function relatedBlock(related) {
  if (!related?.matches?.length) return null;
  const block = el(`
    <div class="alert alert-warning small">
      <div class="mb-1"><strong>⚠️ Этот клиент уже писал по другим объявлениям:</strong></div>
      <ul class="mb-0 ps-3 matches"></ul>
    </div>
  `);
  const ul = block.querySelector(".matches");
  related.matches.forEach(m => {
    const li = el(`<li><a class="link"></a></li>`);
    const a = li.querySelector(".link");
    a.href = `#/thread/${encodeURIComponent(m.thread_id)}`;
    a.textContent = `${m.ad_title ?? "?"} · ${m.ad_price ?? "?"} · ${berlinTime(m.last_at)}`;
    ul.appendChild(li);
  });
  return block;
}

// Карточка черновика: вкладки DE/RU-идея/RU-перевод + бриф + строка ошибки.
// Textareas редактируемые; отправка применяет edit-ru/edit-de только если менялось.
function draftCard(review) {
  const card = el(`
    <div class="draft-card">
      <div class="d-flex gap-2 align-items-center">
        <b class="small dhead"></b>
        <span class="chip c-amb">ждёт проверки</span>
      </div>
      <div class="dtabs">
        <button type="button" data-dt="de" class="on">DE → клиенту</button>
        <button type="button" data-dt="ru">RU идея</button>
        <button type="button" data-dt="back">RU перевод</button>
      </div>
      <div class="dbody">
        <textarea class="de-input" data-pane="de" rows="5"></textarea>
        <textarea class="ru-input d-none" data-pane="ru" rows="5"></textarea>
        <div class="ro back-text d-none" data-pane="back"></div>
      </div>
      <div class="brief"></div>
      <div class="derr text-danger small mt-2 d-none"></div>
    </div>
  `);
  card.querySelector(".dhead").textContent = `Черновик #${review.msg_id}`;
  card.querySelector(".de-input").value = review.draft?.de_answer ?? "";
  card.querySelector(".ru-input").value = review.draft?.ru_answer ?? "";
  card.querySelector(".back-text").textContent = review.draft?.ru_translation ?? "(нет перевода)";

  const brief = review.deal_brief;
  const briefEl = card.querySelector(".brief");
  if (brief) {
    const parts = [];
    if (brief.summary_ru) parts.push(`💬 ${brief.summary_ru}`);
    if (brief.negotiated_price_eur) parts.push(`💰 ${brief.negotiated_price_eur} €`);
    if (brief.client_assessment) parts.push(`🏷 ${brief.client_assessment}`);
    if (brief.expected_next) parts.push(`⏳ ${brief.expected_next}`);
    parts.forEach(p => {
      const s = document.createElement("span");
      s.textContent = p;
      briefEl.appendChild(s);
    });
  } else {
    briefEl.remove();
  }

  // Переключение вкладок — панели уже в DOM, значения textarea сохраняются
  card.querySelectorAll("[data-dt]").forEach(btn => {
    btn.addEventListener("click", () => {
      card.querySelectorAll("[data-dt]").forEach(b => b.classList.toggle("on", b === btn));
      card.querySelectorAll("[data-pane]").forEach(p =>
        p.classList.toggle("d-none", p.dataset.pane !== btn.dataset.dt));
    });
  });
  return card;
}

function lockedByOtherBanner(lock, onRetry, msgId) {
  const block = el(`
    <div class="alert alert-warning">
      <div class="mb-1"><strong>⚠️ Карточка занята оператором <span class="holder"></span></strong>
        <span class="small text-muted">(~<span class="mins"></span> мин)</span></div>
      <div class="small text-muted mb-2">Если оператор уже закрыл MA — заберите карточку, чтобы ответить клиенту.</div>
      <div class="d-flex gap-2">
        <button class="btn btn-sm retry-btn">↻ Проверить снова</button>
        <button class="btn btn-sm btn-warning takeover-btn">🔓 Забрать карточку</button>
      </div>
    </div>
  `);
  block.querySelector(".holder").textContent = lock.holder ?? "?";
  block.querySelector(".mins").textContent = String(lock.remaining_min ?? 0);
  block.querySelector(".retry-btn").addEventListener("click", onRetry);
  // Снимаем чужой лок (release permissive) и сразу переоткрываем тред —
  // render() заберёт лок на текущего оператора. Для случая «narcissk закрыл
  // MA, но лок висит»: не ждём 5-минутный auto-expire.
  block.querySelector(".takeover-btn").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.textContent = "⏳ Забираю…";
    if (msgId != null) await tryReleaseLock(msgId);
    onRetry();
  });
  return block;
}

// Форма «Продано» в bottom-sheet (цена + закрыть остальные переговоры).
function openSoldSheet(msgId, onDone) {
  const form = el(`
    <div>
      <label class="form-label small text-muted">Цена продажи (€)</label>
      <input type="number" inputmode="decimal" min="0" step="0.01"
             class="form-control sold-price mb-2" placeholder="напр. 1300">
      <div class="form-check mb-3">
        <input type="checkbox" class="form-check-input close-other" id="sold-close-other">
        <label class="form-check-label small" for="sold-close-other">
          Закрыть остальные переговоры по этому объявлению
        </label>
      </div>
      <div class="form-error text-danger small mb-2 d-none"></div>
      <div class="d-flex gap-2">
        <button class="btn cancel-btn" style="flex:1">Отмена</button>
        <button class="btn btn-primary save-btn" style="flex:1">✅ Сохранить</button>
      </div>
    </div>
  `);
  const errEl = form.querySelector(".form-error");
  form.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  form.querySelector(".save-btn").addEventListener("click", async () => {
    const priceStr = form.querySelector(".sold-price").value.trim();
    const price = Number(priceStr.replace(",", "."));
    if (!priceStr || Number.isNaN(price) || price < 0) {
      form.querySelector(".sold-price").classList.add("is-invalid");
      return;
    }
    const closeOther = form.querySelector(".close-other").checked;
    const saveBtn = form.querySelector(".save-btn");
    saveBtn.disabled = true;
    saveBtn.textContent = "⏳ Сохраняю…";
    try {
      await api(`/api/ma/messages/${msgId}/sold`, {
        method: "POST",
        body: { price_eur: price, close_other_threads_for_ad: closeOther },
      });
      closeSheet();
      onDone();
    } catch (err) {
      errEl.textContent = err.message ?? String(err);
      errEl.classList.remove("d-none");
      saveBtn.disabled = false;
      saveBtn.textContent = "✅ Сохранить";
    }
  });
  openSheet(form, "💰 Зафиксировать продажу");
  form.querySelector(".sold-price").focus();
}

async function tryReleaseLock(msgId) {
  if (msgId === null) return;
  try {
    await api(`/api/ma/messages/${msgId}/lock/release`, {method: "POST"});
  } catch (e) {
    console.warn("[thread] lock release failed:", e);
  }
}

export async function render(mount, params, opts = {}) {
  // Cleanup: release предыдущий lock (если был на другом thread)
  if (_heldMsgId !== null) {
    await tryReleaseLock(_heldMsgId);
    _heldMsgId = null;
  }

  setLoading(mount, "Загружаю тред…");
  try {
    const data = await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}`);
    // If deep-linked via /thread/{id}/msg/{N} — focus on that msg_id, else find latest pending
    let latestPendingMsgId = params.focus_msg_id ? Number(params.focus_msg_id) : findLatestPending(data.events);

    let review = null;
    let acquired = false;
    let acquireError = null;
    let ourActor = null;

    if (latestPendingMsgId !== null) {
      try {
        review = await api(`/api/ma/messages/${latestPendingMsgId}`);
      } catch (e) {
        console.warn("[thread] review fetch failed:", e);
      }
      // Deep-link форсирует msg_id независимо от статуса: после отправки этот же
      // черновик рисовался как «ждёт проверки» с активной кнопкой «Отправить» —
      // оператор не видел, что письмо ушло, и жал ещё раз (спасал duplicate-guard).
      // Не-pending draft показываем как историю: ищем другой pending в треде.
      if (review && !PENDING_STATUSES.has(review.status)) {
        review = null;
        const fallback = findLatestPending(data.events);
        latestPendingMsgId = fallback !== latestPendingMsgId ? fallback : null;
        if (latestPendingMsgId !== null) {
          try {
            review = await api(`/api/ma/messages/${latestPendingMsgId}`);
          } catch (e) {
            console.warn("[thread] fallback review fetch failed:", e);
          }
        }
      }
    }

    if (latestPendingMsgId !== null) {
      try {
        const lockRes = await api(`/api/ma/messages/${latestPendingMsgId}/lock/acquire`, {method: "POST"});
        acquired = true;
        _heldMsgId = latestPendingMsgId;
        ourActor = lockRes.holder;
        if (review) {
          review.lock = lockRes;
        }
      } catch (e) {
        if (e.message && e.message.includes("HTTP 409")) {
          acquireError = "locked";
        } else {
          acquireError = "network";
          console.warn("[thread] lock acquire failed:", e);
        }
      }
    }

    renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError, ourActor, opts);

  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}

function renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError, ourActor, opts = {}) {
  const container = el(`<div></div>`);
  if (data.header?.ad_title) setTitle(data.header.ad_title);

  container.appendChild(threadHeader(data.header, review?.lock ?? null, ourActor));
  if (opts.sentBanner) {
    container.appendChild(el(
      `<div class="alert alert-success small py-2">✅ Сообщение отправлено клиенту</div>`));
  }
  const related = relatedBlock(data.related);
  if (related) container.appendChild(related);

  if (data.events.length === 0) {
    container.appendChild(el(`<p class="text-muted">Событий пока нет.</p>`));
  } else {
    // Sort events by ts ASC — newest at bottom (chat-style)
    const sortedEvents = [...data.events].sort((a, b) => {
      const ta = a.ts || "";
      const tb = b.ts || "";
      if (ta < tb) return -1;
      if (ta > tb) return 1;
      return 0;
    });
    const chat = el(`<div class="mt-2"></div>`);
    sortedEvents.forEach(e => chat.appendChild(eventBubble(e, data.header, latestPendingMsgId)));
    container.appendChild(chat);
  }

  // ---------- черновик ----------
  let draft = null;
  if (review) {
    draft = draftCard(review);
    container.appendChild(draft);
    if (acquireError === "locked" && review.lock) {
      container.appendChild(lockedByOtherBanner(review.lock, () => render(mount, params), latestPendingMsgId));
    } else if (acquireError === "network") {
      const warn = el(`<p class="text-warning small mt-2">⚠️ Не удалось взять lock — отправка может не пройти. <a href="#" class="reload-link">Перезагрузить</a></p>`);
      warn.querySelector(".reload-link").addEventListener("click", (e) => {
        e.preventDefault();
        render(mount, params);
      });
      container.appendChild(warn);
    }
  }

  function draftError(msg) {
    const errEl = draft?.querySelector(".derr");
    if (!errEl) { alert(`Ошибка: ${msg}`); return; }
    errEl.textContent = `Ошибка: ${msg}`;
    errEl.classList.remove("d-none");
  }

  // ---------- обработчики ----------
  // Значения последнего sync с сервером — edit-ru/edit-de шлём только если менялось
  let lastRu = review?.draft?.ru_answer ?? "";
  let lastDe = review?.draft?.de_answer ?? "";

  // Кириллица при не-кириллическом языке клиента = почти наверняка ошибка
  // (оператор напечатал русский во вкладку «DE → клиенту»). Зеркало серверного guard'а.
  const CYRILLIC_CLIENT_LANGS = new Set(["ru", "uk", "be", "bg", "sr", "mk"]);
  function wrongLangForClient(text, clientLang) {
    const lang = (clientLang || "de").toLowerCase();
    if (CYRILLIC_CLIENT_LANGS.has(lang)) return false;
    const letters = text.match(/\p{L}/gu) || [];
    if (!letters.length) return false;
    const cyr = letters.filter(ch => ch >= "Ѐ" && ch <= "ӿ").length;
    return cyr / letters.length > 0.3;
  }

  // Синхронизация draft-карточки и локального state после серверных мутаций
  // (translate-draft / instruction / edit-de / suggest-reply).
  function syncDraft(fresh) {
    review.draft = fresh.draft;
    const de = fresh.draft?.de_answer ?? "";
    const ru = fresh.draft?.ru_answer ?? "";
    draft.querySelector(".de-input").value = de;
    draft.querySelector(".ru-input").value = ru;
    draft.querySelector(".back-text").textContent = fresh.draft?.ru_translation ?? "(нет перевода)";
    lastDe = de;
    lastRu = ru;
  }

  function curText() {
    return (review?.draft?.de_answer ?? "").trim();
  }

  async function sendHandler(sendBtn) {
    const ruNow = draft.querySelector(".ru-input").value.trim();
    const deNow = draft.querySelector(".de-input").value.trim();
    if (!deNow) {
      draftError("Текст клиенту не может быть пустым");
      return;
    }
    sendBtn.disabled = true;
    sendBtn.textContent = "⏳ Готовлю…";
    try {
      // Правки из textarea применяем ДО флоу: превью в шитах должно показывать
      // ровно то, что уйдёт клиенту (edit-ru перегенерирует DE из RU-идеи).
      let fresh = null;
      if (ruNow !== lastRu && ruNow) {
        fresh = await api(`/api/ma/messages/${latestPendingMsgId}/edit-ru`, {
          method: "POST", body: {text: ruNow},
        });
        lastRu = ruNow;
      }
      if (deNow !== lastDe) {
        fresh = await api(`/api/ma/messages/${latestPendingMsgId}/edit-de`, {
          method: "POST", body: {text: deNow},
        });
        lastDe = deNow;
      }
      if (fresh) syncDraft(fresh);
      else if (review?.draft) review.draft.de_answer = deNow;
    } catch (e) {
      draftError(e.message ?? String(e));
      sendBtn.disabled = false;
      sendBtn.textContent = "📨 Отправить";
      return;
    }
    sendBtn.disabled = false;
    sendBtn.textContent = "📨 Отправить";
    openSendChoiceSheet();
  }

  // ───── Send-флоу (2026-07-19): выбор → подготовка → финальный текст ─────
  // Инвариант: клиенту НИКОГДА не уходит текст на чужом языке без явного
  // разрешения оператора (галочка на финальном шаге + force для серверного guard).

  function flowPreviewBlock(text) {
    const p = el(`<div class="p-2 rounded mb-2"
      style="white-space:pre-wrap; font-size:.92em; max-height:35vh; overflow-y:auto; background:rgba(128,128,128,.14)"></div>`);
    p.textContent = text;
    return p;
  }

  function showFlowErr(box, e) {
    const errEl = box.querySelector(".flow-err");
    errEl.textContent = e.message ?? String(e);
    errEl.classList.remove("d-none");
  }

  // Универсальный текстовый под-шаг (правка текста / инструкции для ЛЛМ).
  function openTextSheet({title, initial, placeholder, submitLabel, busyLabel, onSubmit, onBack}) {
    const box = el(`
      <div>
        <textarea class="form-control txt" rows="6"></textarea>
        <div class="flow-err text-danger small my-2 d-none"></div>
        <div class="d-flex gap-2 mt-2">
          <button class="btn back-btn" style="flex:1">↩ Назад</button>
          <button class="btn btn-primary ok-btn" style="flex:1"></button>
        </div>
      </div>
    `);
    const txt = box.querySelector(".txt");
    txt.value = initial ?? "";
    if (placeholder) txt.placeholder = placeholder;
    const okBtn = box.querySelector(".ok-btn");
    const backBtn = box.querySelector(".back-btn");
    okBtn.textContent = submitLabel;
    backBtn.addEventListener("click", onBack);
    okBtn.addEventListener("click", async () => {
      const value = txt.value.trim();
      if (!value) { txt.classList.add("is-invalid"); return; }
      okBtn.disabled = true;
      backBtn.disabled = true;
      okBtn.textContent = busyLabel;
      try {
        await onSubmit(value);
      } catch (e) {
        showFlowErr(box, e);
        okBtn.disabled = false;
        backBtn.disabled = false;
        okBtn.textContent = submitLabel;
      }
    });
    openSheet(box, title);
    txt.focus();
  }

  // Этап 1: выбор способа отправки.
  function openSendChoiceSheet() {
    const clientLang = (review?.client_lang || "de").toLowerCase();
    const langUp = clientLang.toUpperCase();
    const box = el(`
      <div>
        <div class="small text-muted mb-1">Язык клиента: <b class="lang"></b></div>
        <div class="preview-slot"></div>
        <div class="flow-err text-danger small mb-2 d-none"></div>
        <div class="d-grid gap-2">
          <button class="btn btn-primary tr-btn"></button>
          <button class="btn asis-btn">📨 Отправить как есть</button>
          <button class="btn llm-btn">🤖 Предложить текст от ЛЛМ</button>
          <button class="btn cancel-btn">✕ Отменить</button>
        </div>
      </div>
    `);
    box.querySelector(".lang").textContent = langUp;
    box.querySelector(".preview-slot").appendChild(flowPreviewBlock(curText() || "(черновик пуст)"));
    const trBtn = box.querySelector(".tr-btn");
    const asisBtn = box.querySelector(".asis-btn");
    const llmBtn = box.querySelector(".llm-btn");
    const cancelBtn = box.querySelector(".cancel-btn");
    trBtn.textContent = `🌐 Перевести на ${langUp} и отправить`;
    const allBtns = [trBtn, asisBtn, llmBtn, cancelBtn];

    trBtn.addEventListener("click", async () => {
      allBtns.forEach(b => b.disabled = true);
      trBtn.textContent = "⏳ Перевожу…";
      try {
        const fresh = await api(`/api/ma/messages/${latestPendingMsgId}/translate-draft`, {
          method: "POST", body: {text: curText()},
        });
        syncDraft(fresh);
        openPrepareSheet();
      } catch (e) {
        showFlowErr(box, e);
        allBtns.forEach(b => b.disabled = false);
        trBtn.textContent = `🌐 Перевести на ${langUp} и отправить`;
      }
    });
    asisBtn.addEventListener("click", () => openPrepareSheet());
    llmBtn.addEventListener("click", async () => {
      allBtns.forEach(b => b.disabled = true);
      llmBtn.textContent = "⏳ Генерирую…";
      try {
        await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/suggest-reply`, {method: "POST", timeoutMs: LLM_TIMEOUT_MS});
        const fresh = await api(`/api/ma/messages/${latestPendingMsgId}`);
        syncDraft(fresh);
        openPrepareSheet();
      } catch (e) {
        showFlowErr(box, e);
        allBtns.forEach(b => b.disabled = false);
        llmBtn.textContent = "🤖 Предложить текст от ЛЛМ";
      }
    });
    cancelBtn.addEventListener("click", () => closeSheet());
    openSheet(box, "Отправка — что делаем?");
  }

  // Этап 2: подготовка текста перед отправкой.
  function openPrepareSheet() {
    const box = el(`
      <div>
        <div class="preview-slot"></div>
        <div class="ru-mirror small text-muted mb-2" style="white-space:pre-wrap"></div>
        <div class="flow-err text-danger small mb-2 d-none"></div>
        <div class="d-grid gap-2">
          <button class="btn btn-primary next-btn">📨 Отправить</button>
          <button class="btn edit-btn">✏️ Редактировать</button>
          <button class="btn rewrite-btn">🤖 Переписать идею ЛЛМ…</button>
          <button class="btn cancel-btn">✕ Отменить</button>
        </div>
      </div>
    `);
    box.querySelector(".preview-slot").appendChild(flowPreviewBlock(curText() || "(черновик пуст)"));
    const mirror = review?.draft?.ru_translation;
    const mirrorEl = box.querySelector(".ru-mirror");
    if (mirror && mirror !== curText()) mirrorEl.textContent = `RU: ${mirror}`;
    else mirrorEl.remove();

    box.querySelector(".next-btn").addEventListener("click", () => {
      if (!curText()) { showFlowErr(box, new Error("черновик пуст")); return; }
      openFinalSheet();
    });
    box.querySelector(".edit-btn").addEventListener("click", () => openTextSheet({
      title: "✏️ Редактировать текст",
      initial: curText(),
      submitLabel: "💾 Сохранить",
      busyLabel: "⏳ Сохраняю…",
      onSubmit: async (value) => {
        const fresh = await api(`/api/ma/messages/${latestPendingMsgId}/edit-de`, {
          method: "POST", body: {text: value},
        });
        syncDraft(fresh);
        openPrepareSheet();
      },
      onBack: () => openPrepareSheet(),
    }));
    box.querySelector(".rewrite-btn").addEventListener("click", () => openTextSheet({
      title: "🤖 Переписать идею ЛЛМ",
      initial: "",
      placeholder: "Инструкции: что изменить, какой тон, какая цена…",
      submitLabel: "🤖 Переписать",
      busyLabel: "⏳ Генерирую…",
      onSubmit: async (value) => {
        const fresh = await api(`/api/ma/messages/${latestPendingMsgId}/instruction`, {
          method: "POST", body: {text: value}, timeoutMs: LLM_TIMEOUT_MS,
        });
        syncDraft(fresh);
        openPrepareSheet();
      },
      onBack: () => openPrepareSheet(),
    }));
    box.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
    openSheet(box, "Подготовка к отправке");
  }

  // Этап 3: финальный текст к отправке. Двухслойный языковой guard:
  // мгновенная кириллическая эвристика + LLM-проверка (Haiku) через /lang-check.
  // Чужой язык — отправка ТОЛЬКО после явной галочки-разрешения (force для сервера).
  function openFinalSheet() {
    const clientLang = (review?.client_lang || "de").toLowerCase();
    const langUp = clientLang.toUpperCase();
    const text = curText();
    const box = el(`
      <div>
        <div class="small text-muted mb-1">Уйдёт клиенту (язык: <b class="lang"></b>):</div>
        <div class="preview-slot"></div>
        <div class="lang-status small text-muted mb-2"></div>
        <div class="mismatch d-none">
          <div class="alert alert-danger small py-2 mb-2 mm-text"></div>
          <div class="form-check mb-2">
            <input type="checkbox" class="form-check-input allow-lang" id="allow-lang-cb">
            <label class="form-check-label small" for="allow-lang-cb">
              Разрешаю отправить текст не на языке клиента
            </label>
          </div>
        </div>
        <div class="flow-err text-danger small mb-2 d-none"></div>
        <div class="d-grid gap-2">
          <button class="btn btn-primary send-btn">✅ Отправить</button>
          <button class="btn edit-btn">✏️ Изменить текст вручную</button>
          <button class="btn cancel-btn">✕ Отменить</button>
        </div>
      </div>
    `);
    box.querySelector(".lang").textContent = langUp;
    box.querySelector(".preview-slot").appendChild(flowPreviewBlock(text));
    const sendBtn = box.querySelector(".send-btn");
    const editBtn = box.querySelector(".edit-btn");
    const cancelBtn = box.querySelector(".cancel-btn");
    const allowCb = box.querySelector(".allow-lang");
    const statusEl = box.querySelector(".lang-status");

    let blocked = false;   // язык не совпал (эвристика или Haiku)
    let checking = false;  // Haiku-проверка в полёте — send выключен
    let sending = false;
    function syncSendState() {
      sendBtn.disabled = sending || checking || (blocked && !allowCb.checked);
    }
    function blockLang(msgText) {
      blocked = true;
      box.querySelector(".mm-text").textContent = msgText;
      box.querySelector(".mismatch").classList.remove("d-none");
      syncSendState();
    }
    allowCb.addEventListener("change", syncSendState);

    if (wrongLangForClient(text, clientLang)) {
      // Кириллица чужому клиенту — блок сразу, Haiku не нужен
      blockLang("⚠️ Текст НЕ на языке клиента. Отправка заблокирована — " +
                "вернитесь и переведите, либо явно разрешите ниже.");
    } else {
      // Haiku-проверка: до вердикта отправка выключена
      checking = true;
      statusEl.textContent = "⏳ Проверяю язык (Haiku)…";
      syncSendState();
      api(`/api/ma/messages/${latestPendingMsgId}/lang-check`, {
        method: "POST", body: {text},
      }).then(r => {
        checking = false;
        if (r.match) {
          statusEl.textContent = `✓ Язык совпадает (${(r.detected_lang || "?").toUpperCase()})`;
        } else {
          statusEl.textContent = "";
          blockLang(`⚠️ Haiku определил язык текста: ${(r.detected_lang || "?").toUpperCase()}, ` +
                    `а язык клиента — ${langUp}. Отправка заблокирована — ` +
                    "вернитесь и переведите, либо явно разрешите ниже.");
        }
        syncSendState();
      }).catch(() => {
        checking = false;
        statusEl.textContent = "⚠️ LLM-проверка языка недоступна — сработала только быстрая эвристика";
        syncSendState();
      });
    }

    sendBtn.addEventListener("click", async () => {
      sending = true;
      [sendBtn, editBtn, cancelBtn].forEach(b => b.disabled = true);
      sendBtn.textContent = "⏳ Отправляю…";
      try {
        // force=true только при явной галочке-разрешении оператора
        await api(`/api/ma/messages/${latestPendingMsgId}/send`, {
          method: "POST",
          body: {mode: "as_is", force: blocked && allowCb.checked === true},
        });
        closeSheet();
        // focus_msg_id сбрасываем: иначе перерисовка вернёт только что отправленный
        // черновик как «ждёт проверки» с активной отправкой
        render(mount, {...params, focus_msg_id: null}, {sentBanner: true});
      } catch (e) {
        showFlowErr(box, e);
        sending = false;
        [editBtn, cancelBtn].forEach(b => b.disabled = false);
        syncSendState();
        sendBtn.textContent = "✅ Отправить";
      }
    });
    editBtn.addEventListener("click", () => openTextSheet({
      title: "✏️ Изменить текст вручную",
      initial: text,
      submitLabel: "💾 Сохранить",
      busyLabel: "⏳ Сохраняю…",
      onSubmit: async (value) => {
        const fresh = await api(`/api/ma/messages/${latestPendingMsgId}/edit-de`, {
          method: "POST", body: {text: value},
        });
        syncDraft(fresh);
        openFinalSheet();
      },
      onBack: () => openFinalSheet(),
    }));
    cancelBtn.addEventListener("click", () => closeSheet());
    openSheet(box, "Финальный текст");
  }

  async function regenHandler(btn) {
    btn.disabled = true;
    btn.textContent = "🔁 Генерирую…";
    try {
      await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/suggest-reply`, {method: "POST", timeoutMs: LLM_TIMEOUT_MS});
      // Fetch fresh review payload — новый вариант в textareas
      const fresh = await api(`/api/ma/messages/${latestPendingMsgId}`);
      review.draft = fresh.draft;
      draft.querySelector(".ru-input").value = fresh.draft?.ru_answer ?? "";
      draft.querySelector(".de-input").value = fresh.draft?.de_answer ?? "";
      draft.querySelector(".back-text").textContent = fresh.draft?.ru_translation ?? "(нет перевода)";
    } catch (e) {
      draftError(e.message ?? String(e));
    } finally {
      btn.disabled = false;
      btn.textContent = "🔁 Вариант";
    }
  }

  function suggestHandler(btn) {
    // Нет pending-черновика: suggest СОЗДАЁТ pending, затем re-render
    if (btn) { btn.disabled = true; btn.textContent = "🤖 Генерирую…"; }
    api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/suggest-reply`, {method: "POST", timeoutMs: LLM_TIMEOUT_MS})
      .then(() => render(mount, params))
      .catch(e => {
        alert(`Ошибка: ${e.message ?? String(e)}`);
        if (btn) { btn.disabled = false; btn.textContent = "🤖 Предложить"; }
      });
  }

  function composeHandler() {
    const form = buildComposeForm({
      threadId: params.thread_id,
      onSubmitComplete: () => { closeSheet(); render(mount, params); },
      onCancel: () => closeSheet(),
    });
    openSheet(form, "✉️ Написать клиенту");
  }

  function historyHandler() {
    const email = data.header?.buyer_email;
    if (!email) {
      alert("Email клиента не известен");
      return;
    }
    location.hash = `#/client/${encodeURIComponent(email)}`;
  }

  async function waitHandler() {
    try {
      await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/wait`, {
        method: "POST",
      });
      render(mount, params);
    } catch (e) {
      alert(`Ошибка: ${e.message ?? String(e)}`);
    }
  }

  function instructionHandler() {
    const form = buildEditForm({
      msgId: latestPendingMsgId,
      field: "instruction",
      review,
      onSubmitComplete: () => { closeSheet(); render(mount, params); },
      onCancel: () => closeSheet(),
      onError: (msg) => alert(`Ошибка: ${msg}`),
    });
    openSheet(form, "📝 Своя инструкция");
  }

  async function skipHandler() {
    if (!(await confirmSheet("Пропустить это обращение?", "Пропустить"))) return;
    try {
      await api(`/api/ma/messages/${latestPendingMsgId}/skip`, {method: "POST"});
      render(mount, params);
    } catch (e) {
      alert(`Ошибка: ${e.message ?? String(e)}`);
    }
  }

  const autopilotState = review?.autopilot ?? (data.header.is_autopilot ? {active: true} : null);

  async function autopilotHandler() {
    if (autopilotState?.active) {
      const info = `автопилот · ${autopilotState.messages_sent ?? "?"}/20 · floor ${autopilotState.floor_eur ?? "?"} €`;
      if (!(await confirmSheet(`Остановить автопилот?\n(${info})`, "Остановить"))) return;
      try {
        await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/autopilot/stop`, {method: "POST"});
        render(mount, params);
      } catch (e) {
        alert(`Ошибка: ${e.message ?? String(e)}`);
      }
    } else {
      const form = buildAutopilotForm({
        threadId: params.thread_id,
        onSubmitComplete: () => { closeSheet(); render(mount, params); },
        onCancel: () => closeSheet(),
      });
      openSheet(form, "🚀 Автопилот");
    }
  }

  function moreMenu() {
    const canAct = review !== null && latestPendingMsgId !== null;
    menuSheet([
      { label: "📋 История клиента", onClick: historyHandler },
      canAct ? { label: "📝 Своя инструкция", onClick: instructionHandler } : null,
      { label: "✋ Ждать клиента", onClick: waitHandler },
      {
        label: autopilotState?.active
          ? `🛑 Остановить автопилот (${autopilotState.messages_sent ?? "?"}/20)`
          : "🚀 Автопилот…",
        onClick: autopilotHandler,
      },
      canAct ? { label: "💰 Продано…", onClick: () => openSoldSheet(latestPendingMsgId, () => render(mount, params)) } : null,
      canAct ? { label: "❌ Пропустить", danger: true, onClick: skipHandler } : null,
    ], "Действия");
  }

  // ---------- док ----------
  const dock = el(`<div class="dock"></div>`);
  if (review) {
    const regenBtn = el(`<button class="btn">🔁 Вариант</button>`);
    const sendBtn = el(`<button class="btn btn-primary">📨 Отправить</button>`);
    regenBtn.addEventListener("click", () => regenHandler(regenBtn));
    sendBtn.addEventListener("click", () => sendHandler(sendBtn));
    dock.appendChild(regenBtn);
    dock.appendChild(sendBtn);
  } else {
    const suggestBtn = el(`<button class="btn">🤖 Предложить</button>`);
    const composeBtn = el(`<button class="btn">✉️ Написать</button>`);
    suggestBtn.addEventListener("click", () => suggestHandler(suggestBtn));
    composeBtn.addEventListener("click", composeHandler);
    dock.appendChild(suggestBtn);
    dock.appendChild(composeBtn);
  }
  const moreBtn = el(`<button class="btn more" aria-label="Ещё действия">⋯</button>`);
  moreBtn.addEventListener("click", moreMenu);
  dock.appendChild(moreBtn);
  container.appendChild(dock);

  mount.replaceChildren(container);
}

// Lock release on navigation away
window.addEventListener("hashchange", () => {
  if (_heldMsgId !== null && !location.hash.startsWith("#/thread/")) {
    const msgId = _heldMsgId;
    _heldMsgId = null;
    tryReleaseLock(msgId);
  }
});

// Lock release on TG WebApp close / page hide.
// sendBeacon не умеет ставить X-Telegram-Init-Data → шлём initData в теле
// (text/plain, без preflight); backend валидирует её тем же HMAC. Раньше
// beacon бил в /lock/release без заголовка, ловил 422 и лок висел до
// auto-expire 5 мин — блокировал других операторов после закрытия MA.
window.addEventListener("pagehide", () => {
  if (_heldMsgId !== null && navigator.sendBeacon) {
    const blob = new Blob([initData()], {type: "text/plain"});
    navigator.sendBeacon(`/api/ma/messages/${_heldMsgId}/lock/release-beacon`, blob);
  }
});
