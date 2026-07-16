// 🔎 Разведка рынка — Mini App экран.
// Подвкладки: Машины / Запчасти / Запросы. Данные из /api/ma/scout/*.
import { api } from "../api.js?v=20260716-134131";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260716-134131";
import { openLink } from "../tg.js?v=20260716-134131";

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
  // город-первая навигация
  view: { car: "cities", part: "cities" },   // cities | listings
  city: { car: null, part: null },           // выбранный город (drill-down)
  land: { car: null, part: null },           // фильтр по земле (с карты/списка)
  cityQ: { car: "", part: "" },              // поиск по названию города
  citySort: { car: "count", part: "count" }, // count | price | name
};

function eur(v) {
  if (v === null || v === undefined) return "—";
  return Math.round(v).toLocaleString("de-DE") + " €";
}

// --- карточки объявлений ---
function _cardShell(item, kind, rerender, specs) {
  const card = el(`
    <div class="list-group-item py-2">
      <div class="d-flex align-items-start">
        <a class="title fw-semibold text-truncate flex-grow-1 me-2" role="button"></a>
        <button class="btn btn-sm btn-link p-0 flag" title="пометить неверным">🚩</button>
      </div>
      <div class="small mt-1"><span class="price fw-bold"></span><span class="specs text-muted"></span></div>
      <div class="small text-muted mt-1 loc"></div>
    </div>`);
  const t = card.querySelector(".title");
  t.textContent = item.title || "(без названия)";
  t.addEventListener("click", () => item.url && openLink(item.url));
  card.querySelector(".price").textContent = eur(item.price_eur) + (item.negotiable ? " VB " : " ");
  card.querySelector(".specs").textContent = "· " + specs.join(" · ");
  card.querySelector(".loc").textContent =
    `📍 ${item.plz || ""} ${item.city || ""}${item.bundesland ? " (" + item.bundesland + ")" : ""}` +
    (item.posted_raw ? ` · ${item.posted_raw}` : "");
  const bar = corrBar(kind, item.ad_id, rerender);
  card.appendChild(bar);
  card.querySelector(".flag").addEventListener("click", () => {
    bar.style.display = bar.style.display === "none" ? "" : "none";
  });
  return card;
}

function carCard(c, rerender) {
  const specs = [];
  if (c.year) specs.push(c.year);
  if (c.fuel) specs.push(FUEL_RU[c.fuel] || c.fuel);
  if (c.gearbox) specs.push(GB_RU[c.gearbox] || c.gearbox);
  if (c.mileage_km) specs.push(Math.round(c.mileage_km).toLocaleString("de-DE") + " км");
  if (c.model_family) specs.push(c.model_family);
  return _cardShell(c, "car", rerender, specs);
}

