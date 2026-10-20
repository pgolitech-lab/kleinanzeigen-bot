// 🃏 Разбор (концепция B): очередь «ждут нас» по одной карточке.
// На карточке всё для решения: товар, термометр торга, сообщение клиента
// по-русски (оригинал тапом), готовый ответ ИИ на языке клиента + RU.
// Свайп вправо / «Отправить» → подтверждение с проверкой языка → /send;
// влево / «Позже» — в конец очереди (только в этой сессии); вверх / «Править» —
// открыть переписку. Он же — режим Flip 5 Flex Window («карман»).
// Отмены отправки за 5 с пока нет (нужна отложенная отправка у api) —
// поэтому отправка только после явного подтверждения.
// ИНВАРИАНТ языка: кириллица чужому клиенту или вердикт lang-check «не тот
// язык» — отправка отсюда запрещена, только через переписку.
import { api, LLM_TIMEOUT_MS } from "../api.js";
import { el, setLoading, setError, sendOutcome, parseEur } from "../utils.js";
import { avatar, icon, langBadge, langName, toast } from "../components/kit.js";
import { thermo, hasThermo } from "../components/thermo.js";
import { dealOf } from "../components/thread-row.js";
import { openSheet, closeSheet } from "../components/sheet.js";
import { wrongLangForClient } from "./thread.js";

const PENDING = new Set(["pending", "new", "edited", "approved"]);

// Очередь: /triage (когда api сделает) или «ждут нас» с готовым черновиком из /pipeline
async function loadQueue() {
  try {
    const r = await api("/api/ma/triage");
    // без готового черновика карточку не показываем (лишний запрос и тут же пропуск)
    if (Array.isArray(r?.items)) return r.items.filter(t => t.draft && t.draft.msg_id != null && t.draft.has_text !== false);
  } catch (e) {
    if (e.status !== 404 && e.status !== 405) throw e;
  }
  const p = await api("/api/ma/pipeline");
  const items = [...(p.pinned ?? []), ...(p.red ?? [])]
    .filter(t => t.last_event_kind === "in" && t.pending_drafts_count > 0 && !t.is_autopilot);
  const score = t => (t.is_hot ? 4 : 0) + (t.is_pinned ? 2 : 0);
  return items.sort((a, b) => score(b) - score(a) || String(a.last_event_at).localeCompare(String(b.last_event_at)));
}

