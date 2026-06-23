// 🔎 Разведка рынка — Mini App экран.
// Подвкладки: Машины / Запчасти / Запросы. Данные из /api/ma/scout/*.
import { api } from "../api.js?v=20260624-001500";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260624-001500";
import { openLink } from "../tg.js?v=20260624-001500";

// --- словарики отображения ---
const FUEL_RU = { electric: "⚡эл", diesel: "дизель", petrol: "бензин", hybrid: "гибрид" };
const GB_RU = { automatik: "АКПП", manuell: "МКПП" };
const PT_RU = { seat: "сиденье", bench: "скамейка", rail: "рельсы", other: "другое" };
const COND_RU = { neu: "новое", gebraucht: "б/у" };

// модуль-стейт (живёт между перерисовками подвкладок)
const S = {
  tab: "car",            // car | part | queries
  overview: null,
  cars: null,            // массив или null (не загружено)
  parts: null,
  filters: { car: {}, part: {} },
  sort: { car: "price_asc", part: "price_asc" },
};

function eur(v) {
  if (v === null || v === undefined) return "—";
  return Math.round(v).toLocaleString("de-DE") + " €";
}

// --- карточки объявлений ---
function carCard(c) {
  const card = el(`
    <div class="list-group-item py-2">
      <a class="title fw-semibold d-block text-truncate" role="button"></a>
      <div class="small mt-1"><span class="price fw-bold"></span><span class="specs text-muted"></span></div>
      <div class="small text-muted mt-1 loc"></div>
    </div>`);
  const t = card.querySelector(".title");
  t.textContent = c.title || "(без названия)";
  t.addEventListener("click", () => c.url && openLink(c.url));
  card.querySelector(".price").textContent = eur(c.price_eur) + (c.negotiable ? " VB " : " ");
  const specs = [];
  if (c.year) specs.push(c.year);
  if (c.fuel) specs.push(FUEL_RU[c.fuel] || c.fuel);
  if (c.gearbox) specs.push(GB_RU[c.gearbox] || c.gearbox);
  if (c.mileage_km) specs.push(Math.round(c.mileage_km).toLocaleString("de-DE") + " км");
  if (c.model_family) specs.push(c.model_family);
  card.querySelector(".specs").textContent = "· " + specs.join(" · ");
  card.querySelector(".loc").textContent =
    `📍 ${c.plz || ""} ${c.city || ""}${c.bundesland ? " (" + c.bundesland + ")" : ""}` +
    (c.posted_raw ? ` · ${c.posted_raw}` : "");
  return card;
}

function partCard(p) {
  const card = el(`
    <div class="list-group-item py-2">
      <a class="title fw-semibold d-block text-truncate" role="button"></a>
      <div class="small mt-1"><span class="price fw-bold"></span><span class="specs text-muted"></span></div>
      <div class="small text-muted mt-1 loc"></div>
    </div>`);
  const t = card.querySelector(".title");
  t.textContent = p.title || "(без названия)";
  t.addEventListener("click", () => p.url && openLink(p.url));
  card.querySelector(".price").textContent = eur(p.price_eur) + (p.negotiable ? " VB " : " ");
  const specs = [];
  if (p.part_type) specs.push(PT_RU[p.part_type] || p.part_type);
  if (p.condition) specs.push(COND_RU[p.condition] || p.condition);
  if (p.year) specs.push(p.year);
  card.querySelector(".specs").textContent = "· " + specs.join(" · ");
  card.querySelector(".loc").textContent =
    `📍 ${p.plz || ""} ${p.city || ""}${p.bundesland ? " (" + p.bundesland + ")" : ""}` +
    (p.posted_raw ? ` · ${p.posted_raw}` : "");
  return card;
}

// --- фильтрация + сортировка ---
function uniq(arr) { return [...new Set(arr.filter(Boolean))].sort(); }

// Опции «Значение (N)», отсортированные по убыванию количества — для выпадающих
// списков городов/земель со счётчиком сколько объявлений в каждом.
function countedOptions(all, key) {
  const m = new Map();
  all.forEach(r => { const v = r[key]; if (v) m.set(v, (m.get(v) || 0) + 1); });
  return [...m.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([value, n]) => ({ value, text: `${value} (${n})` }));
}

