// 📥 Входящие — единый инбокс всех аккаунтов (редизайн 2026-07-15).
// Карточки с цветной полоской аккаунта, чипы состояний вместо эмодзи,
// фильтры-чипы, тёмная bulk-панель. Логика API/поллинга без изменений.
import { api } from "../api.js?v=20260716-162502";
import { el, berlinTime, setLoading, setError, accountColor, chip } from "../utils.js?v=20260716-162502";
import { confirmSheet } from "../components/sheet.js?v=20260716-162502";

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

function filterChip(label, active, onClick) {
  const b = el(`<button type="button" class="fch"></button>`);
  b.textContent = label;
  if (active) b.classList.add("on");
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
    <article class="tcard">
      <div class="tc-m">
        <div class="tc-t">
          <span class="acct-slot"></span>
          <b class="title"></b>
          <span class="price"></span>
        </div>
        <div class="tc-sub"></div>
      </div>
      <div class="tc-side">
        <time class="when"></time>
        <div class="badges d-flex gap-1 flex-wrap justify-content-end"></div>
      </div>
    </article>
  `);
  card.style.setProperty("--ac", accountColor(thread.account_id));

  if (selMode) {
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "form-check-input flex-shrink-0 mt-1";
    chk.checked = sel.ids.has(thread.thread_id);
    // stop propagation so checkbox click doesn't also fire card click
    chk.addEventListener("click", e => e.stopPropagation());
    chk.addEventListener("change", () => {
      if (chk.checked) sel.ids.add(thread.thread_id);
      else sel.ids.delete(thread.thread_id);
      updateBulkBar();
    });
    card.prepend(chk);
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

  const titleEl = card.querySelector(".title");
  titleEl.textContent = thread.ad_title ?? "(без названия)";
  if (thread.operator_unread) {
    titleEl.style.fontWeight = "700";
    titleEl.appendChild(el(`<span class="unread-dot"></span>`));
  }

  const acc = accountsById[thread.account_id];
  const accSlot = card.querySelector(".acct-slot");
  if (acc) {
    const badge = el(`<span class="acct"></span>`);
    badge.style.backgroundColor = accountColor(thread.account_id);
    badge.textContent = acc.name;
    accSlot.replaceWith(badge);
  } else {
    accSlot.remove();
  }

  card.querySelector(".price").textContent = thread.ad_price ?? "";

  let preview = (thread.ru_client ?? "").replace(/\s+/g, " ").trim();
  if (!preview && thread.deal_brief_json) {
    try {
      const b = typeof thread.deal_brief_json === "string"
        ? JSON.parse(thread.deal_brief_json) : thread.deal_brief_json;
      if (b?.summary_ru) preview = b.summary_ru;
    } catch (e) { /* skip */ }
  }
  if (preview.length > 110) preview = preview.slice(0, 110) + "…";
  const sub = card.querySelector(".tc-sub");
  sub.textContent = preview
    ? `${thread.buyer_display_name ?? "?"} · ${preview}`
    : (thread.buyer_display_name ?? "?");

  card.querySelector(".when").textContent = berlinTime(thread.last_event_at);

  const badges = card.querySelector(".badges");
  if (thread.is_autopilot) badges.appendChild(chip("amb", "автопилот"));
  if (thread.pending_drafts_count > 0) {
    badges.appendChild(chip("blu", thread.pending_drafts_count > 1
      ? `драфт ${thread.pending_drafts_count}` : "драфт"));
  }
  return card;
}

function renderWith(mount, data) {
  removeBulkBar();
  const accountsById = {};
  (data.accounts ?? []).forEach(a => { accountsById[a.id] = a; });

  const container = el(`<div class="${sel.active ? "pb-5" : ""}"></div>`);

  // Фильтры (только в обычном режиме); ↻ и «Выбрать» — в конце второго ряда
  if (!sel.active) {
    const accBar = el(`<div class="frow"></div>`);
    accBar.appendChild(filterChip("Все аккаунты", state.account === "",
      () => { state.account = ""; _cachedData = null; paint(mount); }));
    (data.accounts ?? []).forEach(a => {
      accBar.appendChild(filterChip(a.name, String(state.account) === String(a.id),
        () => { state.account = a.id; _cachedData = null; paint(mount); }));
    });
    container.appendChild(accBar);
  }

  const stBar = el(`<div class="frow"></div>`);
  if (!sel.active) {
    [["all", "Все"], ["red", "Ждут нас"], ["green", "Ждём"], ["ap", "Автопилот"]]
      .forEach(([k, lbl]) => stBar.appendChild(filterChip(lbl, state.status === k,
        () => { state.status = k; renderWith(mount, data); })));
    const refresh = filterChip("↻", false, async () => { _cachedData = null; await paint(mount); });
    refresh.style.marginLeft = "auto";
    stBar.appendChild(refresh);
  }
  const selToggle = filterChip(sel.active ? "Отмена" : "Выбрать", sel.active, () => {
    sel.active = !sel.active;
    sel.ids.clear();
    renderWith(mount, data); // re-render without API fetch
  });
  if (!sel.active) selToggle.style.flexShrink = "0";
  stBar.appendChild(selToggle);
  container.appendChild(stBar);

  // Секции
  const pinned = (data.pinned ?? []).filter(t => matchFilter(t, "pinned"));
  const red    = (data.red   ?? []).filter(t => matchFilter(t, "red"));
  const green  = (data.green ?? []).filter(t => matchFilter(t, "green"));
  const list = el(`<div></div>`);

  function addSection(label, items, cls) {
    const hdr = el(`<div class="sec"></div>`);
    if (cls) hdr.classList.add(cls);
    hdr.innerHTML = "";
    hdr.appendChild(document.createTextNode(`${label} · `));
    const n = el(`<b></b>`);
    n.textContent = String(items.length);
    hdr.appendChild(n);
    list.appendChild(hdr);
    items.forEach(t => list.appendChild(threadCard(t, accountsById, sel.active)));
  }

  if (!pinned.length && !red.length && !green.length) {
    list.appendChild(el(`<div class="text-muted small fst-italic px-2 py-3 text-center">Нет обращений по фильтру</div>`));
  } else {
    if (pinned.length) addSection("📌 Закреплённые", pinned);
    if (red.length)    addSection("Ждут нас", red, "sec-red");
    if (green.length)  addSection("Ждём клиента", green, "sec-grn");
  }
  container.appendChild(list);
  mount.replaceChildren(container);

  // Bulk-панель (тёмная, токены — не белая)
  if (sel.active) {
    _bulkBar = el(`<div class="bulkbar"></div>`);
    const countLine = el(`<b class="small sel-count">Ничего не выбрано</b>`);
    const btns = el(`<div class="brow"></div>`);
    const actions = [
      ["pin",   "📌 Закрепить",  "btn-sm",        null],
      ["unpin", "Открепить",     "btn-sm",        null],
      ["read",  "Прочитано",     "btn-sm",        null],
      ["unread","Непрочитано",   "btn-sm",        null],
      ["close", "🗑 Убрать",     "btn-sm btn-danger", true],
    ];
    actions.forEach(([action, label, cls, needConfirm]) => {
      const btn = el(`<button class="btn ${cls}" data-action="${action}"></button>`);
      btn.textContent = label;
      btn.disabled = true;
      btn.addEventListener("click", async () => {
        if (!sel.ids.size) return;
        if (needConfirm && !(await confirmSheet(`Убрать ${sel.ids.size} треда(ов) из входящих?`, "Убрать"))) return;
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
    // не дёргаем список пока оператор выбирает карточки
    if (document.visibilityState === "visible" && !sel.active) {
      _cachedData = null;
      await paint(mount);
    }
  }, REFRESH_MS);
  _visHandler = async () => {
    if (document.visibilityState === "visible" && !sel.active) { _cachedData = null; await paint(mount); }
  };
  document.addEventListener("visibilitychange", _visHandler);
  _hashHandler = () => {
    if (!location.hash.startsWith("#/pipeline") && location.hash !== "#/" && location.hash !== "") teardown();
  };
  window.addEventListener("hashchange", _hashHandler);
}
