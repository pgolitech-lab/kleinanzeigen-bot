// 📥 Входящие — единый инбокс всех аккаунтов.
import { api } from "../api.js?v=20260704-203937";
import { el, berlinTime, setLoading, setError, accountBadge } from "../utils.js?v=20260704-203937";

const state = { account: "", status: "all" };
const sel = { active: false, ids: new Set() };
let _bulkBar = null;
let _cachedData = null; // avoid re-fetch when toggling selection mode

function removeBulkBar() {
  if (_bulkBar) { _bulkBar.remove(); _bulkBar = null; }
}

function updateBulkBar() {
  if (!_bulkBar) return;
  const count = sel.ids.size;
  const countEl = _bulkBar.querySelector(".sel-count");
  if (countEl) countEl.textContent = count > 0 ? `Выбрано: ${count}` : "Ничего не выбрано";
  _bulkBar.querySelectorAll("button[data-action]").forEach(b => { b.disabled = count === 0; });
}

let _timer = null, _visHandler = null, _hashHandler = null;
const REFRESH_MS = 20000;

function teardown() {
  removeBulkBar();
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_visHandler) { document.removeEventListener("visibilitychange", _visHandler); _visHandler = null; }
  if (_hashHandler) { window.removeEventListener("hashchange", _hashHandler); _hashHandler = null; }
}

function chip(label, active, onClick) {
  const b = el(`<button class="btn btn-sm me-1 mb-1"></button>`);
  b.textContent = label;
  b.classList.add(active ? "btn-primary" : "btn-outline-secondary");
  b.addEventListener("click", onClick);
  return b;
}

function matchFilter(t, kind) {
  if (state.account && String(t.account_id) !== String(state.account)) return false;
  if (kind === "pinned") return true;
  if (state.status === "ap") return !!t.is_autopilot;
  if (state.status === "red") return kind === "red";
  if (state.status === "green") return kind === "green";
  return true;
}

export function threadCard(thread, accountsById = {}, selMode = false) {
  const card = el(`
    <div class="list-group-item list-group-item-action py-2" style="cursor:pointer">
      <div class="d-flex justify-content-between align-items-start gap-2">
        <div class="flex-grow-1 me-2 min-w-0">
          <div class="title fw-semibold"></div>
          <div class="meta text-muted small mt-1"></div>
          <div class="ru-preview small mt-1"></div>
        </div>
        <div class="text-end small flex-shrink-0">
          <div class="when text-muted"></div>
          <div class="badges mt-1"></div>
        </div>
      </div>
    </div>
  `);

  if (selMode) {
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "form-check-input flex-shrink-0 mt-1 me-2";
    chk.checked = sel.ids.has(thread.thread_id);
    // stop propagation so checkbox click doesn't also fire card click
    chk.addEventListener("click", e => e.stopPropagation());
    chk.addEventListener("change", () => {
      if (chk.checked) sel.ids.add(thread.thread_id);
      else sel.ids.delete(thread.thread_id);
      updateBulkBar();
    });
    card.querySelector(".d-flex").prepend(chk);
    card.addEventListener("click", () => {
      chk.checked = !chk.checked;
      if (chk.checked) sel.ids.add(thread.thread_id);
      else sel.ids.delete(thread.thread_id);
      updateBulkBar();
    });
  } else {
    card.addEventListener("click", () => {
      location.hash = `#/thread/${encodeURIComponent(thread.thread_id)}`;
    });
  }

  const title = card.querySelector(".title");
  if (thread.operator_unread) title.classList.add("fw-bold");
  const acc = accountsById[thread.account_id];
  if (acc) title.appendChild(accountBadge(thread.account_id, acc.name));
  title.appendChild(document.createTextNode(
    ` ${thread.ad_title ?? "(без названия)"} · ${thread.ad_price ?? "?"}`));
  if (thread.operator_unread) {
    const dot = document.createElement("span");
    dot.className = "ms-1 badge rounded-pill bg-primary";
    dot.style.cssText = "width:8px;height:8px;padding:0;vertical-align:middle;display:inline-block";
    title.appendChild(dot);
  }

  card.querySelector(".meta").textContent = `👤 ${thread.buyer_display_name ?? "?"}`;

  let preview = (thread.ru_client ?? "").replace(/\s+/g, " ").trim();
  if (!preview && thread.deal_brief_json) {
    try {
      const b = typeof thread.deal_brief_json === "string"
        ? JSON.parse(thread.deal_brief_json) : thread.deal_brief_json;
      if (b?.summary_ru) preview = b.summary_ru;
    } catch (e) { /* skip */ }
  }
  if (preview.length > 110) preview = preview.slice(0, 110) + "…";
  const ruEl = card.querySelector(".ru-preview");
  if (preview) ruEl.textContent = `💬 ${preview}`; else ruEl.remove();

  card.querySelector(".when").textContent = berlinTime(thread.last_event_at);

  const badges = card.querySelector(".badges");
  if (thread.is_autopilot) {
    badges.appendChild(el(`<span class="badge bg-warning text-dark">🤖</span>`));
  }
  if (thread.pending_drafts_count > 0) {
    const b = el(`<span class="badge bg-info ms-1"></span>`);
    b.textContent = `📝 ${thread.pending_drafts_count}`;
    badges.appendChild(b);
  }
  return card;
}

