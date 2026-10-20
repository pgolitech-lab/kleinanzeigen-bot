// Экран треда (редизайн v3, вид A «премиум-мессенджер» + чипы цены из C).
// Открытие — один запрос POST /threads/{id}/open (тред + черновик + лок).
// Сверху полоса сделки (цена / минимум / клиент / мы + термометр), лента
// двуязычных пузырей, внизу черновик ИИ у дока: «Клиенту · DE» / «По-русски»,
// чипы цены (встречная, −5 %, без торга, короче, мягче, вариант) с кэшем.
// Отправка — 3 тапа: «Отправить» → финальный лист → «✅» (B-05/U-20).
// ИНВАРИАНТ: клиенту не уходит текст не на его языке без явной галочки
// оператора — кириллическая эвристика + LLM lang-check, force только по галочке,
// серверный 409-guard в /send — последняя линия.
import { api, API_BASE, LLM_TIMEOUT_MS } from "../api.js";
import { el, berlinTime, setLoading, setError, sendOutcome, parseEur, accountColor } from "../utils.js";
import { openSoldSheet } from "../components/sold-sheet.js";
import { setTitle } from "../components/backbar.js";
import { openSheet, closeSheet, menuSheet, confirmSheet } from "../components/sheet.js";
import { buildEditForm } from "../components/edit-form.js";
import { buildComposeForm } from "../components/compose-form.js";
import { buildAutopilotForm } from "../components/autopilot-form.js";
import { avatar, icon, langBadge, langName, toast } from "../components/kit.js";
import { thermo, hasThermo } from "../components/thermo.js";
import { initData } from "../tg.js";

const PENDING_STATUSES = new Set(["pending", "new", "edited", "approved"]);
const CYRILLIC_CLIENT_LANGS = new Set(["ru", "uk", "be", "bg", "sr", "mk"]);

// Module-state для lock release at unmount.
let _heldMsgId = null;
// Кэш вариантов черновика по чипам: `${msgId}:${chip}` → текст клиенту
const _variants = new Map();

// Кириллица при не-кириллическом языке клиента = почти наверняка ошибка.
// Зеркало серверного guard'а.
export function wrongLangForClient(text, clientLang) {
  const lang = (clientLang || "de").toLowerCase();
  if (CYRILLIC_CLIENT_LANGS.has(lang)) return false;
  const letters = String(text ?? "").match(/\p{L}/gu) || [];
  if (!letters.length) return false;
  const cyr = letters.filter(ch => ch >= "Ѐ" && ch <= "ӿ").length;
  return cyr / letters.length > 0.3;
}

async function tryReleaseLock(msgId) {
  if (msgId === null) return;
  try {
    await api(`/api/ma/messages/${msgId}/lock/release`, {method: "POST"});
  } catch (e) {
    console.warn("[thread] lock release failed:", e);
  }
}

function openThreadReq(threadId, focus) {
  return api(`/api/ma/threads/${encodeURIComponent(threadId)}/open`, {
    method: "POST", body: {focus: focus ?? null, acquire: true},
  });
}

export async function render(mount, params, opts = {}) {
  // Перерисовка из обработчика, завершившегося уже после ухода с экрана
  // (mount отцеплен роутером) — ничего не делаем.
  if (!mount.isConnected) return;
  if (_heldMsgId !== null) {
    await tryReleaseLock(_heldMsgId);
    _heldMsgId = null;
  }
  setLoading(mount, "Открываю переписку…");
  try {
    let data = await openThreadReq(params.thread_id, params.focus_msg_id ? Number(params.focus_msg_id) : null);
    // Deep-link на уже отправленный черновик: показываем как историю и берём
    // актуальный неотправленный черновик треда (если есть).
    if (data.review && !PENDING_STATUSES.has(data.review.status)) {
      if (data.lock?.acquired) tryReleaseLock(data.review.msg_id);
      data = await openThreadReq(params.thread_id, null);
      if (data.review && !PENDING_STATUSES.has(data.review.status)) { data.review = null; data.lock = null; }
    }
    const review = data.review;
    if (review && data.lock?.acquired) {
      if (!mount.isConnected) {
        // Ушли, пока открывали, — сразу отдаём лок (если его не держит новый экран)
        if (_heldMsgId !== review.msg_id) tryReleaseLock(review.msg_id);
        return;
      }
      _heldMsgId = review.msg_id;
    }
    if (!mount.isConnected) return;
    renderThread(mount, params, data, opts);
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
  }
}

// ---------- кусочки разметки ----------

function sendResultBanner(res) {
  const o = sendOutcome(res);
  const b = el(`<div class="alert small py-2"></div>`);
  b.classList.add(o.level === "ok" ? "alert-success" : o.level === "bad" ? "alert-danger" : "alert-warning");
  b.textContent = o.text;
  return b;
}

// Числа сделки: поля api (offer/floor/our_last) + бриф + автопилот
function dealNumbers(data) {
  const r = data.review, h = data.header ?? {};
  const brief = r?.deal_brief ?? {};
  return {
    price: r?.ad_price_eur ?? h.ad_price_eur ?? parseEur(r?.ad?.price ?? h.ad_price),
    floor: r?.floor_eur ?? h.floor_eur ?? r?.autopilot?.floor_eur ?? null,
    offer: r?.offer_eur ?? h.offer_eur ?? null,
    ours: r?.our_last_eur ?? h.our_last_eur ?? null,
    sugg: brief.negotiated_price_eur != null ? Number(brief.negotiated_price_eur) : null,
  };
}