function applyAndRender(listEl, kind) {
  const all = kind === "car" ? (S.cars || []) : (S.parts || []);
  const f = S.filters[kind];
  const term = (f.q || "").trim().toLowerCase();
  let rows = all.filter(r => {
    if (term && !((r.title || "").toLowerCase().includes(term))) return false;
    if (f.bundesland && r.bundesland !== f.bundesland) return false;
    if (f.city && r.city !== f.city) return false;
    if (kind === "car") {
      if (f.model_family && r.model_family !== f.model_family) return false;
      if (f.fuel && r.fuel !== f.fuel) return false;
      if (f.gearbox && r.gearbox !== f.gearbox) return false;
    } else {
      if (f.part_type && r.part_type !== f.part_type) return false;
      if (f.condition && r.condition !== f.condition) return false;
    }
    return true;
  });
  const sort = S.sort[kind];
  const num = (v) => (v === null || v === undefined) ? Infinity : v;
  const numD = (v) => (v === null || v === undefined) ? -Infinity : v;
  rows.sort((a, b) => {
    switch (sort) {
      case "price_asc": return num(a.price_eur) - num(b.price_eur);
      case "price_desc": return numD(b.price_eur) - numD(a.price_eur);
      case "year_desc": return numD(b.year) - numD(a.year);
      case "year_asc": return num(a.year) - num(b.year);
      case "region": return (a.bundesland || "яя").localeCompare(b.bundesland || "яя");
      default: return 0;
    }
  });

  listEl.replaceChildren();
  const cnt = el(`<div class="text-muted small px-1 mb-1"></div>`);
  cnt.textContent = `показано: ${rows.length} из ${all.length}`;
  listEl.appendChild(cnt);
  if (!rows.length) {
    listEl.appendChild(el(`<div class="text-muted small fst-italic text-center py-3">Ничего не найдено</div>`));
    return;
  }
  const group = el(`<div class="list-group list-group-flush"></div>`);
  rows.forEach(r => group.appendChild(kind === "car" ? carCard(r) : partCard(r)));
  listEl.appendChild(group);
}

function selectFilter(label, options, current, onChange) {
  const wrap = el(`<select class="form-select form-select-sm"></select>`);
  wrap.appendChild(el(`<option value="">${esc(label)}</option>`));
  options.forEach(o => {
    const opt = el(`<option></option>`);
    opt.value = o.value; opt.textContent = o.text;
    if (o.value === current) opt.selected = true;
    wrap.appendChild(opt);
  });
  wrap.addEventListener("change", () => onChange(wrap.value));
  return wrap;
}