function renderWith(mount, data) {
  removeBulkBar();
  const accountsById = {};
  (data.accounts ?? []).forEach(a => { accountsById[a.id] = a; });

  const container = el(`<div class="${sel.active ? "pb-5" : ""}"></div>`);

  // Хедер
  const head = el(`
    <div class="d-flex justify-content-between align-items-center mb-2">
      <h5 class="mb-0">📥 Входящие</h5>
      <div class="d-flex gap-2">
        <button class="btn btn-sm btn-outline-secondary refresh-btn">↻</button>
        <button class="btn btn-sm sel-toggle"></button>
      </div>
    </div>`);

  const selToggle = head.querySelector(".sel-toggle");
  selToggle.textContent = sel.active ? "Отмена" : "Выбрать";
  selToggle.classList.add(sel.active ? "btn-secondary" : "btn-outline-primary");

  head.querySelector(".refresh-btn").addEventListener("click", async () => {
    _cachedData = null;
    await paint(mount);
  });
  selToggle.addEventListener("click", () => {
    sel.active = !sel.active;
    sel.ids.clear();
    renderWith(mount, data); // re-render without API fetch
  });
  container.appendChild(head);

  // Фильтры (только в обычном режиме)
  if (!sel.active) {
    const accBar = el(`<div class="mb-1"></div>`);
    accBar.appendChild(chip("Все аккаунты", state.account === "",
      () => { state.account = ""; _cachedData = null; paint(mount); }));
    (data.accounts ?? []).forEach(a => {
      accBar.appendChild(chip(a.name, String(state.account) === String(a.id),
        () => { state.account = a.id; _cachedData = null; paint(mount); }));
    });
    container.appendChild(accBar);

    const stBar = el(`<div class="mb-2"></div>`);
    [["all", "Все"], ["red", "🔴 ждут нас"], ["green", "🟢 ждём"], ["ap", "🤖 автопилот"]]
      .forEach(([k, lbl]) => stBar.appendChild(chip(lbl, state.status === k,
        () => { state.status = k; renderWith(mount, data); })));
    container.appendChild(stBar);
  }

  // Секции
  const pinned = (data.pinned ?? []).filter(t => matchFilter(t, "pinned"));
  const red    = (data.red   ?? []).filter(t => matchFilter(t, "red"));
  const green  = (data.green ?? []).filter(t => matchFilter(t, "green"));
  const list = el(`<div class="list-group list-group-flush"></div>`);

  function addSection(label, items) {
    const hdr = el(`<div class="text-muted small text-uppercase mt-2 mb-1 px-1"></div>`);
    hdr.textContent = `${label}: ${items.length}`;
    list.appendChild(hdr);
    items.forEach(t => list.appendChild(threadCard(t, accountsById, sel.active)));
  }

  if (!pinned.length && !red.length && !green.length) {
    list.appendChild(el(`<div class="text-muted small fst-italic px-2 py-3 text-center">Нет обращений по фильтру</div>`));
  } else {
    if (pinned.length) addSection("📌 Закреплённые", pinned);
    if (red.length)    addSection("🔴 Ждут нас", red);
    if (green.length)  addSection("🟢 Ждём клиента", green);
  }
  container.appendChild(list);
  mount.replaceChildren(container);

  // Bulk action bar
  if (sel.active) {
    _bulkBar = el(`<div class="position-fixed bottom-0 start-0 end-0 bg-white border-top p-2 shadow-lg" style="z-index:1050"></div>`);
    const countLine = el(`<div class="text-muted small text-center sel-count mb-2">Ничего не выбрано</div>`);
    const btns = el(`<div class="d-flex gap-2 justify-content-center flex-wrap"></div>`);
    const actions = [
      ["pin",   "📌 Закрепить",   "btn-outline-primary",   null],
      ["unpin", "📌 Открепить",   "btn-outline-secondary", null],
      ["read",  "✉️ Прочитано",   "btn-outline-success",   null],
      ["unread","🔔 Непрочитано", "btn-outline-warning",   null],
      ["close", "🗑 Убрать",      "btn-danger",            true],
    ];
    actions.forEach(([action, label, cls, needConfirm]) => {
      const btn = el(`<button class="btn btn-sm ${cls}" data-action="${action}"></button>`);
      btn.textContent = label;
      btn.disabled = true;
      btn.addEventListener("click", async () => {
        if (!sel.ids.size) return;
        if (needConfirm && !confirm(`Убрать ${sel.ids.size} треда(ов) из входящих?`)) return;
        btn.disabled = true;
        try {
          await api("/api/ma/threads/bulk-action", {
            method: "POST",
            body: { thread_ids: [...sel.ids], action },
          });
        } catch (e) {
          alert(`Ошибка: ${e.message}`);
          updateBulkBar();
          return;
        }
        sel.active = false;
        sel.ids.clear();
        _cachedData = null;
        await paint(mount);
      });
      btns.appendChild(btn);
    });
    _bulkBar.appendChild(countLine);
    _bulkBar.appendChild(btns);
    document.body.appendChild(_bulkBar);
    updateBulkBar();
  }
}

async function paint(mount) {
  if (!_cachedData) {
    try {
      _cachedData = await api("/api/ma/pipeline");
    } catch (e) { setError(mount, e.message ?? String(e)); return; }
  }
  renderWith(mount, _cachedData);
}

export async function render(mount, params) {
  teardown();
  sel.active = false;
  sel.ids.clear();
  _cachedData = null;
  setLoading(mount, "Загружаю входящие…");
  await paint(mount);

  _timer = setInterval(async () => {
    if (document.visibilityState === "visible") {
      _cachedData = null;
      await paint(mount);
    }
  }, REFRESH_MS);
  _visHandler = async () => {
    if (document.visibilityState === "visible") { _cachedData = null; await paint(mount); }
  };
  document.addEventListener("visibilitychange", _visHandler);
  _hashHandler = () => {
    if (!location.hash.startsWith("#/pipeline") && location.hash !== "#/" && location.hash !== "") teardown();
  };
  window.addEventListener("hashchange", _hashHandler);
}
