// 👤 Карточка клиента (v3): герой-шапка, плитки, теги и заметка
// (сохраняются сами — U-12), «возможно тот же покупатель», переписки.
import { api } from "../api.js";
import { el, berlinTime, setLoading, setError } from "../utils.js";
import { setTitle } from "../components/backbar.js";
import { avatar, icon, toast } from "../components/kit.js";
import { shortTime } from "../components/thread-row.js";

const ALLOWED_TAGS = ["Серьёзный", "Торгуется", "Тянет время", "Мошенник"];

const STATUS_LABELS = {
  sent: ["", "ждём клиента"],
  sent_debug: ["warn", "на тест-адрес"],     // redirect: клиенту не ушло
  sent_external: ["", "из приложения"],      // ответили на сайте/в приложении KZ
  not_sent_disabled: ["bad", "не отправлено"],
  pending: ["hot", "ждёт ответа"],
  new: ["hot", "ждёт ответа"],
  edited: ["hot", "ждёт ответа"],
  approved: ["hot", "ждёт ответа"],
  skipped: ["mut", "закрыто"],
  skipped_sold: ["good", "продано"],
  archived: ["mut", "архив"],
};

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
}

function threadLine(t, maybe = false) {
  const a = el(`
    <a class="cthread">
      <span class="ct-1"><b></b><span class="ct-price"></span></span>
      <span class="ct-2"><span class="ct-st"></span><span class="ct-brief"></span><time></time></span>
    </a>`);
  a.href = `#/thread/${encodeURIComponent(t.thread_id)}`;
  a.querySelector("b").textContent = t.ad_title ?? "(без названия)";
  a.querySelector(".ct-price").textContent = t.ad_price ?? "";
  a.querySelector("time").textContent = shortTime(t.last_at);
  const [cls, label] = maybe ? ["warn", "похожий"] : (STATUS_LABELS[t.last_status] ?? ["mut", t.last_status ?? ""]);
  const st = a.querySelector(".ct-st");
  st.textContent = label;
  if (cls) st.classList.add(cls);
  const b = t.deal_brief;
  if (b) {
    const parts = [];
    if (b.summary_ru) parts.push(b.summary_ru);
    if (b.negotiated_price_eur) parts.push(`${b.negotiated_price_eur} €`);
    a.querySelector(".ct-brief").textContent = parts.join(" · ");
  }
  return a;
}