// --- подвкладка списка (car|part) ---
async function renderListTab(container, kind) {
  // ленивая загрузка
  if ((kind === "car" ? S.cars : S.parts) === null) {
    container.replaceChildren(el(`<p class="text-muted py-3">Загружаю…</p>`));
    try {
      const data = await api(`/api/ma/scout/listings?kind=${kind}`);
      if (kind === "car") S.cars = data.listings; else S.parts = data.listings;
    } catch (e) { setError(container, e.message ?? String(e)); return; }
  }
  const all = kind === "car" ? S.cars : S.parts;
  const f = S.filters[kind];

  const root = el(`<div></div>`);
  // строка фильтров
  const filterRow = el(`<div class="d-flex flex-wrap gap-1 mb-2"></div>`);
  const search = el(`<input type="search" class="form-control form-control-sm" placeholder="🔎 поиск по названию…" style="min-width:140px;flex:1 1 100%">`);
  search.value = f.q || "";
  const listEl = el(`<div></div>`);
  search.addEventListener("input", () => { f.q = search.value; applyAndRender(listEl, kind); });
  filterRow.appendChild(search);

  filterRow.appendChild(selectFilter("🗺 земля (все)", countedOptions(all, "bundesland"), f.bundesland || "",
    v => { f.bundesland = v; applyAndRender(listEl, kind); }));
  filterRow.appendChild(selectFilter("🏙 город (все)", countedOptions(all, "city"), f.city || "",
    v => { f.city = v; applyAndRender(listEl, kind); }));

  if (kind === "car") {
    const models = uniq(all.map(r => r.model_family)).map(v => ({ value: v, text: v }));
    filterRow.appendChild(selectFilter("модель", models, f.model_family || "",
      v => { f.model_family = v; applyAndRender(listEl, kind); }));
    filterRow.appendChild(selectFilter("топливо",
      Object.entries(FUEL_RU).map(([value, text]) => ({ value, text })), f.fuel || "",
      v => { f.fuel = v; applyAndRender(listEl, kind); }));
    filterRow.appendChild(selectFilter("КПП",
      Object.entries(GB_RU).map(([value, text]) => ({ value, text })), f.gearbox || "",
      v => { f.gearbox = v; applyAndRender(listEl, kind); }));
  } else {
    filterRow.appendChild(selectFilter("тип",
      Object.entries(PT_RU).map(([value, text]) => ({ value, text })), f.part_type || "",
      v => { f.part_type = v; applyAndRender(listEl, kind); }));
    filterRow.appendChild(selectFilter("состояние",
      Object.entries(COND_RU).map(([value, text]) => ({ value, text })), f.condition || "",
      v => { f.condition = v; applyAndRender(listEl, kind); }));
  }

  const sortSel = selectFilter("сортировка", [
    { value: "price_asc", text: "цена ↑" }, { value: "price_desc", text: "цена ↓" },
    { value: "year_desc", text: "год ↓" }, { value: "year_asc", text: "год ↑" },
    { value: "region", text: "по землям" },
  ], S.sort[kind], v => { S.sort[kind] = v || "price_asc"; applyAndRender(listEl, kind); });
  // у сортировки убираем пустую опцию-плейсхолдер: ставим текущую
  sortSel.options[0].textContent = "сортировка";
  filterRow.appendChild(sortSel);

  // сводка по землям (collapsible)
  const regions = kind === "car" ? (S.overview?.car_regions || []) : (S.overview?.part_regions || []);
  const det = el(`<details class="mb-2"><summary class="small text-muted">🗺 Сводка по землям (${regions.length})</summary></details>`);
  const rtab = el(`<div class="list-group list-group-flush small mt-1"></div>`);
  regions.forEach(r => {
    const item = el(`<div class="d-flex justify-content-between py-1"><span class="l"></span><span class="v text-muted"></span></div>`);
    item.querySelector(".l").textContent = r.bundesland;
    item.querySelector(".v").textContent = `${r.cnt} · ${eur(r.min_price)}–${eur(r.max_price)}`;
    rtab.appendChild(item);
  });
  det.appendChild(rtab);

  root.appendChild(filterRow);
  root.appendChild(det);
  root.appendChild(listEl);
  container.replaceChildren(root);
  applyAndRender(listEl, kind);
}