function partCard(p, rerender) {
  const specs = [];
  if (p.part_type) specs.push(PT_RU[p.part_type] || p.part_type);
  if (p.condition) specs.push(COND_RU[p.condition] || p.condition);
  if (p.year) specs.push(p.year);
  return _cardShell(p, "part", rerender, specs);
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

// Tile-grid картограмма земель ФРГ: [row, col, аббревиатура]. Схематично geo.
const TILE_GRID = {
  "Schleswig-Holstein": [0, 1, "SH"],
  "Bremen": [1, 0, "HB"], "Hamburg": [1, 1, "HH"], "Mecklenburg-Vorpommern": [1, 2, "MV"],
  "Niedersachsen": [2, 1, "NI"], "Brandenburg": [2, 2, "BB"], "Berlin": [2, 3, "BE"],
  "Nordrhein-Westfalen": [3, 0, "NW"], "Sachsen-Anhalt": [3, 1, "ST"], "Sachsen": [3, 2, "SN"],
  "Rheinland-Pfalz": [4, 0, "RP"], "Hessen": [4, 1, "HE"], "Thüringen": [4, 2, "TH"],
  "Saarland": [5, 0, "SL"], "Baden-Württemberg": [5, 1, "BW"], "Bayern": [5, 2, "BY"],
};

// Тепловая карта: считает по землям из all, рисует tile-grid с цветом ∝ количеству.
// onPick(land) — клик по плитке (фильтр).
function heatMap(all, onPick) {
  const counts = new Map();
  all.forEach(r => { if (r.bundesland) counts.set(r.bundesland, (counts.get(r.bundesland) || 0) + 1); });
  const max = Math.max(1, ...counts.values());
  const wrap = el(`<div class="heatmap"></div>`);
  for (const [land, [row, col, ab]] of Object.entries(TILE_GRID)) {
    const n = counts.get(land) || 0;
    const t = el(`<div class="heat-tile" role="button"></div>`);
    t.style.gridRow = row + 1;
    t.style.gridColumn = col + 1;
    const intensity = n / max;
    t.style.background = n ? `rgba(239,68,68,${(0.15 + 0.85 * intensity).toFixed(3)})` : "rgba(255,255,255,0.05)";
    t.style.color = intensity > 0.5 ? "#fff" : "var(--bs-secondary-color)";
    t.title = `${land}: ${n}`;
    t.innerHTML = `<div class="ab">${ab}</div><div class="n">${n}</div>`;
    if (n) t.addEventListener("click", () => onPick(land));
    wrap.appendChild(t);
  }
  return wrap;
}

// Операторская правка: переклассифицировать / удалить. Удаляет из кэша + перерисовка.
async function correctListing(kind, adId, correctKind, rerender) {
  try {
    await api(`/api/ma/scout/listings/${encodeURIComponent(adId)}/correct`,
              { method: "POST", body: { correct_kind: correctKind } });
  } catch (e) { alert("Ошибка: " + (e.message || e)); return; }
  const arr = kind === "car" ? S.cars : S.parts;
  const idx = arr.findIndex(r => r.ad_id === adId);
  if (idx >= 0) arr.splice(idx, 1);
  // если переклассифицировали в другой вид — сбросим его кэш, чтоб подтянулся заново
  if (correctKind === "car") S.cars = null;
  if (correctKind === "part") S.parts = null;
  if (rerender) rerender();
}

// Панель правки внутри карточки (toggle по 🚩).
function corrBar(kind, adId, rerender) {
  const bar = el(`<div class="corrbar mt-1" style="display:none"></div>`);
  const mk = (label, ck, cls) => {
    const b = el(`<button class="btn btn-sm ${cls} me-1 mb-1"></button>`);
    b.textContent = label;
    b.addEventListener("click", () => correctListing(kind, adId, ck, rerender));
    return b;
  };
  if (kind === "car") bar.appendChild(mk("🪑 это запчасть", "part", "btn-outline-warning"));
  else bar.appendChild(mk("🚐 это машина", "car", "btn-outline-warning"));
  bar.appendChild(mk("🚫 не то", "other", "btn-outline-secondary"));
  bar.appendChild(mk("🗑 удалить", "remove", "btn-outline-danger"));
  return bar;
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

// Группировка объявлений по городам. Возвращает [{city, bundesland, plzText,
// plzCount, count, min, avg, max}] отсортировано по citySort.
function buildCityGroups(all, kind) {
  const land = S.land[kind];
  const q = (S.cityQ[kind] || "").trim().toLowerCase();
  const m = new Map();
  for (const r of all) {
    if (!r.city) continue;
    if (land && r.bundesland !== land) continue;
    if (q && !r.city.toLowerCase().includes(q)) continue;
    let g = m.get(r.city);
    if (!g) { g = { city: r.city, bundesland: r.bundesland, plz: new Set(), count: 0, sum: 0, n: 0, min: null, max: null }; m.set(r.city, g); }
    g.count++;
    if (r.plz) g.plz.add(r.plz);
    if (r.price_eur != null) {
      g.sum += r.price_eur; g.n++;
      g.min = g.min == null ? r.price_eur : Math.min(g.min, r.price_eur);
      g.max = g.max == null ? r.price_eur : Math.max(g.max, r.price_eur);
    }
  }
  const groups = [...m.values()].map(g => {
    const plzArr = [...g.plz].sort();
    return {
      city: g.city, bundesland: g.bundesland, count: g.count,
      plzText: plzArr.length <= 3 ? plzArr.join(", ") : `${plzArr.length} индексов`,
      plzCount: plzArr.length,
      min: g.min, avg: g.n ? g.sum / g.n : null, max: g.max,
    };
  });
  const s = S.citySort[kind];
  groups.sort((a, b) => {
    if (s === "name") return a.city.localeCompare(b.city);
    if (s === "price") return (a.avg ?? Infinity) - (b.avg ?? Infinity);
    return b.count - a.count;  // count (default)
  });
  return groups;
}

// Группировка по землям: [{land, count, min, avg, max, cities}] сорт. по count.
function buildLandGroups(all) {
  const m = new Map();
  for (const r of all) {
    const land = r.bundesland || "— неизвестно —";
    let g = m.get(land);
    if (!g) { g = { land, count: 0, sum: 0, n: 0, min: null, max: null, cities: new Set() }; m.set(land, g); }
    g.count++;
    if (r.city) g.cities.add(r.city);
    if (r.price_eur != null) {
      g.sum += r.price_eur; g.n++;
      g.min = g.min == null ? r.price_eur : Math.min(g.min, r.price_eur);
      g.max = g.max == null ? r.price_eur : Math.max(g.max, r.price_eur);
    }
  }
  return [...m.values()].map(g => ({
    land: g.land, count: g.count, cities: g.cities.size,
    min: g.min, avg: g.n ? g.sum / g.n : null, max: g.max,
  })).sort((a, b) => b.count - a.count);
}

// --- подвкладка списка (car|part): земля → города → объявления ---
async function renderListTab(container, kind) {
  if ((kind === "car" ? S.cars : S.parts) === null) {
    container.replaceChildren(el(`<p class="text-muted py-3">Загружаю…</p>`));
    try {
      const data = await api(`/api/ma/scout/listings?kind=${kind}`);
      if (kind === "car") S.cars = data.listings; else S.parts = data.listings;
    } catch (e) { setError(container, e.message ?? String(e)); return; }
  }
  const v = S.view[kind];
  if (v === "listings" && S.city[kind]) renderCityDrill(container, kind);
  else if (v === "cities" && S.land[kind]) renderCitiesView(container, kind);
  else renderLandsView(container, kind);
}

// Уровень 1: список ЗЕМЕЛЬ + кликабельная тепловая карта.
function renderLandsView(container, kind) {
  const all = kind === "car" ? S.cars : S.parts;
  const root = el(`<div></div>`);
  const open = (land) => { S.land[kind] = land; S.view[kind] = "cities"; S.cityQ[kind] = ""; renderCitiesView(container, kind); };

  const heatDet = el(`<details class="mb-2" open><summary class="small text-muted">🗺 Тепловая карта — нажми землю</summary></details>`);
  heatDet.appendChild(heatMap(all, open));
  root.appendChild(heatDet);

  const groups = buildLandGroups(all);
  const head = el(`<div class="small text-muted mb-1"></div>`);
  head.textContent = `земель: ${groups.length} · объявлений: ${all.length}`;
  root.appendChild(head);

  const list = el(`<div class="list-group list-group-flush"></div>`);
  groups.forEach(g => {
    const row = el(`
      <a class="list-group-item list-group-item-action py-2" role="button">
        <div class="d-flex justify-content-between align-items-start">
          <div class="flex-grow-1 me-2">
            <div class="fw-semibold land"></div>
            <div class="small text-muted sub"></div>
          </div>
          <div class="text-end">
            <div><span class="badge bg-secondary cnt"></span></div>
            <div class="small text-muted price mt-1"></div>
          </div>
        </div>
      </a>`);
    row.querySelector(".land").textContent = `🗺 ${g.land}`;
    row.querySelector(".sub").textContent = `${g.cities} городов`;
    row.querySelector(".cnt").textContent = g.count;
    row.querySelector(".price").textContent = g.min != null ? `${eur(g.min)}–${eur(g.max)}` : "";
    if (g.land !== "— неизвестно —") row.addEventListener("click", () => open(g.land));
    else row.classList.add("disabled");
    list.appendChild(row);
  });
  root.appendChild(list);
  container.replaceChildren(root);
}

// Уровень 2: список ГОРОДОВ внутри выбранной земли (с PLZ, счётчиками, ценами).
function renderCitiesView(container, kind) {
  const all = kind === "car" ? S.cars : S.parts;
  const root = el(`<div></div>`);

  const back = el(`<button class="btn btn-sm btn-outline-secondary mb-2">← все земли</button>`);
  back.addEventListener("click", () => { S.view[kind] = "lands"; S.land[kind] = null; renderLandsView(container, kind); });
  root.appendChild(back);

  const title = el(`<h6 class="mb-2 t"></h6>`);
  title.textContent = `🗺 ${S.land[kind]}`;
  root.appendChild(title);

  // строка управления: поиск города + сортировка
  const ctrl = el(`<div class="d-flex flex-wrap gap-1 mb-2 align-items-center"></div>`);
  const search = el(`<input type="search" class="form-control form-control-sm" placeholder="🔎 поиск города…" style="min-width:120px;flex:1 1 auto">`);
  search.value = S.cityQ[kind] || "";
  search.addEventListener("input", () => { S.cityQ[kind] = search.value; refresh(); });
  ctrl.appendChild(search);
  const sortSel = selectFilter("сортировка", [
    { value: "count", text: "по количеству" }, { value: "price", text: "по цене" },
    { value: "name", text: "А-Я" },
  ], S.citySort[kind], v => { S.citySort[kind] = v || "count"; refresh(); });
  sortSel.options[0].textContent = "сортировка";
  ctrl.appendChild(sortSel);
  root.appendChild(ctrl);

  const head = el(`<div class="small text-muted mb-1 head"></div>`);
  root.appendChild(head);
  const list = el(`<div class="list-group list-group-flush"></div>`);
  root.appendChild(list);

  function refresh() {
    const groups = buildCityGroups(all, kind);
    head.textContent = `городов: ${groups.length} · объявлений: ${groups.reduce((s, g) => s + g.count, 0)}`;
    list.replaceChildren();
    if (!groups.length) {
      list.appendChild(el(`<div class="text-muted small fst-italic text-center py-3">Нет данных</div>`));
      return;
    }
    groups.forEach(g => {
      const row = el(`
        <a class="list-group-item list-group-item-action py-2" role="button">
          <div class="d-flex justify-content-between align-items-start">
            <div class="flex-grow-1 me-2">
              <div class="fw-semibold city"></div>
              <div class="small text-muted plz"></div>
            </div>
            <div class="text-end">
              <div><span class="badge bg-secondary cnt"></span></div>
              <div class="small text-muted price mt-1"></div>
            </div>
          </div>
        </a>`);
      row.querySelector(".city").textContent = `🏙 ${g.city}`;
      row.querySelector(".plz").textContent = g.plzText;
      row.querySelector(".cnt").textContent = g.count;
      row.querySelector(".price").textContent =
        g.min != null ? `${eur(g.min)}–${eur(g.max)}` : "";
      row.addEventListener("click", () => {
        S.city[kind] = g.city; S.view[kind] = "listings";
        renderCityDrill(container, kind);
      });
      list.appendChild(row);
    });
  }
  refresh();
  container.replaceChildren(root);
}

// Экран объявлений ВНУТРИ города (drill-down).
function renderCityDrill(container, kind) {
  const all = kind === "car" ? S.cars : S.parts;
  const city = S.city[kind];
  const root = el(`<div></div>`);

  const back = el(`<button class="btn btn-sm btn-outline-secondary mb-2 backc">← все города</button>`);
  back.addEventListener("click", () => { S.view[kind] = "cities"; S.city[kind] = null; renderCitiesView(container, kind); });
  root.appendChild(back);

  const title = el(`<div class="d-flex justify-content-between align-items-center mb-2"><h6 class="mb-0 t"></h6></div>`);
  title.querySelector(".t").textContent = `🏙 ${city}`;
  root.appendChild(title);

  // сортировка + (для машин) фильтры топливо/КПП/модель
  const ctrl = el(`<div class="d-flex flex-wrap gap-1 mb-2"></div>`);
  const listEl = el(`<div></div>`);
  const f = S.filters[kind];
  // в drill используем те же фильтры, но город фиксирован
  const sortSel = selectFilter("сортировка", [
    { value: "price_asc", text: "цена ↑" }, { value: "price_desc", text: "цена ↓" },
    { value: "year_desc", text: "год ↓" }, { value: "year_asc", text: "год ↑" },
  ], S.sort[kind], v => { S.sort[kind] = v || "price_asc"; draw(); });
  sortSel.options[0].textContent = "сортировка";
  ctrl.appendChild(sortSel);
  if (kind === "car") {
    const models = uniq(all.filter(r => r.city === city).map(r => r.model_family)).map(v => ({ value: v, text: v }));
    ctrl.appendChild(selectFilter("модель", models, f.model_family || "", v => { f.model_family = v; draw(); }));
    ctrl.appendChild(selectFilter("топливо", Object.entries(FUEL_RU).map(([value, text]) => ({ value, text })), f.fuel || "", v => { f.fuel = v; draw(); }));
  }
  root.appendChild(ctrl);
  root.appendChild(listEl);

  function draw() {
    let rows = all.filter(r => r.city === city);
    if (kind === "car") {
      if (f.model_family) rows = rows.filter(r => r.model_family === f.model_family);
      if (f.fuel) rows = rows.filter(r => r.fuel === f.fuel);
    }
    const sort = S.sort[kind];
    const num = v => v == null ? Infinity : v, numD = v => v == null ? -Infinity : v;
    rows.sort((a, b) => sort === "price_desc" ? numD(b.price_eur) - numD(a.price_eur)
      : sort === "year_desc" ? numD(b.year) - numD(a.year)
      : sort === "year_asc" ? num(a.year) - num(b.year)
      : num(a.price_eur) - num(b.price_eur));
    listEl.replaceChildren();
    const cnt = el(`<div class="text-muted small px-1 mb-1"></div>`);
    cnt.textContent = `объявлений: ${rows.length}`;
    listEl.appendChild(cnt);
    if (!rows.length) {  // город опустел (все поправлены) → назад к городам
      S.view[kind] = "cities"; S.city[kind] = null; renderCitiesView(container, kind); return;
    }
    const group = el(`<div class="list-group list-group-flush"></div>`);
    rows.forEach(r => group.appendChild(kind === "car" ? carCard(r, draw) : partCard(r, draw)));
    listEl.appendChild(group);
  }
  draw();
  container.replaceChildren(root);
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
      <div class="d-flex justify-content-end align-items-center mb-2">
        <span class="small">
          <span class="badge bg-secondary me-1 cc"></span>
          <span class="badge bg-secondary pc"></span>
          <span class="badge bg-warning ms-1 uc" style="display:none"></span>
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
  // стартуем со списка земель (земля → города → объявления)
  S.view = { car: "lands", part: "lands" };
  S.land = { car: null, part: null };
  S.city = { car: null, part: null };
  try {
    S.overview = await api("/api/ma/scout/overview");
  } catch (e) { setError(mount, e.message ?? String(e)); return; }
  renderShell(mount);
}
