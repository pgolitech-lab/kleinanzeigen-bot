/* Концепция C «Командный центр торговца».
   Vanilla JS без сборки. Данные — window.MOCK (../_shared/mock.js), локальные
   дополнения (наше последнее предложение, таймеры, шаблоны тонов) — ниже в EXTRA. */
(function () {
  'use strict';
  const M = window.MOCK;
  const qs = new URLSearchParams(location.search);
  const SHOT = qs.get('shot') === '1';
  const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------- Тема: как Telegram — выставляем --tg-theme-* на :root ----------
  const PRESETS = {
    dark: { // «ночной трейдинг»
      'bg-color': '#0a0e13', 'secondary-bg-color': '#0f151c', 'section-bg-color': '#121a23',
      'header-bg-color': '#0a0e13', 'bottom-bar-bg-color': '#0d1219',
      'text-color': '#e6edf3', 'hint-color': '#7f8c9b', 'subtitle-text-color': '#93a1b1',
      'link-color': '#4cc2ff', 'accent-text-color': '#4cc2ff',
      'button-color': '#17c27a', 'button-text-color': '#03140b',
      'destructive-text-color': '#ff6b6b', 'section-separator-color': '#1e2a36',
    },
    light: { // «бумажный терминал»
      'bg-color': '#f3efe4', 'secondary-bg-color': '#e9e3d4', 'section-bg-color': '#fbf8f1',
      'header-bg-color': '#f3efe4', 'bottom-bar-bg-color': '#ece6d7',
      'text-color': '#1d1a14', 'hint-color': '#7b7261', 'subtitle-text-color': '#5f5848',
      'link-color': '#0b63c4', 'accent-text-color': '#0b63c4',
      'button-color': '#0d7a4b', 'button-text-color': '#ffffff',
      'destructive-text-color': '#c0362c', 'section-separator-color': '#d6cdb8',
    },
  };
  const tgWA = window.Telegram && window.Telegram.WebApp;
  let theme = qs.get('theme');
  if (theme !== 'dark' && theme !== 'light') {
    theme = (tgWA && tgWA.colorScheme) || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  }
  document.documentElement.dataset.theme = theme;
  // Если реальный Telegram передал themeParams — они главнее пресета (кроме явного ?theme=)
  if (!(tgWA && tgWA.themeParams && Object.keys(tgWA.themeParams).length && !qs.get('theme'))) {
    for (const [k, v] of Object.entries(PRESETS[theme])) document.documentElement.style.setProperty('--tg-theme-' + k, v);
  }

  // ---------- Локальные дополнения к моку ----------
  const EXTRA = {
    t1: { ourLast: 1100, waitMin: 3 },
    t2: { ourLast: null, waitMin: 18 },
    t3: { ourLast: 620,  waitMin: 62 },
    t4: { ourLast: 200,  waitMin: 130 },
    t5: { ourLast: 270,  waitMin: 330 },
    t6: { ourLast: 750,  waitMin: 940 },
    t7: { ourLast: null, waitMin: 1100 },
  };
  // Новые сущности (досье, «тот же покупатель», уроки, пометки писем, найденные продажи)
  const FEAT = {
    pinned: ['t3'],
    waitUntil: { t4: 'до завтра 10:00' },
    rowFlags: { t3: { app: true }, t6: { late: true } },
    // флаги событий открытого треда t1 — по индексу в MOCK.thread.events
    eventFlags: { 2: { app: true }, 4: { late: 'Письмо дошло в 12:58 — на 17 мин позже отправки' } },
    dossier: [
      { date: '02.05', text: 'Kopfstützen T6: предлагал 70 € при цене 90 € — не договорились' },
      { date: '14.07', text: 'Tischplatte Multivan: торг 520 → 480 €, купил, забрал сам, наличные' },
      { date: 'Вт–Ср', text: 'Sitzbank T6: совместимость с Comfortline, просил фото снизу' },
      { date: 'сегодня', text: 'предлагает 900 € при цене 1 200 €, самовывоз в субботу' },
    ],
    sameBuyer: { name: 'M. Wagner', acc: 2, why: 'тот же стиль письма и PLZ 30…; в марте спрашивал Sitzbank T6 на другом аккаунте' },
    lessons: [
      { when: '12.10', note: 'Не писать «цена окончательная» — оставлять шаг к сделке', bad: 'Der Preis ist fest.', good: 'Für 1.050 € gehört sie Ihnen.' },
      { when: '03.10', note: 'Всегда подтверждать день самовывоза' },
      { when: '28.09', note: 'Обращаться по имени, в конце «Viele Grüße»' },
    ],
    found: [
      { id: 'f1', ad: 'Isofix Sitz T6.1', buyer: 'Peter H.', acc: 2, price: 320, why: 'клиент написал «Abgeholt, danke!»' },
      { id: 'f2', ad: 'Tisch Schienensystem T5', buyer: 'Jan de V.', acc: 1, price: 200, why: 'объявление снято (Gelöscht) после торга' },
    ],
  };
  const LANGS = { de: 'DE', pl: 'PL', nl: 'NL', tr: 'TR', ru: 'RU' };
  const LANG_NAME = { de: 'немецкий', pl: 'польский', nl: 'нидерландский', tr: 'турецкий', ru: 'русский' };
  // Шаблоны тонов на языке клиента + RU-зеркало (в реальном MA это /instruction к ИИ)
  const TPL = {
    de: {
      hold: (n, p) => `Hallo ${n}, danke für Ihr Angebot. ${p} € ist knapp kalkuliert – darunter kann ich leider nicht gehen. Viele Grüße`,
      deal: (n, p) => `Hallo ${n}, ${p} € kann ich Ihnen anbieten – dann gehört sie Ihnen. Abholung passt gut. Viele Grüße`,
      no:   (n) => `Hallo ${n}, vielen Dank für Ihr Interesse, aber zu diesem Preis kann ich leider nicht verkaufen. Viele Grüße`,
    },
    pl: {
      hold: (n, p) => `Dzień dobry, dziękuję za wiadomość. Cena ${p} € jest ostateczna. Pozdrawiam`,
      deal: (n, p) => `Dzień dobry, mogę zaproponować ${p} €, wysyłka do Polski jest możliwa. Pozdrawiam`,
      no:   () => `Dzień dobry, dziękuję za zainteresowanie, ale w tej cenie nie mogę sprzedać. Pozdrawiam`,
    },
    nl: {
      hold: (n, p) => `Hallo, bedankt voor uw bericht. ${p} € is mijn vaste prijs. Met vriendelijke groet`,
      deal: (n, p) => `Hallo, ik kan u ${p} € aanbieden. Met vriendelijke groet`,
      no:   () => `Hallo, bedankt voor uw interesse, maar voor die prijs kan ik helaas niet verkopen. Met vriendelijke groet`,
    },
    tr: {
      hold: (n, p) => `Merhaba, teklifiniz için teşekkürler. ${p} € son fiyattır. Saygılarımla`,
      deal: (n, p) => `Merhaba, size ${p} € teklif edebilirim. Saygılarımla`,
      no:   () => `Merhaba, ilginiz için teşekkürler ama bu fiyata satamam. Saygılarımla`,
    },
  };
  const TPL_RU = {
    hold: (n, p) => `Здравствуйте${n ? ', ' + n : ''}, спасибо за предложение. ${p} € — уже впритык, ниже, к сожалению, не могу. С уважением`,
    deal: (n, p) => `Здравствуйте${n ? ', ' + n : ''}, могу предложить ${p} € — и товар ваш. С уважением`,
    no:   (n) => `Здравствуйте${n ? ', ' + n : ''}, спасибо за интерес, но за эту цену продать, к сожалению, не могу. С уважением`,
  };

  // ---------- Утилиты ----------
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString('ru-RU').replace(/ | /g, ' '));
  const eur = (n) => (n == null ? '—' : fmt(n) + ' €');
  const acc = (id) => M.accounts.find((a) => a.id === id);
  const thr = (id) => M.threads.find((t) => t.id === id);
  const firstName = (b) => (b || '').split(' ')[0];
  const ext = (t) => Object.assign({ ourLast: null, waitMin: 0 }, EXTRA[t.id]);
  const hasCyr = (s) => /[Ѐ-ӿ]/.test(s);
  const round10 = (n) => Math.round(n / 10) * 10;

  const ICON = {
    back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    send: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l16-8-6 16-2.5-6.5L4 12z" fill="currentColor"/></svg>',
    inbox: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 13l3-8h12l3 8v6H3v-6zm0 0h5l1 2h6l1-2h5" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/></svg>',
    user: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4" fill="none" stroke="currentColor" stroke-width="1.9"/><path d="M4 21c1-4 4.5-6 8-6s7 2 8 6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
    chart: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    deal: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8M15 7h6v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    edit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16v4z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/></svg>',
    ai: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3zM18 15l.9 2.1L21 18l-2.1.9L18 21l-.9-2.1L15 18l2.1-.9L18 15z" fill="currentColor"/></svg>',
    next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l-1 6 4 4H6l4-4-1-6zM12 13v8" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    wait: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="13" r="8" fill="none" stroke="currentColor" stroke-width="1.9"/><path d="M12 9v4l3 2M9 2h6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
    sold: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l5 5L20 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    skip: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5l14 14M19 5L5 19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4" fill="none" stroke="currentColor" stroke-width="1.9"/><path d="M8 12l3 3 5-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    warn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l10 18H2L12 3zM12 10v5M12 18v.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>',
    plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
  };

  // ---------- Состояние ----------
  const SCREENS = ['inbox', 'thread', 'client', 'sales'];
  const S = {
    screen: SCREENS.includes(qs.get('screen')) ? qs.get('screen') : 'inbox',
    sel: 't1',
    accs: new Set(),          // пусто = все аккаунты
    filter: 'in',             // in | out | all
    tone: 'ai',
    drafts: {},               // id -> {text, ru, price, tone, edited}
    allowLang: false,
    ruOpen: false,
    sheet: null,
    from: 'inbox',
    pagerIdx: 0,
    sent: new Set(),
    mode: 'narrow',
    selMode: false, picked: new Set(),
    pinned: new Set(FEAT.pinned), waitT: Object.assign({}, FEAT.waitUntil),
    skipped: new Set(), sold: new Set(), found: FEAT.found.slice(),
    lessonsOpen: false, tags: {}, tagInput: false,
  };
  const tagsOf = (t) => S.tags[t.id] || (S.tags[t.id] = t.tags.slice());
  const closed = (t) => S.skipped.has(t.id) || S.sold.has(t.id);

  // ---------- Черновик ----------
  function toneDraft(t, tone) {
    const e = ext(t);
    const lang = TPL[t.lang] ? t.lang : 'de';
    const n = t.lang === 'de' ? firstName(t.buyer) : '';
    const holdP = e.ourLast || t.price;
    if (tone === 'ai') {
      if (t.id === M.thread.id) return { text: M.thread.draft.text, ru: M.thread.draft.ru, price: M.thread.draft.suggestedPrice };
      const p = t.offer ? round10((t.offer + holdP) / 2 + 20) : holdP;
      const pp = Math.max(p, t.floor);
      return { text: TPL[lang].deal(n, fmt(pp)), ru: TPL_RU.deal(n && firstNameRu(n), fmt(pp)), price: pp };
    }
    if (tone === 'hold') return { text: TPL[lang].hold(n, fmt(holdP)), ru: TPL_RU.hold(n && firstNameRu(n), fmt(holdP)), price: holdP };
    if (tone === 'm5') { const p = Math.max(round10(holdP * 0.95), t.floor); return { text: TPL[lang].deal(n, fmt(p)), ru: TPL_RU.deal(n && firstNameRu(n), fmt(p)), price: p }; }
    if (tone === 'floor') return { text: TPL[lang].deal(n, fmt(t.floor)), ru: TPL_RU.deal(n && firstNameRu(n), fmt(t.floor)), price: t.floor };
    return { text: TPL[lang].no(n), ru: TPL_RU.no(n && firstNameRu(n)), price: null };
  }
  function firstNameRu(n) { return ({ Markus: 'Маркус', Thomas: 'Томас', Sabine: 'Сабина', Peter: 'Петер' })[n] || n; }
  function draftOf(t) {
    if (!S.drafts[t.id]) S.drafts[t.id] = Object.assign({ tone: 'ai', edited: false }, toneDraft(t, 'ai'));
    return S.drafts[t.id];
  }
  function toneList(t) {
    const e = ext(t);
    const holdP = e.ourLast || t.price;
    return [
      { k: 'ai',    label: 'ИИ', val: toneDraft(t, 'ai').price },
      { k: 'hold',  label: 'Держим цену', val: holdP },
      { k: 'm5',    label: '−5 %', val: Math.max(round10(holdP * 0.95), t.floor) },
      { k: 'floor', label: 'До floor', val: t.floor },
      { k: 'no',    label: 'Вежливо отказать', val: null },
    ];
  }

  // ---------- Компоненты ----------
  const langBadge = (l, cls = '') => `<span class="lang ${cls}" title="${esc(LANG_NAME[l] || l)}">${LANGS[l] || l.toUpperCase()}</span>`;
  const accDot = (id) => { const a = acc(id); return `<span class="accdot" style="--c:${a.color}" title="${esc(a.name)}"></span>`; };
  const probCls = (s) => (s >= 75 ? 'hi' : s >= 50 ? 'mid' : 'lo');
  const waitCls = (t) => { const w = ext(t).waitMin; return t.kind !== 'in' ? 'calm' : w >= 60 ? 'hot' : w >= 15 ? 'warm' : 'fresh'; };

  // Термометр торга: шкала floor…цена, маркеры клиент / наше / (черновик)
  function thermo(t, opt = {}) {
    const e = ext(t);
    const d = opt.draft;
    const vals = [t.price, t.floor, t.offer, e.ourLast, d].filter((v) => v != null);
    const lo = Math.min(...vals) * 0.9, hi = t.price * 1.04;
    const pos = (v) => Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100)).toFixed(1);
    const fp = pos(t.floor);
    const m = [];
    m.push(`<i class="tm tm-price" style="left:${pos(t.price)}%"></i>`);
    if (e.ourLast != null) m.push(`<i class="tm tm-our" style="left:${pos(e.ourLast)}%"></i>`);
    if (d != null) m.push(`<i class="tm tm-draft" style="left:${pos(d)}%"></i>`);
    if (t.offer != null) m.push(`<i class="tm tm-offer" style="left:${pos(t.offer)}%"></i>`);
    return `<span class="thermo ${opt.mini ? 'mini' : ''}" style="--fp:${fp}%" role="img" aria-label="Цена ${eur(t.price)}, floor ${eur(t.floor)}, клиент ${eur(t.offer)}, наше ${eur(e.ourLast)}">
      <span class="tr"></span><i class="tm tm-floor" style="left:${fp}%"></i>${m.join('')}</span>`;
  }
  function thermoLegend(t, d) {
    const e = ext(t);
    const cell = (cls, lab, v) => `<div class="tl ${cls}"><span class="tl-k"><i></i>${lab}</span><b class="num">${eur(v)}</b></div>`;
    return `<div class="tlegend">${cell('k-price', 'Цена', t.price)}${cell('k-our', 'Наше', e.ourLast)}${cell('k-offer', 'Клиент', t.offer)}${cell('k-floor', 'Floor', t.floor)}${d != null ? cell('k-draft', 'Черновик', d) : ''}</div>`;
  }

  function spark(arr, w = 120, h = 30) {
    let acc2 = 0; const cum = arr.map((v) => (acc2 += v));
    const max = cum[cum.length - 1] || 1;
    const pts = cum.map((v, i) => [(i / (cum.length - 1)) * w, h - 2 - (v / max) * (h - 4)]);
    const line = pts.map((p) => p.map((x) => x.toFixed(1)).join(',')).join(' ');
    const last = pts[pts.length - 1];
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
      <polygon points="0,${h} ${line} ${w},${h}" class="sp-area"/><polyline points="${line}" class="sp-line" vector-effect="non-scaling-stroke"/>
      <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.6" class="sp-dot"/></svg>`;
  }

  const kindOf = (t) => (S.sent.has(t.id) || S.waitT[t.id] || closed(t) ? 'out' : t.kind);
  const waiting = () => M.threads.filter((t) => kindOf(t) === 'in');
  const todaySum = () => M.sales.recent.filter((r) => r.date === 'сегодня').reduce((a, r) => a + r.price, 0);

  // ---------- Пульс ----------
  function renderPulse() {
    const w = waiting().length;
    const tabs = S.mode === 'wide' ? `<div class="ptabs" role="tablist">${tabItems().map((x) => `<button class="ptab ${x.on ? 'on' : ''}" data-go="${x.k}" role="tab" aria-selected="${x.on}">${x.icon}<span>${x.label}</span></button>`).join('')}</div>` : '';
    $('#pulse').innerHTML = `
      <div class="pbrand"><span class="pled"></span><span class="pname">Пульс</span><span class="ptime num">12:44</span></div>
      <div class="pmetrics">
        <div class="pm pm-wait ${w ? 'alert' : ''}"><span class="pm-k">ждут нас</span><b class="pm-v num" data-count="${w}">${w}</b></div>
        <div class="pm"><span class="pm-k" title="медиана скорости ответа">ответ</span><b class="pm-v num">${esc(M.sales.replyMedian)}</b></div>
        <div class="pm"><span class="pm-k">сегодня</span><b class="pm-v num up" data-count="${todaySum()}" data-eur="1">${eur(todaySum())}</b></div>
        <div class="pm pm-month"><span class="pm-k">${esc(M.sales.period.split(' ')[0].toLowerCase())}</span><b class="pm-v num" data-count="${M.sales.total}" data-eur="1">${eur(M.sales.total)}</b>${spark(M.sales.byDay)}</div>
      </div>${tabs}`;
  }

  // ---------- Очередь ----------
  function renderChips() {
    const cnt = (id) => M.threads.filter((t) => t.acc === id && (S.filter === 'all' || kindOf(t) === S.filter)).length;
    const all = S.accs.size === 0;
    return `<div class="chips" role="group" aria-label="Аккаунты">
      <button class="chip ${all ? 'on' : ''}" data-acc="all">Все</button>
      ${M.accounts.map((a) => `<button class="chip ${S.accs.has(a.id) ? 'on' : ''}" data-acc="${a.id}" style="--c:${a.color}" aria-pressed="${S.accs.has(a.id)}"><i></i>${esc(a.name)}<span class="num">${cnt(a.id)}</span></button>`).join('')}
    </div>`;
  }
  function renderQueue() {
    const inN = M.threads.filter((t) => kindOf(t) === 'in').length;
    const outN = M.threads.length - inN;
    let list = M.threads.filter((t) => (S.filter === 'all' || kindOf(t) === S.filter) && (S.accs.size === 0 || S.accs.has(t.acc)));
    list = list.filter((t) => S.pinned.has(t.id)).concat(list.filter((t) => !S.pinned.has(t.id)));   // закреплённые сверху
    const rows = list.map((t) => {
      const e = ext(t), d = S.drafts[t.id];
      const on = S.sel === t.id && (S.mode === 'wide');
      const f = FEAT.rowFlags[t.id] || {};
      const status = S.sold.has(t.id) ? 'продано' : S.skipped.has(t.id) ? 'пропущ.' : S.sent.has(t.id) ? 'отпр.' : S.waitT[t.id] ? 'жду' : t.ago;
      const flags = [
        S.pinned.has(t.id) ? `<span class="flag pin" title="Закреплён">${ICON.pin}</span>` : '',
        S.waitT[t.id] ? `<span class="flag wait" title="Жду ${esc(S.waitT[t.id])}">${ICON.wait}<span>${esc(S.waitT[t.id])}</span></span>` : '',
        f.app ? '<span class="flag kz" title="Ответ пришёл через сайт/приложение Kleinanzeigen">из приложения</span>' : '',
        f.late ? '<span class="flag late" title="Письмо пришло с опозданием">запоздалое</span>' : '',
      ].join('');
      const pick = S.selMode;
      return `<button class="row ${on ? 'sel' : ''} ${pick ? 'picking' : ''} ${S.picked.has(t.id) ? 'picked' : ''} ${t.unread && kindOf(t) === 'in' ? 'unread' : ''} ${S.sent.has(t.id) || closed(t) ? 'sent' : ''}" ${pick ? `data-pick="${t.id}" aria-pressed="${S.picked.has(t.id)}"` : `data-open="${t.id}"`} style="--c:${acc(t.acc).color}">
        ${pick ? `<span class="pickbox" aria-hidden="true">${S.picked.has(t.id) ? ICON.sold : ''}</span>` : ''}
        <span class="r1"><b class="buyer">${esc(t.buyer)}</b>${langBadge(t.lang)}${t.draft && kindOf(t) === 'in' ? '<span class="aitag" title="Готов черновик ИИ">ИИ</span>' : ''}<span class="timer num ${waitCls(t)}">${esc(status)}</span></span>
        ${flags ? `<span class="rflags">${flags}</span>` : ''}
        <span class="r2">${esc(t.ad)}</span>
        <span class="r2b">${esc(t.last_ru)}</span>
        <span class="r3">${thermo(t, { mini: true })}<span class="rnum num"><em class="o">${t.offer ? fmt(t.offer) : '—'}</em><span class="sl">/</span>${fmt(t.price)}</span><span class="prob num ${probCls(t.score)}" title="Вероятность сделки">${t.score}%</span></span>
      </button>`;
    }).join('');
    $('#col-queue').innerHTML = `
      <div class="qhead">
        ${renderChips()}
        <div class="qbar">
          <div class="seg" role="tablist" aria-label="Фильтр">
            <button class="${S.filter === 'in' ? 'on' : ''}" data-filter="in">Ждут<span class="lg-nas"> нас</span> <span class="num">${inN}</span></button>
            <button class="${S.filter === 'out' ? 'on' : ''}" data-filter="out">Ждём <span class="num">${outN}</span></button>
            <button class="${S.filter === 'all' ? 'on' : ''}" data-filter="all">Все</button>
          </div>
          <button class="selbtn ${S.selMode ? 'on' : ''}" data-selmode aria-pressed="${S.selMode}" aria-label="Выбрать несколько">${ICON.check}</button>
        </div>
      </div>
      <div class="qlist">${rows || '<p class="empty">Пусто. Хорошая работа.</p>'}
        <p class="qlegend"><span><i class="lg-offer"></i>клиент</span><span><i class="lg-our"></i>наше</span><span><i class="lg-floor"></i>floor</span><span>%&nbsp;— шанс сделки</span></p>
      </div>
      ${S.selMode ? `<div class="bulk" role="toolbar" aria-label="Действия с выбранными">
        <span class="bulk-n num">${S.picked.size}</span>
        <button class="btn" data-bulk="pin" ${S.picked.size ? '' : 'disabled'}>${ICON.pin}<span>Закрепить</span></button>
        <button class="btn" data-bulk="wait" ${S.picked.size ? '' : 'disabled'}>${ICON.wait}<span>Жду</span></button>
        <button class="btn" data-bulk="skip" ${S.picked.size ? '' : 'disabled'}>${ICON.skip}<span>Пропуст.</span></button>
        <button class="btn icon" data-selmode aria-label="Отменить выбор">${ICON.close}</button>
      </div>` : ''}`;
  }

  // ---------- Тред ----------
  function threadEvents(t) {
    if (t.id === M.thread.id) return M.thread.events.slice();
    const ev = [{ who: 'in', time: t.time, de: t.last_de, ru: t.last_ru }];
    return ev;
  }
  function renderThread() {
    const t = thr(S.sel);
    const d = draftOf(t);
    const a = acc(t.acc);
    const ev = threadEvents(t);
    if (S.sent.has(t.id)) ev.push({ who: 'out', time: 'сейчас', de: d.text, ru: d.ru, justSent: true });
    const narrow = S.mode !== 'wide';
    const isT1 = t.id === M.thread.id;
    const rf = FEAT.rowFlags[t.id] || {};
    const feed = ev.map((e, i) => {
      const fl = isT1 ? FEAT.eventFlags[i] || {} : (i === 0 ? { app: rf.app, late: rf.late ? 'Письмо пришло с опозданием' : null } : {});
      return `<div class="msg ${e.who} ${e.justSent ? 'fresh' : ''}">
        <div class="mmeta"><span>${e.who === 'in' ? esc(firstName(t.buyer)) : 'мы' + (e.auto ? ' · авто' : '')}</span><span class="num">${esc(e.time)}</span></div>
        ${fl.app || fl.late ? `<div class="mflags">${fl.app ? '<span class="flag kz">из приложения</span>' : ''}${fl.late ? `<span class="flag late" title="${esc(fl.late)}">запоздалое</span>${isT1 ? `<span class="late-why">${esc(fl.late)}</span>` : ''}` : ''}</div>` : ''}
        <div class="mru">${esc(e.ru)}</div>
        <div class="morig">${langBadge(t.lang, 'xs')}${esc(e.de)}</div>
      </div>`;
    }).join('');
    const same = isT1 ? `<button class="warnbar" ${narrow ? 'data-sheet="client"' : 'data-go="client"'}>${ICON.warn}<span><b>Возможно, тот же покупатель:</b> ${esc(FEAT.sameBuyer.name)} · ${esc(acc(FEAT.sameBuyer.acc).name)}</span></button>` : '';
    const stateBar = S.sold.has(t.id) ? '<div class="statebar up">Продано · сделка закрыта</div>' : S.skipped.has(t.id) ? '<div class="statebar">Пропущено</div>' : S.waitT[t.id] ? `<div class="statebar wait">${ICON.wait}Жду ${esc(S.waitT[t.id])} — тред вернётся в «Ждут нас»</div>` : '';
    $('#col-main').innerHTML = `
      <div class="thead">
        ${narrow ? `<button class="back" data-back aria-label="Назад">${ICON.back}<span>Назад</span></button>` : ''}
        <div class="tt">
          <div class="tt1"><b>${esc(t.buyer)}</b>${langBadge(t.lang)}</div>
          <div class="tt2"><span class="acctag" style="--c:${a.color}">${esc(a.name)}</span>${esc(t.ad)}</div>
        </div>
        ${narrow ? `<div class="tbtns"><button class="mini-btn" data-sheet="deal" aria-label="Сделка">${ICON.deal}<span>Сделка</span></button><button class="mini-btn" data-sheet="client" aria-label="Клиент">${ICON.user}<span>Клиент</span></button></div>` : ''}
      </div>
      ${narrow ? `<button class="tstrip" data-sheet="deal" aria-label="Открыть панель сделки">${thermo(t, { draft: d.price })}<span class="ts-n num"><em class="o">${eur(t.offer)}</em> → <b>${eur(d.price)}</b><span class="ts-f">floor ${fmt(t.floor)}</span></span></button>` : ''}
      <div class="feed" id="feed">${same}${feed}</div>${stateBar}
      ${renderConsole(t, d)}`;
    fitDraft();
    const f = $('#feed'); if (f) f.scrollTop = f.scrollHeight;
  }
  function fitDraft() {
    const ta = $('#draft'); if (!ta) return;
    ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 2 + 'px';
  }

  function langState(t, d) {
    const cyr = hasCyr(d.text) && t.lang !== 'ru';
    const empty = !d.text.trim();
    return { ok: !cyr && !empty, cyr, empty };
  }
  function renderConsole(t, d) {
    if (S.sent.has(t.id)) {
      const nx = waiting()[0];
      return `<div class="console done"><div class="donebox"><b>Отправлено</b><span>Ждём ответ ${esc(firstName(t.buyer))}. Ответ ушёл на языке клиента (${LANGS[t.lang]}).</span>
        ${nx ? `<button class="btn primary big" data-open="${nx.id}">Следующая: ${esc(nx.buyer)} ${ICON.next}</button>` : '<span class="hint">Очередь пуста.</span>'}</div></div>`;
    }
    const L = langState(t, d);
    const tones = toneList(t).map((x) => `<button class="tone ${d.tone === x.k ? 'on' : ''} ${x.k === 'no' ? 'neg' : ''}" data-tone="${x.k}">${esc(x.label)}${x.val != null ? `<span class="num">${fmt(x.val)}</span>` : ''}</button>`).join('');
    const badge = L.ok ? `<span class="lbadge ok">${LANGS[t.lang]} ✓</span>` : `<span class="lbadge bad">${L.empty ? 'пусто' : 'RU ≠ ' + LANGS[t.lang]}</span>`;
    const canSend = L.ok || (L.cyr && S.allowLang);
    return `<div class="console" id="console">
      <div class="csteps" aria-label="Этапы отправки"><span class="st done">1 выбор</span><span class="st done">2 подготовка</span><span class="st on">3 финал</span><button class="lessonbtn ${S.lessonsOpen ? 'on' : ''}" data-lessons aria-expanded="${S.lessonsOpen}" title="Правки оператора, которые ИИ учёл">уроки <b class="num">${FEAT.lessons.length}</b></button><span class="cspacer"></span>${d.price != null ? `<span class="cprice num">${eur(d.price)}</span>` : ''}</div>
      ${S.lessonsOpen ? `<ul class="lessons">${FEAT.lessons.map((l) => `<li><span class="num">${esc(l.when)}</span><div>${esc(l.note)}${l.bad ? `<small><s>${esc(l.bad)}</s> → ${esc(l.good)}</small>` : ''}</div></li>`).join('')}</ul>` : ''}
      <div class="tones" role="group" aria-label="Тон и цена">${tones}</div>
      <div class="draft ${L.ok ? '' : 'warn'}">
        <div class="dhead"><span>Клиенту · ${esc(LANG_NAME[t.lang])}</span>${badge}</div>
        <textarea id="draft" rows="4" spellcheck="false" aria-label="Текст ответа клиенту">${esc(d.text)}</textarea>
      </div>
      <button class="mirror ${S.ruOpen ? 'open' : ''}" data-ru aria-expanded="${S.ruOpen}"><span class="mk">RU</span><span class="mt">${esc(d.edited ? 'Зеркало обновится после перевода…' : d.ru)}</span></button>
      ${L.cyr ? `<label class="allow"><input type="checkbox" id="allow" ${S.allowLang ? 'checked' : ''}> Разрешаю отправить текст не на языке клиента</label>` : ''}
      <div class="cact">
        <button class="btn icon" data-act="edit" aria-label="Перевести мой текст">${ICON.edit}</button>
        <button class="btn icon" data-act="regen" aria-label="Переписать идею ИИ">${ICON.ai}</button>
        <button class="btn primary big send" data-act="send" ${canSend ? '' : 'disabled'}>${ICON.send}<span>Отправить</span><span class="sbadge">${L.ok ? LANGS[t.lang] + ' ✓' : L.cyr && S.allowLang ? 'RU!' : '…'}</span></button>
      </div>
    </div>`;
  }

  // ---------- Сделка + клиент (боковая панель / листы) ----------
  function dealPanel(t) {
    const d = draftOf(t);
    const e = ext(t);
    const gap = t.offer != null ? (e.ourLast || t.price) - t.offer : null;
    const toFloor = t.offer != null ? t.offer - t.floor : null;
    const summary = t.id === M.thread.id ? M.thread.deal.summary : (t.tags.length ? 'Метки: ' + t.tags.join(', ') + '.' : 'Без заметок.');
    return `<div class="panel">
      <div class="ph"><span>Сделка</span><span class="prob num ${probCls(t.score)}">${t.score}%</span></div>
      ${thermo(t, { draft: d.price })}
      ${thermoLegend(t, d.price)}
      <div class="kv">
        <div><span>Разрыв торга</span><b class="num">${gap != null ? eur(gap) : '—'}</b></div>
        <div><span>Клиент к floor</span><b class="num ${toFloor != null && toFloor < 0 ? 'dn' : 'up'}">${toFloor != null ? (toFloor > 0 ? '+' : toFloor < 0 ? '−' : '') + eur(Math.abs(toFloor)) : '—'}</b></div>
        <div><span>Скидка</span><b class="num">${d.price != null ? '−' + ((1 - d.price / t.price) * 100).toFixed(1).replace('.', ',') + ' %' : '—'}</b></div>
      </div>
      <p class="psum">${esc(summary)}</p>
      <div class="tags">${tagsOf(t).map((x) => `<button class="tag" data-untag="${esc(x)}" title="Убрать тег">${esc(x)} ×</button>`).join('')}
        ${S.tagInput ? '<input class="taginput" id="taginput" placeholder="тег + Enter" maxlength="24">' : `<button class="tag add" data-addtag>${ICON.plus}тег</button>`}</div>
      <div class="dacts">
        <button class="btn ${S.pinned.has(t.id) ? 'on' : ''}" data-tact="pin">${ICON.pin}<span>${S.pinned.has(t.id) ? 'Открепить' : 'Закрепить'}</span></button>
        <button class="btn ${S.waitT[t.id] ? 'on' : ''}" data-tact="wait">${ICON.wait}<span>${S.waitT[t.id] ? 'Не ждать' : 'Жду'}</span></button>
        <button class="btn sold" data-tact="sold" ${S.sold.has(t.id) ? 'disabled' : ''}>${ICON.sold}<span>Продано</span></button>
        <button class="btn neg" data-tact="skip" ${S.skipped.has(t.id) ? 'disabled' : ''}>${ICON.skip}<span>Пропустить</span></button>
      </div>
    </div>`;
  }
  function clientPanel(compact) {
    const c = M.client;
    const t = thr(S.sel);
    const isKnown = t.id === M.thread.id;
    if (!isKnown) {
      return `<div class="panel"><div class="ph"><span>Клиент</span>${langBadge(t.lang)}</div>
        <p class="psum"><b>${esc(t.buyer)}</b> — первое обращение. Истории покупок нет.</p>
        <div class="kv"><div><span>Средний торг</span><b class="num">—</b></div><div><span>Обращений</span><b class="num">1</b></div></div></div>`;
    }
    return `<div class="panel">
      <div class="ph"><span>Клиент</span>${compact ? `<button class="link" data-go="client">профиль ${ICON.next}</button>` : ''}</div>
      <div class="cname"><b>${esc(c.name)}</b>${langBadge(c.lang)}<span class="hint">с ${esc(c.since)}</span></div>
      ${sameBuyerBox()}
      ${dossierList()}
      <div class="kv">
        <div><span>Куплено</span><b class="num">${c.stats.bought} · ${eur(c.stats.spent)}</b></div>
        <div><span>Средний торг</span><b class="num">−10 %</b></div>
        <div><span>Обращений</span><b class="num">${c.stats.inquiries}</b></div>
      </div>
      <ul class="hist">${c.history.map((h) => `<li><span class="hs ${h.status === 'куплено' ? 'up' : h.status === 'идёт торг' ? 'live' : 'dn'}"></span><span class="ha">${esc(h.ad)}</span><span class="hp num">${eur(h.price)}</span><span class="hd">${esc(h.status)} · ${esc(h.date)}</span></li>`).join('')}</ul>
      <label class="note"><span>Заметка</span><textarea rows="2">${esc(c.note)}</textarea></label>
    </div>`;
  }
  function sameBuyerBox() {
    const sb = FEAT.sameBuyer, a = acc(sb.acc);
    return `<div class="samebox">${ICON.warn}<div><b>Возможно, тот же покупатель</b><span><span class="acctag" style="--c:${a.color}">${esc(a.name)}</span> ${esc(sb.name)} — ${esc(sb.why)}</span></div></div>`;
  }
  function dossierList() {
    return `<div class="dossier"><div class="dk">Досье</div><ol>${FEAT.dossier.map((x) => `<li><span class="num">${esc(x.date)}</span><span>${esc(x.text)}</span></li>`).join('')}</ol></div>`;
  }
  function renderSide() {
    const t = thr(S.sel);
    if (S.screen === 'client') { $('#col-side').innerHTML = dealPanel(t); return; }
    $('#col-side').innerHTML = dealPanel(t) + clientPanel(true);
  }

  // ---------- Профиль клиента ----------
  function renderClient() {
    const c = M.client;
    const t1 = thr(M.thread.id);
    const tiles = [
      ['Обращений', c.stats.inquiries], ['Куплено', c.stats.bought], ['Потратил', eur(c.stats.spent)],
      ['Средний торг', '−10 %'], ['Ответ', c.stats.avgReply], ['Контакт', 'сегодня'],
    ].map(([k, v]) => `<div class="tile"><span>${k}</span><b class="num">${v}</b></div>`).join('');
    $('#col-main').innerHTML = `
      <div class="thead">
        <button class="back" data-back aria-label="Назад">${ICON.back}<span>Назад</span></button>
        <div class="tt"><div class="tt1"><b>${esc(c.name)}</b>${langBadge(c.lang)}</div><div class="tt2">клиент с ${esc(c.since)} · ${esc(c.email)}</div></div>
      </div>
      <div class="scroll pad">
        <div class="tiles">${tiles}</div>
        ${sameBuyerBox()}
        <div class="panel">${dossierList()}</div>
        <div class="panel"><div class="ph"><span>Заметка оператора</span></div><textarea class="notebig" rows="3">${esc(c.note)}</textarea></div>
        <div class="panel"><div class="ph"><span>История</span><span class="hint num">${c.history.length}</span></div>
          <table class="tbl"><tbody>${c.history.map((h) => `<tr><td><span class="hs ${h.status === 'куплено' ? 'up' : h.status === 'идёт торг' ? 'live' : 'dn'}"></span></td><td class="ta">${esc(h.ad)}<small>${esc(h.status)} · ${esc(h.date)}</small></td><td class="num tr">${eur(h.price)}</td></tr>`).join('')}</tbody></table>
        </div>
        ${S.mode === 'wide' ? `<button class="btn primary big wide-btn" data-open="t1">Открыть текущую сделку ${ICON.next}</button>` : `<div class="panel"><div class="ph"><span>Текущая сделка</span><span class="prob num ${probCls(t1.score)}">${t1.score}%</span></div>
          <div class="cur-ad">${esc(t1.ad)}</div>
          ${thermo(t1, { draft: draftOf(t1).price })}
          ${thermoLegend(t1, draftOf(t1).price)}
          <button class="btn primary big wide-btn" data-open="t1">Открыть сделку ${ICON.next}</button>
        </div>`}
      </div>`;
  }

  // ---------- Продажи ----------
  function renderSales() {
    const s = M.sales;
    const avgCheck = s.total / s.count;
    const tiles = [
      ['Выручка', eur(s.total), s.total, true, 'up'], ['Сделок', s.count, s.count, false, ''],
      ['Средний чек', eur(avgCheck), avgCheck, true, ''], ['Средний торг', '−' + String(s.avgDiscount).replace('.', ',') + ' %', null, false, 'dn'],
      ['Ответ, медиана', s.replyMedian, null, false, ''],
    ].map(([k, v, n, isE, cls]) => `<div class="tile"><span>${k}</span><b class="num ${cls}" ${n != null ? `data-count="${Math.round(n)}" ${isE ? 'data-eur="1"' : ''}` : ''}>${v}</b></div>`).join('');
    const max = Math.max(...s.byDay);
    const W = 100 / s.byDay.length;
    const bars = s.byDay.map((v, i) => {
      const h = v ? Math.max(3, (v / max) * 100) : 0;
      return `<g><rect x="${(i * W + W * 0.15).toFixed(2)}" y="${(100 - h).toFixed(2)}" width="${(W * 0.7).toFixed(2)}" height="${h.toFixed(2)}" class="bar ${v === max ? 'top' : ''}" style="--i:${i}"><title>${i + 1} окт: ${eur(v)}</title></rect>${v ? '' : `<rect x="${(i * W + W * 0.3).toFixed(2)}" y="98.6" width="${(W * 0.4).toFixed(2)}" height="1.4" class="bar0"/>`}</g>`;
    }).join('');
    const tot = s.byAccount.reduce((a, b) => a + b.sum, 0);
    const stack = s.byAccount.map((b) => `<i style="flex:${b.sum};--c:${acc(b.acc).color}"></i>`).join('');
    const legend = s.byAccount.slice().sort((a, b) => b.sum - a.sum).map((b) => `<li style="--c:${acc(b.acc).color}"><i></i><span>${esc(acc(b.acc).name)}</span><b class="num">${eur(b.sum)}</b><em class="num">${Math.round((b.sum / tot) * 100)}%</em></li>`).join('');
    const narrow = S.mode !== 'wide';
    const addSale = `<button class="btn addsale" data-sheet="manual">${ICON.plus}<span>Продажа</span></button>`;
    $('#col-main').innerHTML = `
      ${narrow ? `<div class="thead"><div class="tt"><div class="tt1"><b>Продажи</b></div><div class="tt2">${esc(s.period)}</div></div>${addSale}</div>` : `<div class="shead"><b>Продажи</b><span class="hint">${esc(s.period)}</span><span class="cspacer"></span>${addSale}</div>`}
      <div class="scroll pad sales">
        <div class="tiles kpi">${tiles}</div>
        ${S.found.length ? `<div class="panel found">
          <div class="ph"><span>Найденные продажи</span><span class="hint num">${S.found.length}</span></div>
          ${S.found.map((f) => `<div class="frow" style="--c:${acc(f.acc).color}">
            <div class="fmain"><b>${esc(f.ad)}</b><span class="num up">${eur(f.price)}</span></div>
            <div class="fwhy">${esc(f.buyer)} · ${esc(acc(f.acc).name)} — ${esc(f.why)}</div>
            <div class="fact"><button class="btn primary" data-found="ok:${f.id}">${ICON.sold}<span>Подтвердить</span></button><button class="btn" data-found="no:${f.id}">${ICON.skip}<span>Отклонить</span></button></div>
          </div>`).join('')}
        </div>` : ''}
        <div class="panel chart">
          <div class="ph"><span>По дням</span><span class="hint num">макс. ${eur(max)}</span></div>
          <svg class="bars" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Выручка по дням">${bars}</svg>
          <div class="axis num"><span>1</span><span>5</span><span>10</span><span>15</span><span>20</span></div>
        </div>
        <div class="panel">
          <div class="ph"><span>Доля аккаунтов</span><span class="hint num">${eur(tot)}</span></div>
          <div class="stack">${stack}</div>
          <ul class="alist">${legend}</ul>
        </div>
        <div class="panel">
          <div class="ph"><span>Последние продажи</span></div>
          <table class="tbl"><tbody>${s.recent.map((r) => `<tr><td>${accDot(r.acc)}</td><td class="ta">${esc(r.ad)}<small>${esc(acc(r.acc).name)} · ${esc(r.date)}</small></td><td class="num tr up">${eur(r.price)}</td></tr>`).join('')}</tbody></table>
        </div>
      </div>`;
  }

  // ---------- Таб-бар ----------
  function tabItems() {
    const scr = S.screen === 'thread' ? 'inbox' : S.screen;
    return [
      { k: 'inbox', label: 'Сделки', icon: ICON.inbox, on: scr === 'inbox', badge: waiting().length },
      { k: 'client', label: 'Клиент', icon: ICON.user, on: scr === 'client' },
      { k: 'sales', label: 'Продажи', icon: ICON.chart, on: scr === 'sales' },
    ];
  }
  function renderTabbar() {
    $('#tabbar').innerHTML = tabItems().map((x) => `<button class="tab ${x.on ? 'on' : ''}" data-go="${x.k}" aria-current="${x.on ? 'page' : 'false'}">${x.icon}<span>${x.label}</span>${x.badge ? `<em class="num">${x.badge}</em>` : ''}</button>`).join('');
  }

  // ---------- Пейджер (Flip Flex Window) ----------
  function renderPager() {
    const el = $('#pager');
    if (S.screen === 'sales') {
      el.innerHTML = `<div class="pg-top"><span class="num">${esc(M.sales.period)}</span></div>
        <div class="pg-kpi"><span>выручка</span><b class="num up">${eur(M.sales.total)}</b>${spark(M.sales.byDay, 200, 34)}</div>
        <div class="pg-row num"><span>${M.sales.count} сделок</span><span>торг −${String(M.sales.avgDiscount).replace('.', ',')} %</span></div>
        <div class="pg-act"><button class="btn big" data-go="inbox">${ICON.back}<span>Назад</span></button></div>`;
      return;
    }
    if (S.screen === 'client') {
      const c = M.client;
      el.innerHTML = `<div class="pg-top"><b>${esc(c.name)}</b>${langBadge(c.lang)}</div>
        <p class="pg-msg">${esc(c.note)}</p>
        <div class="pg-row num"><span>куплено ${c.stats.bought} · ${eur(c.stats.spent)}</span><span>торг −10 %</span></div>
        <div class="pg-act"><button class="btn big" data-back>${ICON.back}<span>Назад</span></button></div>`;
      return;
    }
    const q = waiting();
    if (!q.length) {
      el.innerHTML = `<div class="pg-empty"><b>Очередь пуста</b><span>Все ответы отправлены.</span></div><div class="pg-act"><button class="btn big" data-go="sales">${ICON.chart}<span>Продажи</span></button></div>`;
      return;
    }
    if (S.pagerIdx >= q.length) S.pagerIdx = 0;
    const t = q[S.pagerIdx];
    const d = draftOf(t);
    const ok = langState(t, d).ok;
    el.innerHTML = `
      <div class="pg-top" style="--c:${acc(t.acc).color}"><span class="pg-n num">${S.pagerIdx + 1}/${q.length}</span><i class="accbar"></i><b>${esc(t.buyer)}</b>${langBadge(t.lang)}<span class="timer num ${waitCls(t)}">${esc(t.ago)}</span></div>
      <p class="pg-msg" data-swipe>${esc(t.last_ru)}</p>
      ${t.draft ? `<p class="pg-draft"><span class="mk">RU</span>${esc(d.ru)}</p>` : ''}
      <div class="pg-row num"><span>клиент <em class="o">${eur(t.offer)}</em></span><span>ответ <b>${eur(d.price)}</b></span></div>
      <div class="pg-act">
        <button class="btn primary big" data-act="pager-send" ${ok && t.draft ? '' : 'disabled'}>${ICON.send}<span>${t.draft ? 'Отправить' : 'Нет черновика'}</span>${t.draft ? `<span class="sbadge">${LANGS[t.lang]} ✓</span>` : ''}</button>
        <div class="pg-2"><button class="btn" data-back>${ICON.back}<span>Назад</span></button><button class="btn" data-act="pager-next" aria-label="Следующая сделка"><span>Далее</span>${ICON.next}</button></div>
      </div>`;
  }

  // ---------- Лист снизу ----------
  function openSheet(kind) {
    S.sheet = kind;
    const t = thr(S.sel);
    const sh = $('#sheet'), bd = $('#sheet-bd');
    if (kind === 'manual') {
      sh.innerHTML = `<div class="sh-grip" data-grip></div>
        <div class="sh-head"><b class="sh-title">Продажа вручную</b><button class="btn icon" data-close aria-label="Закрыть">${ICON.close}</button></div>
        <div class="sh-body"><div class="panel form">
          <label>Объявление<select id="ms-ad">${M.threads.map((x) => `<option value="${x.id}">${esc(x.ad)}</option>`).join('')}</select></label>
          <div class="frm2"><label>Цена, €<input id="ms-price" type="number" inputmode="numeric" value="${M.threads[0].floor}"></label>
          <label>Аккаунт<select id="ms-acc">${M.accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join('')}</select></label></div>
          <button class="btn primary big" data-act="manual-save">${ICON.sold}<span>Записать продажу</span></button>
        </div></div>`;
      sh.hidden = false; bd.hidden = false;
      requestAnimationFrame(() => { sh.classList.add('open'); bd.classList.add('open'); });
      return;
    }
    sh.innerHTML = `<div class="sh-grip" data-grip></div>
      <div class="sh-head"><b>${kind === 'deal' ? 'Сделка' : 'Клиент'}</b><span class="sh-tabs"><button class="${kind === 'deal' ? 'on' : ''}" data-sheet="deal">Сделка</button><button class="${kind === 'client' ? 'on' : ''}" data-sheet="client">Клиент</button></span><button class="btn icon" data-close aria-label="Закрыть">${ICON.close}</button></div>
      <div class="sh-body">${kind === 'deal' ? dealPanel(t) : clientPanel(true)}</div>`;
    sh.hidden = false; bd.hidden = false;
    requestAnimationFrame(() => { sh.classList.add('open'); bd.classList.add('open'); });
  }
  function closeSheet() {
    const sh = $('#sheet'), bd = $('#sheet-bd');
    S.sheet = null;
    sh.classList.remove('open'); bd.classList.remove('open');
    sh.style.transform = '';
    setTimeout(() => { if (!S.sheet) { sh.hidden = true; bd.hidden = true; } }, REDUCED ? 0 : 260);
  }

  // ---------- Счётчики ----------
  function animateCounters(root) {
    if (SHOT || REDUCED) return;
    root.querySelectorAll('[data-count]').forEach((el) => {
      const to = +el.dataset.count, isE = el.dataset.eur === '1';
      if (!to) return;
      const t0 = performance.now(), dur = 520;
      const step = (now) => {
        const k = Math.min(1, (now - t0) / dur), v = to * (1 - Math.pow(1 - k, 3));
        el.textContent = isE ? eur(v) : fmt(v);
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }

  // ---------- Рендер всего ----------
  function computeMode() {
    const w = innerWidth, h = innerHeight;
    return (w <= 360 && h <= 460) ? 'pager' : w >= 760 ? 'wide' : 'narrow';
  }
  function render(opts = {}) {
    S.mode = computeMode();
    const app = $('#app');
    app.className = `app mode-${S.mode} scr-${S.screen}`;
    if (S.mode === 'pager') { renderPager(); return; }
    renderPulse();
    renderQueue();
    if (S.screen === 'sales') renderSales();
    else if (S.screen === 'client') renderClient();
    else renderThread();
    if (S.mode === 'wide' && S.screen !== 'sales') renderSide(); else $('#col-side').innerHTML = '';
    renderTabbar();
    if (opts.count !== false) animateCounters(app);
  }

  function go(screen, push = true) {
    if (screen === S.screen) { render({ count: false }); return; }
    if (screen === 'client' || screen === 'thread') S.from = S.screen === 'client' ? S.from : S.screen;
    const swap = () => { S.screen = screen; S.allowLang = false; closeSheetQuick(); render(); };
    if (push) history.pushState({ screen }, '', '?' + new URLSearchParams({ ...Object.fromEntries(qs), screen }).toString());
    if (document.startViewTransition && !REDUCED && !SHOT) document.startViewTransition(swap);
    else { swap(); if (!REDUCED) { const m = $('#col-main'); m.classList.remove('enter'); void m.offsetWidth; m.classList.add('enter'); } }
  }
  function closeSheetQuick() { if (S.sheet) { S.sheet = null; $('#sheet').hidden = true; $('#sheet-bd').hidden = true; $('#sheet').classList.remove('open'); $('#sheet-bd').classList.remove('open'); } }
  function back() {
    if (S.mode === 'pager' && S.screen !== 'client') { S.pagerIdx = Math.max(0, S.pagerIdx - 1); renderPager(); return; }
    if (S.screen === 'client') go(S.from === 'thread' ? 'thread' : 'inbox');
    else go('inbox');
  }

  function toast(html) {
    const el = $('#toast');
    el.innerHTML = html; el.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove('show'), 2600);
  }

  function send(t) {
    const d = draftOf(t);
    S.sent.add(t.id);
    toast(`<b>Отправлено</b> ${esc(t.buyer)} · ${LANGS[t.lang]}${d.price != null ? ' · ' + eur(d.price) : ''}`);
    S.allowLang = false;
    if (S.mode === 'wide') {
      render({ count: false });
    } else if (S.mode === 'pager') {
      renderPager();
    } else {
      render({ count: false });
    }
  }

  // ---------- События ----------
  document.addEventListener('click', (ev) => {
    const b = ev.target.closest('button, [data-open]');
    if (!b) { if (ev.target.id === 'sheet-bd') closeSheet(); return; }
    const ds = b.dataset;
    if (ds.open) { S.sel = ds.open; S.allowLang = false; S.ruOpen = false; if (S.screen === 'thread') render({ count: false }); else go('thread'); return; }
    if (ds.go) { go(ds.go); return; }
    if ('back' in ds) { back(); return; }
    if (ds.acc) {
      if (ds.acc === 'all') S.accs.clear();
      else { const id = +ds.acc; S.accs.has(id) ? S.accs.delete(id) : S.accs.add(id); }
      renderQueue(); return;
    }
    if (ds.filter) { S.filter = ds.filter; renderQueue(); return; }
    if (ds.sheet) { if (S.mode === 'wide' && ds.sheet !== 'manual') return; openSheet(ds.sheet); return; }
    if ('selmode' in ds) { S.selMode = !S.selMode; S.picked.clear(); renderQueue(); return; }
    if (ds.pick) { S.picked.has(ds.pick) ? S.picked.delete(ds.pick) : S.picked.add(ds.pick); renderQueue(); return; }
    if (ds.bulk) {
      const ids = [...S.picked], n = ids.length;
      ids.forEach((id) => { if (ds.bulk === 'pin') S.pinned.add(id); else if (ds.bulk === 'wait') S.waitT[id] = 'до завтра 10:00'; else S.skipped.add(id); });
      toast(`${{ pin: 'Закреплено', wait: 'Ждут до завтра', skip: 'Пропущено' }[ds.bulk]}: <b class="num">${n}</b>`);
      S.selMode = false; S.picked.clear(); render({ count: false }); return;
    }
    if (ds.tact) {
      const t = thr(S.sel), k = ds.tact;
      if (k === 'pin') { S.pinned.has(t.id) ? S.pinned.delete(t.id) : S.pinned.add(t.id); toast(S.pinned.has(t.id) ? 'Закреплён сверху очереди' : 'Откреплён'); }
      if (k === 'wait') { if (S.waitT[t.id]) { delete S.waitT[t.id]; toast('Больше не ждём'); } else { S.waitT[t.id] = 'до завтра 10:00'; toast('Жду до завтра 10:00 — потом вернётся в очередь'); } }
      if (k === 'sold') { S.sold.add(t.id); const p = draftOf(t).price || t.offer || t.price; M.sales.recent.unshift({ ad: t.ad, price: p, acc: t.acc, date: 'сегодня' }); M.sales.total += p; M.sales.count += 1; toast(`<b>Продано</b> ${esc(t.ad)} · ${eur(p)}`); }
      if (k === 'skip') { S.skipped.add(t.id); toast('Тред пропущен'); }
      if (S.sheet === 'deal') openSheet('deal');
      render({ count: false }); return;
    }
    if ('addtag' in ds) { S.tagInput = true; refreshDeal(); const i = $('#taginput'); i && i.focus(); return; }
    if (ds.untag) { const t = thr(S.sel); S.tags[t.id] = tagsOf(t).filter((x) => x !== ds.untag); refreshDeal(); return; }
    if ('lessons' in ds) { S.lessonsOpen = !S.lessonsOpen; renderThread(); return; }
    if (ds.found) {
      const [k, id] = ds.found.split(':'); const f = S.found.find((x) => x.id === id);
      S.found = S.found.filter((x) => x.id !== id);
      if (k === 'ok') { M.sales.recent.unshift({ ad: f.ad, price: f.price, acc: f.acc, date: 'сегодня' }); M.sales.total += f.price; M.sales.count += 1; toast(`<b>Подтверждено</b> ${esc(f.ad)} · ${eur(f.price)}`); }
      else toast('Отклонено — не продажа');
      render({ count: false }); return;
    }
    if (ds.act === 'manual-save') {
      const t = thr($('#ms-ad').value), p = +$('#ms-price').value || 0, a = +$('#ms-acc').value;
      M.sales.recent.unshift({ ad: t.ad, price: p, acc: a, date: 'сегодня' }); M.sales.total += p; M.sales.count += 1;
      closeSheet(); toast(`<b>Записано</b> ${esc(t.ad)} · ${eur(p)}`); render({ count: false }); return;
    }
    if ('close' in ds) { closeSheet(); return; }
    if (ds.tone) {
      const t = thr(S.sel);
      S.drafts[t.id] = Object.assign({ tone: ds.tone, edited: false }, toneDraft(t, ds.tone));
      S.allowLang = false;
      renderThread(); if (S.mode === 'wide') renderSide();
      const ta = $('#draft'); if (ta && !REDUCED) { ta.classList.add('flash'); }
      return;
    }
    if ('ru' in ds) { S.ruOpen = !S.ruOpen; b.classList.toggle('open', S.ruOpen); b.setAttribute('aria-expanded', S.ruOpen); return; }
    if (ds.act === 'send') { const t = thr(S.sel); if (!b.disabled) send(t); return; }
    if (ds.act === 'regen') { const t = thr(S.sel); const d = draftOf(t); S.drafts[t.id] = Object.assign({ tone: d.tone, edited: false }, toneDraft(t, d.tone)); renderThread(); toast('ИИ переписал идею: тон сохранён'); return; }
    if (ds.act === 'edit') {
      // Имитация /edit-ru: русский текст оператора переводится на язык клиента
      const t = thr(S.sel); const d = draftOf(t);
      if (hasCyr(d.text)) { Object.assign(d, toneDraft(t, d.tone), { edited: false }); S.allowLang = false; renderThread(); toast(`Переведено на ${LANG_NAME[t.lang]}`); }
      else { const ta = $('#draft'); ta && ta.focus(); }
      return;
    }
    if (ds.act === 'pager-send') { const q = waiting(); const t = q[S.pagerIdx]; if (t) send(t); return; }
    if (ds.act === 'pager-next') { S.pagerIdx = (S.pagerIdx + 1) % Math.max(1, waiting().length); renderPager(); return; }
  });

  document.addEventListener('input', (ev) => {
    if (ev.target.id === 'draft') {
      const t = thr(S.sel); const d = draftOf(t);
      d.text = ev.target.value; d.edited = true; fitDraft();
      // Перерисовываем только бейджи/кнопку, не трогая textarea (фокус и каретка остаются)
      const L = langState(t, d);
      const box = ev.target.closest('.draft');
      box.classList.toggle('warn', !L.ok);
      box.querySelector('.lbadge').outerHTML = L.ok ? `<span class="lbadge ok">${LANGS[t.lang]} ✓</span>` : `<span class="lbadge bad">${L.empty ? 'пусто' : 'RU ≠ ' + LANGS[t.lang]}</span>`;
      const hasAllow = !!$('#allow');
      if (L.cyr !== hasAllow) { const pos = ev.target.selectionStart; renderThread(); const ta = $('#draft'); ta.focus(); ta.setSelectionRange(pos, pos); return; }
      const sb = $('.send'); sb.disabled = !(L.ok || (L.cyr && S.allowLang));
      sb.querySelector('.sbadge').textContent = L.ok ? LANGS[t.lang] + ' ✓' : '…';
      $('.mirror .mt').textContent = 'Зеркало обновится после перевода…';
    }
  });
  document.addEventListener('change', (ev) => {
    if (ev.target.id === 'allow') {
      S.allowLang = ev.target.checked;
      const t = thr(S.sel); const L = langState(t, draftOf(t));
      const sb = $('.send'); sb.disabled = !(L.ok || (L.cyr && S.allowLang));
      sb.querySelector('.sbadge').textContent = S.allowLang ? 'RU!' : '…';
    }
  });
  function refreshDeal() {
    if (S.mode === 'wide') renderSide(); else if (S.sheet === 'deal') openSheet('deal');
  }
  document.addEventListener('keydown', (e) => {
    if (e.target.id === 'taginput' && e.key === 'Enter') {
      const v = e.target.value.trim(); const t = thr(S.sel);
      if (v && !tagsOf(t).includes(v)) tagsOf(t).push(v);
      S.tagInput = false; refreshDeal();
    }
  });
  document.addEventListener('animationend', (ev) => ev.target.classList && ev.target.classList.remove('flash'));

  // Жесты: свайп вправо = «Назад» (узкий режим), свайп в пейджере = соседняя сделка, лист — тянуть вниз
  let sx = 0, sy = 0, st = 0, sheetDrag = false;
  document.addEventListener('touchstart', (e) => {
    const p = e.touches[0]; sx = p.clientX; sy = p.clientY; st = Date.now();
    sheetDrag = !!e.target.closest('[data-grip], .sh-head');
  }, { passive: true });
  document.addEventListener('touchmove', (e) => {
    if (!sheetDrag) return;
    const dy = e.touches[0].clientY - sy;
    if (dy > 0) $('#sheet').style.transform = `translateY(${dy}px)`;
  }, { passive: true });
  document.addEventListener('touchend', (e) => {
    const p = e.changedTouches[0]; const dx = p.clientX - sx, dy = p.clientY - sy, dt = Date.now() - st;
    if (sheetDrag) { sheetDrag = false; if (dy > 80) closeSheet(); else $('#sheet').style.transform = ''; return; }
    if (dt > 600 || Math.abs(dx) < 70 || Math.abs(dy) > Math.abs(dx) * 0.6) return;
    if (e.target.closest('.tones, .chips, textarea')) return;
    if (S.mode === 'pager' && S.screen !== 'client' && S.screen !== 'sales') {
      const n = Math.max(1, waiting().length);
      S.pagerIdx = (S.pagerIdx + (dx < 0 ? 1 : n - 1)) % n; renderPager(); return;
    }
    if (S.mode === 'narrow' && dx > 0 && sx < 60 && (S.screen === 'thread' || S.screen === 'client')) back();
  }, { passive: true });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { if (S.sheet) closeSheet(); else if (S.screen === 'thread' || S.screen === 'client') back(); }
  });

  window.addEventListener('popstate', (e) => {
    const s = (e.state && e.state.screen) || new URLSearchParams(location.search).get('screen') || 'inbox';
    S.screen = SCREENS.includes(s) ? s : 'inbox'; closeSheetQuick(); render({ count: false });
  });
  let lastMode = null;
  window.addEventListener('resize', () => { const m = computeMode(); if (m !== lastMode) { lastMode = m; closeSheetQuick(); render({ count: false }); } });

  // Telegram: кнопка «Назад» в шапке клиента, развернуть на весь экран
  if (tgWA) {
    try { tgWA.ready(); tgWA.expand(); } catch (e) { /* вне Telegram */ }
  }

  lastMode = computeMode();
  render();
})();