// --- подвкладка запросов ---
async function renderQueriesTab(container) {
  const ov = S.overview;
  const root = el(`<div></div>`);

  // генерация + запуск
  const actions = el(`
    <div class="d-grid gap-2 mb-3">
      <button class="btn btn-outline-info btn-sm gen">🤖 Сгенерировать запросы (LLM)</button>
      <button class="btn btn-primary btn-sm runall">▶ Запустить разведку (все)</button>
      <button class="btn btn-outline-secondary btn-sm verify">🔍 Проверить тип (Haiku)</button>
      <div class="small text-muted status"></div>
    </div>`);
  const statusEl = actions.querySelector(".status");
  if (ov.running) statusEl.textContent = "⏳ идёт прогон…";
  else if (ov.counts.unverified) statusEl.textContent = `❓ не проверено: ${ov.counts.unverified}`;
  actions.querySelector(".verify").addEventListener("click", async (e) => {
    const btn = e.target; btn.disabled = true;
    try {
      await api("/api/ma/scout/verify", { method: "POST", body: {} });
      statusEl.textContent = "🔍 проверка Haiku запущена… (обновите вкладку через минуту)";
    } catch (err) { statusEl.textContent = "ошибка: " + (err.message || err); btn.disabled = false; }
  });
  actions.querySelector(".gen").addEventListener("click", async (e) => {
    const btn = e.target; btn.disabled = true; btn.textContent = "🤖 генерирую…";
    try {
      const r = await api("/api/ma/scout/generate", { method: "POST", body: { extra: "" } });
      statusEl.textContent = `LLM добавил ${r.added} запросов ($${(r.cost_usd || 0).toFixed(4)})`;
      S.overview = await api("/api/ma/scout/overview");
      renderQueriesTab(container);
    } catch (err) { statusEl.textContent = "ошибка: " + (err.message || err); btn.disabled = false; btn.textContent = "🤖 Сгенерировать запросы (LLM)"; }
  });
  actions.querySelector(".runall").addEventListener("click", async (e) => {
    const btn = e.target; btn.disabled = true;
    try {
      await api("/api/ma/scout/run", { method: "POST", body: {} });
      statusEl.textContent = "⏳ разведка запущена в фоне…";
      pollStatus(statusEl);
    } catch (err) { statusEl.textContent = "ошибка: " + (err.message || err); btn.disabled = false; }
  });
  root.appendChild(actions);

  // добавить запрос
  const addForm = el(`
    <div class="card mb-3"><div class="card-body p-2">
      <div class="small fw-semibold mb-2">+ Новый запрос</div>
      <div class="d-flex gap-1 mb-2">
        <select class="form-select form-select-sm kind" style="width:46%">
          <option value="car">🚐 авто</option><option value="part">🪑 запчасть</option>
        </select>
        <input class="form-control form-control-sm mp" type="number" min="1" max="10" value="5" style="width:54px" title="страниц">
      </div>
      <div class="d-flex gap-1">
        <input class="form-control form-control-sm kw" placeholder="нем. фраза, напр. opel zafira life">
        <button class="btn btn-sm btn-success add">+</button>
      </div>
    </div></div>`);
  addForm.querySelector(".add").addEventListener("click", async () => {
    const kind = addForm.querySelector(".kind").value;
    const kw = addForm.querySelector(".kw").value.trim();
    const mp = parseInt(addForm.querySelector(".mp").value, 10) || 5;
    if (!kw) return;
    try {
      await api("/api/ma/scout/queries", { method: "POST", body: {
        kind, keywords: kw, category: kind === "part" ? "c223" : "c216", max_pages: mp } });
      S.overview = await api("/api/ma/scout/overview");
      renderQueriesTab(container);
    } catch (err) { alert("Ошибка: " + (err.message || err)); }
  });
  root.appendChild(addForm);

  // список запросов
  const list = el(`<div class="list-group list-group-flush"></div>`);
  ov.queries.forEach(q => {
    const item = el(`
      <div class="list-group-item py-2 px-2">
        <div class="d-flex align-items-center gap-2">
          <span class="kind"></span>
          <span class="kw flex-grow-1 text-truncate small"></span>
          <button class="btn btn-sm tg"></button>
          <button class="btn btn-sm btn-outline-primary run">▶</button>
          <button class="btn btn-sm btn-outline-danger del">🗑</button>
        </div>
        <div class="small text-muted mt-1 meta"></div>
      </div>`);
    item.querySelector(".kind").textContent = q.kind === "car" ? "🚐" : "🪑";
    item.querySelector(".kw").textContent = q.keywords;
    const tgBtn = item.querySelector(".tg");
    tgBtn.textContent = q.enabled ? "вкл" : "выкл";
    tgBtn.className = "btn btn-sm " + (q.enabled ? "btn-success" : "btn-outline-secondary");
    item.querySelector(".meta").textContent =
      `${q.category} · ${q.max_pages}стр · ${q.source || ""}` +
      (q.last_count !== null ? ` · найдено ${q.last_count}` : "") +
      (q.last_run_at ? ` · ${berlinTime(q.last_run_at)}` : "");
    if (!q.enabled) item.classList.add("opacity-50");
    tgBtn.addEventListener("click", async () => {
      try { await api(`/api/ma/scout/queries/${q.id}/toggle`, { method: "POST", body: {} });
        S.overview = await api("/api/ma/scout/overview"); renderQueriesTab(container);
      } catch (e) { alert("Ошибка: " + (e.message || e)); }
    });
    item.querySelector(".run").addEventListener("click", async () => {
      try { await api("/api/ma/scout/run", { method: "POST", body: { query_id: q.id } });
        S.overview.running = true; renderQueriesTab(container);
      } catch (e) { alert("Ошибка: " + (e.message || e)); }
    });
    item.querySelector(".del").addEventListener("click", async () => {
      if (!confirm("Удалить запрос?")) return;
      try { await api(`/api/ma/scout/queries/${q.id}/delete`, { method: "POST", body: {} });
        S.overview = await api("/api/ma/scout/overview"); renderQueriesTab(container);
      } catch (e) { alert("Ошибка: " + (e.message || e)); }
    });
    list.appendChild(item);
  });
  if (!ov.queries.length) {
    list.appendChild(el(`<div class="text-muted small fst-italic text-center py-3">Нет запросов — сгенерируйте через LLM</div>`));
  }
  root.appendChild(list);
  container.replaceChildren(root);
}

