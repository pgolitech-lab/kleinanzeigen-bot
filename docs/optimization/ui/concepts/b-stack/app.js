// Концепция B «Стек карточек: разбор одним пальцем».
// Vanilla JS, без сборки. Данные — window.MOCK (не меняем), недостающее — локально ниже.
(function () {
  "use strict";
  const M = window.MOCK;
  const Q = new URLSearchParams(location.search);
  const SHOT = Q.get("shot") === "1";
  const RM = matchMedia("(prefers-reduced-motion: reduce)");
  const WIDE = matchMedia("(min-width: 760px) and (min-height: 500px)");
  const MINI = matchMedia("(max-height: 440px)");
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const eur = (n) => (n == null ? "—" : n.toLocaleString("de-DE").replace(/\./g, " ") + " €");
  const acc = (id) => M.accounts.find((a) => a.id === id);
  const initials = (n) => n.split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();
  const LANG_NAME = { de: "немецкий", pl: "польский", nl: "нидерландский", tr: "турецкий", ru: "русский" };
  const motion = () => !RM.matches && !SHOT;

  // ---------- тема: как это сделал бы Telegram (выставляем --tg-theme-* на :root) ----------
  const PRESETS = {
    dark: {
      "bg-color": "#0d0f1c", "secondary-bg-color": "#14172a", "section-bg-color": "#1a1e35",
      "text-color": "#eef0fb", "hint-color": "#8d93b3", "link-color": "#8aa8ff",
      "button-color": "#5b7cff", "button-text-color": "#ffffff", "accent-text-color": "#8aa8ff",
      "subtitle-text-color": "#a7acc8", "destructive-text-color": "#ff6b6b",
      "section-separator-color": "rgba(255,255,255,.08)",
    },
    light: {
      "bg-color": "#f4efe6", "secondary-bg-color": "#ebe4d6", "section-bg-color": "#ffffff",
      "text-color": "#1c1d27", "hint-color": "#757684", "link-color": "#2d56e8",
      "button-color": "#2d56e8", "button-text-color": "#ffffff", "accent-text-color": "#2d56e8",
      "subtitle-text-color": "#55576a", "destructive-text-color": "#d93a3a",
      "section-separator-color": "rgba(30,25,10,.1)",
    },
  };
  const tg = window.Telegram && window.Telegram.WebApp;
  const theme = Q.get("theme") || (tg && tg.colorScheme) || null;
  if (theme && PRESETS[theme]) {
    document.documentElement.dataset.theme = theme;
    for (const [k, v] of Object.entries(PRESETS[theme])) document.documentElement.style.setProperty("--tg-theme-" + k, v);
  }

  // ---------- локальные дополнения к моку ----------
  const DRAFTS = {
    t1: Object.assign({}, M.thread.draft),
    t2: { lang: "pl", suggestedPrice: 380,
      text: "Dzień dobry, tak, wysyłka do Polski jest możliwa – ok. 35 € (DHL, 3–4 dni). Cena konsoli to 380 €. Pozdrawiam",
      ru: "Добрый день, да, доставка в Польшу возможна — около 35 € (DHL, 3–4 дня). Цена консоли 380 €. С уважением" },
    t3: { lang: "de", suggestedPrice: 600,
      text: "Hallo Thomas, 600 € passt – heute ab 17 Uhr können Sie abholen. Die Adresse schicke ich gleich. Viele Grüße",
      ru: "Здравствуйте, Томас, 600 € подходит — сегодня после 17:00 можно забрать. Адрес пришлю сразу. С уважением" },
    // намеренно «не тот язык»: показывает языковой guard (клиент TR, текст DE)
    t6: { lang: "de", suggestedPrice: 720,
      text: "Hallo Mehmet, 500 € ist leider zu wenig. Mein letzter Preis: 720 € bar bei Abholung. Viele Grüße",
      ru: "Здравствуйте, Мехмет, 500 € — к сожалению, мало. Моя последняя цена: 720 € наличными при самовывозе. С уважением",
      fix: { lang: "tr", text: "Merhaba Mehmet, 500 € maalesef az. Son fiyatım: teslim alırken nakit 720 €. Saygılarımla" } },
  };
  const OUR_LAST = { t1: M.thread.deal.ourLast, t2: null, t3: 650, t4: 220, t5: 270, t6: 800, t7: 340 };
  const PING = {
    de: "Hallo, ich wollte kurz nachfragen, ob noch Interesse besteht. Viele Grüße",
    nl: "Hallo, ik wilde even vragen of u nog interesse heeft. Met vriendelijke groet",
    pl: "Dzień dobry, chciałem zapytać, czy oferta jest nadal aktualna. Pozdrawiam",
    tr: "Merhaba, hâlâ ilgileniyor musunuz diye sormak istedim. Saygılarımla",
  };
  const PING_RU = "Здравствуйте, хотел уточнить, актуально ли ещё. С уважением";
  const GOAL = 10000; // цель месяца (локальный мок)
  // новые данные (досье, «тот же покупатель», уроки, «из приложения», «запоздалое»)
  const EXTRA = {
    t1: { via: "app", lessons: [0, 1],
      similar: { name: "Mark Weber", acc: 3, ad: "Sitzbank T6 (dieter)", why: "тот же город и стиль письма, спрашивал ту же лавку 2 нед. назад" } },
    t2: { lessons: [1] },
    t3: { late: "6 ч", lessons: [2] },
    t6: { via: "app", lessons: [0] },
  };
  const LESSONS = [
    { bad: "Der Preis ist fest.", fix: "Не отказывать сразу — предлагать встречную цену выше минимума", when: "12.10", used: 14 },
    { bad: "Mit freundlichen Grüßen", fix: "Подпись — «Viele Grüße», без официоза", when: "03.10", used: 41 },
    { bad: "Abholung jederzeit möglich.", fix: "Называть конкретное время самовывоза", when: "28.09", used: 9 },
  ];
  const DOSSIER = [
    { date: "02.05", ad: "Kopfstützen T6 (2 Stk)", what: "просил фото, торговался", prices: "хотел 70 € · наша 90 €", res: "не договорились", cls: "lost" },
    { date: "14.07", ad: "Tischplatte Multivan", what: "забрал сам, наличные", prices: "520 € → 480 €", res: "купил", cls: "won" },
    { date: "сегодня", ad: "Sitzbank 3er VW T6 Multivan", what: "подходит ли к Comfortline, фото снизу", prices: "предлагает 900 € · наша 1 100 €", res: "идёт торг", cls: "live" },
  ];
  const DETECTED = [
    { id: "d1", ad: "Sitzbank Vito 2+1", buyer: "Mehmet Y.", acc: 5, price: 720, why: "клиент написал «Ok, ich nehme sie, komme morgen»", conf: 86 },
    { id: "d2", ad: "Tisch Schienensystem T5", buyer: "Jan de V.", acc: 1, price: 200, why: "объявление снято с сайта через час после переписки", conf: 61 },
  ];
  const TAG_PRESETS = ["торгуется", "самовывоз", "доставка", "готов купить", "постоянный", "низкая цена", "VIP"];
  const SALES = JSON.parse(JSON.stringify(M.sales));
  const X = (id) => EXTRA[id] || {};

  const threads = M.threads.map((t) => Object.assign({}, t));
  const T = (id) => threads.find((t) => t.id === id);
  const prio = (t) => t.score + (t.unread ? 15 : 0) + (t.draft ? 5 : 0);
  const reason = (t) => {
    if (t.score >= 85) return ["готов купить", ""];
    if (t.unread && t.score >= 70) return ["свежее · торг", ""];
    if (t.score >= 55) return ["вопрос", "mid"];
    return ["низкая цена", "low"];
  };
  const eventsOf = (id) => {
    if (id === M.thread.id) {
      const ev = M.thread.events.map((e) => Object.assign({}, e));
      ev[2].late = "4 ч"; ev[ev.length - 1].via = "app";
      return ev;
    }
    const t = T(id);
    const ev = [{ who: "in", time: t.time, de: t.last_de, ru: t.last_ru, via: X(id).via, late: X(id).late }];
    if (t.kind === "out") ev.unshift({ who: "in", time: "ранее", de: "Hallo, ist der Artikel noch verfügbar?", ru: "Здравствуйте, товар ещё доступен?" });
    return ev;
  };
  const dealOf = (id) => {
    const t = T(id);
    return { price: t.price, floor: t.floor, offer: t.offer, ourLast: OUR_LAST[id], ai: (state.drafts[id] || {}).suggestedPrice };
  };

  // ---------- состояние ----------
  const state = {
    tab: "inbox",
    detail: null,                 // {type:'thread'|'client', id}
    queue: threads.filter((t) => t.kind === "in").sort((a, b) => prio(b) - prio(a)).map((t) => t.id),
    total: 0,
    sent: [], later: [],
    drafts: JSON.parse(JSON.stringify(DRAFTS)),
    allow: {},                    // id → оператор разрешил «не тот язык»
    check: {},                    // id → 'wait'|'ok'|'bad' (lang-check)
    showOrig: {},
    events: {},
    edit: null,                   // id треда в режиме правки
    filter: "all", query: "", accFilter: null,
    pending: null,                // отправка в окне undo
    pinned: new Set(["t5"]), waiting: new Set(), sold: new Set(),
    selMode: false, sel: new Set(),
    tags: Object.fromEntries(threads.map((t) => [t.id, t.tags.slice()])),
    note: M.client.note, noteEdit: false,
    detected: DETECTED.map((d) => Object.assign({}, d)),
    similarDone: {},
  };
  state.total = state.queue.length;
  threads.forEach((t) => (state.events[t.id] = eventsOf(t.id)));

  const scr = Q.get("screen");
  if (scr === "thread") state.detail = { type: "thread", id: M.thread.id };
  if (scr === "client") state.detail = { type: "client", id: M.thread.id };
  if (scr === "sales") state.tab = "sales";
  if (scr === "all") state.tab = "all";
  if (Q.get("empty") === "1") { state.sent = state.queue.slice(0, -1); state.later = state.queue.slice(-1); state.queue = []; }

  // ---------- языковой guard ----------
  const effLang = (id) => {
    const d = state.drafts[id];
    if (!d) return null;
    return /[А-Яа-яЁё]/.test(d.text) ? "ru" : d.lang;
  };
  const langOk = (id) => effLang(id) === T(id).lang;
  function runCheck(id) {
    if (!state.drafts[id]) return;
    if (!motion()) { state.check[id] = langOk(id) ? "ok" : "bad"; return; }
    state.check[id] = "wait";
    setTimeout(() => { state.check[id] = langOk(id) ? "ok" : "bad"; refreshLang(id); }, 650);
  }
  const canSend = (id) => state.drafts[id] && state.check[id] !== "wait" && (langOk(id) || state.allow[id]);
  threads.forEach((t) => { if (state.drafts[t.id]) state.check[t.id] = langOk(t.id) ? "ok" : "bad"; });

  function badgeHTML(id) {
    const t = T(id), L = (effLang(id) || "").toUpperCase(), C = t.lang.toUpperCase();
    const c = state.check[id];
    if (c === "wait") return `<span class="badge wait"><i class="spin"></i>проверка</span>`;
    if (c === "ok") return `<span class="badge" title="Язык текста совпадает с языком клиента">${L} ✓</span>`;
    return `<span class="badge bad" title="Текст не на языке клиента">${L} ≠ ${C}</span>`;
  }
  function guardHTML(id) {
    if (state.check[id] !== "bad") return "";
    const d = state.drafts[id], t = T(id);
    return `<div class="guard">
      ${d.fix ? `<button class="gbtn" data-act="fix" data-id="${id}">Перевести на ${t.lang.toUpperCase()} (${LANG_NAME[t.lang]})</button>` : ""}
      <label class="chk"><input type="checkbox" data-act="allow" data-id="${id}" ${state.allow[id] ? "checked" : ""}>
        <span>Разрешаю отправить текст не на языке клиента</span></label>
    </div>`;
  }

  // ---------- шкала торга ----------
  function scaleHTML(d, compact) {
    const vals = [d.price, d.floor, d.offer, d.ourLast, d.ai].filter((v) => v != null);
    const lo = Math.min(...vals) * 0.9, hi = Math.max(...vals) * 1.03;
    const p = (v) => (((v - lo) / (hi - lo)) * 100).toFixed(1) + "%";
    const items = [
      d.offer != null && { k: "cl", v: d.offer, l: "клиент", c: "var(--btn)" },
      { k: "fl", v: d.floor, l: "мин.", c: "var(--text)" },
      d.ai != null && { k: "ai", v: d.ai, l: "ответ", c: "var(--go)" },
      { k: "pr", v: d.price, l: "цена", c: "var(--hint)" },
    ].filter(Boolean);
    let note = "";
    if (d.offer != null) {
      const g = d.offer - d.floor;
      note = g < 0 ? `<div class="gap-note bad">Клиент на ${eur(-g)} ниже минимума</div>`
                   : `<div class="gap-note ok">Клиент выше минимума на ${eur(g)} — можно закрывать</div>`;
    } else note = `<div class="gap-note" style="color:var(--hint)">Клиент цену ещё не называл</div>`;
    return `<div class="scale" aria-label="Шкала торга">
      <div class="track">
        <span class="z z-lo" style="width:${p(d.floor)}"></span>
        <span class="z z-hi" style="left:${p(d.floor)}"></span>
        ${d.ourLast != null && d.ourLast !== d.price ? `<span class="mk m-ol" style="left:${p(d.ourLast)}" title="наше последнее ${eur(d.ourLast)}"></span>` : ""}
        ${items.map((i) => `<span class="mk m-${i.k}" style="left:${p(i.v)}"></span>`).join("")}
      </div>
      <div class="legend">${items.map((i) => `<div class="lg" style="--d:${i.c}"><small>${i.l}</small><b class="num">${eur(i.v)}</b></div>`).join("")}</div>
      ${compact ? "" : note}
    </div>`;
  }

  // ---------- пометки: из приложения / запоздалое / закреп / «тот же покупатель» / уроки ----------
  function flagsHTML(id) {
    const x = X(id), out = [];
    if (state.pinned.has(id)) out.push(`<span class="flag pin" title="Закреплён">📌</span>`);
    if (x.via === "app") out.push(`<span class="flag app" title="Ответ пришёл через сайт/приложение Kleinanzeigen">из приложения</span>`);
    if (x.similar && !state.similarDone[id]) out.push(`<button class="flag sim" data-client="${id}" title="Возможно, тот же покупатель">⚠ похож на ${esc(x.similar.name)}</button>`);
    if (x.late) out.push(`<span class="flag late" title="Письмо пришло с опозданием">запоздалое · ${esc(x.late)}</span>`);
    return out.join("");
  }
  function similarHTML(id, compact) {
    const s = X(id).similar;
    if (!s || state.similarDone[id]) return "";
    const a2 = acc(s.acc);
    if (compact) return `<button class="similar" data-client="${id}"><b>⚠ Возможно, тот же покупатель</b><span>«${esc(s.name)}» · ${esc(a2.name)} ›</span></button>`;
    return `<div class="similar big"><b>⚠ Возможно, тот же покупатель</b>
      <span>«${esc(s.name)}» писал на аккаунт <i class="acc-pill" style="--ac:${a2.color}">${esc(a2.name)}</i> — ${esc(s.why)}.</span>
      <div class="sim-act"><button class="chip" data-act="sim-yes" data-id="${id}">Это он — связать</button><button class="chip" data-act="sim-no" data-id="${id}">Нет, другой</button></div></div>`;
  }
  function threadStatus(id) {
    const s = [];
    if (state.pinned.has(id)) s.push("📌 закреплён");
    if (state.waiting.has(id)) s.push("✋ жду — тред вне очереди, пока клиент не ответит");
    if (state.sold.has(id)) s.push("💰 продано");
    return s.length ? `<div class="tstatus">${s.map(esc).join(" · ")}</div>` : "";
  }
  // общий нижний лист
  function sheet(html, onClick) {
    const sh = $("#sheet");
    sh.innerHTML = `<div class="sheet" role="dialog"><span class="grip"></span>${html}<button class="wbtn" data-sh="close">Закрыть</button></div>`;
    sh.hidden = false;
    sh.onclick = (e) => {
      const b = e.target.closest("[data-sh]");
      if (e.target === sh || (b && b.dataset.sh === "close")) { sh.hidden = true; return; }
      if (b && onClick(b.dataset.sh, b) !== false) sh.hidden = true;
    };
  }
  function lessonsSheet(id) {
    const l = (X(id).lessons || []).map((i) => LESSONS[i]);
    sheet(`<h3>Уроки, которые ИИ учёл</h3><p>Ваши прошлые правки черновиков. ИИ подмешивает самые похожие в каждый ответ.</p>
      ${l.map((x) => `<div class="lesson"><div class="ls-bad"><small>было у ИИ</small>${esc(x.bad)}</div><div class="ls-fix"><small>правка оператора · ${esc(x.when)} · применено ${x.used}×</small>${esc(x.fix)}</div></div>`).join("")}`, () => true);
  }
  function soldSheet(id) {
    const t = T(id);
    sheet(`<h3>Продано вручную</h3><p>${esc(t.buyer)} · ${esc(t.ad)}</p>
      <label class="fld"><span>Цена продажи, €</span><input id="soldp" type="number" inputmode="numeric" value="${t.offer && t.offer >= t.floor ? t.offer : (state.drafts[id]?.suggestedPrice || t.price)}"></label>
      <button class="wbtn pri" data-sh="ok">💰 Отметить проданным</button>`, (k) => {
      if (k !== "ok") return false;
      markSold(id, +$("#soldp").value || t.price);
    });
  }
  function markSold(id, price) {
    const t = T(id);
    state.sold.add(id); state.queue = state.queue.filter((x) => x !== id); state.waiting.delete(id);
    SALES.total += price; SALES.count += 1;
    SALES.recent.unshift({ ad: t.ad, price, acc: t.acc, date: "сегодня" });
    const ba = SALES.byAccount.find((x) => x.acc === t.acc); if (ba) ba.sum += price;
    render(); toast(`Продано: ${t.ad} · ${eur(price)}`);
  }
  function threadMenu(id) {
    const t = T(id), pin = state.pinned.has(id), wait = state.waiting.has(id);
    sheet(`<h3>${esc(t.buyer)}</h3><p>${esc(t.ad)}</p>
      <button class="opt" data-sh="client"><span class="oi">👤</span><span><b>Карточка клиента</b><small>Досье, теги, заметка</small></span></button>
      <button class="opt" data-sh="pin"><span class="oi">📌</span><span><b>${pin ? "Открепить" : "Закрепить"}</b><small>Закреплённые — сверху списка и первыми в разборе</small></span></button>
      <button class="opt" data-sh="wait"><span class="oi">✋</span><span><b>${wait ? "Снять «Жду»" : "Жду"}</b><small>Убрать из очереди, пока клиент не ответит</small></span></button>
      <button class="opt" data-sh="sold"><span class="oi">💰</span><span><b>Продано вручную</b><small>Указать цену, тред закроется</small></span></button>
      <div class="sec" style="margin:4px 2px 8px">Теги</div>
      <div class="tagedit" style="margin-bottom:10px">${TAG_PRESETS.map((g) => `<button class="tag ${state.tags[id].includes(g) ? "on" : "add"}" data-sh="tag" data-tag="${esc(g)}">${state.tags[id].includes(g) ? "✓ " : "+ "}${esc(g)}</button>`).join("")}</div>
      <button class="opt" data-sh="skip"><span class="oi">❌</span><span><b>Пропустить</b><small>Не отвечать на это сообщение</small></span></button>`, (k, b) => {
      if (k === "client") { setTimeout(() => openDetail("client", id), 30); }
      else if (k === "pin") { togglePin(id); }
      else if (k === "wait") { toggleWait(id); }
      else if (k === "sold") { setTimeout(() => soldSheet(id), 50); }
      else if (k === "tag") { toggleTag(id, b.dataset.tag); threadMenu(id); return false; }
      else if (k === "skip") { state.queue = state.queue.filter((x) => x !== id); render(); toast("Пропущено"); }
    });
  }
  function togglePin(id) {
    if (state.pinned.has(id)) state.pinned.delete(id);
    else { state.pinned.add(id); if (state.queue.includes(id)) { state.queue = [id, ...state.queue.filter((x) => x !== id)]; const s = $("#stack"); if (s) s.innerHTML = ""; } }
    render(); toast(state.pinned.has(id) ? "Закреплено" : "Откреплено");
  }
  function toggleWait(id) {
    if (state.waiting.has(id)) { state.waiting.delete(id); if (T(id).kind === "in" && !state.sold.has(id)) state.queue.push(id); }
    else { state.waiting.add(id); state.queue = state.queue.filter((x) => x !== id); }
    render(); toast(state.waiting.has(id) ? "Жду — тред убран из очереди" : "Тред вернулся в очередь");
  }
  function toggleTag(id, g) {
    const a = state.tags[id];
    state.tags[id] = a.includes(g) ? a.filter((x) => x !== g) : [...a, g];
    rerenderCard(id);
  }
  function manualSaleSheet() {
    const open = threads.filter((t) => !state.sold.has(t.id));
    sheet(`<h3>Продажа вручную</h3><p>Выберите тред — цена подставится из торга.</p>
      ${open.map((t) => `<button class="opt" data-sh="pick" data-id="${t.id}" style="--ac:${acc(t.acc).color}"><span class="oi" style="background:var(--ac);color:#fff;font-weight:800;font-size:.75rem">${initials(t.buyer)}</span><span><b>${esc(t.ad)}</b><small>${esc(t.buyer)} · ${esc(acc(t.acc).name)} · ${eur(t.price)}</small></span></button>`).join("")}`,
      (k, b) => { if (k === "pick") setTimeout(() => soldSheet(b.dataset.id), 50); });
  }

  function lessonsChip(id) {
    const l = X(id).lessons;
    if (!l || !l.length || !state.drafts[id]) return "";
    return `<button class="lchip mini-hide" data-act="lessons" data-id="${id}" title="Правки оператора, которые ИИ учёл">🎓 уроки ${l.length}</button>`;
  }

  // ---------- карточка стопки ----------
  function cardInner(id) {
    const t = T(id), a = acc(t.acc), d = state.drafts[id];
    const [rs, rc] = reason(t);
    const orig = state.showOrig["c" + id];
    return `
      <span class="grip"></span>
      <span class="stamp s-go">ОТПРАВИТЬ</span><span class="stamp s-later">ПОЗЖЕ</span><span class="stamp s-open">ОТКРЫТЬ ТРЕД</span>
      <div class="c-head">
        <div class="ava">${initials(t.buyer)}</div>
        <div class="c-who">
          <div class="c-name">${esc(t.buyer)}</div>
          <div class="c-meta"><span class="acc-pill">${esc(a.name)}</span><span class="lang">${t.lang.toUpperCase()}</span><span>${esc(t.ago)}</span>${flagsHTML(id)}</div>
        </div>
        <span class="hot ${rc} mini-hide">${rs}</span>
      </div>
      <div class="c-body">
        <div class="c-ad">${esc(t.ad)}<span class="mini-only"> · ${eur(t.price)}</span></div>
        ${id === M.thread.id ? `<button class="dossier mini-hide" data-client="${id}"><span class="dz">›</span><b>Досье</b> 3-е обращение · купил 1× за 480 € · торгуется ~10 %</button>` : ""}
        <div class="mini-hide">${scaleHTML(dealOf(id))}</div>
        <div class="say">
          <div class="say-lbl">Клиент · по-русски</div>
          <p>${esc(t.last_ru)}</p>
          ${orig ? `<div class="orig" lang="${t.lang}">${esc(t.last_de)}</div>` : ""}
          <button class="linkish mini-hide" data-act="orig" data-id="${id}">${orig ? "Скрыть оригинал" : "Показать оригинал (" + t.lang.toUpperCase() + ")"}</button>
        </div>
        ${d ? `<div class="ai ${state.check[id] === "bad" ? "bad" : ""}" data-lang-box="${id}">
          <div class="ai-lbl">Ответ ИИ<span class="lname">· ${LANG_NAME[effLang(id)] || ""}</span> <span data-badge="${id}" class="bslot">${badgeHTML(id)}</span>${lessonsChip(id)}</div>
          <p class="client-text" lang="${effLang(id)}">${esc(d.text)}</p>
          <div class="mirror mini-hide">${esc(d.ru)}</div>
          <div data-guard="${id}">${guardHTML(id)}</div>
        </div>` : `<div class="ai"><div class="ai-lbl">Черновика нет</div><p>Откройте тред и напишите ответ.</p></div>`}
        ${state.tags[id].length ? `<div class="tags mini-hide">${state.tags[id].map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : ""}
      </div>`;
  }

  // ---------- рендер разбора ----------
  const ICON = {
    go: '<svg viewBox="0 0 24 24"><path d="M4 12l16-8-6 16-3-6z"/><path d="M11 14l9-10"/></svg>',
    later: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>',
    edit: '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>',
    back: '<svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>',
    ai: '<svg viewBox="0 0 24 24"><path d="M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z"/><path d="M19 15l1 2 2 1-2 1-1 2-1-2-2-1 2-1z"/></svg>',
    more: '<svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h10"/></svg>',
  };

  function renderInbox(view) {
    if (!state.queue.length) return renderDone(view);
    const pos = state.sent.length + state.later.length + 1;
    const total = state.total;
    let bar = "";
    const order = [...state.sent.map(() => "sent"), ...state.later.map(() => "later")];
    for (let i = 0; i < total; i++) bar += `<i class="${order[i] || (i === order.length ? "cur" : "")}"></i>`;
    if (!view.querySelector(".inbox")) {
      view.innerHTML = `<div class="inbox" style="display:contents">
        <header class="qhead">
          <div class="qrow"><div class="qtitle" id="qtitle"></div><div class="qsub" id="qsub"></div></div>
          <div class="qbar" id="qbar"></div>
        </header>
        <div class="stack" id="stack"></div>
        <div class="hint-line mini-hide">свайп → отправить · ← позже · ↑ весь тред</div>
        <div class="actions">
          <button class="act later" data-act="later">${ICON.later}<span class="k">Позже</span></button>
          <button class="act edit" data-act="edit">${ICON.edit}<span class="k">Править</span></button>
          <button class="act go" data-act="send">${ICON.go}<span>Отправить</span></button>
        </div>
      </div>`;
    }
    $("#qtitle").innerHTML = `<b class="num">${state.queue.length}</b> ждут ответа`;
    $("#qsub").textContent = `${pos} из ${total}`;
    $("#qbar").innerHTML = bar;
    const top = state.queue[0];
    const goBtn = view.querySelector(".act.go");
    goBtn.disabled = !canSend(top);
    goBtn.querySelector("span").textContent = state.check[top] === "bad" && !state.allow[top] ? "Язык ≠" : "Отправить";
    renderStack();
  }

  function renderStack() {
    const stack = $("#stack");
    if (!stack) return;
    const vis = state.queue.slice(0, 3);
    [...stack.children].forEach((el) => { if (!vis.includes(el.dataset.id) && !el.classList.contains("flying")) el.remove(); });
    vis.forEach((id, i) => {
      let el = stack.querySelector(`.card[data-id="${id}"]:not(.flying)`);
      if (!el) {
        el = document.createElement("article");
        el.className = "card";
        el.dataset.id = id;
        el.dataset.depth = String(Math.min(i + 1, 2));
        el.style.setProperty("--ac", acc(T(id).acc).color);
        el.innerHTML = cardInner(id);
        stack.appendChild(el);
        void el.offsetWidth;
      }
      el.dataset.depth = String(i);
      el.style.zIndex = String(10 - i);
      if (i === 0 && !el._bound) { bindSwipe(el); el._bound = true; }
    });
  }
  function rerenderCard(id) {
    const el = document.querySelector(`.card[data-id="${id}"]`);
    if (el) { const sc = el.querySelector(".c-body")?.scrollTop || 0; el.innerHTML = cardInner(id); const b = el.querySelector(".c-body"); if (b) b.scrollTop = sc; }
  }

  function renderDone(view) {
    const sum = state.sent.reduce((s, id) => s + (state.drafts[id]?.suggestedPrice || T(id).price), 0);
    const burst = motion() ? Array.from({ length: 18 }, (_, i) =>
      `<i style="left:${5 + i * 5.2}%;background:${M.accounts[i % 5].color};animation-delay:${(i % 6) * 0.08}s"></i>`).join("") : "";
    view.innerHTML = `<div class="done">
      <div class="done-hero"><div class="burst">${burst}</div>
        <div class="big">✓</div>
        <h2>Всё разобрано</h2>
        <p>Никто не ждёт ответа. Новые карточки появятся сами.</p>
      </div>
      <div class="tiles">
        <div class="tile go"><small>Отправлено</small><b class="num">${state.sent.length}</b></div>
        <div class="tile later"><small>Отложено</small><b class="num">${state.later.length}</b></div>
        <div class="tile acc"><small>В торге на</small><b class="num">${eur(sum)}</b></div>
        <div class="tile"><small>Медиана ответа</small><b class="num">${esc(M.sales.replyMedian)}</b></div>
      </div>
      ${state.later.length ? `<button class="wbtn pri" data-act="relater">Вернуть отложенные (${state.later.length})</button>` : ""}
      <button class="wbtn" data-tab-go="sales">Итог месяца: ${eur(SALES.total)}</button>
    </div>`;
  }

  // ---------- все треды ----------
  function renderAll(view) {
    const q = state.query.trim().toLowerCase();
    const list = threads.filter((t) => {
      if (state.filter === "us" && !(t.kind === "in" && state.queue.includes(t.id))) return false;
      if (state.filter === "them" && t.kind === "in" && state.queue.includes(t.id)) return false;
      if (state.accFilter && t.acc !== state.accFilter) return false;
      if (q && !(t.buyer + " " + t.ad + " " + t.last_ru + " " + t.last_de).toLowerCase().includes(q)) return false;
      return true;
    });
    const nUs = threads.filter((t) => state.queue.includes(t.id)).length;
    const selId = state.detail && state.detail.type === "thread" ? state.detail.id : null;
    list.sort((a, b) => (state.pinned.has(b.id) ? 1 : 0) - (state.pinned.has(a.id) ? 1 : 0));
    const rows = list.map((t) => {
      const us = state.queue.includes(t.id);
      const st = state.sold.has(t.id) ? ["won", "продано"] : state.waiting.has(t.id) ? ["wait", "жду"] : us ? ["us", "ждёт нас"] : ["them", "ждём"];
      const chk = state.selMode ? `<span class="rchk ${state.sel.has(t.id) ? "on" : ""}">${state.sel.has(t.id) ? "✓" : ""}</span>` : `<span class="ava">${initials(t.buyer)}</span>`;
      return `<button class="row ${selId === t.id ? "sel" : ""} ${state.sel.has(t.id) ? "picked" : ""}" style="--ac:${acc(t.acc).color}" ${state.selMode ? `data-pick="${t.id}"` : `data-open="${t.id}"`}>
        ${chk}
        <span class="r1">${state.pinned.has(t.id) ? "📌" : ""}<b>${esc(t.buyer)}</b><span class="lang">${t.lang.toUpperCase()}</span><span class="st ${st[0]}">${st[1]}</span><span class="ago">${esc(t.ago)}</span></span>
        <span class="r2">${esc(acc(t.acc).name)} · ${esc(t.ad)} · ${eur(t.price)}</span>
        <span class="r3">${esc(t.last_ru)}</span>
      </button>`;
    }).join("") || `<div class="empty">Ничего не нашлось</div>`;
    const keepFocus = document.activeElement && document.activeElement.id === "q";
    view.innerHTML = `
      <div class="mini-only"><div class="hdr"><button class="back" data-tab-go="inbox">${ICON.back}Разбор</button><div class="ttl"><b>Все треды</b></div></div></div>
      <div class="h1 mini-hide h1row">Все треды<button class="chip ${state.selMode ? "on" : ""}" data-act="selmode">${state.selMode ? "Готово" : "Выбрать"}</button></div>
      <div class="search"><input id="q" type="search" placeholder="Имя, товар, текст…" value="${esc(state.query)}" aria-label="Поиск по тредам"></div>
      <div class="chips">
        <button class="chip ${state.filter === "all" ? "on" : ""}" data-filter="all">Все · ${threads.length}</button>
        <button class="chip ${state.filter === "us" ? "on" : ""}" data-filter="us">Ждут нас · ${nUs}</button>
        <button class="chip ${state.filter === "them" ? "on" : ""}" data-filter="them">Ждём клиента · ${threads.length - nUs}</button>
        ${M.accounts.map((a) => `<button class="chip ${state.accFilter === a.id ? "on" : ""}" style="--ac:${a.color}" data-accf="${a.id}"><span class="dot"></span>${esc(a.name)}</button>`).join("")}
      </div>
      <div class="list">${rows}</div>
      ${state.selMode ? `<div class="bulk"><span class="num">Выбрано: ${state.sel.size}</span>
        <button class="chip" data-act="bulk-pin" ${state.sel.size ? "" : "disabled"}>📌 Закрепить</button>
        <button class="chip" data-act="bulk-wait" ${state.sel.size ? "" : "disabled"}>✋ Жду</button></div>` : ""}`;
    if (keepFocus) { const i = $("#q"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
  }

  // ---------- продажи ----------
  function renderSales(view) {
    const S = SALES, max = Math.max(...S.byDay), maxAcc = Math.max(...S.byAccount.map((x) => x.sum));
    const pct = Math.round((S.total / GOAL) * 100);
    const best = S.byAccount.reduce((a, b) => (b.sum > a.sum ? b : a));
    view.innerHTML = `
      <div class="mini-only"><div class="hdr"><button class="back" data-tab-go="inbox">${ICON.back}Разбор</button><div class="ttl"><b>Продажи</b><small>${esc(S.period)}</small></div></div></div>
      <div class="h1 mini-hide">Продажи <span style="color:var(--hint);font-weight:650;font-size:.8rem">· ${esc(S.period)}</span></div>
      <div class="scroll wide-pad"><div class="sales-grid"><div>
        <div class="hero" style="--ac:var(--go)">
          <small style="color:var(--sub);font-weight:700;font-size:.74rem;text-transform:uppercase;letter-spacing:.05em">Выручка за месяц</small>
          <div class="money num">${S.total.toLocaleString("de-DE").replace(/\./g, " ")}<em> €</em></div>
          <div class="goal"><div class="trust"><div class="tl"><span>Цель ${eur(GOAL)}</span><span>${pct}%</span></div><div class="meter"><i style="width:${pct}%"></i></div></div></div>
          <div style="margin-top:8px;font-size:.8rem;color:var(--sub)">До цели ${eur(GOAL - S.total)} — это ≈ ${Math.ceil((GOAL - S.total) / (S.total / S.count))} продажи по среднему чеку</div>
        </div>
        ${state.detected.length ? `<div class="sec">Найденные продажи · подтвердите</div>
        <div class="found">${state.detected.map((d) => `<div class="fcard" style="--ac:${acc(d.acc).color}">
          <div class="fc-h"><b>${esc(d.ad)}</b><span class="pr num">${eur(d.price)}</span></div>
          <small>${esc(d.buyer)} · ${esc(acc(d.acc).name)} · уверенность ${d.conf}%</small>
          <div class="fc-why">${esc(d.why)}</div>
          <div class="fc-act"><button class="act edit" data-act="det-no" data-id="${d.id}">✕ Не продано</button><button class="act go" data-act="det-yes" data-id="${d.id}">✓ Продано</button></div>
        </div>`).join("")}</div>` : ""}
        <button class="wbtn" data-act="manual-sale">＋ Отметить продажу вручную</button>
        <div class="today">
          <div class="td-stack" aria-hidden="true">${[0, 1, 2].map((i) => `<i style="--ac:${M.accounts[i].color}"></i>`).join("")}</div>
          <div class="td-m"><small>Сегодня</small><b>${state.sent.length ? `${state.sent.length} отвечено · ` : ""}${state.queue.length ? `${state.queue.length} ждут ответа` : "всё разобрано"}</b></div>
          ${state.queue.length ? `<button class="td-go" data-tab-go="inbox">Разобрать</button>` : ""}
        </div>
        <div class="tiles">
          <div class="tile go"><small>Продаж</small><b class="num">${S.count}</b></div>
          <div class="tile"><small>Средний чек</small><b class="num">${eur(Math.round(S.total / S.count))}</b></div>
          <div class="tile later"><small>Уступили в ср.</small><b class="num">${String(S.avgDiscount).replace(".", ",")}<em>%</em></b></div>
          <div class="tile acc"><small>Ответ, медиана</small><b class="num">${esc(S.replyMedian)}</b></div>
        </div>
      </div><div>
        <div class="sec">По дням</div>
        <div><div class="chart" role="img" aria-label="Выручка по дням">${S.byDay.map((v) => `<i class="${v === 0 ? "z" : v === max ? "max" : ""}" style="height:${v ? Math.max(6, (v / max) * 100) : 3}%" title="${eur(v)}"></i>`).join("")}</div>
          <div class="chart-cap"><span>1 окт</span><span>лучший день ${eur(max)}</span><span>${S.byDay.length} окт</span></div></div>
        <div class="sec">По аккаунтам</div>
        <div class="accbars">${S.byAccount.map((x) => { const a = acc(x.acc); return `<div class="ab" style="--ac:${a.color}"><span>${esc(a.name)}${x === best ? " ★" : ""}</span><span class="bar"><i style="width:${(x.sum / maxAcc) * 100}%"></i></span><b class="num">${eur(x.sum)}</b></div>`; }).join("")}</div>
        <div class="sec">Последние продажи</div>
        ${S.recent.map((r) => `<div class="hcard" style="--ac:${acc(r.acc).color}"><span class="ava" style="width:38px;height:38px;border-radius:12px;font-size:.7rem">${esc(acc(r.acc).name.slice(0, 2).toUpperCase())}</span><div class="hm"><b>${esc(r.ad)}</b><small>${esc(acc(r.acc).name)} · ${esc(r.date)}</small></div><span class="pr num">${eur(r.price)}</span></div>`).join("")}
      </div></div></div>`;
  }

  // ---------- тред ----------
  function renderThread(pane, id, wideAuto) {
    const t = T(id), a = acc(t.acc), d = state.drafts[id];
    const ev = state.events[id];
    const pend = state.queue.includes(id);
    let lastDay = null;
    const feed = ev.map((e, i) => {
      const day = /^(Пн|Вт|Ср|Чт|Пт|Сб|Вс)/.test(e.time) ? e.time.slice(0, 2) : e.time === "ранее" ? "Ранее" : "Сегодня";
      const sep = day !== lastDay ? `<div class="day">${{ Вт: "Вторник", Ср: "Среда" }[day] || day}</div>` : "";
      lastDay = day;
      const k = id + ":" + i, o = state.showOrig[k];
      return `${sep}<div class="bub ${e.who} ${e.fresh ? "fresh" : ""}" data-orig="${k}">
        <p>${esc(e.ru)}</p>${o ? `<div class="o" lang="${t.lang}">${esc(e.de)}</div>` : ""}
        <div class="bm">${e.auto ? "<span>🤖 авто</span>" : ""}${e.via === "app" ? `<span class="flag app">из приложения</span>` : ""}${e.late ? `<span class="flag late">пришло с опозданием ${esc(e.late)}</span>` : ""}<span>${o ? "скрыть оригинал" : t.lang.toUpperCase() + " — тап"}</span><span>${esc(e.time.replace(/^(Пн|Вт|Ср|Чт|Пт|Сб|Вс) /, ""))}</span></div>
      </div>`;
    }).join("");
    pane.innerHTML = `
      <header class="hdr">
        <button class="back ${wideAuto ? "wide-hide" : ""}" data-act="back">${ICON.back}<span>Назад</span></button>
        <div class="ttl"><b>${esc(t.buyer)}</b><small><span class="acc-pill" style="--ac:${a.color}">${esc(a.name)}</span><span class="lang">${t.lang.toUpperCase()}</span></small></div>
        <button class="ibtn hm-btn" data-act="tmenu" data-id="${id}" aria-label="Действия с тредом"><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg></button>
        <button class="ava" style="--ac:${a.color}" data-client="${id}" aria-label="Карточка клиента">${initials(t.buyer)}</button>
      </header>
      <div class="thread">
        <div class="deal"><div class="c-ad">${esc(t.ad)}</div>${scaleHTML(dealOf(id), true)}</div>
        <div class="feed" id="feed">${similarHTML(id, false)}${threadStatus(id)}${feed}</div>
        ${wideAuto ? clientStrip(id) : dockHTML(id, pend)}
      </div>`;
    const f = $("#feed", pane); f.scrollTop = f.scrollHeight;
  }

  // широкий режим: справа под лентой — сводка по клиенту (ответ и кнопки — в карточке слева)
  function clientStrip(id) {
    const t = T(id), C = M.client, known = C.name === t.buyer;
    const tiles = known
      ? [["обращений", C.stats.inquiries], ["купил", C.stats.bought], ["потратил", eur(C.stats.spent)], ["отвечает", C.stats.avgReply]]
      : [["обращений", 1], ["купил", 0], ["в торге", eur(t.price)], ["язык", t.lang.toUpperCase()]];
    return `<div class="dock cstrip">
      <button class="cs-who" data-client="${id}"><b>${esc(t.buyer)}</b><span>${known ? "клиент с " + esc(C.since) : "новый клиент"} · карточка ›</span></button>
      <div class="cs-tiles">${tiles.map(([l, v]) => `<div><b class="num">${esc(v)}</b><small>${l}</small></div>`).join("")}</div>
      ${known ? `<div class="cs-note">${esc(C.note)}</div>` : ""}
      <div class="cs-hint">← ответ и «Отправить» — в карточке слева</div>
    </div>`;
  }

  function dockHTML(id, pend) {
    const t = T(id), d = state.drafts[id];
    if (state.sold.has(id)) {
      const r = SALES.recent.find((x) => x.ad === t.ad);
      return `<div class="dock"><div class="waiting" style="color:var(--go);font-weight:750">💰 Продано${r ? " за " + eur(r.price) : ""} · тред закрыт</div></div>`;
    }
    if (state.waiting.has(id)) {
      return `<div class="dock"><div class="waiting">✋ Жду — тред вне очереди</div><div class="drow"><button class="act edit" data-act="unwait" data-id="${id}" style="flex:1">Вернуть в очередь</button></div></div>`;
    }
    if (!d || !pend) {
      return `<div class="dock">
        <div class="waiting">Ждём ответа клиента · ${esc(t.ago)}</div>
        <div class="drow"><button class="act go" data-act="ping" data-id="${id}">${ICON.ai}<span>Предложить напоминание</span></button></div>
      </div>`;
    }
    const step = (n, l, s) => `<span class="${s}"><i>${s === "ok" ? "✓" : n}</i><span class="lbl">${l}</span></span>`;
    const editing = state.edit === id;
    const allowed = canSend(id);
    return `<div class="dock" data-dock="${id}">
      <div class="steps">${step(1, "выбор", "ok")}<span class="ln"></span>${step(2, "текст", editing ? "cur" : "ok")}<span class="ln"></span>${step(3, "проверка", editing ? "" : "cur")}</div>
      ${editing
        ? `<textarea id="edtxt" aria-label="Текст для клиента">${esc(d.text)}</textarea>
           <div class="drow"><button class="act edit" data-act="cancel-edit" style="flex:1">Отмена</button><button class="act go" data-act="save-edit" data-id="${id}" style="flex:1.4">Готово</button></div>`
        : `<div class="ai ${state.check[id] === "bad" ? "bad" : ""}" data-lang-box="${id}">
            <div class="ai-lbl">Уйдёт клиенту <span data-badge="${id}" class="bslot">${badgeHTML(id)}</span>${lessonsChip(id)}</div>
            <p class="client-text" lang="${effLang(id)}">${esc(d.text)}</p>
            <div class="mirror">${esc(d.ru)}</div>
            <div data-guard="${id}">${guardHTML(id)}</div>
          </div>
          <div class="drow">
            <button class="ibtn mini-hide" data-act="options" data-id="${id}" aria-label="Варианты текста">${ICON.more}</button>
            <button class="ibtn" data-act="edit-here" data-id="${id}" aria-label="Редактировать">${ICON.edit}</button>
            <button class="act go" data-act="send-thread" data-id="${id}" ${allowed ? "" : "disabled"}>${ICON.go}<span>${state.check[id] === "wait" ? "Проверка…" : allowed ? "Отправить" : "Язык ≠"}</span></button>
          </div>`}
    </div>`;
  }

  function refreshLang(id) {
    rerenderCard(id);
    if (state.tab === "inbox" && state.queue[0] === id) renderInbox($("#tabview"));
    const dock = document.querySelector(`[data-dock="${id}"]`);
    if (dock) dock.outerHTML = dockHTML(id, state.queue.includes(id));
  }

  // ---------- клиент ----------
  function renderClient(pane, id, wideAuto) {
    const C = M.client, t = T(id), a = acc(t.acc);
    const trust = Math.round(((C.stats.bought / C.stats.inquiries) * 0.6 + 0.35) * 100);
    const cls = { "куплено": "won", "идёт торг": "live", "не договорились": "lost" };
    pane.innerHTML = `
      <header class="hdr">
        <button class="back ${wideAuto ? "wide-hide" : ""}" data-act="back">${ICON.back}<span>Назад</span></button>
        <div class="ttl"><b>Клиент</b><small>${esc(C.name)}</small></div>
      </header>
      <div class="scroll">
        <div class="hero" style="--ac:${a.color}">
          <div class="hrow"><span class="ava" style="--ac:${a.color}">${initials(C.name)}</span>
            <div style="min-width:0"><h2>${esc(C.name)}</h2>
              <div class="meta"><span class="lang">${C.lang.toUpperCase()}</span><span>с ${esc(C.since)}</span></div>
              <div class="meta" style="overflow-wrap:anywhere">${esc(C.email)}</div></div></div>
          <div class="trust"><div class="tl"><span>Надёжность</span><span>${trust}%</span></div><div class="meter"><i style="width:${trust}%"></i></div></div>
        </div>
        <div class="tiles">
          <div class="tile acc"><small>Обращений</small><b class="num">${C.stats.inquiries}</b></div>
          <div class="tile go"><small>Купил</small><b class="num">${C.stats.bought}</b></div>
          <div class="tile"><small>Потратил</small><b class="num">${eur(C.stats.spent)}</b></div>
          <div class="tile later"><small>Отвечает за</small><b class="num">${esc(C.stats.avgReply)}</b></div>
        </div>
        ${similarHTML(id, false)}
        <div class="sec">Теги</div>
        <div class="tagedit">${state.tags[id].map((g) => `<button class="tag on" data-act="tag-del" data-id="${id}" data-tag="${esc(g)}">${esc(g)} ✕</button>`).join("")}
          ${TAG_PRESETS.filter((g) => !state.tags[id].includes(g)).slice(0, 3).map((g) => `<button class="tag add" data-act="tag-add" data-id="${id}" data-tag="${esc(g)}">+ ${esc(g)}</button>`).join("")}</div>
        <div class="note"><small>Заметка о клиенте <button class="linkish" data-act="note-edit" style="float:right;padding:0;min-height:0">${state.noteEdit ? "" : "Изменить"}</button></small>
          ${state.noteEdit ? `<textarea id="notetx" class="notetx">${esc(state.note)}</textarea><button class="wbtn pri" data-act="note-save" style="min-height:46px;margin-top:8px">Сохранить</button>` : esc(state.note)}</div>
        <div class="sec">Досье · когда писал, о чём, по каким ценам</div>
        <div class="dossier-tl">${DOSSIER.map((h) => `<div class="dt"><i class="dt-dot ${h.cls}"></i><div class="dt-m"><div class="dt-h"><b>${esc(h.ad)}</b><span class="stc ${h.cls}">${esc(h.res)}</span></div><small>${esc(h.date)} · ${esc(h.what)}</small><div class="dt-p num">${esc(h.prices)}</div></div></div>`).join("")}</div>
        <button class="wbtn pri" data-open="${id}">Открыть переписку</button>
      </div>`;
  }

  // ---------- общий рендер ----------
  function render() {
    const app = $("#app"), view = $("#tabview"), right = $("#right");
    const wide = WIDE.matches;
    app.classList.toggle("has-detail", !!state.detail);
    app.classList.toggle("full", state.tab === "sales" && !state.detail);
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === state.tab));
    $("#tabBadge").textContent = state.queue.length || "";
    if (state.tab !== "inbox") view.innerHTML = "";
    if (state.tab === "inbox") renderInbox(view);
    else if (state.tab === "all") renderAll(view);
    else renderSales(view);
    // правая панель
    if (state.detail) {
      if (state.detail.type === "thread") renderThread(right, state.detail.id, false);
      else renderClient(right, state.detail.id, false);
    } else if (wide && state.tab !== "sales") {
      const id = state.tab === "inbox" ? state.queue[0] : null;
      if (id) renderThread(right, id, true);
      else right.innerHTML = `<div class="done" style="justify-content:center"><div class="empty">${state.tab === "all" ? "Выберите тред слева" : "Очередь пуста — откройте любой тред во вкладке «Все треды»"}</div></div>`;
    } else right.innerHTML = "";
  }

  function transition(fn) {
    if (motion() && document.startViewTransition) document.startViewTransition(fn);
    else fn();
  }
  function openDetail(type, id) {
    const deep = !WIDE.matches;
    transition(() => { state.detail = { type, id }; render(); });
    if (deep) history.pushState({ d: 1 }, "");
    if (tg && tg.BackButton) tg.BackButton.show();
  }
  function closeDetail(fromPop) {
    transition(() => { state.detail = null; state.edit = null; render(); });
    if (!fromPop && history.state && history.state.d) history.back();
    if (tg && tg.BackButton) tg.BackButton.hide();
  }
  addEventListener("popstate", () => { if (state.detail) closeDetail(true); });
  if (tg && tg.BackButton) tg.BackButton.onClick(() => closeDetail());

  // ---------- действия очереди ----------
  function flyTop(dir, after) {
    const el = document.querySelector("#stack .card[data-depth='0']");
    if (!el || !motion()) { after(); return; }
    const w = el.offsetWidth;
    el.classList.add("flying"); el.classList.remove("dragging");
    el.style.transition = "transform .38s cubic-bezier(.4,0,.8,.6), opacity .38s";
    el.style.transform = dir === "up" ? `translateY(-120%) scale(.9)` : `translate(${dir * w * 1.5}px, -30px) rotate(${dir * 26}deg)`;
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 400);
    setTimeout(after, 140);
  }
  function snapBack(el) { el.classList.remove("dragging"); el.style.transform = ""; el.querySelectorAll(".stamp").forEach((s) => (s.style.opacity = 0)); }
  function shake(el) { if (!el) return; el.classList.remove("shake"); void el.offsetWidth; el.classList.add("shake"); }

  function doSend(id, from) {
    if (!canSend(id)) {
      shake(document.querySelector(`.card[data-id="${id}"][data-depth="0"]`));
      toast(state.check[id] === "wait" ? "Подождите — проверяем язык" : "Текст не на языке клиента. Переведите или поставьте галочку.");
      return false;
    }
    commitPending();
    const go = () => {
      state.queue = state.queue.filter((x) => x !== id);
      state.sent.push(id);
      state.pending = { id, from };
      render();
      toast(`Отправлено ${T(id).buyer} · ${T(id).lang.toUpperCase()}`, () => undoSend(id), true);
    };
    from === "stack" ? flyTop(1, go) : go();
    return true;
  }
  function commitPending() {
    const p = state.pending;
    if (!p) return;
    const t = T(p.id), d = state.drafts[p.id];
    state.events[p.id].push({ who: "out", time: "сейчас", de: d.text, ru: d.ru, fresh: true });
    t.kind = "out"; t.draft = false; t.ago = "сейчас"; t.last_de = d.text; t.last_ru = d.ru;
    state.pending = null;
  }
  function undoSend(id) {
    state.pending = null;
    state.sent = state.sent.filter((x) => x !== id);
    state.queue.unshift(id);
    render();
    const el = document.querySelector(`.card[data-id="${id}"]`);
    if (el && motion()) { el.classList.add("enter-back"); setTimeout(() => el.classList.remove("enter-back"), 500); }
    toast("Отправка отменена — письмо не ушло");
  }
  function doLater(id) {
    commitPending();
    flyTop(-1, () => {
      state.queue = state.queue.filter((x) => x !== id);
      state.later.push(id);
      render();
      toast(`${T(id).buyer} — позже`, () => {
        state.later = state.later.filter((x) => x !== id); state.queue.unshift(id); render();
      });
    });
  }

  // ---------- тост с undo 5 с ----------
  let toastTimer = null;
  function toast(text, undo, isSend) {
    const el = $("#toast");
    clearTimeout(toastTimer);
    el.innerHTML = `<span class="tx">${esc(text)}</span>${undo ? `<button class="ub">Отменить</button>` : ""}${undo ? `<i class="tbar"></i>` : ""}`;
    el.classList.add("show");
    if (undo) {
      el.querySelector(".ub").onclick = () => { clearTimeout(toastTimer); el.classList.remove("show"); undo(); };
      const bar = el.querySelector(".tbar"); void bar.offsetWidth; bar.classList.add("run");
    }
    toastTimer = setTimeout(() => { el.classList.remove("show"); if (isSend) { commitPending(); if (state.detail) render(); } }, undo ? 5000 : 2400);
  }

  // ---------- свайпы ----------
  function bindSwipe(el) {
    let sx = 0, sy = 0, dx = 0, dy = 0, on = false, moved = false, pid = null;
    const stamps = () => [el.querySelector(".s-go"), el.querySelector(".s-later"), el.querySelector(".s-open")];
    el.addEventListener("pointerdown", (e) => {
      if (el.dataset.depth !== "0" || e.button > 0) return;
      if (e.target.closest("button, a, input, label, textarea")) return;
      on = true; moved = false; sx = e.clientX; sy = e.clientY; dx = dy = 0; pid = e.pointerId;
    });
    el.addEventListener("pointermove", (e) => {
      if (!on || e.pointerId !== pid) return;
      dx = e.clientX - sx; dy = e.clientY - sy;
      if (!moved && Math.hypot(dx, dy) > 8) { moved = true; el.classList.add("dragging"); try { el.setPointerCapture(pid); } catch (_) {} }
      if (!moved) return;
      const up = Math.min(0, dy);
      el.style.transform = `translate(${dx}px, ${up * 0.8 + Math.max(0, dy) * 0.15}px) rotate(${motion() ? dx / 16 : 0}deg)`;
      const [g, l, o] = stamps();
      g.style.opacity = Math.max(0, Math.min(1, dx / 110));
      l.style.opacity = Math.max(0, Math.min(1, -dx / 110));
      o.style.opacity = Math.abs(dx) < 50 ? Math.max(0, Math.min(1, -dy / 120)) : 0;
    });
    const end = (e, cancel) => {
      if (!on || (e && e.pointerId !== pid)) return;
      on = false;
      if (!moved) return;
      const w = el.offsetWidth, h = el.offsetHeight, id = el.dataset.id;
      if (!cancel && dx > w * 0.3) { if (!doSend(id, "stack")) snapBack(el); }
      else if (!cancel && dx < -w * 0.3) doLater(id);
      else if (!cancel && dy < -h * 0.16 && Math.abs(dy) > Math.abs(dx)) { snapBack(el); openDetail("thread", id); }
      else snapBack(el);
    };
    el.addEventListener("pointerup", (e) => end(e, false));
    el.addEventListener("pointercancel", (e) => end(e, true));
  }

  // ---------- нижний лист: этап 1 «выбор» ----------
  function openOptions(id) {
    const t = T(id), sh = $("#sheet");
    sh.innerHTML = `<div class="sheet" role="dialog" aria-label="Как отправить">
      <span class="grip"></span>
      <h3>Как отправить?</h3><p>Клиент пишет на языке: ${LANG_NAME[t.lang]}. Текст всегда проверяется перед отправкой.</p>
      <button class="opt" data-sh="translate"><span class="oi">🌐</span><span><b>Перевести на язык клиента</b><small>Ваш текст → ${t.lang.toUpperCase()} + RU-зеркало</small></span></button>
      <button class="opt" data-sh="asis"><span class="oi">📨</span><span><b>Отправить как есть</b><small>Без перевода, с проверкой языка</small></span></button>
      <button class="opt" data-sh="suggest"><span class="oi">✨</span><span><b>Предложить текст ИИ</b><small>Новый черновик с учётом торга</small></span></button>
      <textarea id="idea" placeholder="Идея для ИИ по-русски: «уступи до 1000, но только самовывоз»"></textarea>
      <button class="opt" data-sh="rewrite"><span class="oi">🔁</span><span><b>Переписать идею ИИ</b><small>По вашей инструкции выше</small></span></button>
      <button class="wbtn" data-sh="close">Отменить</button>
    </div>`;
    sh.hidden = false;
    sh.onclick = (e) => {
      const b = e.target.closest("[data-sh]");
      if (e.target === sh || (b && b.dataset.sh === "close")) { sh.hidden = true; return; }
      if (!b) return;
      const d = state.drafts[id], k = b.dataset.sh;
      if (k === "suggest" || k === "rewrite") {
        const p = Math.round(((d.suggestedPrice || t.price) * 0.97) / 10) * 10;
        if (id === "t1") { d.text = `Hallo Markus, die Sitzbank ist noch da. Ich komme Ihnen entgegen: ${p} € bei Abholung am Samstag. Viele Grüße`; d.ru = `Здравствуйте, Маркус, сиденье ещё есть. Иду навстречу: ${p} € при самовывозе в субботу. С уважением`; d.suggestedPrice = p; }
        toast(k === "rewrite" ? "ИИ переписал по вашей идее" : "Новый черновик готов");
      } else if (k === "translate" && d.fix) { d.text = d.fix.text; d.lang = d.fix.lang; toast("Переведено на язык клиента"); }
      else toast("Текст оставлен как есть — проверяем язык");
      sh.hidden = true; runCheck(id); render();
    };
  }

  // ---------- делегирование кликов ----------
  document.addEventListener("click", (e) => {
    const t = e.target;
    const tab = t.closest(".tab");
    if (tab) { transition(() => { state.tab = tab.dataset.tab; if (!WIDE.matches) state.detail = null; else if (state.detail && state.tab === "sales") state.detail = null; render(); }); return; }
    const tg2 = t.closest("[data-tab-go]");
    if (tg2) { transition(() => { state.tab = tg2.dataset.tabGo; state.detail = null; render(); }); return; }
    const pk = t.closest("[data-pick]");
    if (pk) { const v = pk.dataset.pick; state.sel.has(v) ? state.sel.delete(v) : state.sel.add(v); render(); return; }
    const op = t.closest("[data-open]");
    if (op) { openDetail("thread", op.dataset.open); return; }
    const cl = t.closest("[data-client]");
    if (cl) { openDetail("client", cl.dataset.client); return; }
    const fl = t.closest("[data-filter]");
    if (fl) { state.filter = fl.dataset.filter; render(); return; }
    const af = t.closest("[data-accf]");
    if (af) { const v = +af.dataset.accf; state.accFilter = state.accFilter === v ? null : v; render(); return; }
    const bo = t.closest("[data-orig]");
    if (bo && !t.closest("button")) { const k = bo.dataset.orig; state.showOrig[k] = !state.showOrig[k]; const f = $("#feed"); const st = f ? f.scrollTop : 0; render(); const f2 = $("#feed"); if (f2) f2.scrollTop = st; return; }
    const a = t.closest("[data-act]");
    if (!a) return;
    const top = state.queue[0], id = a.dataset.id;
    switch (a.dataset.act) {
      case "send": if (top) doSend(top, "stack"); break;
      case "later": if (top) doLater(top); break;
      case "edit": if (top) { state.edit = top; openDetail("thread", top); } break;
      case "orig": state.showOrig["c" + id] = !state.showOrig["c" + id]; rerenderCard(id); break;
      case "fix": { const d = state.drafts[id]; d.text = d.fix.text; d.lang = d.fix.lang; runCheck(id); refreshLang(id); if (state.detail) render(); toast("Переведено на " + LANG_NAME[d.lang]); break; }
      case "back": closeDetail(); break;
      case "edit-here": state.edit = id; render(); setTimeout(() => $("#edtxt") && $("#edtxt").focus(), 30); break;
      case "cancel-edit": state.edit = null; render(); break;
      case "save-edit": { const v = $("#edtxt").value.trim(); if (v) { state.drafts[id].text = v; state.drafts[id].ru = /[А-Яа-яЁё]/.test(v) ? v : state.drafts[id].ru; } state.edit = null; runCheck(id); render(); break; }
      case "options": openOptions(id); break;
      case "send-thread": {
        const wasDeep = !WIDE.matches;
        if (doSend(id, "thread")) {
          if (wasDeep) closeDetail(); else render();
        }
        break;
      }
      case "ping": {
        const th = T(id);
        state.drafts[id] = { lang: th.lang, text: PING[th.lang] || PING.de, ru: PING_RU, suggestedPrice: null };
        if (!state.queue.includes(id)) { state.queue.unshift(id); state.total++; }
        runCheck(id); render(); toast("ИИ предложил напоминание — проверьте и отправьте");
        break;
      }
      case "lessons": lessonsSheet(id); break;
      case "unwait": toggleWait(id); break;
      case "tmenu": threadMenu(id); break;
      case "sim-yes": state.similarDone[id] = true; render(); toast("Связано: история Mark Weber добавлена в досье"); break;
      case "sim-no": state.similarDone[id] = true; render(); toast("Отмечено: другой покупатель"); break;
      case "tag-add": case "tag-del": toggleTag(id, a.dataset.tag); render(); break;
      case "note-edit": state.noteEdit = true; render(); setTimeout(() => $("#notetx") && $("#notetx").focus(), 30); break;
      case "note-save": state.note = $("#notetx").value.trim() || state.note; state.noteEdit = false; render(); toast("Заметка сохранена"); break;
      case "det-yes": case "det-no": {
        const d = state.detected.find((x) => x.id === id);
        state.detected = state.detected.filter((x) => x.id !== id);
        if (a.dataset.act === "det-yes") {
          SALES.total += d.price; SALES.count += 1; SALES.recent.unshift({ ad: d.ad, price: d.price, acc: d.acc, date: "сегодня" });
          const ba = SALES.byAccount.find((x) => x.acc === d.acc); if (ba) ba.sum += d.price;
          toast(`Подтверждено: +${eur(d.price)}`);
        } else toast("Отклонено — это не продажа");
        render(); break;
      }
      case "manual-sale": manualSaleSheet(); break;
      case "selmode": state.selMode = !state.selMode; state.sel.clear(); render(); break;
      case "bulk-pin": state.sel.forEach((x) => state.pinned.add(x)); toast(`Закреплено: ${state.sel.size}`); state.sel.clear(); state.selMode = false; render(); break;
      case "bulk-wait": state.sel.forEach((x) => { state.waiting.add(x); state.queue = state.queue.filter((q) => q !== x); }); toast(`«Жду»: ${state.sel.size}`); state.sel.clear(); state.selMode = false; render(); break;
      case "relater": state.queue = state.later.slice(); state.later = []; render(); break;
    }
  });
  document.addEventListener("change", (e) => {
    const c = e.target.closest('[data-act="allow"]');
    if (c) { state.allow[c.dataset.id] = c.checked; refreshLang(c.dataset.id); }
  });
  document.addEventListener("input", (e) => { if (e.target.id === "q") { state.query = e.target.value; render(); } });

  // ---------- клавиатура (десктоп/отладка): → отправить, ← позже, ↑ открыть ----------
  document.addEventListener("keydown", (e) => {
    if (/INPUT|TEXTAREA/.test(document.activeElement.tagName) || state.detail || state.tab !== "inbox" || !state.queue[0]) return;
    if (e.key === "ArrowRight") doSend(state.queue[0], "stack");
    if (e.key === "ArrowLeft") doLater(state.queue[0]);
    if (e.key === "ArrowUp") openDetail("thread", state.queue[0]);
  });

  WIDE.addEventListener("change", render);
  MINI.addEventListener("change", render);
  if (tg) { try { tg.ready(); tg.expand(); } catch (_) {} }
  render();
})();