const fmtEur = n => (n == null ? "—" : `${Math.round(n).toLocaleString("de-DE")} €`);

function threadHead(data, onClient) {
  const h = data.header ?? {};
  const lang = data.review?.client_lang;
  const head = el(`
    <div class="th-head">
      <button class="th-who" type="button">
        <span class="th-av"></span>
        <span class="th-txt"><b class="th-name"></b><small class="th-sub"><i class="dot"></i><span class="th-acc"></span> · <span class="th-ad"></span></small></span>
      </button>
      <a class="ibtn th-link" target="_blank" rel="noopener" aria-label="Объявление на Kleinanzeigen">↗</a>
    </div>`);
  head.querySelector(".th-av").appendChild(avatar(h.buyer_display_name, h.account_id, lang, "md"));
  head.querySelector(".th-name").textContent = h.buyer_display_name || "Покупатель";
  head.querySelector(".dot").style.background = h.account_id != null ? accountColor(h.account_id) : "var(--line2)";
  head.querySelector(".th-acc").textContent = h.account_name ?? "аккаунт";
  head.querySelector(".th-ad").textContent = h.ad_title ?? "(без названия)";
  head.querySelector(".th-who").addEventListener("click", onClient);
  const link = head.querySelector(".th-link");
  if (h.ad_url) link.href = h.ad_url; else link.remove();
  return head;
}

function chipsLine(data) {
  const h = data.header ?? {};
  const line = el(`<div class="th-chips"></div>`);
  const add = (cls, text) => { const c = el(`<span class="tchip"></span>`); c.classList.add(cls); c.textContent = text; line.appendChild(c); };
  if ((h.tags ?? data.review?.tags ?? []).some(t => /мошенник/i.test(t))) add("bad", "⚠️ отмечен как мошенник — не высылать товар до оплаты");
  if (h.is_autopilot) add("warn", "🤖 автопилот");
  if (data.lock && !data.lock.acquired && data.lock.holder) add("bad", `в работе у ${data.lock.holder} · ${data.lock.remaining_min ?? "?"} мин`);
  return line.children.length ? line : null;
}

// Полоса сделки (A): цифры + термометр; тап — бриф ИИ
function dealStrip(data) {
  const d = dealNumbers(data);
  if (d.price == null && d.offer == null) return null;
  const strip = el(`
    <div class="deal-strip" role="button" tabindex="0" aria-expanded="false">
      <div class="ds-nums"></div>
      <div class="ds-scale"></div>
      <div class="ds-more"></div>
    </div>`);
  const nums = strip.querySelector(".ds-nums");
  const cell = (label, v, cls) => {
    if (v == null) return;
    const c = el(`<div class="dn"><small></small><b></b></div>`);
    if (cls) c.classList.add(cls);
    c.querySelector("small").textContent = label;
    c.querySelector("b").textContent = Math.round(v).toLocaleString("de-DE");
    nums.appendChild(c);
  };
  cell("цена", d.price, "");
  cell("минимум", d.floor, "floor");
  cell("клиент", d.offer, d.floor != null && d.offer != null && d.offer < d.floor ? "bad" : "offer");
  cell("мы", d.ours, "ours");
  if (hasThermo(d)) strip.querySelector(".ds-scale").appendChild(thermo(d, "lg"));
  else strip.querySelector(".ds-scale").remove();

  const more = strip.querySelector(".ds-more");
  const brief = data.review?.deal_brief;
  const lines = [];
  if (d.offer != null && d.floor != null && d.offer < d.floor) lines.push(["bad", `Клиент ниже минимума на ${fmtEur(d.floor - d.offer)}`]);
  if (brief?.summary_ru) lines.push(["", brief.summary_ru]);
  if (brief?.expected_next) lines.push(["", `Дальше: ${brief.expected_next}`]);
  if (brief?.client_assessment) lines.push(["", `Клиент: ${brief.client_assessment}`]);
  lines.forEach(([cls, t]) => { const p = el(`<p></p>`); if (cls) p.classList.add(cls); p.textContent = t; more.appendChild(p); });
  if (!lines.length) more.remove();
  else {
    const toggle = () => {
      const open = strip.classList.toggle("open");
      strip.setAttribute("aria-expanded", open ? "true" : "false");
    };
    strip.addEventListener("click", toggle);
    strip.addEventListener("keydown", e => { if (e.key === "Enter") toggle(); });
  }
  return strip;
}

// Связанные треды: точно он (тот же email) и «возможно тот же покупатель»
// (то же имя в том же аккаунте — у Kleinanzeigen relay-почта своя на каждое объявление)
function relatedCard(related) {
  const exact = related?.matches ?? [], maybe = related?.maybe_same ?? [];
  if (!exact.length && !maybe.length) return null;
  const box = el(`<div class="maybe-same"><div class="ms-ic"></div><div class="ms-t"></div></div>`);
  box.querySelector(".ms-ic").appendChild(icon("warn"));
  const t = box.querySelector(".ms-t");
  const group = (title, list) => {
    if (!list.length) return;
    const b = el(`<b></b>`);
    b.textContent = title;
    const l = el(`<div class="ms-list"></div>`);
    list.forEach(m => {
      const a = el(`<a></a>`);
      a.href = `#/thread/${encodeURIComponent(m.thread_id)}`;
      a.textContent = `${m.ad_title ?? "?"} · ${m.ad_price ?? "?"} · ${berlinTime(m.last_at)}`;
      l.appendChild(a);
    });
    t.append(b, l);
  };
  group("Этот покупатель уже писал", exact);
  group("Возможно, тот же покупатель", maybe);
  return box;
}

