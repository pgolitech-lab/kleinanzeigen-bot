// 📦 Склад (контракт docs/optimization/stock.md): товар ≠ объявление.
//   #/stock            — итоги + товары (залежалые сверху), «+ приход» строкой,
//                        плашки «тот же товар?» и «продажи без товара»;
//   #/stock/{id}       — карточка товара: объявления, движения, действия;
//   #/stock/questions  — очередь «это тот же товар?» (Да / Нет);
//   #/stock/month      — отчёт за месяц.
// Неизвестные деньги/остаток (None) показываем как «—», не как 0.
import { api } from "../api.js";
import { el, setLoading, setError, eur, berlinTime, parseEur } from "../utils.js";
import { openSheet, closeSheet, menuSheet, confirmSheet } from "../components/sheet.js";
import { setTitle } from "../components/backbar.js";
import { icon, toast } from "../components/kit.js";
import { adviceSheet, tipsSheet } from "./market.js";

const money = v => (v == null ? "—" : eur(Math.round(v)));
const days = v => (v == null ? "—" : `${Math.round(v)} дн.`);

// Эндпоинты склада выкатывает api — пока их нет, честно говорим об этом
function notReady(mount, e) {
  if (e?.status === 404 && (!e.detail || e.detail === "Not Found")) {
    const box = el(`<div class="empty"><p></p></div>`);
    box.prepend(icon("cards"));
    box.querySelector("p").textContent = "Склад ещё не подключён на сервере — экран заработает, как только api выкатит /api/ma/stock.";
    mount.replaceChildren(box);
    return true;
  }
  return false;
}

export async function render(mount, params = {}) {
  if (params.view === "questions") return renderQuestions(mount);
  if (params.view === "month") return renderMonth(mount);
  if (params.view === "types") return renderTypes(mount);
  if (params.id) return renderProduct(mount, params.id);
  return renderList(mount);
}

// ================= список =================