export async function render(mount) {
  setLoading(mount, "Собираю очередь…");
  let queue;
  try {
    queue = await loadQueue();
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const total = queue.length;
  const stats = { sent: 0, later: 0, skipped: 0, notSent: 0 };
  let laterRun = 0;  // «Позже» подряд без отправки — когда обошли всю очередь, итог
  const cache = new Map();  // msg_id → review

  async function review(t) {
    if (cache.has(t.msg_id)) return cache.get(t.msg_id);
    const r = t.review ?? await api(`/api/ma/messages/${t.msg_id}`);
    cache.set(t.msg_id, r);
    return r;
  }

  async function next() {
    if (!mount.isConnected) return;
    if (!queue.length) { done(); return; }
    const t = queue[0];
    let r;
    try {
      r = await review(t);
    } catch (e) {
      queue.shift();
      stats.skipped++;
      toast(`Не открылось: ${e.message ?? e}`, { level: "bad" });
      return next();
    }
    if (!mount.isConnected) return;
    if (!PENDING.has(r.status) || !r.draft?.de_answer) {
      // черновика уже нет (отправлен с другого устройства / пустой) — дальше, но учитываем
      queue.shift();
      stats.skipped++;
      return next();
    }
    paint(t, r);
    // подгружаем следующую заранее
    if (queue[1]) review(queue[1]).catch(() => { /* догрузим при показе */ });
  }

  function progress() {
    const p = el(`<div class="tr-prog"><b></b><span></span><div class="tr-bar"><i></i></div></div>`);
    const doneN = total - queue.length;
    p.querySelector("b").textContent = `${queue.length} ждут ответа`;
    p.querySelector("span").textContent = `${Math.min(doneN + 1, total)} из ${total}`;
    p.querySelector("i").style.width = `${total ? (doneN / total) * 100 : 100}%`;
    return p;
  }

  function paint(t, r) {
    const lang = (r.client_lang || "de").toLowerCase();
    const wrap = el(`<div class="triage"></div>`);
    wrap.appendChild(progress());
    const card = el(`
      <article class="tcard3" tabindex="0">
        <div class="t3-head">
          <span class="t3-av"></span>
          <div class="t3-who"><b></b><small></small></div>
          <span class="t3-time"></span>
        </div>
        <div class="t3-ad"></div>
        <div class="t3-deal"></div>
        <div class="t3-msg"><div class="t3-l">Клиент · по-русски</div><div class="t3-ru"></div><button class="t3-orig" type="button">Показать оригинал</button></div>
        <div class="t3-ans"><div class="t3-l"><span>Ответ ИИ</span><span class="t3-badge"></span></div><div class="t3-de"></div><div class="t3-mirror"></div></div>
        <div class="t3-stamp"></div>
      </article>`);
    card.querySelector(".t3-av").appendChild(avatar(t.buyer_display_name, t.account_id, lang, "md"));
    card.querySelector(".t3-who b").textContent = t.buyer_display_name || "Покупатель";
    const acc = (t.account_name ?? r.account_name ?? "");
    card.querySelector(".t3-who small").textContent = [acc, langName(lang)].filter(Boolean).join(" · ");
    card.querySelector(".t3-ad").textContent = r.ad?.title ?? t.ad_title ?? "";
    const d = { ...dealOf(t) };
    if (d.price == null) d.price = parseEur(r.ad?.price);
    const deal = card.querySelector(".t3-deal");
    if (hasThermo(d)) deal.appendChild(thermo(d, "lg"));
    else if (d.price != null) deal.textContent = `Цена ${Math.round(d.price).toLocaleString("de-DE")} €`;
    else deal.remove();
    const ruEl = card.querySelector(".t3-ru");
    const ru = r.client_message?.ru || t.client_message_ru || t.ru_client || "", orig = r.client_message?.raw || "";
    ruEl.textContent = ru || orig;
    const ob = card.querySelector(".t3-orig");
    if (orig && ru && orig !== ru) {
      ob.addEventListener("click", e => {
        e.stopPropagation();
        const showOrig = ob.dataset.on !== "1";
        ob.dataset.on = showOrig ? "1" : "";
        ruEl.textContent = showOrig ? orig : ru;
        ob.textContent = showOrig ? "Показать по-русски" : "Показать оригинал";
      });
    } else ob.remove();
    const text = r.draft.de_answer.trim();
    card.querySelector(".t3-de").textContent = text;
    const mirror = r.draft.ru_translation;
    card.querySelector(".t3-mirror").textContent = mirror && mirror !== text ? `RU: ${mirror}` : "";
    const wrong = wrongLangForClient(text, lang);
    card.querySelector(".t3-badge").appendChild(langBadge(wrong ? "RU" : lang.toUpperCase(), !wrong));
    wrap.appendChild(card);

    const hint = el(`<div class="tr-hint">свайп → отправить · ← позже · ↑ переписка</div>`);
    wrap.appendChild(hint);
    const acts = el(`<div class="tr-acts"></div>`);
    const mk = (cls, ic, label, fn) => {
      const b = el(`<button type="button"><span></span></button>`);
      b.classList.add(cls);
      b.prepend(icon(ic));
      b.querySelector("span").textContent = label;
      b.addEventListener("click", fn);
      acts.appendChild(b);
      return b;
    };
    mk("ta-later", "clock", "Позже", () => fling(card, -1, later));
    mk("ta-edit", "chev", "Править", () => openThread(t));
    const sendB = mk("ta-send", "send", wrong ? "Язык ≠" : "Отправить", () => confirmSend(t, r, card));
    if (wrong) sendB.classList.add("warn");
    wrap.appendChild(acts);
    mount.replaceChildren(wrap);
    bindCardSwipe(card, {
      right: () => confirmSend(t, r, card),
      left: () => fling(card, -1, later),
      up: () => openThread(t),
    });
  }

  function later() {
    stats.later++;
    laterRun++;
    queue.push(queue.shift());
    // обошли всю оставшуюся очередь одними «Позже» — итог, а не та же карточка по кругу
    if (laterRun >= queue.length && queue.length) { done(); return; }
    next();
  }

  function openThread(t) {
    location.hash = `#/thread/${encodeURIComponent(t.thread_id)}`;
  }

  function fling(card, dir, then) {
    card.classList.add(dir > 0 ? "fly-r" : "fly-l");
    setTimeout(then, 260);
  }

  // Подтверждение отправки: текст + проверка языка (эвристика + lang-check)
  async function confirmSend(t, r, card) {
    const lang = (r.client_lang || "de").toLowerCase();
    const text = r.draft.de_answer.trim();
    const box = el(`
      <div class="final">
        <div class="f-head"><span class="f-l">Уйдёт клиенту</span><span class="f-badge"></span></div>
        <div class="f-text"></div>
        <div class="f-status"></div>
        <div class="flow-err text-danger small my-2" hidden></div>
        <div class="sheet-actions">
          <button class="btn cancel-btn" type="button">Отмена</button>
          <button class="btn btn-primary send-btn" type="button" disabled>✅ Отправить</button>
        </div>
      </div>`);
    box.querySelector(".f-text").textContent = text;
    const badge = box.querySelector(".f-badge"), status = box.querySelector(".f-status");
    const sendBtn = box.querySelector(".send-btn");
    box.querySelector(".cancel-btn").addEventListener("click", () => { closeSheet(); card.classList.remove("drag"); });
    const blockToThread = msg => {
      badge.replaceChildren(langBadge(lang.toUpperCase(), false));
      status.textContent = msg;
      sendBtn.disabled = false;
      sendBtn.textContent = "Открыть переписку";
      sendBtn.onclick = () => { closeSheet(); openThread(t); };
    };
    openSheet(box, "Отправить ответ?");
    if (wrongLangForClient(text, lang)) {
      blockToThread(`Текст не на языке клиента (${langName(lang)}). Переведите в переписке.`);
      return;
    }
    badge.replaceChildren(langBadge(lang.toUpperCase(), null));
    status.textContent = "Проверяю язык…";
    try {
      const lc = await api(`/api/ma/messages/${t.msg_id}/lang-check`, { method: "POST", body: {text}, timeoutMs: LLM_TIMEOUT_MS });
      if (!lc?.match) {
        blockToThread(`ИИ определил язык: ${langName(lc.detected_lang)}, а клиент пишет: ${langName(lang)}. Откройте переписку.`);
        return;
      }
      badge.replaceChildren(langBadge(lang.toUpperCase(), true));
      status.textContent = `Проверено: ${langName(lang)}`;
    } catch (e) {
      // ИИ-проверка недоступна — отсюда без проверки не отправляем: в переписке
      // оператор может явно разрешить отправку галочкой
      blockToThread("ИИ-проверка языка сейчас недоступна — отправьте из переписки.");
      return;
    }
    sendBtn.disabled = false;
    sendBtn.onclick = async () => {
      sendBtn.disabled = true;
      sendBtn.textContent = "⏳ Отправляю…";
      let held = false;
      try {
        await api(`/api/ma/messages/${t.msg_id}/lock/acquire`, { method: "POST" });
        held = true;
        // черновик могли перегенерировать после показа карточки — проверяли не тот текст
        const fresh = await api(`/api/ma/messages/${t.msg_id}`);
        if ((fresh?.draft?.de_answer ?? "").trim() !== text) {
          cache.set(t.msg_id, fresh);
          closeSheet();
          toast("Черновик изменился — проверьте новый текст", { ms: 4000 });
          paint(t, fresh);
          return;
        }
        const res = await api(`/api/ma/messages/${t.msg_id}/send`, { method: "POST", body: {mode: "as_is", force: false} });
        closeSheet();
        const o = sendOutcome(res ?? {});
        toast(o.text, { level: o.level === "bad" ? "bad" : "", ms: 4500 });
        laterRun = 0;
        if (o.level === "ok") stats.sent++;
        else stats.notSent++;  // режим выключен / тестовый адрес — клиенту не ушло, в итоге видно
        queue.shift();
        fling(card, 1, next);
      } catch (e) {
        const x = box.querySelector(".flow-err");
        x.textContent = e.status === 409 ? `${e.message} — откройте переписку.` : (e.message ?? String(e));
        x.hidden = false;
        sendBtn.disabled = false;
        sendBtn.textContent = "✅ Отправить";
      } finally {
        if (held) api(`/api/ma/messages/${t.msg_id}/lock/release`, { method: "POST" }).catch(() => { /* лок истечёт сам */ });
      }
    };
  }

  function done() {
    const box = el(`
      <div class="tr-done">
        <div class="td-ic"></div>
        <h2>Всё разобрано</h2>
        <p class="td-s"></p>
        <div class="td-acts">
          <a class="btn btn-primary" href="#/pipeline">Во Входящие</a>
          <button class="btn again" type="button">Ещё раз</button>
        </div>
      </div>`);
    box.querySelector(".td-ic").appendChild(icon("check"));
    const parts = [`отправлено ${stats.sent}`];
    if (stats.notSent) parts.push(`не ушло клиенту ${stats.notSent} (режим отправки)`);
    if (queue.length) parts.push(`отложено ${queue.length}`);
    if (stats.skipped) parts.push(`пропущено ${stats.skipped} (черновика нет)`);
    box.querySelector(".td-s").textContent = total ? parts.join(" · ") : "Готовых ответов нет — на всё ответили.";
    box.querySelector("h2").textContent = queue.length ? "Остались отложенные" : "Всё разобрано";
    box.querySelector(".again").addEventListener("click", () => render(mount));
    mount.replaceChildren(box);
  }

  next();
}

// Жесты карточки: вправо/влево — с порогом, вверх — от шапки
function bindCardSwipe(card, on) {
  let x0 = 0, y0 = 0, dx = 0, dy = 0, pid = null, mode = null;
  card.addEventListener("pointerdown", e => {
    if (e.button > 0 || e.target.closest("button")) return;
    x0 = e.clientX; y0 = e.clientY; dx = dy = 0; pid = e.pointerId; mode = null;
  });
  card.addEventListener("pointermove", e => {
    if (pid !== e.pointerId) return;
    dx = e.clientX - x0; dy = e.clientY - y0;
    if (!mode) {
      if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy)) mode = "x";
      else if (dy < -24 && Math.abs(dy) > Math.abs(dx) * 1.5 && e.target.closest(".t3-head")) mode = "y";
      else return;
      try { card.setPointerCapture(pid); } catch (err) { /* указатель отпущен */ }
      card.classList.add("drag");
    }
    if (mode === "x") {
      card.style.transform = `translateX(${dx}px) rotate(${dx / 22}deg)`;
      card.dataset.dir = dx > 40 ? "r" : dx < -40 ? "l" : "";
    } else {
      card.style.transform = `translateY(${Math.min(0, dy)}px)`;
    }
  });
  const end = () => {
    if (pid === null) return;
    const m = mode;
    pid = null; mode = null;
    card.style.transform = "";
    card.dataset.dir = "";
    if (m === "x" && dx > 90) on.right();
    else if (m === "x" && dx < -90) on.left();
    else if (m === "y" && dy < -80) on.up();
    else card.classList.remove("drag");
  };
  card.addEventListener("pointerup", end);
  card.addEventListener("pointercancel", end);
}
