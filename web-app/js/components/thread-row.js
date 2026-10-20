// Строка переписки во Входящих (вид A): аватар с цветом аккаунта и флагом
// языка, имя, пометки, объявление, последнее сообщение по-русски, термометр
// торга. Свайп влево открывает действия (Жду / Продано), долгое нажатие —
// режим выбора. data-key = thread_id (подсветка открытого на Fold).
import { el, parseEur, berlinTime, accountColor } from "../utils.js";
import { avatar, icon } from "./kit.js";
import { thermo, hasThermo } from "./thermo.js";

// Числа сделки из элемента pipeline (поля появятся у api; до того — что есть)
export function dealOf(t) {
  return {
    price: t.ad_price_eur ?? parseEur(t.ad_price),
    floor: t.floor_eur ?? null,
    offer: t.offer_eur ?? null,
    ours: t.our_last_eur ?? null,
  };
}

// Сегодня — «12:41», вчера — «вчера», раньше — «18.10»
export function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (isNaN(d)) return "";
  const tz = "Europe/Berlin";
  const day = x => new Intl.DateTimeFormat("ru-RU", { timeZone: tz, day: "2-digit", month: "2-digit", year: "numeric" }).format(x);
  const now = new Date();
  if (day(d) === day(now)) return new Intl.DateTimeFormat("ru-RU", { timeZone: tz, hour: "2-digit", minute: "2-digit" }).format(d);
  if (day(d) === day(new Date(now - 864e5))) return "вчера";
  return berlinTime(iso).split(",")[0];
}

const isFraud = t => (t.tags ?? []).some(x => /мошенник/i.test(x));

function marks(t) {
  const m = el(`<span class="r-marks"></span>`);
  const add = (name, cls, title) => {
    const s = el(`<span class="rm"></span>`);
    s.classList.add(cls);
    s.title = title;
    s.setAttribute("aria-label", title);
    s.appendChild(icon(name));
    m.appendChild(s);
  };
  if (t.is_hot) add("fire", "rm-hot", t.hot_reason ? `Горячий: ${t.hot_reason}` : "Горячий — готов купить");
  if (t.is_pinned) add("pin", "rm-pin", "Закреплено");
  if (isFraud(t)) add("warn", "rm-bad", "Отмечен как мошенник");
  else if (t.maybe_same) add("warn", "rm-warn", "Возможно, тот же покупатель");
  if (t.is_late) add("late", "rm-late", "Запоздалое письмо");
  if (t.from_app) add("phone", "rm-app", "Из приложения Kleinanzeigen");
  return m;
}

