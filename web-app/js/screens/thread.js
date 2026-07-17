// Экран треда (редизайн 2026-07-15): чат + черновик одной карточкой с вкладками
// (DE клиенту / RU идея / RU перевод) + плавающий док действий внизу.
// Второстепенные действия — в «⋯»-меню (bottom-sheet), подтверждения — confirmSheet.
// Логика локов, send-пайплайна (edit-ru → edit-de → send) и deep-link сохранена.
import { api } from "../api.js?v=20260717-132503";
import { el, berlinTime, setLoading, setError, accountColor } from "../utils.js?v=20260717-132503";
import { setTitle } from "../components/backbar.js?v=20260717-132503";
import { openSheet, closeSheet, menuSheet, confirmSheet } from "../components/sheet.js?v=20260717-132503";
import { buildEditForm } from "../components/edit-form.js?v=20260717-132503";
import { buildComposeForm } from "../components/compose-form.js?v=20260717-132503";
import { buildAutopilotForm } from "../components/autopilot-form.js?v=20260717-132503";

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

function lockedByOtherBanner(lock, onRetry) {
  const block = el(`
    <div class="alert alert-warning">
      <div class="mb-1"><strong>⚠️ Карточка занята оператором <span class="holder"></span></strong>
        <span class="small text-muted">(~<span class="mins"></span> мин)</span></div>
      <button class="btn btn-sm retry-btn">↻ Проверить снова</button>
    </div>
  `);
  block.querySelector(".holder").textContent = lock.holder ?? "?";
  block.querySelector(".mins").textContent = String(lock.remaining_min ?? 0);
  block.querySelector(".retry-btn").addEventListener("click", onRetry);
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

export async function render(mount, params) {
  // Cleanup: release предыдущий lock (если был на другом thread)
  if (_heldMsgId !== null) {
    await tryReleaseLock(_heldMsgId);
    _heldMsgId = null;
  }

  setLoading(mount, "Загружаю тред…");
  try {
    const data = await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}`);
    // If deep-linked via /thread/{id}/msg/{N} — focus on that msg_id, else find latest pending
    const latestPendingMsgId = params.focus_msg_id ? Number(params.focus_msg_id) : findLatestPending(data.events);

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

    renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError, ourActor);

  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}

function renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError, ourActor) {
  const container = el(`<div></div>`);
  if (data.header?.ad_title) setTitle(data.header.ad_title);

  container.appendChild(threadHeader(data.header, review?.lock ?? null, ourActor));
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
      container.appendChild(lockedByOtherBanner(review.lock, () => render(mount, params)));
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
  // Значения на момент загрузки — edit-ru/edit-de шлём только если менялось
  const lastRu = review?.draft?.ru_answer ?? "";
  const lastDe = review?.draft?.de_answer ?? "";

  async function sendHandler(sendBtn) {
    const ruNow = draft.querySelector(".ru-input").value.trim();
    const deNow = draft.querySelector(".de-input").value.trim();
    if (!deNow) {
      draftError("DE текст не может быть пустым (это что уйдёт клиенту)");
      return;
    }
    sendBtn.disabled = true;
    sendBtn.textContent = "⏳ Отправляю…";
    try {
      // 1. Apply RU edit if changed
      if (ruNow !== lastRu && ruNow) {
        await api(`/api/ma/messages/${latestPendingMsgId}/edit-ru`, {
          method: "POST", body: {text: ruNow},
        });
      }
      // 2. Apply DE edit if changed
      if (deNow !== lastDe) {
        await api(`/api/ma/messages/${latestPendingMsgId}/edit-de`, {
          method: "POST", body: {text: deNow},
        });
      }
      // 3. Send
      await api(`/api/ma/messages/${latestPendingMsgId}/send`, {method: "POST"});
      render(mount, params);
    } catch (e) {
      draftError(e.message ?? String(e));
      sendBtn.disabled = false;
      sendBtn.textContent = "📨 Отправить";
    }
  }

  async function regenHandler(btn) {
    btn.disabled = true;
    btn.textContent = "🔁 Генерирую…";
    try {
      await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/suggest-reply`, {method: "POST"});
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
    api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/suggest-reply`, {method: "POST"})
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

// Lock release on TG WebApp close / page hide (best-effort via sendBeacon)
window.addEventListener("pagehide", () => {
  if (_heldMsgId !== null && navigator.sendBeacon) {
    // sendBeacon без auth header — backup; primary защита auto-expire 5 мин
    navigator.sendBeacon(`/api/ma/messages/${_heldMsgId}/lock/release`);
  }
});
