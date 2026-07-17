// 📊 Обзор (редизайн 2026-07-15): KPI-плитки с моно-цифрами + живой инбокс
// (красная/зелёная секции, карточки из pipeline). Клик по плитке → раздел.
import { api } from "../api.js?v=20260717-132503";
import { el, setLoading, setError } from "../utils.js?v=20260717-132503";
import { threadCard } from "./pipeline.js?v=20260717-132503";

function kpi(value, label, colorCls, href) {
  const tag = href ? "a" : "div";
  const t = el(`<${tag} class="kpi"><div class="kv"></div><div class="kl"></div></${tag}>`);
  if (href) t.href = href;
  if (colorCls) t.classList.add(colorCls);
  t.querySelector(".kv").textContent = String(value);
  t.querySelector(".kl").textContent = label;
  return t;
}

function statsBlock(d) {
  const p = d.pipeline || {}, t = d.today || {}, b = d.api_balance || {};
  const s7 = d.sales_7d || {}, s30 = d.sales_30d || {};
  const wrap = el(`<div></div>`);

  const g1 = el(`<div class="kgrid"></div>`);
  g1.appendChild(kpi(p.red ?? 0, "ждут нас", "k-red", "#/pipeline"));
  g1.appendChild(kpi(p.green ?? 0, "ждём клиента", "k-grn", "#/pipeline"));
  g1.appendChild(kpi(p.drafts ?? 0, "драфты", "k-blu", "#/pipeline"));
  g1.appendChild(kpi(p.autopilot_active ?? 0, "автопилот", "k-amb", "#/pipeline"));
  wrap.appendChild(g1);

  wrap.appendChild(el(`<div class="sec">Сегодня</div>`));
  const g2 = el(`<div class="kgrid k3"></div>`);
  g2.appendChild(kpi(t.new ?? 0, "новых"));
  g2.appendChild(kpi(t.sent ?? 0, "отправлено"));
  g2.appendChild(kpi(t.sold ?? 0, "продано", "k-grn"));
  wrap.appendChild(g2);

  wrap.appendChild(el(`<div class="sec">Ресурсы</div>`));
  const g3 = el(`<div class="kgrid"></div>`);
  let balText = "—", balLabel = "баланс API", balCls = "";
  if (b.remaining_usd != null) {
    balText = `$${b.remaining_usd}`;
    if (b.days_remaining != null && b.days_remaining < 3650) balLabel = `баланс API · ~${Math.round(b.days_remaining)} дн`;
    balCls = b.remaining_usd < 2 ? "k-red" : (b.remaining_usd < 10 ? "k-amb" : "k-grn");
  }
  g3.appendChild(kpi(balText, balLabel, balCls, "#/settings"));
  g3.appendChild(kpi(`${s7.count ?? 0} / ${s30.count ?? 0}`, "продажи 7д / 30д", "", "#/sales"));
  wrap.appendChild(g3);

  return wrap;
}

function requestsBlock(title, cls, threads, accountsById) {
  const sec = el(`<div><div class="sec sec-title"></div><div class="sec-list"></div></div>`);
  const hdr = sec.querySelector(".sec-title");
  hdr.classList.add(cls);
  hdr.textContent = `${title} · ${threads.length}`;
  const list = sec.querySelector(".sec-list");
  if (!threads.length) {
    list.appendChild(el(`<div class="text-muted small fst-italic px-2 py-1">пусто</div>`));
  } else {
    threads.forEach(t => list.appendChild(threadCard(t, accountsById)));
  }
  return sec;
}

export async function render(mount, params) {
  setLoading(mount, "Загрузка дашборда…");
  let stats, pipe;
  try {
    [stats, pipe] = await Promise.all([api("/api/ma/dashboard"), api("/api/ma/pipeline")]);
  } catch (e) { setError(mount, e.message ?? String(e)); return; }

  const accountsById = {};
  (pipe.accounts ?? []).forEach(a => { accountsById[a.id] = a; });

  const root = el(`<div></div>`);

  const head = el(`
    <div class="frow">
      <button type="button" class="fch refresh">↻ Обновить</button>
      <a class="fch" href="#/settings" style="text-decoration:none">⚙ Настройки</a>
    </div>`);
  head.querySelector(".refresh").addEventListener("click", () => render(mount, params));
  root.appendChild(head);

  root.appendChild(statsBlock(stats));

  root.appendChild(requestsBlock("Ждут нас", "sec-red", pipe.red ?? [], accountsById));
  root.appendChild(requestsBlock("Ждём клиента", "sec-grn", pipe.green ?? [], accountsById));

  mount.replaceChildren(root);
}
