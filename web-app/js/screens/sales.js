import { api } from "../api.js?v=20260724-044812";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260724-044812";

const PERIODS = [
  {key: "all",   label: "Все время"},
  {key: "week",  label: "Эта неделя"},
  {key: "month", label: "Этот месяц"},
  {key: "year",  label: "Этот год"},
];

const GROUPS = [
  {key: "day",   label: "по дням"},
  {key: "week",  label: "по неделям"},
  {key: "month", label: "по месяцам"},
  {key: "year",  label: "по годам"},
];

const state = {
  period: "month",
  account_id: "",
  q: "",
  group_by: "day",
};

function buildAddSaleForm(accounts, onSubmitOk, onCancel) {
  const today = new Date().toISOString().slice(0, 10);
  const form = el(`
    <div class="card border-success">
      <div class="card-body p-3">
        <div class="fw-semibold mb-2">➕ Новая продажа (вне бота)</div>
        <div class="row g-2">
          <div class="col-12">
            <input type="text" class="form-control form-control-sm ad-title" placeholder="Название товара" maxlength="200">
          </div>
          <div class="col-7">
            <input type="number" inputmode="decimal" min="0" step="0.01" class="form-control form-control-sm sold-price" placeholder="Цена продажи €" autofocus>
          </div>
          <div class="col-5">
            <input type="date" class="form-control form-control-sm sold-date" value="${today}">
          </div>
          <div class="col-12">
            <select class="form-select form-select-sm account-select"></select>
          </div>
          <div class="col-12">
            <input type="text" class="form-control form-control-sm ad-price-listed" placeholder="(опц.) исходная цена объявления">
          </div>
          <div class="col-12">
            <input type="text" class="form-control form-control-sm buyer-name" placeholder="(опц.) имя покупателя">
          </div>
          <div class="col-12">
            <textarea class="form-control form-control-sm notes" rows="2" placeholder="(опц.) заметки"></textarea>
          </div>
        </div>
        <div class="form-error text-danger small mt-2 d-none"></div>
        <div class="d-flex gap-2 mt-3">
          <button class="btn btn-sm btn-success flex-grow-1 save-btn">✅ Сохранить</button>
          <button class="btn btn-sm btn-outline-secondary cancel-btn">Отмена</button>
        </div>
      </div>
    </div>
  `);

  const accSel = form.querySelector(".account-select");
  if (!accounts.length) {
    accSel.appendChild(el(`<option value="">— нет активных аккаунтов —</option>`));
  } else {
    for (const a of accounts) {
      const opt = el(`<option></option>`);
      opt.value = String(a.id);
      opt.textContent = a.name;
      accSel.appendChild(opt);
    }
  }

  const errEl = form.querySelector(".form-error");

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".save-btn").addEventListener("click", async () => {
    errEl.classList.add("d-none");
    const title = form.querySelector(".ad-title").value.trim();
    const priceRaw = form.querySelector(".sold-price").value.trim().replace(",", ".");
    const price = Number(priceRaw);
    const date = form.querySelector(".sold-date").value;
    const accId = form.querySelector(".account-select").value;
    const adPrice = form.querySelector(".ad-price-listed").value.trim() || null;
    const buyer = form.querySelector(".buyer-name").value.trim() || null;
    const notes = form.querySelector(".notes").value.trim() || null;

    if (!title) { errEl.textContent = "Название обязательно"; errEl.classList.remove("d-none"); return; }
    if (!Number.isFinite(price) || price < 0) { errEl.textContent = "Некорректная цена"; errEl.classList.remove("d-none"); return; }
    if (!date) { errEl.textContent = "Дата обязательна"; errEl.classList.remove("d-none"); return; }
    if (!accId) { errEl.textContent = "Выбери аккаунт"; errEl.classList.remove("d-none"); return; }

    form.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      await api("/api/ma/sales/manual", {
        method: "POST",
        body: {
          account_id: Number(accId),
          ad_title: title,
          ad_price: adPrice,
          sold_price_eur: price,
          sold_at: date,
          buyer_name: buyer,
          notes,
        },
      });
      onSubmitOk();
    } catch (e) {
      errEl.textContent = e.message ?? String(e);
      errEl.classList.remove("d-none");
      form.querySelectorAll("button").forEach(b => b.disabled = false);
    }
  });

  return form;
}


function eur(n) {
  if (n == null) return "—";
  return n.toLocaleString("de-DE", {minimumFractionDigits: 0, maximumFractionDigits: 2}) + "€";
}