export async function render(mount, params) {
  const email = params.email;
  setLoading(mount, "Открываю клиента…");
  let data;
  try {
    data = await api(`/api/ma/clients/${encodeURIComponent(email)}/history`);
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const name = data.display_name || email;
  setTitle(name);
  let tags = Array.isArray(data.tags) ? [...data.tags] : [];

  const root = el(`
    <div class="client">
      <div class="c-hero">
        <span class="c-av"></span>
        <h2 class="c-name"></h2>
        <div class="c-mail"></div>
        <div class="c-fraud" hidden>⚠️ Отмечен как мошенник — не высылать товар до оплаты</div>
        <a class="btn btn-primary c-write" hidden>✉️ К переписке</a>
      </div>
      <div class="c-tiles"></div>
      <section class="c-sec">
        <h3>Метки и заметка <small class="c-saved"></small></h3>
        <div class="c-tags"></div>
        <textarea class="form-control c-note" rows="2" placeholder="Заметка: как платит, когда забирает, сколько торгуется…"></textarea>
      </section>
      <section class="c-sec c-maybe" hidden><h3>Возможно, тот же покупатель</h3><div class="c-maybe-list"></div></section>
      <section class="c-sec"><h3>Переписки</h3><div class="c-threads"></div></section>
    </div>`);
  root.querySelector(".c-av").appendChild(avatar(name, data.account_id ?? null, data.client_lang, "xl"));
  root.querySelector(".c-name").textContent = name;
  root.querySelector(".c-mail").textContent = email;
  if (data.last_active_thread_id) {
    const w = root.querySelector(".c-write");
    w.hidden = false;
    w.href = `#/thread/${encodeURIComponent(data.last_active_thread_id)}`;
  }

  const tiles = root.querySelector(".c-tiles");
  const tile = (v, l, cls) => {
    const t = el(`<div class="c-tile"><b></b><small></small></div>`);
    if (cls) t.classList.add(cls);
    t.querySelector("b").textContent = v;
    t.querySelector("small").textContent = l;
    tiles.appendChild(t);
  };
  const nT = data.threads?.length ?? 0, nS = data.sold_count ?? 0;
  tile(String(nT), plural(nT, "обращение", "обращения", "обращений"));
  tile(String(nS), plural(nS, "покупка", "покупки", "покупок"), nS ? "good" : "");
  if (data.total_negotiated_eur) tile(`${Math.round(data.total_negotiated_eur).toLocaleString("de-DE")} €`, "сумма");
  const last = data.threads?.[0]?.last_at;
  if (last) tile(berlinTime(last).split(",")[0], "последнее");

  // ---- метки и заметка: сохраняются сами ----
  const savedEl = root.querySelector(".c-saved");
  const fraudEl = root.querySelector(".c-fraud");
  const noteEl = root.querySelector(".c-note");
  noteEl.value = data.note || "";
  let saveTimer = null, lastSaved = JSON.stringify({ tags, note: noteEl.value });
  async function save() {
    const payload = { tags, note: noteEl.value };
    const key = JSON.stringify(payload);
    if (key === lastSaved) return;
    savedEl.textContent = "сохраняю…";
    try {
      await api(`/api/ma/clients/${encodeURIComponent(email)}/profile`, { method: "POST", body: payload });
      lastSaved = key;
      savedEl.textContent = "✓ сохранено";
    } catch (e) {
      savedEl.textContent = "⚠️ не сохранено";
      toast(`Не сохранилось: ${e.message ?? e}`, { level: "bad", ms: 5000 });
    }
  }
  const saveSoon = (ms = 600) => { clearTimeout(saveTimer); saveTimer = setTimeout(save, ms); };
  noteEl.addEventListener("input", () => saveSoon(1500));
  noteEl.addEventListener("blur", () => saveSoon(0));
  const tagWrap = root.querySelector(".c-tags");
  const paintFraud = () => { fraudEl.hidden = !tags.includes("Мошенник"); };
  ALLOWED_TAGS.forEach(tag => {
    const b = el(`<button type="button" class="tagbtn"></button>`);
    b.textContent = tag;
    if (tag === "Мошенник") b.classList.add("danger");
    b.classList.toggle("on", tags.includes(tag));
    b.setAttribute("aria-pressed", tags.includes(tag) ? "true" : "false");
    b.addEventListener("click", () => {
      tags = tags.includes(tag) ? tags.filter(x => x !== tag) : [...tags, tag];
      b.classList.toggle("on", tags.includes(tag));
      b.setAttribute("aria-pressed", tags.includes(tag) ? "true" : "false");
      paintFraud();
      saveSoon(300);
    });
    tagWrap.appendChild(b);
  });
  paintFraud();

  // ---- возможно тот же покупатель ----
  if (data.maybe_same?.length) {
    const box = root.querySelector(".c-maybe");
    box.hidden = false;
    box.querySelector("h3").prepend(icon("warn"), " ");
    data.maybe_same.forEach(t => box.querySelector(".c-maybe-list").appendChild(threadLine(t, true)));
  }

  const list = root.querySelector(".c-threads");
  if (!data.threads?.length) list.appendChild(el(`<div class="empty"><p>Нет переписок</p></div>`));
  else data.threads.forEach(t => list.appendChild(threadLine(t)));

  mount.replaceChildren(root);
  // уход с экрана — несохранённую заметку не теряем
  const flush = () => { if (!mount.isConnected) { window.removeEventListener("hashchange", flush); clearTimeout(saveTimer); save(); } };
  window.addEventListener("hashchange", flush);
  // MA уходит в фон / закрывается — сохраняем сразу, не дожидаясь паузы ввода
  const onHide = () => {
    if (!mount.isConnected) { document.removeEventListener("visibilitychange", onHide); return; }
    if (document.visibilityState === "hidden") { clearTimeout(saveTimer); save(); }
  };
  document.addEventListener("visibilitychange", onHide);
}