// Язык пузырей: по-русски крупно (оператор не читает по-немецки) или оригинал
function bubbleMainRu() {
  try { return localStorage.getItem("kab_bub_main") !== "orig"; } catch (e) { return true; }
}
function setBubbleMainRu(v) {
  try { localStorage.setItem("kab_bub_main", v ? "ru" : "orig"); } catch (e) { /* без запоминания */ }
}

function bubble(ev) {
  const isIn = ev.kind === "in";
  const b = el(`<div class="bub2"><div class="b-main"></div><div class="b-alt"></div><div class="b-meta"></div></div>`);
  b.classList.add(isIn ? "in" : "out");
  const orig = ev.text ?? "", ru = ev.ru_text && ev.ru_text !== ev.text ? ev.ru_text : null;
  const paint = () => {
    const ruMain = bubbleMainRu() && ru;
    b.querySelector(".b-main").textContent = ruMain ? ru : orig;
    const alt = b.querySelector(".b-alt");
    alt.textContent = ru ? (ruMain ? orig : ru) : "";
    alt.hidden = !ru;
  };
  paint();
  if (ru) {
    b.classList.add("bilingual");
    b._paint = paint;
    b.addEventListener("click", () => {
      setBubbleMainRu(!bubbleMainRu());
      // перекрасить все пузыри ленты разом
      b.parentElement?.querySelectorAll(".bub2").forEach(x => x._paint?.());
    });
  }
  const meta = b.querySelector(".b-meta");
  const tags = [];
  if (!isIn && ev.is_auto_ack) tags.push("авто-привет");
  if (ev.status === "sent_external" || ev.from_app) tags.push("📱 из приложения");
  if (ev.is_late) tags.push("⏰ запоздалое");
  if (!isIn && ev.status === "sent_debug") tags.push("🧪 тестовый адрес");
  tags.forEach(t => { const s = el(`<span class="b-tag"></span>`); s.textContent = t; meta.appendChild(s); });
  const time = el(`<time></time>`);
  time.textContent = berlinTime(ev.ts);
  meta.appendChild(time);
  return b;
}

function dayLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z");
  if (isNaN(d)) return "";
  const f = x => new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Berlin", day: "numeric", month: "long" }).format(x);
  const today = f(new Date()), yest = f(new Date(Date.now() - 864e5)), day = f(d);
  return day === today ? "Сегодня" : day === yest ? "Вчера" : day;
}

function feed(data) {
  const box = el(`<div class="feed"></div>`);
  // Исходящее с pending-статусом — черновик, а не отправленное письмо (B-02)
  const events = (data.events ?? [])
    .filter(e => !(e.kind === "out" && PENDING_STATUSES.has(e.status)))
    .sort((a, b) => (a.ts || "") < (b.ts || "") ? -1 : (a.ts || "") > (b.ts || "") ? 1 : 0);
  if (!events.length) {
    box.appendChild(el(`<p class="muted-c">Событий пока нет.</p>`));
    return box;
  }
  let lastDay = null;
  events.forEach(ev => {
    const day = dayLabel(ev.ts);
    if (day && day !== lastDay) {
      const s = el(`<div class="day-sep"><span></span></div>`);
      s.querySelector("span").textContent = day;
      box.appendChild(s);
      lastDay = day;
    }
    box.appendChild(bubble(ev));
  });
  return box;
}

const LANG_PHRASE = { de: "по-немецки", pl: "по-польски", nl: "по-нидерландски", tr: "по-турецки", fr: "по-французски",
  en: "по-английски", ru: "по-русски", uk: "по-украински", it: "по-итальянски", es: "по-испански" };
const langPhrase = code => LANG_PHRASE[code] ?? `на ${code.toUpperCase()}`;

// ---------- основной рендер ----------