function summaryCard(summary) {
  const card = el(`
    <div class="card mb-3">
      <div class="card-body p-3">
        <div class="row g-2 small text-center">
          <div class="col-6 col-md-3"><div class="text-muted">Сделок</div><div class="fs-5 fw-semibold count"></div></div>
          <div class="col-6 col-md-3"><div class="text-muted">Всего</div><div class="fs-5 fw-semibold total"></div></div>
          <div class="col-6 col-md-3"><div class="text-muted">⌀</div><div class="fs-5 fw-semibold avg"></div></div>
          <div class="col-6 col-md-3"><div class="text-muted">min / max</div><div class="fs-6 minmax"></div></div>
        </div>
      </div>
    </div>
  `);
  card.querySelector(".count").textContent = summary.count;
  card.querySelector(".total").textContent = eur(summary.total_eur);
  card.querySelector(".avg").textContent = eur(summary.avg_eur);
  card.querySelector(".minmax").textContent = summary.count
    ? `${eur(summary.min_eur)} / ${eur(summary.max_eur)}`
    : "—";
  return card;
}

function breakdownTable(breakdown, groupBy) {
  const title = GROUPS.find(g => g.key === groupBy)?.label || groupBy;
  const wrap = el(`
    <div class="card mb-3">
      <div class="card-body p-2">
        <div class="small text-muted mb-2">Разбивка ${esc(title)} <span class="text-muted">(тап — список товаров)</span></div>
        <table class="table table-sm table-hover mb-0 small">
          <thead><tr><th>Период</th><th class="text-end">Сделок</th><th class="text-end">Сумма</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  `);
  const tbody = wrap.querySelector("tbody");
  if (!breakdown.length) {
    tbody.appendChild(el(`<tr><td colspan="3" class="text-muted text-center fst-italic">данных нет</td></tr>`));
    return wrap;
  }

  for (const b of breakdown) {
    const tr = el(`
      <tr class="period-row" role="button" style="cursor: pointer">
        <td class="label"><span class="caret me-1">▸</span><span class="period-text"></span></td>
        <td class="text-end count"></td>
        <td class="text-end total"></td>
      </tr>
    `);
    tr.querySelector(".period-text").textContent = b.period_label;
    tr.querySelector(".count").textContent = b.count;
    tr.querySelector(".total").textContent = eur(b.total_eur);

    const itemsTr = el(`
      <tr class="items-row d-none">
        <td colspan="3" class="ps-4">
          <ul class="list-unstyled mb-0 small"></ul>
        </td>
      </tr>
    `);
    const ul = itemsTr.querySelector("ul");
    for (const it of (b.items || [])) {
      const li = el(`
        <li class="d-flex justify-content-between gap-2 py-1 border-bottom border-secondary-subtle">
          <a class="text-decoration-none text-body item-title flex-grow-1 text-truncate"></a>
          <span class="text-success fw-semibold item-price flex-shrink-0"></span>
        </li>
      `);
      const link = li.querySelector(".item-title");
      link.textContent = it.ad_title;
      link.href = `#/thread/${encodeURIComponent(it.thread_id)}`;
      li.querySelector(".item-price").textContent = eur(it.sold_price_eur);
      ul.appendChild(li);
    }

    tr.addEventListener("click", () => {
      const collapsed = itemsTr.classList.contains("d-none");
      itemsTr.classList.toggle("d-none", !collapsed);
      tr.querySelector(".caret").textContent = collapsed ? "▾" : "▸";
    });

    tbody.appendChild(tr);
    tbody.appendChild(itemsTr);
  }
  return wrap;
}

function saleCard(sale) {
  const a = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="title fw-semibold mb-1"></div>
      <div class="d-flex justify-content-between align-items-baseline gap-2">
        <div class="d-flex align-items-baseline gap-2 flex-wrap">
          <div class="fs-5 fw-bold text-success price"></div>
          <small class="text-muted disc"></small>
        </div>
        <small class="text-muted when"></small>
      </div>
      <div class="d-flex justify-content-between gap-2 mt-1">
        <small class="text-muted meta text-truncate"></small>
        <small class="text-muted account flex-shrink-0"></small>
      </div>
    </a>
  `);
  a.href = `#/thread/${encodeURIComponent(sale.thread_id)}`;
  a.querySelector(".title").textContent = sale.ad_title;
  a.querySelector(".price").textContent = eur(sale.sold_price_eur);

  if (sale.discount_eur != null) {
    const sign = sale.discount_eur >= 0 ? "−" : "+";
    a.querySelector(".disc").textContent =
      `(${eur(sale.ad_price_listed_eur)} → ${sign}${eur(Math.abs(sale.discount_eur))}, ${sale.discount_pct >= 0 ? "−" : "+"}${Math.abs(sale.discount_pct).toFixed(1)}%)`;
  } else if (sale.ad_price_listed) {
    a.querySelector(".disc").textContent = `(в объявлении: ${sale.ad_price_listed})`;
  }

  const metaParts = [];
  if (sale.buyer_display_name) metaParts.push(`👤 ${sale.buyer_display_name}`);
  a.querySelector(".meta").textContent = metaParts.join(" · ");

  a.querySelector(".when").textContent = sale.sold_at ? berlinTime(sale.sold_at) : "—";
  a.querySelector(".account").textContent = sale.account_name ? `@${sale.account_name}` : "";
  return a;
}

