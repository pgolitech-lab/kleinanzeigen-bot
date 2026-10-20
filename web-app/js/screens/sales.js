// 💰 Продажи (v3): крупный итог периода, столбики по дням/неделям, список
// продаж. «+ Продажа» — лист из 3 полей (товар, цена, аккаунт; дата = сегодня,
// остальное под «Подробнее»). Ссылка на найденные продажи с числом.
// Разбивка по группам и поиск — всегда доступны, на Fold видны сразу.
import { api } from "../api.js";
import { el, setLoading, setError, eur, berlinTime } from "../utils.js";
import { openSheet, closeSheet, menuSheet } from "../components/sheet.js";
import { toast } from "../components/kit.js";

const PERIODS = [
  { key: "week", label: "Неделя" },
  { key: "month", label: "Месяц" },
  { key: "year", label: "Год" },
  { key: "all", label: "Всё" },
];
const GROUPS = [
  { key: "day", label: "по дням" },
  { key: "week", label: "по неделям" },
  { key: "month", label: "по месяцам" },
  { key: "year", label: "по годам" },
];
const state = { period: "month", account_id: "", q: "", group_by: "day" };
const PERIOD_TITLE = { week: "Эта неделя", month: "Этот месяц", year: "Этот год", all: "За всё время" };

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
}

// ---- «+ Продажа» ----
function addSaleSheet(accounts, onOk) {
  const today = new Date().toISOString().slice(0, 10);
  const f = el(`
    <div class="sale-form">
      <input type="text" class="form-control s-title" placeholder="Что продали" maxlength="200">
      <input type="number" inputmode="decimal" min="0" step="1" class="form-control s-price" placeholder="Цена, €">
      <select class="form-select s-acc"></select>
      <details class="s-more"><summary>Подробнее</summary>
        <input type="date" class="form-control s-date">
        <input type="text" class="form-control s-listed" placeholder="Цена в объявлении (необязательно)">
        <input type="text" class="form-control s-buyer" placeholder="Покупатель (необязательно)">
        <textarea class="form-control s-notes" rows="2" placeholder="Заметка (необязательно)"></textarea>
      </details>
      <div class="form-error text-danger small" hidden></div>
      <div class="sheet-actions">
        <button class="btn cancel-btn" type="button">Отмена</button>
        <button class="btn btn-primary save-btn" type="button">✅ Записать</button>
      </div>
    </div>`);
  f.querySelector(".s-date").value = today;
  const acc = f.querySelector(".s-acc");
  if (!accounts.length) acc.appendChild(el(`<option value="">— нет аккаунтов —</option>`));
  accounts.forEach(a => {
    const o = el(`<option></option>`);
    o.value = String(a.id);
    o.textContent = a.name;
    acc.appendChild(o);
  });
  const err = f.querySelector(".form-error");
  const fail = t => { err.textContent = t; err.hidden = false; };
  f.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  f.querySelector(".save-btn").addEventListener("click", async () => {
    err.hidden = true;
    const title = f.querySelector(".s-title").value.trim();
    const price = Number(f.querySelector(".s-price").value.trim().replace(",", "."));
    if (!title) return fail("Напишите, что продали");
    if (!f.querySelector(".s-price").value.trim() || !Number.isFinite(price) || price < 0) return fail("Укажите цену");
    if (!acc.value) return fail("Выберите аккаунт");
    const btn = f.querySelector(".save-btn");
    btn.disabled = true;
    try {
      await api("/api/ma/sales/manual", {
        method: "POST",
        body: {
          account_id: Number(acc.value), ad_title: title, sold_price_eur: price,
          sold_at: f.querySelector(".s-date").value || today,
          ad_price: f.querySelector(".s-listed").value.trim() || null,
          buyer_name: f.querySelector(".s-buyer").value.trim() || null,
          notes: f.querySelector(".s-notes").value.trim() || null,
        },
      });
      closeSheet();
      toast(`✅ Продажа ${eur(price)} записана`);
      onOk();
    } catch (e) {
      fail(e.message ?? String(e));
      btn.disabled = false;
    }
  });
  openSheet(f, "➕ Продажа вне бота");
  f.querySelector(".s-title").focus();
}

// ---- столбики разбивки ----
function bars(breakdown) {
  const box = el(`<div class="s-bars" role="img"></div>`);
  const items = [...breakdown].reverse().slice(-31);
  const max = Math.max(1, ...items.map(b => Number(b.total_eur) || 0));
  items.forEach(b => {
    const col = el(`<span class="sb"><i></i></span>`);
    const v = Number(b.total_eur) || 0;
    col.querySelector("i").style.height = `${Math.max(v ? 6 : 2, (v / max) * 100)}%`;
    if (!v) col.classList.add("zero");
    col.title = `${b.period_label}: ${eur(v)} · ${b.count}`;
    box.appendChild(col);
  });
  box.setAttribute("aria-label", items.map(b => `${b.period_label} ${eur(b.total_eur)}`).join(", "));
  return box;
}

