// Дашборд-экран MA: сводка (pipeline-счётчики, сегодня, баланс, продажи)
// + КЛИКАБЕЛЬНЫЙ список входящих (клик → переписка треда с историей).
import { api } from "../api.js?v=20260624-013000";
import { el, setLoading, setError } from "../utils.js?v=20260624-013000";
import { threadCard } from "./pipeline.js?v=20260624-013000";

function statsBlock(d) {
  const p = d.pipeline || {}, t = d.today || {}, b = d.api_balance || {};
  const s7 = d.sales_7d || {}, s30 = d.sales_30d || {};
  const wrap = el(`<div class="d-flex flex-column gap-2"></div>`);

  const pc = el(`
    <a href="#/pipeline" class="card text-decoration-none text-reset" role="button"><div class="card-body p-3">
      <div class="row text-center g-2">
        <div class="col"><div class="fs-4 fw-bold red">0</div><div class="small text-muted">🔴 ждут нас</div></div>
        <div class="col"><div class="fs-4 fw-bold green">0</div><div class="small text-muted">🟢 ждём</div></div>
        <div class="col"><div class="fs-4 fw-bold drafts">0</div><div class="small text-muted">📝 драфты</div></div>
        <div class="col"><div class="fs-4 fw-bold ap">0</div><div class="small text-muted">🤖 автопилот</div></div>
      </div>
    </div></a>`);
  pc.querySelector(".red").textContent = p.red ?? 0;
  pc.querySelector(".green").textContent = p.green ?? 0;
  pc.querySelector(".drafts").textContent = p.drafts ?? 0;
  pc.querySelector(".ap").textContent = p.autopilot_active ?? 0;
  wrap.appendChild(pc);

  const tc = el(`
    <div class="card"><div class="card-body p-2">
      <div class="d-flex justify-content-around align-items-center">
        <div class="text-center"><span class="fw-bold tnew">0</span> <span class="small text-muted">🆕 сегодня</span></div>
        <div class="text-center"><span class="fw-bold tsent">0</span> <span class="small text-muted">✉️ отпр.</span></div>
        <div class="text-center"><span class="fw-bold tsold">0</span> <span class="small text-muted">💰 продано</span></div>
      </div>
    </div></div>`);
  tc.querySelector(".tnew").textContent = t.new ?? 0;
  tc.querySelector(".tsent").textContent = t.sent ?? 0;
  tc.querySelector(".tsold").textContent = t.sold ?? 0;
  wrap.appendChild(tc);

  const row = el(`
    <div class="row g-2">
      <div class="col-6"><div class="card h-100 balcard"><div class="card-body p-2 text-center">
        <div class="small text-muted">💵 баланс API</div><div class="fw-bold balv"></div></div></div></div>
      <div class="col-6"><a href="#/sales" class="card h-100 text-decoration-none text-reset" role="button"><div class="card-body p-2 text-center">
        <div class="small text-muted">📈 продажи 7/30д</div><div class="fw-bold salesv"></div></div></a></div>
    </div>`);
  let balText = "—";
  if (b.remaining_usd != null) {
    balText = `~$${b.remaining_usd}`;
    if (b.days_remaining != null && b.days_remaining < 3650) balText += ` (${Math.round(b.days_remaining)}д)`;
    const cls = b.remaining_usd < 2 ? "border-danger" : (b.remaining_usd < 10 ? "border-warning" : "border-success");
    row.querySelector(".balcard").classList.add(cls);
  }
  row.querySelector(".balv").textContent = balText;
  row.querySelector(".salesv").textContent = `${s7.count ?? 0}шт · ${s30.count ?? 0}шт`;
  wrap.appendChild(row);
  return wrap;
}

function requestsBlock(color, title, threads, accountsById) {
  const sec = el(`
    <div class="mb-2">
      <h6 class="text-muted small text-uppercase mb-1 sec-title"></h6>
      <div class="list-group list-group-flush sec-list"></div>
    </div>`);
  sec.querySelector(".sec-title").textContent = `${color} ${title}: ${threads.length}`;
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

  const root = el(`<div class="d-flex flex-column gap-3"></div>`);
  const head = el(`
    <div class="d-flex justify-content-between align-items-center">
      <h5 class="mb-0">📊 Обзор</h5>
      <div class="d-flex gap-1">
        <button class="btn btn-sm btn-outline-secondary refresh">↻</button>
        <a class="btn btn-sm btn-outline-secondary" href="#/settings">⚙</a>
      </div>
    </div>`);
  head.querySelector(".refresh").addEventListener("click", () => render(mount, params));
  root.appendChild(head);

  root.appendChild(statsBlock(stats));

  const reqs = el(`<div><div class="fw-semibold mb-2">📨 Входящие запросы</div></div>`);
  reqs.appendChild(requestsBlock("🔴", "ждут нас", pipe.red ?? [], accountsById));
  reqs.appendChild(requestsBlock("🟢", "ждём клиента", pipe.green ?? [], accountsById));
  root.appendChild(reqs);

  mount.replaceChildren(root);
}
