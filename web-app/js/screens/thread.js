import { api } from "../api.js?v=20260510-9";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260510-9";
import { buildActionGrid } from "../components/action-grid.js?v=20260510-9";
import { buildEditForm } from "../components/edit-form.js?v=20260510-9";

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

function threadHeader(header, lock) {
  const card = el(`
    <div class="border-bottom pb-2 mb-3">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <div class="fw-semibold ad-title"></div>
          <div class="text-muted small ad-price-buyer"></div>
          <div class="text-muted small account-info"></div>
          <div class="lock-slot"></div>
        </div>
        <div class="text-end small">
          <div class="autopilot-badge"></div>
          <a class="ad-link" target="_blank" rel="noopener">📎</a>
        </div>
      </div>
    </div>
  `);
  card.querySelector(".ad-title").textContent = header.ad_title ?? "(без названия)";
  card.querySelector(".ad-price-buyer").textContent =
    `${header.ad_price ?? "?"} · 👤 ${header.buyer_display_name ?? "?"}${header.buyer_email ? ` · ${header.buyer_email}` : ""}`;
  card.querySelector(".account-info").textContent =
    `🏪 ${header.account_name ?? "?"} (${header.account_email ?? "?"})`;
  if (header.ad_url) {
    card.querySelector(".ad-link").href = header.ad_url;
  } else {
    card.querySelector(".ad-link").remove();
  }
  if (header.is_autopilot) {
    const b = el(`<span class="badge bg-warning text-dark">🤖 автопилот</span>`);
    card.querySelector(".autopilot-badge").appendChild(b);
  }
  if (lock?.holder) {
    const badge = el(`<div class="text-danger small mt-1 lock-badge">🟥 в работе у <span class="holder"></span> (<span class="mins"></span> мин)</div>`);
    badge.querySelector(".holder").textContent = lock.holder;
    badge.querySelector(".mins").textContent = String(lock.remaining_min);
    card.querySelector(".lock-slot").appendChild(badge);
  }
  return card;
}