function controlsBar(accounts) {
  const bar = el(`
    <div class="mb-3">
      <div class="btn-group btn-group-sm w-100 mb-2 period-pills" role="group"></div>
      <div class="row g-2 mb-2">
        <div class="col-7">
          <input type="search" class="form-control form-control-sm q-input" placeholder="🔍 объявление / покупатель…">
        </div>
        <div class="col-5">
          <select class="form-select form-select-sm account-select">
            <option value="">Все аккаунты</option>
          </select>
        </div>
      </div>
      <div class="row g-2">
        <div class="col-12">
          <select class="form-select form-select-sm group-select"></select>
        </div>
      </div>
    </div>
  `);

  const pills = bar.querySelector(".period-pills");
  for (const p of PERIODS) {
    const btn = el(`<button type="button" class="btn btn-outline-secondary" data-period="${p.key}"></button>`);
    btn.textContent = p.label;
    if (p.key === state.period) btn.classList.add("active");
    pills.appendChild(btn);
  }

  const accountSel = bar.querySelector(".account-select");
  for (const a of accounts) {
    const opt = el(`<option></option>`);
    opt.value = String(a.id);
    opt.textContent = a.name;
    if (String(a.id) === String(state.account_id)) opt.selected = true;
    accountSel.appendChild(opt);
  }

  const groupSel = bar.querySelector(".group-select");
  for (const g of GROUPS) {
    const opt = el(`<option></option>`);
    opt.value = g.key;
    opt.textContent = `Разбивка ${g.label}`;
    if (g.key === state.group_by) opt.selected = true;
    groupSel.appendChild(opt);
  }

  return bar;
}

async function load(mount) {
  const params = new URLSearchParams({
    period: state.period,
    group_by: state.group_by,
  });
  if (state.account_id) params.set("account_id", state.account_id);
  if (state.q) params.set("q", state.q);

  setLoading(mount, "Загружаю…");
  try {
    const data = await api(`/api/ma/sales?${params.toString()}`);
    render(mount, data);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}

function render(mount, data) {
  const root = el(`<div></div>`);

  const header = el(`
    <div class="d-flex gap-2 mb-2">
      <button class="btn btn-sm btn-success add-sale-btn">➕ Добавить</button>
      <a class="btn btn-sm btn-outline-warning" href="#/detected">🔍 Проверка детекций</a>
    </div>
  `);
  root.appendChild(header);

  const formSlot = el(`<div class="add-sale-slot mb-3"></div>`);
  root.appendChild(formSlot);

  header.querySelector(".add-sale-btn").addEventListener("click", () => {
    if (formSlot.children.length) {
      formSlot.replaceChildren();  // toggle off
      return;
    }
    formSlot.appendChild(buildAddSaleForm(data.accounts || [], () => {
      formSlot.replaceChildren();
      load(mount);  // re-fetch
    }, () => {
      formSlot.replaceChildren();
    }));
  });

  const bar = controlsBar(data.accounts || []);
  root.appendChild(bar);

  root.appendChild(summaryCard(data.summary));
  root.appendChild(breakdownTable(data.breakdown || [], state.group_by));

  const list = el(`<div class="list-group"></div>`);
  if (!data.sales.length) {
    list.appendChild(el(`<div class="list-group-item text-muted fst-italic text-center">Продаж в этом периоде нет</div>`));
  } else {
    for (const s of data.sales) list.appendChild(saleCard(s));
  }
  root.appendChild(list);

  mount.replaceChildren(root);

  // Wire controls
  bar.querySelectorAll("[data-period]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.period = btn.dataset.period;
      load(mount);
    });
  });
  bar.querySelector(".account-select").addEventListener("change", e => {
    state.account_id = e.target.value;
    load(mount);
  });
  bar.querySelector(".group-select").addEventListener("change", e => {
    state.group_by = e.target.value;
    load(mount);
  });
  let qTimer = null;
  bar.querySelector(".q-input").value = state.q;
  bar.querySelector(".q-input").addEventListener("input", e => {
    state.q = e.target.value.trim();
    if (qTimer) clearTimeout(qTimer);
    qTimer = setTimeout(() => load(mount), 350);
  });
}

export async function renderScreen(mount, params) {
  await load(mount);
}

// router expects `render`
export { renderScreen as render };