async function renderList(mount) {
  setLoading(mount, "Считаю склад…");
  let rep;
  try {
    rep = await api("/api/ma/stock/report");
  } catch (e) {
    if (!mount.isConnected) return;
    if (!notReady(mount, e)) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const T = rep.totals ?? {};
  const items = (rep.products ?? []).filter(p => !p.archived);
  const root = el(`
    <div class="stock">
      <div class="st-hero">
        <div class="st-k"><b class="v-qty"></b><small>на складе, шт.</small></div>
        <div class="st-k"><b class="v-cost"></b><small>вложено</small></div>
        <div class="st-k"><b class="v-rev"></b><small>ожидаемо</small></div>
        <div class="st-k"><b class="v-prof"></b><small>прибыль</small></div>
      </div>
      <div class="st-banners"></div>
      <div class="st-actions">
        <button class="btn btn-primary st-in" type="button">➕ Приход</button>
        <a class="btn st-month" href="#/stock/month">📊 Отчёт за месяц</a>
        <a class="btn st-market" href="#/stock/market">🧭 Дубли и перевыкладывание</a>
        <a class="btn st-types" href="#/stock/types">🏷 Типы и себестоимость</a>
      </div>
      <input type="search" class="form-control st-q" placeholder="Товар…" aria-label="Поиск товара">
      <div class="lsec"><span>Товары</span><i></i></div>
      <div class="st-list"></div>
    </div>`);
  root.querySelector(".v-qty").textContent = T.tracked ? String(T.qty ?? 0) : "—";
  root.querySelector(".v-cost").textContent = money(T.stock_cost);
  root.querySelector(".v-rev").textContent = money(T.expected_revenue_list);
  root.querySelector(".v-prof").textContent = money(T.expected_profit_list);
  if (!T.tracked) {
    // пустой склад: вместо плитки из прочерков — одна крупная кнопка
    const hero = root.querySelector(".st-hero");
    const first = el(`<div class="st-first"><p>Остатки ещё не внесены.</p><p class="small">Внесите первый приход — сколько штук и за сколько куплено. Тогда склад покажет вложения и прибыль.</p><button class="btn btn-primary st-first-b" type="button">➕ Первый приход</button></div>`);
    hero.replaceWith(first);
    first.querySelector(".st-first-b").addEventListener("click", () => receiptSheet(items, () => renderList(mount)));
  }
  const banners = root.querySelector(".st-banners");
  if (T.open_questions) banners.appendChild(banner("#/stock/questions", `❓ Тот же товар? Вопросов: ${T.open_questions}`, "warn"));
  if (T.unlinked_sales) banners.appendChild(banner("#/stock/questions", `🧾 Продажи без товара: ${T.unlinked_sales}`, ""));
  if (T.stale) banners.appendChild(banner(null, `🐌 Залежалось больше 60 дней: ${T.stale}`, ""));

  root.querySelector(".st-in").addEventListener("click", () => receiptSheet(items, () => renderList(mount)));
  const list = root.querySelector(".st-list");
  const q = root.querySelector(".st-q");
  const paint = () => {
    const term = q.value.trim().toLowerCase();
    const shown = term ? items.filter(p => (p.title || "").toLowerCase().includes(term)) : items;
    root.querySelector(".lsec i").textContent = String(shown.length);
    list.replaceChildren(...shown.map(productRow));
    if (!shown.length) list.appendChild(el(`<div class="empty"><p>Товаров не найдено</p></div>`));
  };
  q.addEventListener("input", paint);
  paint();
  mount.replaceChildren(root);
}

function banner(href, text, cls) {
  const b = el(href ? `<a class="st-banner"></a>` : `<div class="st-banner"></div>`);
  if (href) b.href = href;
  if (cls) b.classList.add(cls);
  b.textContent = text;
  return b;
}

function productRow(p) {
  const a = el(`
    <a class="prow">
      <span class="pr-qty"></span>
      <span class="pr-main"><b></b><small></small></span>
      <span class="pr-side"><b></b><small></small></span>
    </a>`);
  a.href = `#/stock/${p.product_id}`;
  a.dataset.key = String(p.product_id);
  const qty = a.querySelector(".pr-qty");
  if (!p.stock_tracked) { qty.textContent = "?"; qty.classList.add("unk"); qty.title = "остаток не внесён"; }
  else if (p.negative_stock) { qty.textContent = "!"; qty.classList.add("bad"); qty.title = "минусовой остаток — проверьте приход"; }
  else { qty.textContent = String(p.qty ?? 0); if (!p.qty) qty.classList.add("zero"); }
  a.querySelector(".pr-main b").textContent = p.title;
  const meta = [];
  if (p.listings) meta.push(`объявлений ${p.listings}`);
  if (p.listed_days != null) meta.push(`висит ${days(p.listed_days)}`);
  if (p.stale) meta.push("🐌 залежалось");
  a.querySelector(".pr-main small").textContent = meta.join(" · ");
  a.querySelector(".pr-side b").textContent = money(p.list_price_eur);
  a.querySelector(".pr-side small").textContent = p.unit_cost_eur != null ? `себест. ${money(p.unit_cost_eur)}` : "";
  return a;
}

// ---------- «+ приход» одной строкой ----------
// «3 скамейки T6, 2 столика, 3 VIP за 1350» → позиции (кол-во + название) и
// общая сумма. Сначала просим разобрать сервер (/stock/receipt/parse, если
// api его сделает), иначе разбираем сами и даём выбрать товар по каждой строке.
function parseReceipt(text) {
  let total = null;
  const m = text.match(/\bза\s*(\d[\d\s.,]*)\s*(€|eur|евро)?\s*$/i);
  let body = text;
  // «за 1.350» / «за 1 350» — немецкая запись тысяч (parseEur), не 1,35 €
  if (m) { total = parseEur(m[1].replace(/\s/g, "")); body = text.slice(0, m.index); }
  const lines = body.split(/[,;\n]|\sи\s/).map(s => s.trim()).filter(Boolean).map(s => {
    const q = s.match(/^(\d+)\s*(шт\.?|x|×)?\s*(.+)$/i);
    return q ? { qty: Number(q[1]), name: q[3].trim() } : { qty: 1, name: s };
  });
  return { lines, total: Number.isFinite(total) ? total : null };
}

function guessProduct(name, products) {
  const toks = name.toLowerCase().split(/[^\p{L}\p{N}+]+/u).filter(t => t.length > 1);
  let best = null, bestScore = 0;
  products.forEach(p => {
    const t = (p.title || "").toLowerCase();
    const score = toks.reduce((s, k) => s + (t.includes(k) ? (/\d/.test(k) ? 3 : 1) : 0), 0);
    if (score > bestScore) { best = p; bestScore = score; }
  });
  return bestScore > 0 ? best : null;
}

function receiptSheet(products, onDone) {
  const box = el(`
    <div class="rcpt">
      <input type="text" class="form-control r-text" placeholder="3 скамейки T6, 2 столика, 3 VIP за 1350">
      <button class="btn r-parse" type="button">Разобрать</button>
      <div class="r-lines"></div>
      <div class="r-totalrow" hidden>
        <label class="form-label small">Вся партия стоила, € (необязательно)</label>
        <input type="text" inputmode="decimal" class="form-control r-total" placeholder="например 1350">
        <small class="r-hint">Разделим по позициям пропорционально цене объявлений. Пусто — себестоимость неизвестна.</small>
      </div>
      <div class="form-error text-danger small" hidden></div>
      <div class="sheet-actions">
        <button class="btn cancel-btn" type="button">Отмена</button>
        <button class="btn btn-primary save-btn" type="button" disabled>✅ Оприходовать</button>
      </div>
    </div>`);
  const linesEl = box.querySelector(".r-lines"), err = box.querySelector(".form-error"), save = box.querySelector(".save-btn");
  let rows = [];
  box.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  box.querySelector(".r-parse").addEventListener("click", async () => {
    const text = box.querySelector(".r-text").value.trim();
    if (!text) return;
    err.hidden = true;
    let parsed = null;
    try {
      parsed = await api("/api/ma/stock/receipt/parse", { method: "POST", body: {text} });
    } catch (e) {
      if (e.status !== 404 && e.status !== 405) { err.textContent = e.message ?? String(e); err.hidden = false; }
    }
    if (Array.isArray(parsed?.items)) {
      rows = parsed.items.map(x => ({ qty: x.qty, name: x.name ?? x.title ?? "", product_id: x.product_id ?? null }));
      if (parsed.total_cost_eur != null) box.querySelector(".r-total").value = String(parsed.total_cost_eur);
    } else {
      const { lines, total } = parseReceipt(text);
      rows = lines.map(l => ({ ...l, product_id: guessProduct(l.name, products)?.product_id ?? null }));
      if (total != null) box.querySelector(".r-total").value = String(total);
    }
    box.querySelector(".r-totalrow").hidden = !rows.length;
    paintRows();
  });
  function paintRows() {
    linesEl.replaceChildren();
    rows.forEach(r => {
      const row = el(`
        <div class="r-row">
          <div class="r-src"></div>
          <div class="r-ctl">
            <input type="number" min="1" step="1" class="form-control r-qty" aria-label="Количество">
            <button type="button" class="fchip r-prod"></button>
          </div>
        </div>`);
      row.querySelector(".r-src").textContent = `«${r.name}»`;
      const qIn = row.querySelector(".r-qty"), pb = row.querySelector(".r-prod");
      qIn.value = String(r.qty);
      qIn.addEventListener("input", () => { r.qty = Number(qIn.value); sync(); });
      const p = products.find(x => x.product_id === r.product_id);
      pb.textContent = p ? `${p.title} ▾` : "Выбрать товар ▾";
      pb.classList.toggle("on", !!p);
      pb.addEventListener("click", () => pickProduct(r));
      linesEl.appendChild(row);
    });
    sync();
  }
  // выбор товара — во вложенном меню; после выбора возвращаемся в этот же лист
  function pickProduct(r) {
    const guess = guessProduct(r.name, products);
    const sorted = guess ? [guess, ...products.filter(p => p !== guess)] : products;
    menuSheet([
      { label: "↩ Назад к приходу", onClick: () => openSheet(box, "➕ Приход") },
      ...sorted.slice(0, 80).map(p => ({ label: p.title, onClick: () => { r.product_id = p.product_id; openSheet(box, "➕ Приход"); paintRows(); } })),
    ], "Какой товар?");
  }
  function sync() { save.disabled = !rows.length || rows.some(r => !r.product_id || !(r.qty > 0)); }
  save.addEventListener("click", async () => {
    err.hidden = true;
    const rawTotal = box.querySelector(".r-total").value.trim();
    const total = rawTotal === "" ? null : parseEur(rawTotal);
    if (rawTotal !== "" && (total == null || total < 0)) { err.textContent = "Сумма партии — число, например 1350"; err.hidden = false; return; }
    // один товар в двух строках — складываем штуки (сервер дубль не принимает)
    const byProd = new Map();
    rows.forEach(r => byProd.set(r.product_id, (byProd.get(r.product_id) ?? 0) + r.qty));
    save.disabled = true;
    save.textContent = "⏳ Записываю…";
    try {
      // партия целиком или ничего: частичного прихода не бывает
      await api("/api/ma/stock/batches", { method: "POST", body: {
        items: [...byProd].map(([product_id, qty]) => ({ product_id, qty })), total_cost_eur: total } });
      closeSheet();
      toast(`✅ Оприходовано позиций: ${byProd.size}${total != null ? ` на ${eur(total)}` : ""}`);
      onDone();
    } catch (e) {
      err.textContent = e.message ?? String(e);
      err.hidden = false;
      save.textContent = "✅ Оприходовать";
      sync();
    }
  });
  openSheet(box, "➕ Приход");
  box.querySelector(".r-text").focus();
}

// ================= карточка товара =================

async function renderProduct(mount, id) {
  setLoading(mount, "Открываю товар…");
  let d;
  try {
    d = await api(`/api/ma/stock/products/${encodeURIComponent(id)}`);
  } catch (e) {
    if (!mount.isConnected) return;
    // 404 «Not Found» без detail — эндпоинта нет; с detail — товар не найден
    if (e.status === 404 && e.detail && e.detail !== "Not Found") setError(mount, `Товар не найден: ${e.detail}`);
    else if (!notReady(mount, e)) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const p = d.product ?? d;
  const ctx = d.context ?? d.ad_context ?? {};
  setTitle(p.title ?? "Товар");
  const qty = p.qty ?? ctx.qty ?? null;
  const tracked = !!(p.stock_tracked ?? ctx.stock_tracked);
  const root = el(`
    <div class="product">
      <div class="pd-hero"><div class="pd-qty"></div><h2></h2><div class="pd-sub"></div></div>
      <div class="c-tiles"></div>
      <div class="pd-acts"></div>
      <section class="c-sec"><h3>Объявления</h3><div class="pd-list"></div></section>
      <section class="c-sec"><h3>Движения</h3><div class="pd-moves"></div></section>
    </div>`);
  const qEl = root.querySelector(".pd-qty");
  qEl.textContent = tracked ? `${qty ?? 0} шт.` : "остаток не внесён";
  if (!tracked) qEl.classList.add("unk");
  root.querySelector("h2").textContent = p.title ?? "";
  root.querySelector(".pd-sub").textContent = [p.category, p.condition].filter(Boolean).join(" · ");
  if (ctx.kit && (ctx.components ?? []).length) {
    root.querySelector(".pd-sub").textContent += ` · комплект из ${ctx.components.length} поз.`;
  }
  const tiles = root.querySelector(".c-tiles");
  [["цена", money(p.list_price_eur)], ["минимум", money(p.min_price_eur)], ["себестоимость", money(p.unit_cost_eur)]].forEach(([l, v]) => {
    const t = el(`<div class="c-tile"><b></b><small></small></div>`);
    t.querySelector("b").textContent = v;
    t.querySelector("small").textContent = l;
    tiles.appendChild(t);
  });
  const acts = root.querySelector(".pd-acts");
  const act = (label, fn, cls = "") => {
    const b = el(`<button class="btn" type="button"></button>`);
    b.textContent = label;
    if (cls) b.classList.add(cls);
    b.addEventListener("click", fn);
    acts.appendChild(b);
  };
  const reload = () => renderProduct(mount, id);
  act("➕ Приход", () => numberSheet("Приход", [["qty", "Сколько штук", 1], ["unit_cost_eur", "Себестоимость за штуку, € (необязательно)", null]],
    v => api(`/api/ma/stock/products/${id}/receipt`, { method: "POST", body: v }), reload), "btn-primary");
  act("📋 Инвентаризация", () => numberSheet("Сколько реально лежит", [["qty", "Штук на складе", qty ?? 0]],
    v => api(`/api/ma/stock/products/${id}/count`, { method: "POST", body: v }), reload));
  act("➖ Списать", () => numberSheet("Списание", [["qty", "Сколько штук", 1]],
    v => api(`/api/ma/stock/products/${id}/writeoff`, { method: "POST", body: v }), reload));
  act("✏️ Цены", () => numberSheet("Цены товара", [["list_price_eur", "Цена в объявлениях, €", p.list_price_eur], ["min_price_eur", "Минимум, €", p.min_price_eur], ["unit_cost_eur", "Себестоимость, €", p.unit_cost_eur]],
    v => api(`/api/ma/stock/products/${id}`, { method: "PATCH", body: v }), reload, true));
  act("💶 Цена и рынок", () => adviceSheet(id));
  act("👀 Как стать заметнее", () => tipsSheet(id));
  act(`🧩 ${ctx.kit ? "Состав комплекта" : "Комплект…"}`, () => kitSheet(id, ctx.components ?? [], reload));
  act("🙈 Не запчасть", async () => {
    if (!(await confirmSheet("Скрыть товар со склада (гараж, личное, не запчасть)? Из отчётов он пропадёт, переписка останется.", "Скрыть"))) return;
    try { await api(`/api/ma/stock/products/${id}/non-stock`, { method: "POST", body: { non_stock: true } }); toast("Скрыт со склада"); location.hash = "#/stock"; }
    catch (e) { toast(`Ошибка: ${e.message ?? e}`, { level: "bad" }); }
  });
  act("🗄 В архив", async () => {
    if (!(await confirmSheet("Убрать товар в архив?", "В архив"))) return;
    try { await api(`/api/ma/stock/products/${id}/archive`, { method: "POST", body: { archived: true } }); location.hash = "#/stock"; }
    catch (e) { toast(`Ошибка: ${e.message ?? e}`, { level: "bad" }); }
  });

  const list = root.querySelector(".pd-list");
  (d.listings ?? []).forEach(l => {
    const a = el(`<a class="cthread" target="_blank" rel="noopener"><span class="ct-1"><b></b><span class="ct-price"></span></span><span class="ct-2"><span class="ct-st"></span><span class="ct-brief"></span></span></a>`);
    if (l.url) a.href = l.url; else a.removeAttribute("target");
    a.querySelector("b").textContent = l.title ?? l.ad_id;
    a.querySelector(".ct-price").textContent = money(l.price_eur);
    const st = a.querySelector(".ct-st");
    st.textContent = { active: "активно", deleted: "удалено", reserved: "резерв" }[l.state] ?? (l.state ?? "");
    if (l.state === "active") st.classList.add("good");
    a.querySelector(".ct-brief").textContent = [l.account_name ?? (l.account_id ? `аккаунт ${l.account_id}` : ""), l.first_seen_at ? `с ${berlinTime(l.first_seen_at).split(",")[0]}` : ""].filter(Boolean).join(" · ");
    list.appendChild(a);
  });
  if (!list.children.length) list.appendChild(el(`<div class="empty"><p>Объявлений нет</p></div>`));
  const moves = root.querySelector(".pd-moves");
  const KIND = { receipt: "приход", sale: "продажа", writeoff: "списание", adjust: "инвентаризация" };
  (d.movements ?? []).forEach(m => {
    const r = el(`<div class="mv"><span class="mv-q"></span><span class="mv-t"></span><time></time></div>`);
    r.querySelector(".mv-q").textContent = `${m.qty > 0 ? "+" : ""}${m.qty}`;
    r.querySelector(".mv-q").classList.add(m.qty > 0 ? "good" : "bad");
    const price = m.kind === "sale" ? m.unit_price_eur : m.unit_cost_eur;
    r.querySelector(".mv-t").textContent = `${KIND[m.kind] ?? m.kind}${price != null ? ` · ${money(price)}` : ""}${m.note ? ` · ${m.note}` : ""}`;
    r.querySelector("time").textContent = m.occurred_at ? berlinTime(m.occurred_at).split(",")[0] : "";
    if (m.voided_at) r.classList.add("void");
    moves.appendChild(r);
  });
  if (!moves.children.length) moves.appendChild(el(`<div class="empty"><p>Движений нет</p></div>`));
  mount.replaceChildren(root);
}

// Лист с числовыми полями; fields = [[key, label, initial]]
function numberSheet(title, fields, submit, onDone, clearable = false) {
  const box = el(`<div class="num-form"><div class="nf"></div><div class="form-error text-danger small" hidden></div>
    <div class="sheet-actions"><button class="btn cancel-btn" type="button">Отмена</button><button class="btn btn-primary save-btn" type="button">✅ Сохранить</button></div></div>`);
  const nf = box.querySelector(".nf");
  fields.forEach(([k, label, init]) => {
    const l = el(`<label class="form-label small"></label>`);
    l.textContent = label;
    const i = el(`<input type="number" inputmode="decimal" min="0" step="1" class="form-control">`);
    i.dataset.k = k;
    if (init != null) i.value = String(init);
    nf.append(l, i);
  });
  const err = box.querySelector(".form-error");
  box.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  box.querySelector(".save-btn").addEventListener("click", async e => {
    const body = {};
    for (const i of nf.querySelectorAll("input")) {
      if (i.value === "") { if (clearable) body[i.dataset.k] = null; continue; }
      const v = Number(i.value);
      if (!Number.isFinite(v) || v < 0) { i.classList.add("is-invalid"); return; }
      body[i.dataset.k] = v;
    }
    if (!Object.keys(body).length) { closeSheet(); return; }
    e.currentTarget.disabled = true;
    try {
      await submit(body);
      closeSheet();
      toast("✅ Сохранено");
      onDone();
    } catch (ex) {
      err.textContent = ex.message ?? String(ex);
      err.hidden = false;
      e.currentTarget.disabled = false;
    }
  });
  openSheet(box, title);
}

// ================= «тот же товар?» =================

async function renderQuestions(mount) {
  setLoading(mount, "Загружаю вопросы…");
  let qs, unl;
  try {
    [qs, unl] = await Promise.all([
      api("/api/ma/stock/questions"),
      api("/api/ma/stock/unlinked-sales").catch(e => { console.warn("[stock] unlinked-sales:", e); return { error: e }; }),
    ]);
  } catch (e) {
    if (!mount.isConnected) return;
    if (!notReady(mount, e)) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const items = Array.isArray(qs) ? qs : (qs?.items ?? qs?.questions ?? []);
  const root = el(`<div class="stockq"><div class="lsec"><span>Это тот же товар?</span><i></i></div><div class="q-list"></div></div>`);
  root.querySelector(".lsec i").textContent = String(items.length);
  const list = root.querySelector(".q-list");
  items.forEach(q => {
    const c = el(`
      <article class="qcard">
        <div class="q-pair"><div class="q-a"><small>Объявление</small><b></b><span></span></div><div class="q-vs">=?</div><div class="q-b"><small>Товар на складе</small><b></b><span></span></div></div>
        <div class="q-why"></div>
        <div class="q-acts"><button class="btn q-no" type="button">Нет, разные</button><button class="btn btn-primary q-yes" type="button">Да, тот же</button></div>
      </article>`);
    c.querySelector(".q-a b").textContent = q.ad_title ?? q.ad_id;
    c.querySelector(".q-a span").textContent = money(q.ad_price_eur);
    c.querySelector(".q-b b").textContent = q.suggested_title ?? "?";
    c.querySelector(".q-b span").textContent = money(q.suggested_price_eur);
    c.querySelector(".q-why").textContent = q.reason ?? "";
    const answer = async same => {
      c.querySelectorAll("button").forEach(b => { b.disabled = true; });
      try {
        await api(`/api/ma/stock/questions/${q.id}`, { method: "POST", body: { same } });
        c.remove();
        const left = list.querySelectorAll(".qcard").length;
        root.querySelector(".lsec i").textContent = String(left);
        if (!left) list.appendChild(el(`<div class="empty"><p>Вопросов нет — склад разобран.</p></div>`));
      } catch (e) {
        toast(`Ошибка: ${e.message ?? e}`, { level: "bad" });
        c.querySelectorAll("button").forEach(b => { b.disabled = false; });
      }
    };
    c.querySelector(".q-yes").addEventListener("click", () => answer(true));
    c.querySelector(".q-no").addEventListener("click", () => answer(false));
    list.appendChild(c);
  });
  if (!items.length) list.appendChild(el(`<div class="empty"><p>Вопросов нет — склад разобран.</p></div>`));
  if (unl?.error) root.appendChild(el(`<div class="st-note">Список продаж без товара сейчас недоступен.</div>`));
  const unlinked = Array.isArray(unl) ? unl : (unl?.items ?? []);
  if (unlinked.length) {
    const sec = el(`<section class="c-sec"><h3>Продажи без товара</h3><div class="u-list"></div><small class="muted">Привяжите в карточке товара или через переписку.</small></section>`);
    unlinked.forEach(u => {
      const a = el(`<a class="cthread"><span class="ct-1"><b></b><span class="ct-price"></span></span></a>`);
      if (u.thread_id) a.href = `#/thread/${encodeURIComponent(u.thread_id)}`;
      a.querySelector("b").textContent = u.ad_title ?? u.title ?? "?";
      a.querySelector(".ct-price").textContent = money(u.price_eur ?? u.sold_price_eur);
      sec.querySelector(".u-list").appendChild(a);
    });
    root.appendChild(sec);
  }
  mount.replaceChildren(root);
}

// ================= отчёт за месяц =================

let _monthSeq = 0;

async function renderMonth(mount, ym = null) {
  const seq = ++_monthSeq;
  const now = new Date();
  const [y, m] = ym ?? [now.getFullYear(), now.getMonth() + 1];
  setLoading(mount, "Считаю месяц…");
  let r;
  try {
    r = await api(`/api/ma/stock/month?year=${y}&month=${m}`);
  } catch (e) {
    if (!mount.isConnected || seq !== _monthSeq) return;
    if (!notReady(mount, e)) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected || seq !== _monthSeq) return;
  const title = new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric" }).format(new Date(y, m - 1, 1));
  setTitle(`Отчёт: ${title}`);
  const root = el(`
    <div class="month">
      <div class="m-nav"><button class="ibtn tonal prev" type="button" aria-label="Прошлый месяц">‹</button><b></b><button class="ibtn tonal next" type="button" aria-label="Следующий месяц">›</button></div>
      <div class="s-hero"><div class="s-period">прибыль за месяц</div><div class="s-total"></div><div class="s-sub"></div></div>
      <div class="c-tiles"></div>
      <section class="c-sec m-lots"><h3>Партии</h3><div class="m-lots-l"></div></section>
      <section class="c-sec"><h3>По товарам</h3><div class="m-prod"></div></section>
    </div>`);
  root.querySelector(".m-nav b").textContent = title;
  root.querySelector(".prev").addEventListener("click", () => renderMonth(mount, m === 1 ? [y - 1, 12] : [y, m - 1]));
  root.querySelector(".next").addEventListener("click", () => renderMonth(mount, m === 12 ? [y + 1, 1] : [y, m + 1]));
  root.querySelector(".s-total").textContent = money(r.profit);
  root.querySelector(".s-sub").textContent = `выручка ${money(r.revenue)} · себестоимость ${money(r.cogs)}`;
  const tiles = root.querySelector(".c-tiles");
  const se = r.stock_end ?? {};
  [["вложено", money(r.invested)], ["продано", r.sold_qty != null ? `${r.sold_qty} шт.` : "—"],
   ["остаток", se.qty != null ? `${se.qty} шт.` : "—"], ["остаток по цене", money(se.list_value)]].forEach(([l, v]) => {
    const t = el(`<div class="c-tile"><b></b><small></small></div>`);
    t.querySelector("b").textContent = v;
    t.querySelector("small").textContent = l;
    tiles.appendChild(t);
  });
  const lots = r.lots ?? [];
  const ll = root.querySelector(".m-lots-l");
  lots.forEach(l => {
    const row = el(`<div class="mv"><span class="mv-q"></span><span class="mv-t"></span><time></time></div>`);
    row.querySelector(".mv-q").textContent = l.payback === "paid" ? "✓" : l.payback === "not_yet" ? `${Math.round(l.payback_pct ?? 0)}%` : "?";
    if (l.payback === "paid") row.querySelector(".mv-q").classList.add("good");
    row.querySelector(".mv-t").textContent = `${l.title ?? "партия"} · ${l.qty_in ?? l.received_qty ?? "?"} шт. · ${money(l.cost)}`;
    row.querySelector("time").textContent = l.date ? berlinTime(l.date).split(",")[0] : "";
    ll.appendChild(row);
  });
  if (!lots.length) root.querySelector(".m-lots").remove();
  const mp = root.querySelector(".m-prod");
  (r.products ?? []).forEach(p => {
    const row = el(`<div class="mv"><span class="mv-q"></span><span class="mv-t"></span><b class="mv-p"></b></div>`);
    row.querySelector(".mv-q").textContent = String(p.sold ?? p.sold_qty ?? 0);
    row.querySelector(".mv-t").textContent = p.title ?? "";
    row.querySelector(".mv-p").textContent = money(p.profit);
    mp.appendChild(row);
  });
  if (!mp.children.length) mp.appendChild(el(`<div class="empty"><p>В этом месяце движений нет</p></div>`));
  mount.replaceChildren(root);
}


// ================= комплект =================
// Состав комплекта: что и по сколько штук входит. Пустой состав = не комплект.
async function kitSheet(productId, current, onDone) {
  let all;
  try {
    all = (await api("/api/ma/stock/products")).items ?? [];
  } catch (e) { toast(`Ошибка: ${e.message ?? e}`, { level: "bad" }); return; }
  const titleOf = id => all.find(x => x.id === id)?.title ?? `товар ${id}`;
  let comps = current.map(c => ({ product_id: c.product_id, qty: c.per_kit ?? c.qty ?? 1 }));
  const box = el(`
    <div class="kit">
      <p class="small kit-warn" hidden>Текущий состав сервер не отдал — сохранение задаст его заново.</p>
      <div class="kit-rows"></div>
      <button class="btn kit-add" type="button">➕ Добавить товар</button>
      <div class="form-error text-danger small" hidden></div>
      <div class="sheet-actions"><button class="btn cancel-btn" type="button">Отмена</button><button class="btn btn-primary save-btn" type="button">💾 Сохранить</button></div>
    </div>`);
  box.querySelector(".kit-warn").hidden = comps.length > 0;
  const rowsEl = box.querySelector(".kit-rows"), err = box.querySelector(".form-error");
  const paint = () => {
    rowsEl.replaceChildren();
    comps.forEach(c => {
      const r = el(`<div class="kit-row"><span class="kr-t"></span><input type="number" min="1" step="1" class="form-control kr-q" aria-label="Штук в комплекте"><button class="ibtn kr-x" type="button" aria-label="Убрать">✕</button></div>`);
      r.querySelector(".kr-t").textContent = titleOf(c.product_id);
      const q = r.querySelector(".kr-q");
      q.value = String(c.qty);
      q.addEventListener("input", () => { c.qty = Number(q.value); });
      r.querySelector(".kr-x").addEventListener("click", () => { comps = comps.filter(x => x !== c); paint(); });
      rowsEl.appendChild(r);
    });
    if (!comps.length) rowsEl.appendChild(el(`<div class="empty"><p>Не комплект</p></div>`));
  };
  paint();
  box.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  box.querySelector(".kit-add").addEventListener("click", () => {
    menuSheet([
      { label: "↩ Назад к составу", onClick: () => openSheet(box, "🧩 Комплект") },
      ...all.filter(x => x.id !== productId && !comps.some(c => c.product_id === x.id)).slice(0, 120).map(x => ({
        label: x.title, onClick: () => { comps.push({ product_id: x.id, qty: 1 }); openSheet(box, "🧩 Комплект"); paint(); } })),
    ], "Что входит в комплект?");
  });
  box.querySelector(".save-btn").addEventListener("click", async ev => {
    if (comps.some(c => !Number.isInteger(c.qty) || c.qty < 1)) { err.textContent = "Штук в комплекте — целое число от 1"; err.hidden = false; return; }
    if (!comps.length && current.length && !(await confirmSheet("Убрать состав? Товар перестанет быть комплектом.", "Убрать"))) return;
    ev.currentTarget.disabled = true;
    try {
      await api(`/api/ma/stock/products/${productId}/kit`, { method: "PUT", body: { components: comps } });
      closeSheet();
      toast(comps.length ? "✅ Состав сохранён" : "Больше не комплект");
      onDone();
    } catch (e) {
      err.textContent = e.message ?? String(e);
      err.hidden = false;
      ev.currentTarget.disabled = false;
    }
  });
  openSheet(box, "🧩 Комплект");
}

// ================= типы и стандартная себестоимость =================
async function renderTypes(mount) {
  setLoading(mount, "Загружаю типы…");
  let d;
  try {
    d = await api("/api/ma/stock/types");
  } catch (e) {
    if (mount.isConnected && !notReady(mount, e)) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const root = el(`<div class="types"><section class="c-sec ty-q"><h3>Какой это тип?</h3><div class="ty-qs"></div></section><section class="c-sec"><h3>Стандартная себестоимость за штуку</h3><div class="ty-table"></div></section></div>`);
  const qs = root.querySelector(".ty-qs");
  const questions = d.questions ?? [];
  const drop = (card) => { card.remove(); if (!qs.children.length) root.querySelector(".ty-q").remove(); };
  questions.forEach(q => {
    const c = el(`<article class="ty-card"><b class="ty-t"></b><small class="ty-m"></small><div class="fchips ty-opts"></div></article>`);
    c.querySelector(".ty-t").textContent = q.title;
    c.querySelector(".ty-m").textContent = [q.list_price_eur != null ? `цена ${money(q.list_price_eur)}` : "", `сейчас: ${q.category}`].filter(Boolean).join(" · ");
    (q.options ?? []).forEach(o => {
      const b = el(`<button class="fchip" type="button"></button>`);
      b.textContent = o.standard_cost_eur != null ? `${o.label} · ${money(o.standard_cost_eur)}` : o.label;
      b.addEventListener("click", async () => {
        const body = { product_id: q.product_id, category: o.category };
        c.querySelectorAll("button").forEach(x => { x.disabled = true; });
        try {
          await api("/api/ma/stock/types", { method: "POST", body });
          toast(`✅ ${q.title}: ${o.label}`);
          drop(c);
        } catch (e) {
          toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 });
          c.querySelectorAll("button").forEach(x => { x.disabled = false; });
        }
      });
      c.querySelector(".ty-opts").appendChild(b);
    });
    qs.appendChild(c);
  });
  if (!questions.length) root.querySelector(".ty-q").remove();

  const table = root.querySelector(".ty-table");
  (d.table ?? []).forEach(t => {
    const r = el(`<div class="ty-row"><span class="ty-l"></span><input type="text" inputmode="decimal" class="form-control ty-v" placeholder="нет"><button class="btn ty-s" type="button" hidden>💾</button></div>`);
    r.querySelector(".ty-l").textContent = t.label;
    const inp = r.querySelector(".ty-v"), btn = r.querySelector(".ty-s");
    const orig = t.standard_cost_eur != null ? String(t.standard_cost_eur) : "";
    inp.value = orig;
    inp.addEventListener("input", () => { btn.hidden = inp.value.trim() === orig; });
    btn.addEventListener("click", async () => {
      const raw = inp.value.trim();
      const v = raw === "" ? null : parseEur(raw);
      if (raw !== "" && (v == null || v < 0)) { toast("Нужно число, например 40", { level: "bad" }); return; }
      btn.disabled = true;
      try {
        await api("/api/ma/stock/category-costs", { method: "PUT", body: { category: t.category, unit_cost_eur: v } });
        toast(v == null ? `Стандарт «${t.label}» убран` : `✅ ${t.label}: ${money(v)} за штуку`);
        btn.hidden = true;
        btn.disabled = false;
        t.standard_cost_eur = v;
      } catch (e) {
        toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 });
        btn.disabled = false;
      }
    });
    table.appendChild(r);
  });
  mount.replaceChildren(root);
}
