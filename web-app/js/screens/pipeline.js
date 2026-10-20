// 📥 Входящие (редизайн v3, вид A «премиум-мессенджер»).
// Сверху полоса-итог (бывший «Обзор»): ждут / ждём / продано сегодня, плашка
// найденных продаж. Фильтры одним рядом: статус + аккаунт «Все ▾» (на Flex и
// 280 px два ряда съедали пол-экрана). Строки — threadRow: термометр торга,
// пометки (🔥, 📌, мошенник, похожий покупатель, запоздалое, из приложения),
// свайп «Жду / Продано», долгое нажатие — выбор нескольких.
// Опрос каждые 20 с, устаревшие ответы отбрасываются (см. paint).
import { api } from "../api.js";
import { el, setLoading, setError, soldToday } from "../utils.js";
import { confirmSheet, menuSheet } from "../components/sheet.js";
import { threadRow, dealOf } from "../components/thread-row.js";
import { icon, toast } from "../components/kit.js";
import { openSoldSheet } from "../components/sold-sheet.js";
import { setTabBadge } from "../components/tabbar.js";

const state = { account: "", status: "all" };
const sel = { active: false, ids: new Set() };
let _cachedData = null;   // последний ответ /pipeline (перерисовка без запроса)
let _detected = { n: null, at: 0 };  // найденные продажи — не чаще раза в 5 мин
let _soldToday = null;  // {count, eur} — из /sales, если api не прислал summary

let _timer = null, _visHandler = null, _hashHandler = null;
const REFRESH_MS = 20000;

function teardown() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_visHandler) { document.removeEventListener("visibilitychange", _visHandler); _visHandler = null; }
  if (_hashHandler) { window.removeEventListener("hashchange", _hashHandler); _hashHandler = null; }
}

function matchFilter(t, kind) {
  if (state.account && String(t.account_id) !== String(state.account)) return false;
  if (kind === "pinned") return true;
  if (state.status === "ap") return !!t.is_autopilot;
  if (state.status === "red") return kind === "red";
  if (state.status === "green") return kind === "green";
  return true;
}

// Горячие и с готовым черновиком — выше в «Ждут ответа»
function hotFirst(list) {
  const score = t => (t.is_hot ? 2 : 0) + (t.pending_drafts_count > 0 ? 1 : 0);
  return [...list].sort((a, b) => score(b) - score(a));
}

function openThread(t) {
  location.hash = `#/thread/${encodeURIComponent(t.thread_id)}`;
}

// ---------- полоса-итог и плашки ----------

function summaryStrip(data, mount) {
  const s = data.summary ?? {};
  // единый счёт для полосы, чипов и значка вкладки: «ждут» = красные + закреплённые, где последним писал клиент
  const pinIn = (data.pinned ?? []).filter(t => t.last_event_kind === "in").length;
  const waiting = (data.red ?? []).length + pinIn;
  const waitingClient = (data.green ?? []).length + (data.pinned ?? []).length - pinIn;
  const strip = el(`<div class="sumstrip"></div>`);
  const cell = (n, label, cls, onClick) => {
    const c = el(`<button class="sc"><b></b><small></small></button>`);
    if (cls) c.classList.add(cls);
    c.querySelector("b").textContent = n;
    c.querySelector("small").textContent = label;
    c.addEventListener("click", onClick);
    strip.appendChild(c);
  };
  cell(String(waiting), pinIn ? `ждут ответа (в т.ч. ${pinIn} закреплён)` : "ждут ответа", waiting ? "hot" : "", () => { state.status = "red"; renderWith(mount, data); });
  cell(String(waitingClient), "ждём клиента", "", () => { state.status = "green"; renderWith(mount, data); });
  const soldCell = (n, sumEur) => {
    const sum = sumEur ? ` · ${Math.round(sumEur).toLocaleString("de-DE")} €` : "";
    cell(`${n}${sum}`, "продано сегодня", n ? "good" : "", () => { location.hash = "#/sales"; });
  };
  if (s.sold_today_count != null) soldCell(s.sold_today_count, s.sold_today_eur);
  else if (_soldToday) soldCell(_soldToday.count, _soldToday.eur);
  return strip;
}

