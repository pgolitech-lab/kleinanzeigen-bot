// 📥 Входящие — единый инбокс всех аккаунтов. Фильтры (аккаунт/статус),
// цветные бейджи аккаунтов, превью последнего сообщения по-русски.
// Клик по карточке → переписка треда (#/thread/<id>).
import { api } from "../api.js?v=20260624-020000";
import { el, esc, berlinTime, setLoading, setError, accountBadge } from "../utils.js?v=20260624-020000";

export function threadCard(thread, accountsById = {}) {
  const card = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1 me-2">
          <div class="title fw-semibold"></div>
          <div class="meta text-muted small mt-1"></div>
          <div class="ru-preview small mt-1"></div>
        </div>
        <div class="text-end small">
          <div class="when text-muted"></div>
          <div class="badges mt-1"></div>
        </div>
      </div>
    </a>
  `);
  card.href = `#/thread/${encodeURIComponent(thread.thread_id)}`;

  // Заголовок: бейдж аккаунта + товар · цена
  const title = card.querySelector(".title");
  const acc = accountsById[thread.account_id];
  if (acc) title.appendChild(accountBadge(thread.account_id, acc.name));
  title.appendChild(document.createTextNode(
    ` ${thread.ad_title ?? "(без названия)"} · ${thread.ad_price ?? "?"}`));

  card.querySelector(".meta").textContent =
    `👤 ${thread.buyer_display_name ?? "?"}`;

  // Превью последнего сообщения по-русски (или сводка сделки)
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

const state = { account: "", status: "all" };  // status: all|red|green|ap

let _timer = null, _visHandler = null, _hashHandler = null;
const REFRESH_MS = 20000;

function teardown() {
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
  if (state.status === "ap") return !!t.is_autopilot;
  if (state.status === "red") return kind === "red";
  if (state.status === "green") return kind === "green";
  return true;
}

async function paint(mount) {
  let data;
  try {
    data = await api("/api/ma/pipeline");
  } catch (e) { setError(mount, e.message ?? String(e)); return; }

  const accountsById = {};
  (data.accounts ?? []).forEach(a => { accountsById[a.id] = a; });

  const container = el(`<div></div>`);

  // header
  const head = el(`
    <div class="d-flex justify-content-between align-items-center mb-2">
      <h5 class="mb-0">📥 Входящие</h5>
      <button class="btn btn-sm btn-outline-secondary refresh">↻</button>
    </div>`);
  head.querySelector(".refresh").addEventListener("click", () => paint(mount));
  container.appendChild(head);

  // фильтр: аккаунты
  const accBar = el(`<div class="mb-1"></div>`);
  accBar.appendChild(chip("Все аккаунты", state.account === "", () => { state.account = ""; paint(mount); }));
  (data.accounts ?? []).forEach(a => {
    accBar.appendChild(chip(a.name, String(state.account) === String(a.id),
      () => { state.account = a.id; paint(mount); }));
  });
  container.appendChild(accBar);

  // фильтр: статус
  const stBar = el(`<div class="mb-2"></div>`);
  [["all", "Все"], ["red", "🔴 ждут нас"], ["green", "🟢 ждём"], ["ap", "🤖 автопилот"]]
    .forEach(([k, lbl]) => stBar.appendChild(chip(lbl, state.status === k, () => { state.status = k; paint(mount); })));
  container.appendChild(stBar);

  // список
  const red = (data.red ?? []).filter(t => matchFilter(t, "red"));
  const green = (data.green ?? []).filter(t => matchFilter(t, "green"));
  const list = el(`<div class="list-group list-group-flush"></div>`);
  const all = [...red, ...green];
  if (!all.length) {
    list.appendChild(el(`<div class="text-muted small fst-italic px-2 py-3 text-center">Нет обращений по фильтру</div>`));
  } else {
    if (red.length) {
      list.appendChild(el(`<div class="text-muted small text-uppercase mt-2 mb-1">🔴 ждут нас: ${red.length}</div>`));
      red.forEach(t => list.appendChild(threadCard(t, accountsById)));
    }
    if (green.length) {
      list.appendChild(el(`<div class="text-muted small text-uppercase mt-3 mb-1">🟢 ждём клиента: ${green.length}</div>`));
      green.forEach(t => list.appendChild(threadCard(t, accountsById)));
    }
  }
  container.appendChild(list);
  mount.replaceChildren(container);
}

export async function render(mount, params) {
  teardown();
  setLoading(mount, "Загружаю входящие…");
  await paint(mount);

  _timer = setInterval(() => { if (document.visibilityState === "visible") paint(mount); }, REFRESH_MS);
  _visHandler = () => { if (document.visibilityState === "visible") paint(mount); };
  document.addEventListener("visibilitychange", _visHandler);
  _hashHandler = () => {
    if (!location.hash.startsWith("#/pipeline") && location.hash !== "#/" && location.hash !== "") teardown();
  };
  window.addEventListener("hashchange", _hashHandler);
}
