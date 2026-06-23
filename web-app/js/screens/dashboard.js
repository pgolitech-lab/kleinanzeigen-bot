// Дашборд-экран MA: pipeline-счётчики, сегодня, баланс API, продажи 7/30д.
import { api } from "../api.js?v=20260623-160000";
import { el, setLoading, setError } from "../utils.js?v=20260623-160000";

export async function render(mount, params) {
  setLoading(mount, "Загрузка дашборда…");
  let d;
  try {
    d = await api("/api/ma/dashboard");
  } catch (e) {
    setError(mount, e.message ?? String(e));
    return;
  }

  const p = d.pipeline || {}, t = d.today || {}, b = d.api_balance || {};
  const s7 = d.sales_7d || {}, s30 = d.sales_30d || {};

  const root = el(`<div class="d-flex flex-column gap-3"></div>`);
  root.appendChild(el(`<h5 class="mb-0">📊 Дашборд</h5>`));

  // --- pipeline ---
  const pc = el(`
    <div class="card"><div class="card-body p-3">
      <div class="row text-center g-2">
        <div class="col"><div class="fs-4 fw-bold red">0</div><div class="small text-muted">🔴 ждут нас</div></div>
        <div class="col"><div class="fs-4 fw-bold green">0</div><div class="small text-muted">🟢 ждём</div></div>
        <div class="col"><div class="fs-4 fw-bold drafts">0</div><div class="small text-muted">📝 драфты</div></div>
        <div class="col"><div class="fs-4 fw-bold ap">0</div><div class="small text-muted">🤖 автопилот</div></div>
      </div>
    </div></div>`);
  pc.querySelector(".red").textContent = p.red ?? 0;
  pc.querySelector(".green").textContent = p.green ?? 0;
  pc.querySelector(".drafts").textContent = p.drafts ?? 0;
  pc.querySelector(".ap").textContent = p.autopilot_active ?? 0;
  root.appendChild(pc);

  // --- сегодня ---
  const tc = el(`
    <div class="card"><div class="card-body p-3">
      <div class="small text-muted mb-1">Сегодня</div>
      <div class="d-flex justify-content-around">
        <div class="text-center"><div class="fs-5 fw-bold tnew">0</div><div class="small text-muted">🆕 новые</div></div>
        <div class="text-center"><div class="fs-5 fw-bold tsent">0</div><div class="small text-muted">✉️ отпр.</div></div>
        <div class="text-center"><div class="fs-5 fw-bold tsold">0</div><div class="small text-muted">💰 продано</div></div>
      </div>
    </div></div>`);
  tc.querySelector(".tnew").textContent = t.new ?? 0;
  tc.querySelector(".tsent").textContent = t.sent ?? 0;
  tc.querySelector(".tsold").textContent = t.sold ?? 0;
  root.appendChild(tc);

  // --- баланс API ---
  const bc = el(`
    <div class="card"><div class="card-body p-3 d-flex justify-content-between align-items-center">
      <span class="small text-muted">💵 Баланс API</span>
      <span class="fw-bold balv"></span>
    </div></div>`);
  let balText = "—";
  if (b.remaining_usd != null) {
    balText = `~$${b.remaining_usd}`;
    if (b.days_remaining != null) balText += ` (≈${b.days_remaining} дн)`;
    const cls = b.remaining_usd < 2 ? "border-danger"
      : (b.remaining_usd < 10 ? "border-warning" : "border-success");
    bc.classList.add(cls);
  }
  bc.querySelector(".balv").textContent = balText;
  root.appendChild(bc);

  // --- продажи ---
  const sc = el(`
    <div class="card"><div class="card-body p-3">
      <div class="small text-muted mb-1">📈 Продажи</div>
      <div class="d-flex justify-content-around">
        <div class="text-center"><div class="fw-bold s7"></div><div class="small text-muted">7 дней</div></div>
        <div class="text-center"><div class="fw-bold s30"></div><div class="small text-muted">30 дней</div></div>
      </div>
    </div></div>`);
  sc.querySelector(".s7").textContent = `${s7.count ?? 0} · ${s7.total_eur ?? 0}€`;
  sc.querySelector(".s30").textContent = `${s30.count ?? 0} · ${s30.total_eur ?? 0}€`;
  root.appendChild(sc);

  // --- навигация ---
  const nav = el(`
    <div class="d-flex gap-2">
      <button class="btn btn-primary flex-grow-1 go-pipe">📋 Pipeline</button>
      <button class="btn btn-outline-secondary go-sales">📈 Продажи</button>
      <button class="btn btn-outline-secondary go-set">⚙️</button>
    </div>`);
  nav.querySelector(".go-pipe").addEventListener("click", () => { location.hash = "#/pipeline"; });
  nav.querySelector(".go-sales").addEventListener("click", () => { location.hash = "#/sales"; });
  nav.querySelector(".go-set").addEventListener("click", () => { location.hash = "#/settings"; });
  root.appendChild(nav);

  const rb = el(`<button class="btn btn-sm btn-outline-secondary w-100 refresh">🔄 Обновить</button>`);
  rb.addEventListener("click", () => render(mount, params));
  root.appendChild(rb);

  mount.replaceChildren(root);
}