function detectedBanner(n) {
  const b = el(`<a class="banner-detected" href="#/detected"><span class="bd-t"></span><span class="bd-go"><b>Проверить </b>›</span></a>`);
  b.querySelector(".bd-t").textContent = `💰 Найдено продаж: ${n}`;
  return b;
}

// Счётчик найденных продаж: из summary, иначе отдельный запрос (раз в 5 мин)
async function refreshDetected(data, mount) {
  if (data.summary?.detected_pending != null) { _detected = { n: data.summary.detected_pending, at: Date.now() }; return; }
  // без summary — не чаще раза в 30 мин: /detected-sales на сервере переводит цитаты через LLM
  if (Date.now() - _detected.at < 1800000) return;
  _detected.at = Date.now();
  try {
    const r = await api("/api/ma/detected-sales");
    const items = Array.isArray(r) ? r : (r?.items ?? r?.detections ?? []);
    _detected.n = items.length;
    if (mount.isConnected && _cachedData) renderWith(mount, _cachedData);
  } catch (e) {
    console.warn("[pipeline] detected-sales failed:", e);  // плашка просто не покажется
  }
}

// ---------- фильтры ----------

function toolbar(data, mount, counts) {
  const bar = el(`<div class="tbar"><div class="fchips"></div><div class="tbar-r"></div></div>`);
  const chips = bar.querySelector(".fchips");
  [["all", "Все", counts.all], ["red", "Ждут ответа", counts.red], ["green", "Ждём", counts.green], ["ap", "Авто", counts.ap]]
    .forEach(([k, label, n]) => {
      if (k === "ap" && !n && state.status !== "ap") return;
      const c = el(`<button class="fchip"><span></span><b></b></button>`);
      c.querySelector("span").textContent = label;
      c.querySelector("b").textContent = String(n);
      c.classList.toggle("on", state.status === k);
      c.setAttribute("aria-pressed", state.status === k ? "true" : "false");
      c.addEventListener("click", () => { state.status = k; renderWith(mount, data); });
      chips.appendChild(c);
    });
  // аккаунт — один чип со списком (вместо второго ряда фильтров)
  const accs = data.accounts ?? [];
  if (accs.length > 1) {
    const cur = accs.find(a => String(a.id) === String(state.account));
    const ac = el(`<button class="fchip acc-chip"><span></span> ▾</button>`);
    ac.querySelector("span").textContent = cur ? cur.name : "Все аккаунты";
    ac.classList.toggle("on", !!cur);
    ac.addEventListener("click", () => menuSheet([
      { label: "Все аккаунты", onClick: () => { state.account = ""; renderWith(mount, data); } },
      ...accs.map(a => ({ label: a.name, onClick: () => { state.account = a.id; renderWith(mount, data); } })),
    ], "Аккаунт"));
    chips.appendChild(ac);
  }
  const right = bar.querySelector(".tbar-r");
  const triBtn = el(`<a class="ibtn tri-btn" href="#/triage" aria-label="Разбор по одной карточке"></a>`);
  triBtn.appendChild(icon("cards"));
  right.appendChild(triBtn);
  const selBtn = el(`<button class="ibtn" aria-label="Выбрать несколько"></button>`);
  selBtn.appendChild(icon("sel"));
  selBtn.addEventListener("click", () => { sel.active = true; sel.ids.clear(); renderWith(mount, data); });
  right.appendChild(selBtn);
  return bar;
}

// ---------- выбор нескольких ----------

const BULK = [
  ["pin", "📌 Закрепить"], ["unpin", "Открепить"], ["read", "Прочитано"],
  ["unread", "Непрочитано"], ["wait", "✋ Жду"], ["close", "🗑 Убрать"],
];