function saleRow(s) {
  const a = el(`
    <a class="srow">
      <span class="sr-main"><b class="sr-t"></b><small class="sr-meta"></small></span>
      <span class="sr-side"><b class="sr-p"></b><small class="sr-d"></small></span>
    </a>`);
  a.href = `#/thread/${encodeURIComponent(s.thread_id)}`;
  a.querySelector(".sr-t").textContent = s.ad_title;
  const meta = [];
  if (s.sold_at) meta.push(berlinTime(s.sold_at).split(",")[0]);
  if (s.account_name) meta.push(s.account_name);
  if (s.buyer_display_name) meta.push(s.buyer_display_name);
  a.querySelector(".sr-meta").textContent = meta.join(" · ");
  a.querySelector(".sr-p").textContent = eur(s.sold_price_eur);
  const d = a.querySelector(".sr-d");
  if (s.discount_pct != null && s.discount_eur > 0) d.textContent = `−${Math.abs(s.discount_pct).toFixed(0)} %`;
  else if (s.ad_price_listed) d.textContent = `из ${s.ad_price_listed}`;
  return a;
}

async function load(mount) {
  const q = new URLSearchParams({ period: state.period, group_by: state.group_by });
  if (state.account_id) q.set("account_id", state.account_id);
  if (state.q) q.set("q", state.q);
  setLoading(mount, "Считаю продажи…");
  let data;
  try {
    data = await api(`/api/ma/sales?${q}`);
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  paint(mount, data);
}

function paint(mount, data) {
  const s = data.summary ?? {};
  const accounts = data.accounts ?? [];
  const root = el(`
    <div class="sales">
      <div class="s-hero">
        <div class="s-period"></div>
        <div class="s-total"></div>
        <div class="s-sub"></div>
        <div class="s-chart"></div>
      </div>
      <div class="tbar"><div class="fchips s-per"></div><div class="tbar-r"></div></div>
      <div class="s-actions">
        <button class="btn btn-primary s-add" type="button">➕ Продажа</button>
        <a class="btn s-det" href="#/detected">🔍 Найденные продажи</a>
      </div>
      <div class="s-filters">
        <input type="search" class="form-control s-q" placeholder="Товар или покупатель…" aria-label="Поиск продаж">
        <button class="fchip s-acc" type="button"></button>
        <button class="fchip s-grp" type="button"></button>
      </div>
      <div class="lsec"><span>Продажи</span><i></i></div>
      <div class="s-list"></div>
    </div>`);
  root.querySelector(".s-period").textContent = PERIOD_TITLE[state.period] ?? "";
  root.querySelector(".s-total").textContent = eur(s.total_eur ?? 0);
  const n = s.count ?? 0;
  root.querySelector(".s-sub").textContent = n
    ? `${n} ${plural(n, "продажа", "продажи", "продаж")} · в среднем ${eur(s.avg_eur)}`
    : "продаж нет";
  const chart = root.querySelector(".s-chart");
  if ((data.breakdown ?? []).length > 1) chart.appendChild(bars(data.breakdown));
  else chart.remove();

  const per = root.querySelector(".s-per");
  PERIODS.forEach(p => {
    const b = el(`<button class="fchip" type="button"></button>`);
    b.textContent = p.label;
    b.classList.toggle("on", p.key === state.period);
    b.addEventListener("click", () => { state.period = p.key; load(mount); });
    per.appendChild(b);
  });

  root.querySelector(".s-add").addEventListener("click", () => addSaleSheet(accounts, () => load(mount)));

  const accBtn = root.querySelector(".s-acc");
  const cur = accounts.find(a => String(a.id) === String(state.account_id));
  accBtn.textContent = `${cur ? cur.name : "Все аккаунты"} ▾`;
  accBtn.classList.toggle("on", !!cur);
  accBtn.addEventListener("click", () => menuSheet([
    { label: "Все аккаунты", onClick: () => { state.account_id = ""; load(mount); } },
    ...accounts.map(a => ({ label: a.name, onClick: () => { state.account_id = String(a.id); load(mount); } })),
  ], "Аккаунт"));
  const grpBtn = root.querySelector(".s-grp");
  grpBtn.textContent = `${GROUPS.find(g => g.key === state.group_by)?.label ?? ""} ▾`;
  grpBtn.addEventListener("click", () => menuSheet(
    GROUPS.map(g => ({ label: `Столбики ${g.label}`, onClick: () => { state.group_by = g.key; load(mount); } })), "Разбивка"));

  const qIn = root.querySelector(".s-q");
  qIn.value = state.q;
  let qTimer = null;
  qIn.addEventListener("input", () => {
    state.q = qIn.value.trim();
    clearTimeout(qTimer);
    qTimer = setTimeout(() => load(mount), 400);
  });

  const sales = data.sales ?? [];
  root.querySelector(".lsec i").textContent = String(sales.length);
  const list = root.querySelector(".s-list");
  if (!sales.length) list.appendChild(el(`<div class="empty"><p>В этом периоде продаж нет</p></div>`));
  else sales.forEach(x => list.appendChild(saleRow(x)));

  // число найденных продаж на кнопке
  api("/api/ma/detected-sales").then(r => {
    const c = r?.count ?? (r?.items ?? []).length;
    if (c && root.isConnected) root.querySelector(".s-det").textContent = `🔍 Найдено: ${c}`;
  }).catch(e => console.warn("[sales] detected-sales:", e));

  mount.replaceChildren(root);
}

export async function render(mount) {
  await load(mount);
}