function pollStatus(statusEl) {
  api("/api/ma/scout/status").then(s => {
    if (s.running) {
      statusEl.textContent = `⏳ идёт прогон… (машин ${s.counts.cars}, з/ч ${s.counts.parts})`;
      setTimeout(() => pollStatus(statusEl), 5000);
    } else {
      // прогон завершён — сбросить кэши и перерисовать
      S.cars = null; S.parts = null;
      const sum = s.summary || {};
      statusEl.textContent = `✅ готово: машин ${s.counts.cars}, з/ч ${s.counts.parts}` +
        (sum.errors && sum.errors.length ? ` (ошибок ${sum.errors.length})` : "");
      api("/api/ma/scout/overview").then(ov => { S.overview = ov; });
    }
  }).catch(() => setTimeout(() => pollStatus(statusEl), 8000));
}

// --- основной render ---
function renderShell(mount) {
  const ov = S.overview;
  const root = el(`
    <div>
      <div class="d-flex justify-content-between align-items-center mb-2">
        <h5 class="mb-0">🔎 Разведка рынка</h5>
        <span class="small">
          <span class="badge bg-secondary me-1 cc"></span>
          <span class="badge bg-secondary pc"></span>
          <span class="badge bg-warning text-dark ms-1 uc" style="display:none"></span>
        </span>
      </div>
      <div class="small text-muted mb-2 autostat"></div>
      <ul class="nav nav-pills nav-fill mb-2 subnav">
        <li class="nav-item"><a class="nav-link py-1" data-tab="car" role="button">🚐 Машины</a></li>
        <li class="nav-item"><a class="nav-link py-1" data-tab="part" role="button">🪑 Запчасти</a></li>
        <li class="nav-item"><a class="nav-link py-1" data-tab="queries" role="button">⚙️ Запросы</a></li>
      </ul>
      <div class="tabbody"></div>
    </div>`);
  root.querySelector(".cc").textContent = `🚐 ${ov.counts.cars}`;
  root.querySelector(".pc").textContent = `🪑 ${ov.counts.parts}`;
  const uc = root.querySelector(".uc");
  if (ov.counts.unverified) { uc.style.display = ""; uc.textContent = `❓ ${ov.counts.unverified}`; }
  root.querySelector(".autostat").textContent =
    (ov.auto_enabled ? `авто-прогон каждые ${ov.interval_hours}ч` : "авто-прогон выключен") +
    (ov.counts.other ? ` · 🚫 другое: ${ov.counts.other}` : "") +
    (ov.running ? " · ⏳ идёт прогон" : "");

  const body = root.querySelector(".tabbody");
  const links = root.querySelectorAll(".subnav .nav-link");
  function paint() {
    links.forEach(a => a.classList.toggle("active", a.dataset.tab === S.tab));
  }
  function openTab(tab) {
    S.tab = tab; paint();
    if (tab === "queries") renderQueriesTab(body);
    else renderListTab(body, tab);
  }
  links.forEach(a => a.addEventListener("click", () => openTab(a.dataset.tab)));
  mount.replaceChildren(root);
  openTab(S.tab);
}

export async function render(mount, params) {
  setLoading(mount, "Загружаю разведку…");
  // сбрасываем кэши списков при заходе (overview всегда свежий)
  S.cars = null; S.parts = null;
  try {
    S.overview = await api("/api/ma/scout/overview");
  } catch (e) { setError(mount, e.message ?? String(e)); return; }
  renderShell(mount);
}
