// 📊 Обзор: KPI-плитки (живой список — во Входящих, полоса-итог там же).
// Клик по плитке → раздел. «Продано сегодня» — из /sales (как в «Продажах»).
import { api } from "../api.js";
import { el, setLoading, setError, soldToday } from "../utils.js";

function kpi(value, label, colorCls, href) {
  const tag = href ? "a" : "div";
  const t = el(`<${tag} class="kpi"><div class="kv"></div><div class="kl"></div></${tag}>`);
  if (href) t.href = href;
  if (colorCls) t.classList.add(colorCls);
  t.querySelector(".kv").textContent = String(value);
  t.querySelector(".kl").textContent = label;
  return t;
}

function statsBlock(d, sold) {
  const p = d.pipeline || {}, t = d.today || {};
  const s7 = d.sales_7d || {}, s30 = d.sales_30d || {};
  const wrap = el(`<div></div>`);

  const g1 = el(`<div class="kgrid"></div>`);
  g1.appendChild(kpi(p.red ?? 0, "ждут нас", "k-red", "#/pipeline"));
  g1.appendChild(kpi(p.green ?? 0, "ждём клиента", "k-grn", "#/pipeline"));
  g1.appendChild(kpi(p.drafts ?? 0, "готовых черновиков", "k-blu", "#/triage"));
  g1.appendChild(kpi(p.autopilot_active ?? 0, "на автопилоте", "k-amb", "#/pipeline"));
  wrap.appendChild(g1);

  wrap.appendChild(el(`<div class="lsec"><span>Сегодня</span></div>`));
  const g2 = el(`<div class="kgrid k3"></div>`);
  g2.appendChild(kpi(t.new ?? 0, "новых писем"));
  g2.appendChild(kpi(t.sent ?? 0, "отправлено"));
  // «продано» — из /sales, как на экране «Продажи» (одно число везде, день по Берлину)
  g2.appendChild(kpi(sold ? sold.count : "—", "продано", "k-grn", "#/sales"));
  wrap.appendChild(g2);

  wrap.appendChild(el(`<div class="lsec"><span>Продажи</span></div>`));
  const g3 = el(`<div class="kgrid"></div>`);
  g3.appendChild(kpi(s7.count ?? 0, "за 7 дней", "", "#/sales"));
  g3.appendChild(kpi(s30.count ?? 0, "за 30 дней", "", "#/sales"));
  wrap.appendChild(g3);
  return wrap;
}

export async function render(mount, params) {
  setLoading(mount, "Загружаю обзор…");
  let stats, sold = null;
  try {
    [stats, sold] = await Promise.all([api("/api/ma/dashboard"), soldToday(api).catch(() => null)]);
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const root = el(`<div class="dash"></div>`);
  root.appendChild(statsBlock(stats, sold));
  mount.replaceChildren(root);
}