function selBar(data, mount) {
  const bar = el(`<div class="selbar"><button class="ibtn x" aria-label="Отменить выбор"></button><b class="sel-n"></b><span class="sel-l">выбрано</span></div>`);
  bar.querySelector(".x").appendChild(icon("x"));
  bar.querySelector(".sel-n").textContent = String(sel.ids.size);
  bar.querySelector(".x").addEventListener("click", () => { sel.active = false; sel.ids.clear(); renderWith(mount, data); });
  return bar;
}

function bulkDock(data, mount) {
  const dock = el(`<div class="bulkdock"></div>`);
  BULK.forEach(([action, label]) => {
    const b = el(`<button class="btn btn-sm"></button>`);
    b.textContent = label;
    if (action === "close") b.classList.add("btn-danger");
    b.disabled = sel.ids.size === 0;
    b.addEventListener("click", async () => {
      const ids = [...sel.ids];
      if (!ids.length) return;
      if (action === "close" && !(await confirmSheet(`Убрать ${ids.length} переписк(у/и) из входящих?`, "Убрать"))) return;
      dock.querySelectorAll("button").forEach(x => { x.disabled = true; });
      try {
        if (action === "wait") {
          // массового «Жду» у api нет — по одному
          for (const id of ids) await api(`/api/ma/threads/${encodeURIComponent(id)}/wait`, { method: "POST" });
        } else {
          await api("/api/ma/threads/bulk-action", { method: "POST", body: { thread_ids: ids, action } });
        }
        toast(`Готово: ${label.replace(/^\S+\s/, "")} · ${ids.length}`, { level: "ok" });
      } catch (e) {
        toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 });
      }
      sel.active = false;
      sel.ids.clear();
      _cachedData = null;
      await paint(mount);
    });
    dock.appendChild(b);
  });
  return dock;
}

// ---------- отрисовка ----------

function renderWith(mount, data) {
  const pinnedAll = data.pinned ?? [], redAll = data.red ?? [], greenAll = data.green ?? [];
  setTabBadge("inbox", redAll.length + pinnedAll.filter(t => t.last_event_kind === "in").length);
  const inAcc = t => !state.account || String(t.account_id) === String(state.account);
  const all = [...pinnedAll, ...redAll, ...greenAll].filter(inAcc);
  const counts = {
    all: all.length,
    red: [...pinnedAll.filter(t => t.last_event_kind === "in"), ...redAll].filter(inAcc).length,
    green: [...pinnedAll.filter(t => t.last_event_kind !== "in"), ...greenAll].filter(inAcc).length,
    ap: all.filter(t => t.is_autopilot).length,
  };

  const container = el(`<div class="inbox"></div>`);
  if (sel.active) {
    container.appendChild(selBar(data, mount));
  } else {
    container.appendChild(summaryStrip(data, mount));
    if (_detected.n) container.appendChild(detectedBanner(_detected.n));
    container.appendChild(toolbar(data, mount, counts));
  }

  const rowOpts = t => ({
    selecting: sel.active,
    picked: sel.ids.has(t.thread_id),
    onOpen: sel.active ? () => {
      if (sel.ids.has(t.thread_id)) sel.ids.delete(t.thread_id); else sel.ids.add(t.thread_id);
      renderWith(mount, data);
    } : openThread,
    onLongPress: () => { sel.active = true; sel.ids = new Set([t.thread_id]); renderWith(mount, data); },
    onWait: async th => {
      try {
        await api(`/api/ma/threads/${encodeURIComponent(th.thread_id)}/wait`, { method: "POST" });
        toast(`✋ Ждём ответа: ${th.buyer_display_name || "покупатель"}`, { level: "ok" });
        _cachedData = null;
        paint(mount);
      } catch (e) {
        toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 });
      }
    },
    onSold: th => {
      const d = dealOf(th);
      openSoldSheet({
        threadId: th.thread_id, msgId: th.msg_id, title: th.ad_title,
        price: d.ours ?? d.offer ?? d.price, prices: [d.price, d.ours, d.offer],
        onDone: () => { _cachedData = null; paint(mount); },
      });
    },
  });

  const list = el(`<div class="tlist"></div>`);
  const section = (title, items, cls) => {
    if (!items.length) return;
    const h = el(`<div class="lsec"><span></span><i></i></div>`);
    if (cls) h.classList.add(cls);
    h.querySelector("span").textContent = title;
    h.querySelector("i").textContent = String(items.length);
    list.appendChild(h);
    items.forEach(t => list.appendChild(threadRow(t, rowOpts(t))));
  };
  const pinned = pinnedAll.filter(t => matchFilter(t, "pinned") && (state.status === "all" ||
    (state.status === "red" ? t.last_event_kind === "in" : state.status === "green" ? t.last_event_kind !== "in" : !!t.is_autopilot)));
  const red = hotFirst(redAll.filter(t => matchFilter(t, "red")));
  const green = greenAll.filter(t => matchFilter(t, "green"));
  section("Закреплённые", pinned, "pin");
  section("Ждут ответа", red, "hot");
  section("Ждём клиента", green);
  if (!list.children.length) {
    const empty = el(`<div class="empty"><p></p></div>`);
    empty.prepend(icon("check"));
    empty.querySelector("p").textContent = state.status === "all" && !state.account
      ? "Пусто — на все письма ответили." : "Нет переписок по этому фильтру.";
    list.appendChild(empty);
  }
  container.appendChild(list);
  if (sel.active) container.appendChild(bulkDock(data, mount));
  mount.replaceChildren(container);
}