function renderThread(mount, params, data, opts = {}) {
  const review = data.review;
  const msgId = review?.msg_id ?? null;
  const lockedByOther = !!(review && data.lock && !data.lock.acquired);
  const canAct = !!review && !lockedByOther;
  const h = data.header ?? {};
  const clientLang = (review?.client_lang || "de").toLowerCase();
  const LANG = clientLang.toUpperCase();
  setTitle(h.buyer_display_name || h.ad_title || "Переписка");

  const root = el(`<div class="th"><div class="th-main"></div><aside class="th-aside"></aside></div>`);
  const main = root.querySelector(".th-main");

  main.appendChild(threadHead(data, historyHandler));
  const chips = chipsLine(data);
  if (chips) main.appendChild(chips);
  const strip = dealStrip(data);
  if (strip) main.appendChild(strip);

  if (opts.conflictText) {
    const b = el(`<div class="alert alert-warning small py-2"><div class="t"></div>
      <button class="btn btn-sm btn-primary mt-2 compose-btn">✉️ Написать клиенту</button></div>`);
    b.querySelector(".t").textContent = opts.conflictText;
    b.querySelector(".compose-btn").addEventListener("click", () => composeHandler());
    main.appendChild(b);
  } else if (opts.sendResult) {
    main.appendChild(sendResultBanner(opts.sendResult));
  }
  const rel = relatedCard(data.related);
  if (rel) main.appendChild(rel);
  main.appendChild(feed(data));

  if (lockedByOther) main.appendChild(lockedBanner());

  // ---------- черновик ----------
  let lastDe = review?.draft?.de_answer ?? "";
  let lastRu = review?.draft?.ru_answer ?? "";
  const draftBox = review ? draftCard() : null;
  if (draftBox) main.appendChild(draftBox);

  // ---------- док ----------
  main.appendChild(dock());

  // Fold ландшафт: справа панель сделки и клиента
  fillAside(root.querySelector(".th-aside"));

  mount.replaceChildren(root);
  // к последнему письму (U-02)
  requestAnimationFrame(() => {
    // в самый низ: последнее письмо, черновик и чипы над доком
    const pane = mount.closest(".pane");
    if (pane) pane.scrollTop = pane.scrollHeight;
  });

  // ===== черновик: «Клиенту · DE» / «По-русски» + чипы =====
  function draftCard() {
    const card = el(`
      <section class="draft2">
        <div class="d2-head">
          <span class="d2-t"></span>
          <span class="d2-badge"></span>
          <span class="d2-sp"></span>
          <button class="ibtn sm d2-lessons" type="button" hidden></button>
        </div>
        <div class="d2-tabs" role="tablist">
          <button type="button" class="on" data-tab="client"></button>
          <button type="button" data-tab="ru">По-русски</button>
        </div>
        <div class="d2-pane" data-pane="client">
          <textarea class="d2-de" rows="3" aria-label="Текст клиенту"></textarea>
          <div class="d2-mirror"></div>
        </div>
        <div class="d2-pane" data-pane="ru" hidden>
          <textarea class="d2-ru" rows="3" aria-label="Идея по-русски"></textarea>
          <div class="d2-hint"></div>
        </div>
        <div class="d2-err" hidden></div>
        <div class="pult"></div>
      </section>`);
    const t = card.querySelector(".d2-t");
    t.prepend(icon("spark"));
    t.append(" Черновик ИИ");
    card.querySelector('[data-tab="client"]').textContent = `Клиенту · ${LANG}`;
    card.querySelector(".d2-hint").textContent = `Измените идею — при отправке ИИ переведёт её: ${langName(clientLang)}.`;
    const de = card.querySelector(".d2-de"), ru = card.querySelector(".d2-ru");
    de.value = lastDe;
    ru.value = lastRu;
    const badgeSlot = card.querySelector(".d2-badge");
    const paintBadge = () => {
      const bad = wrongLangForClient(de.value, clientLang);
      badgeSlot.replaceChildren(langBadge(bad ? "RU" : LANG, !bad));
    };
    paintBadge();
    de.addEventListener("input", () => { paintBadge(); autoGrow(de); });
    ru.addEventListener("input", () => autoGrow(ru));
    requestAnimationFrame(() => { autoGrow(de); autoGrow(ru); });
    paintMirror(card);
    card.querySelectorAll("[data-tab]").forEach(btn => btn.addEventListener("click", () => {
      card.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b === btn));
      card.querySelectorAll("[data-pane]").forEach(p => { p.hidden = p.dataset.pane !== btn.dataset.tab; });
      autoGrow(btn.dataset.tab === "ru" ? ru : de);
    }));
    // «уроки» — правки оператора, которые ИИ учитывает (если api отдаёт)
    const lessons = review?.lessons ?? [];
    const lb = card.querySelector(".d2-lessons");
    if (lessons.length) {
      lb.hidden = false;
      lb.textContent = `🎓 ${lessons.length}`;
      lb.setAttribute("aria-label", `Уроки: ${lessons.length}`);
      lb.addEventListener("click", () => lessonsSheet(lessons));
    }
    if (!canAct) { de.readOnly = true; ru.readOnly = true; }
    else buildPult(card.querySelector(".pult"));
    return card;
  }

  function paintMirror(card = draftBox) {
    const m = card?.querySelector(".d2-mirror");
    if (!m) return;
    const tr = review?.draft?.ru_translation;
    m.textContent = tr && tr !== lastDe ? `RU: ${tr}` : "";
    m.hidden = !m.textContent;
  }

  function autoGrow(ta) {
    if (!ta.isConnected || ta.offsetParent === null) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight + 2, Math.max(140, window.innerHeight * 0.4))}px`;
  }

  function draftError(msg) {
    const e = draftBox?.querySelector(".d2-err");
    if (!e) { if (msg) toast(`Ошибка: ${msg}`, { level: "bad", ms: 5000 }); return; }
    e.textContent = msg;
    e.hidden = !msg;
  }

  // Синхронизация после серверных мутаций (translate/instruction/edit/regenerate)
  function syncDraft(fresh) {
    if (!fresh?.draft) return;
    review.draft = fresh.draft;
    lastDe = fresh.draft.de_answer ?? "";
    lastRu = fresh.draft.ru_answer ?? "";
    if (draftBox) {
      const de = draftBox.querySelector(".d2-de"), ru = draftBox.querySelector(".d2-ru");
      de.value = lastDe;
      ru.value = lastRu;
      de.dispatchEvent(new Event("input"));
      autoGrow(ru);
      paintMirror();
    }
  }

  // Правки из полей — на сервер до отправки (edit-ru перегенерирует текст клиенту из идеи)
  async function flushEdits() {
    const deNow = draftBox.querySelector(".d2-de").value.trim();
    const ruNow = draftBox.querySelector(".d2-ru").value.trim();
    if (ruNow && ruNow !== lastRu.trim()) {
      syncDraft(await api(`/api/ma/messages/${msgId}/edit-ru`, { method: "POST", body: {text: ruNow}, timeoutMs: LLM_TIMEOUT_MS }));
    } else if (deNow !== lastDe.trim()) {
      syncDraft(await api(`/api/ma/messages/${msgId}/edit-de`, { method: "POST", body: {text: deNow} }));
    }
  }

  // ===== пульт: чипы цены и тона (из концепции C) =====
  function buildPult(pult) {
    const d = dealNumbers(data);
    const base = d.ours ?? d.price;
    const list = [];
    if (d.floor != null && (d.offer == null || d.offer < d.floor)) {
      list.push({ key: `price:${d.floor}`, label: `🤝 Встречная ${fmtEur(d.floor)}`, price: d.floor });
    } else if (d.offer != null && base != null && d.offer < base) {
      const mid = Math.round((d.offer + base) / 2 / 5) * 5;
      list.push({ key: `price:${mid}`, label: `🤝 Встречная ${fmtEur(mid)}`, price: mid });
    }
    if (base != null) {
      const p5 = Math.round(base * 0.95 / 5) * 5;
      if (!list.some(x => x.price === p5)) list.push({ key: `price:${p5}`, label: `−5 % · ${fmtEur(p5)}`, price: p5 });
    }
    list.push({ key: "s:fest", label: "🔒 Без торга", strategy: "fest" });
    list.push({ key: "s:short", label: "Короче", strategy: "short" });
    list.push({ key: "s:friend", label: "Мягче", strategy: "friend" });
    list.push({ key: "s:regen", label: "🔁 Вариант", strategy: "regen" });
    list.forEach(c => {
      const b = el(`<button type="button" class="pchip"></button>`);
      b.textContent = c.label;
      if (c.price != null && d.floor != null && c.price < d.floor) b.classList.add("below");
      b.addEventListener("click", () => applyChip(c, b, d));
      pult.appendChild(b);
    });
  }

  async function applyChip(c, btn, d) {
    if (c.price != null && d.floor != null && c.price < d.floor &&
        !(await confirmSheet(`Ниже минимума ${fmtEur(d.floor)} — точно предложить ${fmtEur(c.price)}?`, "Да, предложить"))) return;
    const pult = btn.parentElement;
    pult.querySelectorAll(".pchip").forEach(x => { x.disabled = true; x.classList.remove("on"); });
    btn.classList.add("busy");
    draftError("");
    const cacheKey = `${msgId}:${c.key}`;
    try {
      const cached = _variants.get(cacheKey);
      let fresh;
      if (cached) {
        // вариант уже генерировали — возвращаем без LLM
        fresh = await api(`/api/ma/messages/${msgId}/edit-de`, { method: "POST", body: {text: cached} });
      } else if (c.price != null) {
        fresh = await api(`/api/ma/messages/${msgId}/price`, { method: "POST", body: {eur: c.price}, timeoutMs: LLM_TIMEOUT_MS });
      } else {
        fresh = await api(`/api/ma/messages/${msgId}/regenerate`, { method: "POST", body: {strategy: c.strategy}, timeoutMs: LLM_TIMEOUT_MS });
      }
      syncDraft(fresh);
      if (c.strategy !== "regen" && review.draft?.de_answer) _variants.set(cacheKey, review.draft.de_answer);
      btn.classList.add("on");
    } catch (e) {
      draftError(e.message ?? String(e));
    } finally {
      btn.classList.remove("busy");
      pult.querySelectorAll(".pchip").forEach(x => { x.disabled = false; });
    }
  }

  function lessonsSheet(lessons) {
    const box = el(`<div class="lessons"></div>`);
    lessons.forEach(l => {
      const it = el(`<div class="lesson"><div class="l-bad"></div><div class="l-good"></div></div>`);
      it.querySelector(".l-bad").textContent = l.bad ?? l.bot_draft ?? "";
      it.querySelector(".l-good").textContent = l.good ?? l.operator_edit ?? "";
      box.appendChild(it);
    });
    openSheet(box, "🎓 Уроки: так ИИ больше не пишет");
  }

  // ===== док =====
  function dock() {
    const dk = el(`<div class="tdock"></div>`);
    const soldBtn = el(`<button class="ibtn tonal d-sold" type="button" aria-label="Продано"></button>`);
    soldBtn.appendChild(icon("money"));
    soldBtn.addEventListener("click", soldHandler);
    dk.appendChild(soldBtn);
    if (canAct) {
      const send = el(`<button class="btn-send" type="button"><span class="bs-t"><b></b><small></small></span></button>`);
      send.appendChild(icon("send"));
      const bl = send.querySelector("b");
      bl.append(el(`<span class="l-long"></span>`), el(`<span class="l-short">Отправить</span>`));
      bl.querySelector(".l-long").textContent = `Отправить ${langPhrase(clientLang)}`;
      send.querySelector("small").textContent = "язык проверю перед отправкой";
      send.addEventListener("click", () => sendHandler(send));
      dk.appendChild(send);
    } else if (!review) {
      const sug = el(`<button class="btn" type="button">🤖 Предложить</button>`);
      sug.addEventListener("click", () => suggestHandler(sug));
      const cmp = el(`<button class="btn btn-primary" type="button">✉️ Написать</button>`);
      cmp.addEventListener("click", composeHandler);
      dk.append(sug, cmp);
    } else {
      dk.appendChild(el(`<div class="dock-ro">Черновик занят другим оператором</div>`));
    }
    const more = el(`<button class="ibtn tonal" type="button" aria-label="Ещё действия">⋯</button>`);
    more.addEventListener("click", moreMenu);
    dk.appendChild(more);
    return dk;
  }

  // ===== отправка: 3 тапа =====
  async function sendHandler(btn) {
    const deNow = draftBox.querySelector(".d2-de").value.trim();
    if (!deNow && !draftBox.querySelector(".d2-ru").value.trim()) { draftError("Текст клиенту пуст"); return; }
    btn.disabled = true;
    btn.classList.add("busy");
    try {
      await flushEdits();
    } catch (e) {
      draftError(e.message ?? String(e));
      return;
    } finally {
      btn.disabled = false;
      btn.classList.remove("busy");
    }
    openFinalSheet();
  }

  // Финальный лист: ровно то, что уйдёт клиенту. Двухслойный языковой guard.
  function openFinalSheet() {
    const text = (review?.draft?.de_answer ?? "").trim();
    const box = el(`
      <div class="final">
        <div class="f-head"><span class="f-l">Уйдёт клиенту</span><span class="f-badge"></span></div>
        <div class="f-text"></div>
        <div class="f-ru"></div>
        <div class="f-status"></div>
        <div class="mismatch" hidden>
          <div class="alert alert-danger small py-2 mb-2 mm-text"></div>
          <button class="btn btn-primary w-100 tr-btn mb-2" type="button"></button>
          <label class="form-check"><input type="checkbox" class="form-check-input allow-lang">
            <span class="form-check-label">Разрешаю отправить текст не на языке клиента</span></label>
        </div>
        <div class="flow-err text-danger small my-2" hidden></div>
        <div class="sheet-actions">
          <button class="btn edit-btn" type="button">✏️ Править</button>
          <button class="btn btn-primary send-btn" type="button">✅ Отправить</button>
        </div>
      </div>`);
    box.querySelector(".f-text").textContent = text || "(текст пуст)";
    const mirror = review?.draft?.ru_translation;
    const ruEl = box.querySelector(".f-ru");
    if (mirror && mirror !== text) ruEl.textContent = `RU: ${mirror}`; else ruEl.remove();
    const badge = box.querySelector(".f-badge");
    const sendBtn = box.querySelector(".send-btn"), editBtn = box.querySelector(".edit-btn");
    const allowCb = box.querySelector(".allow-lang"), statusEl = box.querySelector(".f-status");
    const trBtn = box.querySelector(".tr-btn");
    const trLabel = `🌐 Перевести: ${langName(clientLang)}`;
    trBtn.textContent = trLabel;
    let blocked = false, checking = false, sending = false;
    const sync = () => { sendBtn.disabled = sending || checking || !text || (blocked && !allowCb.checked); };
    const showErr = e => { const x = box.querySelector(".flow-err"); x.textContent = e.message ?? String(e); x.hidden = false; };
    const block = msg => {
      blocked = true;
      badge.replaceChildren(langBadge(LANG, false));
      box.querySelector(".mm-text").textContent = msg;
      box.querySelector(".mismatch").hidden = false;
      sync();
    };
    allowCb.addEventListener("change", sync);
    trBtn.addEventListener("click", async () => {
      trBtn.disabled = true;
      trBtn.textContent = "⏳ Перевожу…";
      try {
        const fresh = await api(`/api/ma/messages/${msgId}/translate-draft`, { method: "POST", body: {text}, timeoutMs: LLM_TIMEOUT_MS });
        syncDraft(fresh);
        openFinalSheet();
      } catch (e) {
        showErr(e);
        trBtn.disabled = false;
        trBtn.textContent = trLabel;
      }
    });

    if (wrongLangForClient(text, clientLang)) {
      block(`Текст не на языке клиента (${langName(clientLang)}). Переведите или явно разрешите отправку.`);
    } else {
      checking = true;
      badge.replaceChildren(langBadge(LANG, null));
      statusEl.textContent = "Проверяю язык…";
      sync();
      api(`/api/ma/messages/${msgId}/lang-check`, { method: "POST", body: {text} }).then(r => {
        checking = false;
        if (r.match) {
          badge.replaceChildren(langBadge(LANG, true));
          statusEl.textContent = `Проверено: язык клиента (${langName(clientLang)})`;
        } else {
          statusEl.textContent = "";
          block(`ИИ определил язык текста: ${langName(r.detected_lang)}, а клиент пишет: ${langName(clientLang)}. Переведите или явно разрешите отправку.`);
        }
        sync();
      }).catch(() => {
        // LLM-проверка недоступна: эвристика уже пропустила текст, серверный 409 — страховка
        checking = false;
        badge.replaceChildren(langBadge(LANG, true));
        statusEl.textContent = "⚠️ ИИ-проверка языка недоступна — сработала только быстрая проверка";
        sync();
      });
    }

    sendBtn.addEventListener("click", async () => {
      sending = true;
      sendBtn.disabled = true;
      editBtn.disabled = true;
      sendBtn.textContent = "⏳ Отправляю…";
      try {
        // force=true только при явной галочке-разрешении оператора
        const res = await api(`/api/ma/messages/${msgId}/send`, {
          method: "POST", body: {mode: "as_is", force: blocked && allowCb.checked === true},
        });
        closeSheet();
        const o = sendOutcome(res ?? {});
        toast(o.text, { level: o.level === "bad" ? "bad" : "", ms: 4500 });
        render(mount, {...params, focus_msg_id: null}, {sendResult: res ?? {}});
      } catch (e) {
        if (e.status === 409 && /уже отправлено/i.test(e.message)) {
          closeSheet();
          render(mount, {...params, focus_msg_id: null}, {conflictText: e.message});
          return;
        }
        showErr(e);
        sending = false;
        editBtn.disabled = false;
        sendBtn.textContent = "✅ Отправить";
        sync();
      }
    });
    editBtn.addEventListener("click", () => openTextSheet({
      title: "✏️ Текст клиенту", initial: text, submitLabel: "💾 Сохранить", busyLabel: "⏳ Сохраняю…",
      onSubmit: async value => {
        syncDraft(await api(`/api/ma/messages/${msgId}/edit-de`, { method: "POST", body: {text: value} }));
        openFinalSheet();
      },
      onBack: () => openFinalSheet(),
    }));
    openSheet(box, "Финальный текст");
  }

  function openTextSheet({title, initial, placeholder, submitLabel, busyLabel, onSubmit, onBack}) {
    const box = el(`
      <div>
        <textarea class="form-control txt" rows="6"></textarea>
        <div class="flow-err text-danger small my-2" hidden></div>
        <div class="sheet-actions">
          <button class="btn back-btn" type="button">↩ Назад</button>
          <button class="btn btn-primary ok-btn" type="button"></button>
        </div>
      </div>`);
    const txt = box.querySelector(".txt");
    txt.value = initial ?? "";
    if (placeholder) txt.placeholder = placeholder;
    const ok = box.querySelector(".ok-btn"), back = box.querySelector(".back-btn");
    ok.textContent = submitLabel;
    back.addEventListener("click", onBack);
    ok.addEventListener("click", async () => {
      const value = txt.value.trim();
      if (!value) { txt.classList.add("is-invalid"); return; }
      ok.disabled = true;
      back.disabled = true;
      ok.textContent = busyLabel;
      try {
        await onSubmit(value);
      } catch (e) {
        const x = box.querySelector(".flow-err");
        x.textContent = e.message ?? String(e);
        x.hidden = false;
        ok.disabled = false;
        back.disabled = false;
        ok.textContent = submitLabel;
      }
    });
    openSheet(box, title);
    txt.focus();
  }

  // ===== прочие действия =====
  function suggestHandler(btn) {
    if (btn) { btn.disabled = true; btn.textContent = "🤖 Генерирую…"; }
    api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/suggest-reply`, {method: "POST", timeoutMs: LLM_TIMEOUT_MS})
      .then(() => render(mount, params))
      .catch(e => {
        toast(`Ошибка: ${e.message ?? String(e)}`, { level: "bad", ms: 5000 });
        if (btn) { btn.disabled = false; btn.textContent = "🤖 Предложить"; }
      });
  }

  function composeHandler() {
    const form = buildComposeForm({
      threadId: params.thread_id,
      onSubmitComplete: res => { closeSheet(); render(mount, params, {sendResult: res ?? {}}); },
      onCancel: () => closeSheet(),
    });
    openSheet(form, "✉️ Написать клиенту");
  }

  function historyHandler() {
    const email = h.buyer_email;
    if (!email) { toast("Почта клиента неизвестна"); return; }
    location.hash = `#/client/${encodeURIComponent(email)}`;
  }

  async function waitHandler() {
    try {
      await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/wait`, {method: "POST"});
      toast(`✋ Ждём ответа: ${h.buyer_display_name || "покупатель"}`);
      // с телефона — обратно во Входящие (S-05); на Fold список и так слева
      if (!document.querySelector("#app.has-list")) location.hash = "#/pipeline";
      else render(mount, params);
    } catch (e) {
      toast(`Ошибка: ${e.message ?? String(e)}`, { level: "bad", ms: 5000 });
    }
  }

  function instructionHandler() {
    const form = buildEditForm({
      msgId, field: "instruction", review,
      onSubmitComplete: () => { closeSheet(); render(mount, params); },
      onCancel: () => closeSheet(),
      onError: msg => toast(`Ошибка: ${msg}`, { level: "bad", ms: 5000 }),
    });
    openSheet(form, "📝 Своя инструкция для ИИ");
  }

  async function skipHandler() {
    if (!(await confirmSheet("Не отвечать на это письмо?", "Не отвечать"))) return;
    try {
      await api(`/api/ma/messages/${msgId}/skip`, {method: "POST"});
      render(mount, params);
    } catch (e) {
      toast(`Ошибка: ${e.message ?? String(e)}`, { level: "bad", ms: 5000 });
    }
  }

  function soldHandler() {
    const d = dealNumbers(data);
    const lastMsg = msgId ?? [...(data.events ?? [])].reverse().find(e => e.msg_id)?.msg_id ?? null;
    openSoldSheet({
      threadId: params.thread_id, msgId: lastMsg, title: h.ad_title,
      price: d.ours ?? d.offer ?? d.price, prices: [d.price, d.ours, d.offer, d.floor],
      onDone: () => render(mount, params),
    });
  }

  const autopilotState = review?.autopilot ?? (h.is_autopilot ? {active: true} : null);

  async function autopilotHandler() {
    if (autopilotState?.active) {
      const info = `${autopilotState.messages_sent ?? "?"} отправлено · минимум ${autopilotState.floor_eur ?? "?"} €`;
      if (!(await confirmSheet(`Остановить автопилот?\n(${info})`, "Остановить"))) return;
      try {
        await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/autopilot/stop`, {method: "POST"});
        render(mount, params);
      } catch (e) {
        toast(`Ошибка: ${e.message ?? String(e)}`, { level: "bad", ms: 5000 });
      }
    } else {
      const form = buildAutopilotForm({
        threadId: params.thread_id,
        onSubmitComplete: () => { closeSheet(); render(mount, params); },
        onCancel: () => closeSheet(),
      });
      openSheet(form, "🚀 Автопилот");
    }
  }

  function moreMenu() {
    menuSheet([
      { label: "👤 Клиент и история", onClick: historyHandler },
      review ? null : { label: "🤖 Предложить ответ ИИ", onClick: () => suggestHandler(null) },
      { label: "✉️ Написать своими словами", onClick: composeHandler },
      canAct ? { label: "📝 Своя инструкция для ИИ", onClick: instructionHandler } : null,
      { label: "✋ Жду ответа клиента", onClick: waitHandler },
      { label: autopilotState?.active ? "🛑 Остановить автопилот" : "🚀 Автопилот…", onClick: autopilotHandler },
      { label: "💰 Продано…", onClick: soldHandler },
      canAct ? { label: "🙅 Не отвечать", danger: true, onClick: skipHandler } : null,
    ], "Действия");
  }

  function lockedBanner() {
    const lock = data.lock ?? {};
    const box = el(`
      <div class="alert alert-warning">
        <div class="mb-1"><b>Черновик занят: <span class="holder"></span></b> <small>(~<span class="mins"></span> мин)</small></div>
        <div class="small mb-2">Если оператор уже закрыл MA — заберите черновик, чтобы ответить.</div>
        <div class="d-flex gap-2">
          <button class="btn btn-sm retry-btn" type="button">↻ Проверить</button>
          <button class="btn btn-sm btn-warning takeover-btn" type="button">🔓 Забрать</button>
        </div>
      </div>`);
    box.querySelector(".holder").textContent = lock.holder ?? "?";
    box.querySelector(".mins").textContent = String(lock.remaining_min ?? 0);
    box.querySelector(".retry-btn").addEventListener("click", () => render(mount, params));
    box.querySelector(".takeover-btn").addEventListener("click", async ev => {
      ev.currentTarget.disabled = true;
      ev.currentTarget.textContent = "⏳ Забираю…";
      if (msgId != null) await tryReleaseLock(msgId);
      render(mount, params);
    });
    return box;
  }

  // Fold ландшафт (split3): сделка + клиент справа (на узких скрыто CSS)
  function fillAside(aside) {
    const d = dealNumbers(data);
    const sec = title => {
      const s = el(`<section class="as-card"><h3></h3></section>`);
      s.querySelector("h3").textContent = title;
      aside.appendChild(s);
      return s;
    };
    const deal = sec("Сделка");
    const grid = el(`<div class="as-grid"></div>`);
    [["Цена", d.price, ""], ["Минимум", d.floor, "floor"], ["Клиент", d.offer, "offer"], ["Мы", d.ours, "ours"]].forEach(([l, v, cls]) => {
      const c = el(`<div class="as-n"><small></small><b></b></div>`);
      if (cls) c.classList.add(cls);
      c.querySelector("small").textContent = l;
      c.querySelector("b").textContent = fmtEur(v);
      grid.appendChild(c);
    });
    deal.appendChild(grid);
    if (hasThermo(d)) deal.appendChild(thermo(d, "lg"));
    const brief = review?.deal_brief;
    if (brief?.summary_ru) { const p = el(`<p class="as-p"></p>`); p.textContent = brief.summary_ru; deal.appendChild(p); }
    const acts = el(`<div class="as-acts"></div>`);
    [["✋ Жду", waitHandler], ["💰 Продано", soldHandler]].forEach(([l, fn]) => {
      const b = el(`<button class="btn" type="button"></button>`);
      b.textContent = l;
      b.addEventListener("click", fn);
      acts.appendChild(b);
    });
    deal.appendChild(acts);
    const cl = sec("Клиент");
    const who = el(`<button class="as-who" type="button"><span class="aw-av"></span><span class="aw-t"><b></b><small></small></span></button>`);
    who.querySelector(".aw-av").appendChild(avatar(h.buyer_display_name, h.account_id, review?.client_lang, "md"));
    who.querySelector("b").textContent = h.buyer_display_name || "Покупатель";
    who.querySelector("small").textContent = h.buyer_email || "";
    who.addEventListener("click", historyHandler);
    cl.appendChild(who);
    const note = review?.client_note ?? h.client_note;
    if (note) { const p = el(`<p class="as-p"></p>`); p.textContent = note; cl.appendChild(p); }
    const dossier = review?.dossier ?? h.dossier;
    if (Array.isArray(dossier) && dossier.length) {
      const ul = el(`<ul class="as-dossier"></ul>`);
      dossier.slice(0, 5).forEach(x => {
        const li = el(`<li></li>`);
        li.textContent = typeof x === "string" ? x : `${x.date ?? ""} — ${x.text ?? ""}`;
        ul.appendChild(li);
      });
      cl.appendChild(ul);
    }
  }
}

// Снятие лока при уходе с треда
window.addEventListener("hashchange", () => {
  if (_heldMsgId !== null && !location.hash.startsWith("#/thread/")) {
    const id = _heldMsgId;
    _heldMsgId = null;
    tryReleaseLock(id);
  }
});

// Снятие лока при закрытии MA / уходе в фон.
// sendBeacon не умеет ставить X-Telegram-Init-Data → initData в теле
// (text/plain, без preflight), бэкенд валидирует тем же HMAC.
// URL обязательно абсолютный (${API_BASE}): страница живёт на github.io.
window.addEventListener("pagehide", () => {
  if (_heldMsgId !== null && navigator.sendBeacon) {
    const blob = new Blob([initData()], {type: "text/plain"});
    navigator.sendBeacon(`${API_BASE}/api/ma/messages/${_heldMsgId}/lock/release-beacon`, blob);
  }
});