// opts: {onOpen, onWait, onSold, onLongPress, selecting, picked}
export function threadRow(t, opts = {}) {
  const rw = el(`
    <div class="rw">
      <div class="row-acts"></div>
      <div class="row" role="button" tabindex="0">
        <span class="av-wrap"></span>
        <div class="row-main">
          <div class="r1"><b class="r-name"></b><span class="r-time"></span></div>
          <div class="r2"><span class="r-acc"></span><span class="r-ad"></span></div>
          <div class="r3"><span class="r-msg"></span></div>
        </div>
      </div>
    </div>`);
  const row = rw.querySelector(".row");
  row.dataset.key = t.thread_id;
  const waitingUs = t.last_event_kind === "in";
  if (t.operator_unread) row.classList.add("unread");

  const avw = rw.querySelector(".av-wrap");
  avw.appendChild(avatar(t.buyer_display_name, t.account_id, t.client_lang));
  if (opts.selecting) {
    row.classList.add("selecting");
    const box = el(`<span class="pick-box"></span>`);
    box.appendChild(icon("check"));
    avw.appendChild(box);
    if (opts.picked) row.classList.add("picked");
    row.setAttribute("aria-pressed", opts.picked ? "true" : "false");
  }

  const name = rw.querySelector(".r-name");
  name.textContent = t.buyer_display_name || "Покупатель";
  name.after(marks(t));
  rw.querySelector(".r-time").textContent = shortTime(t.last_event_at);
  const acc = rw.querySelector(".r-acc");
  acc.style.setProperty("--acc", t.account_id != null ? accountColor(t.account_id) : "var(--line2)");
  rw.querySelector(".r-ad").textContent = t.ad_title || "(без названия)";

  let preview = (t.ru_client ?? "").replace(/\s+/g, " ").trim();
  if (!preview && t.deal_brief_json) {
    try {
      const b = typeof t.deal_brief_json === "string" ? JSON.parse(t.deal_brief_json) : t.deal_brief_json;
      if (b?.summary_ru) preview = b.summary_ru;
    } catch (e) { /* бриф битый — без превью */ }
  }
  rw.querySelector(".r-msg").textContent = preview || (waitingUs ? "ждёт ответа" : "ждём клиента");
  const r3 = rw.querySelector(".r3");
  if (t.is_autopilot) {
    const ap = el(`<span class="pill-ap"></span>`);
    ap.appendChild(icon("bot"));
    ap.append("авто");
    r3.appendChild(ap);
  } else if (waitingUs && t.pending_drafts_count > 0) {
    const p = el(`<span class="pill-draft"></span>`);
    p.appendChild(icon("spark"));
    p.append("готов");
    r3.appendChild(p);
  } else if (t.operator_unread) {
    r3.appendChild(el(`<span class="dot-unread"></span>`));
  }

  // Термометр торга + «предложение / цена»
  const d = dealOf(t);
  if (d.price != null) {
    const r4 = el(`<div class="r4"></div>`);
    if (hasThermo(d)) r4.appendChild(thermo(d, "xs"));
    const pr = el(`<span class="r-price"></span>`);
    if (d.offer != null) {
      const em = el(`<em></em>`);
      em.classList.add(d.floor != null && d.offer < d.floor ? "bad" : "good");
      em.textContent = Math.round(d.offer).toLocaleString("de-DE");
      pr.append(em, " / ");
    }
    pr.append(`${Math.round(d.price).toLocaleString("de-DE")} €`);
    r4.appendChild(pr);
    rw.querySelector(".row-main").appendChild(r4);
  }

  // Действия свайпа
  const acts = rw.querySelector(".row-acts");
  const addAct = (cls, ic, label, fn) => {
    if (!fn) return;
    const b = el(`<button class="ra"><span></span></button>`);
    b.classList.add(cls);
    b.prepend(icon(ic));
    b.querySelector("span").textContent = label;
    b.addEventListener("click", e => { e.stopPropagation(); close(); fn(t); });
    acts.appendChild(b);
  };
  addAct("ra-later", "clock", "Жду", waitingUs ? opts.onWait : null);
  addAct("ra-sold", "sold", "Продано", opts.onSold);
  rw.style.setProperty("--n", String(acts.children.length));

  function close() { rw.classList.remove("open"); }
  bindSwipe(rw, row, acts, opts);
  row.addEventListener("click", () => {
    if (rw.dataset.swiped) return;
    if (rw.classList.contains("open")) { close(); return; }
    opts.onOpen?.(t);
  });
  row.addEventListener("keydown", e => { if (e.key === "Enter") opts.onOpen?.(t); });
  return rw;
}

// Свайп влево — показать действия; долгое нажатие — режим выбора
function bindSwipe(rw, row, acts, opts) {
  let x0 = 0, y0 = 0, dx = 0, mode = null, base = 0, pid = null, lp = null;
  const width = () => acts.offsetWidth;
  row.addEventListener("pointerdown", e => {
    if (e.button > 0) return;
    x0 = e.clientX; y0 = e.clientY; dx = 0; mode = null; pid = e.pointerId;
    base = rw.classList.contains("open") ? -width() : 0;
    clearTimeout(lp);
    if (opts.onLongPress && !opts.selecting) {
      lp = setTimeout(() => {
        if (!mode) { rw.dataset.swiped = "1"; setTimeout(() => delete rw.dataset.swiped, 400); opts.onLongPress(); }
      }, 550);
    }
  });
  row.addEventListener("pointermove", e => {
    if (pid !== e.pointerId || opts.selecting || !acts.children.length) return;
    const mx = e.clientX - x0, my = e.clientY - y0;
    if (!mode) {
      if (Math.abs(mx) > 10 && Math.abs(mx) > Math.abs(my) * 1.3) {
        mode = "swipe"; clearTimeout(lp);
        try { row.setPointerCapture(pid); } catch (err) { /* указатель уже отпущен */ }
        rw.classList.add("drag");
      } else if (Math.abs(my) > 10) { mode = "scroll"; clearTimeout(lp); }
    }
    if (mode === "swipe") {
      dx = Math.min(0, Math.max(-width() - 24, base + mx));
      row.style.transform = `translateX(${dx}px)`;
      acts.style.setProperty("--reveal", Math.min(1, -dx / width()).toFixed(2));
    }
  });
  const end = () => {
    clearTimeout(lp);
    if (mode === "swipe") {
      rw.classList.remove("drag");
      row.style.transform = "";
      acts.style.removeProperty("--reveal");
      const open = dx < -width() * 0.35;
      rw.parentElement?.querySelectorAll(".rw.open").forEach(o => o !== rw && o.classList.remove("open"));
      rw.classList.toggle("open", open);
      rw.dataset.swiped = "1";
      setTimeout(() => delete rw.dataset.swiped, 60);
    }
    pid = null; mode = null;
  };
  row.addEventListener("pointerup", end);
  row.addEventListener("pointercancel", end);
}