let _paintSeq = 0;

async function paint(mount) {
  if (!_cachedData) {
    const seq = ++_paintSeq;
    let data;
    try {
      data = await api("/api/ma/pipeline");
    } catch (e) {
      if (mount.isConnected && seq === _paintSeq) setError(mount, e.message ?? String(e));
      return;
    }
    // Ушли с экрана, пока шёл запрос, или уже ушёл более свежий запрос
    // (таймер + ручное обновление) — устаревший ответ не рисуем и не кэшируем.
    if (!mount.isConnected || seq !== _paintSeq) return;
    _cachedData = data;
    refreshDetected(data, mount);
    if (data.summary?.sold_today_count == null) {
      soldToday(api).then(v => {
        const changed = !_soldToday || _soldToday.count !== v.count;
        _soldToday = v;
        if (changed && mount.isConnected && _cachedData && !sel.active) renderWith(mount, _cachedData);
      }).catch(e => {
        console.warn("[pipeline] sold today:", e);
        _soldToday = { count: "—", eur: 0 };  // не показываем старое число как «сегодня»
      });
    }
  }
  renderWith(mount, _cachedData);
}

export async function render(mount, params) {
  teardown();
  sel.active = false;
  sel.ids.clear();
  _cachedData = null;
  setLoading(mount, "Загружаю входящие…");
  await paint(mount);
  // Пока грузились, оператор ушёл на другой экран — таймеры не заводим
  if (!mount.isConnected) return;

  const refresh = async () => {
    if (!mount.isConnected) { teardown(); return; }
    // не дёргаем список, пока оператор выбирает карточки
    if (document.visibilityState === "visible" && !sel.active) {
      _cachedData = null;
      await paint(mount);
    }
  };
  _timer = setInterval(refresh, REFRESH_MS);
  _visHandler = refresh;
  document.addEventListener("visibilitychange", _visHandler);
  // Ушли с экрана — стоп. На Fold список живёт в левой панели и при открытии
  // треда остаётся в DOM: тогда опрос продолжается.
  _hashHandler = () => {
    if (!mount.isConnected) teardown();
  };
  window.addEventListener("hashchange", _hashHandler);
}