function eventBubble(event, latestPendingMsgId) {
  const isIn = event.kind === "in";
  const align = isIn ? "" : "ms-auto";
  const bg = isIn ? "bg-secondary-subtle" : "bg-primary-subtle";
  const tag = event.is_auto_ack ? "🤖 ack" : (isIn ? "👤" : "🏪");
  const isPending = isIn && event.msg_id === latestPendingMsgId &&
                    PENDING_STATUSES.has(event.status);
  const bubble = el(`
    <div class="d-flex mb-2">
      <div class="bubble rounded p-2 ${align} ${bg}" style="max-width:80%">
        <div class="text-muted small d-flex justify-content-between mb-1">
          <span class="who"></span>
          <span class="when"></span>
        </div>
        <div class="text"></div>
        <div class="ru-text text-muted small fst-italic mt-1"></div>
      </div>
    </div>
  `);
  bubble.querySelector(".who").textContent = isPending ? `${tag} 📝` : tag;
  bubble.querySelector(".when").textContent = berlinTime(event.ts);
  bubble.querySelector(".text").textContent = event.text ?? "";
  if (event.ru_text && event.ru_text !== event.text) {
    bubble.querySelector(".ru-text").textContent = event.ru_text;
  } else {
    bubble.querySelector(".ru-text").remove();
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

function pendingDraftBlock(review) {
  const block = el(`
    <div class="border-top pt-3 mt-3 pending-draft">
      <div class="text-muted small mb-2 fw-semibold draft-header"></div>
      <div class="ru-answer-block mb-2">
        <div class="text-muted small">RU (идея от GPT)</div>
        <div class="ru-answer p-2 rounded bg-secondary-subtle"></div>
      </div>
      <div class="de-answer-block mb-2">
        <div class="text-muted small">DE → клиенту</div>
        <div class="de-answer p-2 rounded bg-primary-subtle"></div>
      </div>
      <div class="ru-translation-block mb-2">
        <div class="text-muted small">RU обратный перевод (для верификации)</div>
        <div class="ru-translation p-2 rounded fst-italic small"></div>
      </div>
      <div class="deal-brief-block text-muted small"></div>
    </div>
  `);
  block.querySelector(".draft-header").textContent =
    `📝 Наш ответ #${review.msg_id} (черновик · ${review.status})`;
  block.querySelector(".ru-answer").textContent = review.draft?.ru_answer ?? "";
  block.querySelector(".de-answer").textContent = review.draft?.de_answer ?? "";
  block.querySelector(".ru-translation").textContent = review.draft?.ru_translation ?? "";

  const brief = review.deal_brief;
  if (brief) {
    const parts = [];
    if (brief.summary_ru) parts.push(`💬 ${brief.summary_ru}`);
    if (brief.negotiated_price_eur) parts.push(`💰 торг: ${brief.negotiated_price_eur}€`);
    if (brief.client_assessment) parts.push(`🏷 ${brief.client_assessment}`);
    if (brief.expected_next) parts.push(`⏳ ${brief.expected_next}`);
    block.querySelector(".deal-brief-block").textContent = parts.join(" · ");
  } else {
    block.querySelector(".deal-brief-block").remove();
  }

  return block;
}

function lockedByOtherBanner(lock, onRetry) {
  const block = el(`
    <div class="alert alert-warning mt-3">
      <div class="mb-2"><strong>⚠️ Карточка занята оператором <span class="holder"></span>.</strong></div>
      <div class="small text-muted mb-2">Действия недоступны (осталось ~<span class="mins"></span> мин).</div>
      <button class="btn btn-sm btn-outline-primary retry-btn">↻ Проверить снова</button>
    </div>
  `);
  block.querySelector(".holder").textContent = lock.holder ?? "?";
  block.querySelector(".mins").textContent = String(lock.remaining_min ?? 0);
  block.querySelector(".retry-btn").addEventListener("click", onRetry);
  return block;
}

function backToPipeline() {
  return el(`<a class="btn btn-sm btn-outline-secondary mt-3" href="#/pipeline">↩ К pipeline</a>`);
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
    const latestPendingMsgId = findLatestPending(data.events);

    let review = null;
    let acquired = false;
    let acquireError = null;

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

    renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError);

  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}


function renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError) {
  const container = el(`<div></div>`);

  container.appendChild(threadHeader(data.header, review?.lock ?? null));
  const related = relatedBlock(data.related);
  if (related) container.appendChild(related);
  if (data.events.length === 0) {
    container.appendChild(el(`<p class="text-muted">Событий пока нет.</p>`));
  } else {
    data.events.forEach(e => container.appendChild(eventBubble(e, latestPendingMsgId)));
  }

  if (review) {
    const draftBlock = pendingDraftBlock(review);
    container.appendChild(draftBlock);

    if (acquireError === "locked" && review.lock) {
      container.appendChild(lockedByOtherBanner(review.lock, () => render(mount, params)));
    } else if (acquired) {
      const grid = buildActionGrid({
        msgId: latestPendingMsgId,
        onActionComplete: (action, res) => {
          render(mount, params);
        },
        onError: (action, message) => {
          alert(`Ошибка: ${message}`);
        },
        onEditRequest: (field) => {
          const oldGrid = container.querySelector(".action-grid");
          const form = buildEditForm({
            msgId: latestPendingMsgId,
            field,
            review,
            onSubmitComplete: () => render(mount, params),
            onCancel: () => render(mount, params),
            onError: (msg) => alert(`Ошибка: ${msg}`),
          });
          if (oldGrid && form) oldGrid.replaceWith(form);
        },
      });
      container.appendChild(grid);
    } else if (acquireError === "network") {
      const warn = el(`<p class="text-warning small mt-3">⚠️ Не удалось взять lock — действия недоступны. <a href="#" class="reload-link">Перезагрузить</a></p>`);
      warn.querySelector(".reload-link").addEventListener("click", (e) => {
        e.preventDefault();
        render(mount, params);
      });
      container.appendChild(warn);
    }
  }

  container.appendChild(backToPipeline());
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
