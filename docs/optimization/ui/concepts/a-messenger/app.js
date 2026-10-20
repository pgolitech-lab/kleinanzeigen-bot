/* Концепция A «Тёмный премиум-мессенджер».
   Vanilla JS, без сборки. Данные — window.MOCK (../_shared/mock.js) + локальные добавки ниже. */
(() => {
  'use strict';

  const M = window.MOCK;
  const qs = new URLSearchParams(location.search);
  const SHOT = qs.has('shot');
  const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const TG = window.Telegram && window.Telegram.WebApp;
  const root = document.getElementById('app');
  const layer = document.getElementById('layer');

  /* ---------- тема: пресеты так, как их выставил бы Telegram ---------- */
  const PRESETS = {
    dark: {
      'bg-color': '#0a0e15', 'secondary-bg-color': '#1a2230', 'section-bg-color': '#141b27',
      'header-bg-color': '#0a0e15', 'bottom-bar-bg-color': '#0d121b',
      'text-color': '#eef2f8', 'hint-color': '#8590a5', 'subtitle-text-color': '#a3adbf',
      'link-color': '#7aa7ff', 'accent-text-color': '#7aa7ff',
      'button-color': '#4f7cff', 'button-text-color': '#ffffff', 'destructive-text-color': '#ff5d6c',
    },
    light: {
      'bg-color': '#f4f1ea', 'secondary-bg-color': '#e9e4d9', 'section-bg-color': '#fffdf8',
      'header-bg-color': '#f4f1ea', 'bottom-bar-bg-color': '#faf8f3',
      'text-color': '#1a1c22', 'hint-color': '#80838c', 'subtitle-text-color': '#5d6170',
      'link-color': '#2f5bea', 'accent-text-color': '#2f5bea',
      'button-color': '#2f5bea', 'button-text-color': '#ffffff', 'destructive-text-color': '#d93a49',
    },
  };
  function applyTheme() {
    const forced = qs.get('theme');
    const hasTg = TG && TG.themeParams && Object.keys(TG.themeParams).length > 0;
    let t = forced || (hasTg ? TG.colorScheme : (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
    if (t !== 'light') t = 'dark';
    document.documentElement.dataset.theme = t;
    // В реальном Telegram themeParams уже лежат в --tg-theme-*; ?theme= принудительно подставляет пресет
    if (forced || !hasTg) {
      for (const [k, v] of Object.entries(PRESETS[t])) document.documentElement.style.setProperty('--tg-theme-' + k, v);
    }
  }
  applyTheme();
  if (SHOT) document.documentElement.classList.add('shot');

  /* ---------- данные ---------- */
  const ACC = Object.fromEntries(M.accounts.map(a => [a.id, a]));
  const LANG = {
    de: { code: 'DE', adv: 'по-немецки', name: 'немецкий' },
    pl: { code: 'PL', adv: 'по-польски', name: 'польский' },
    nl: { code: 'NL', adv: 'по-нидерландски', name: 'нидерландский' },
    tr: { code: 'TR', adv: 'по-турецки', name: 'турецкий' },
  };
  // Локальные добавки к моку: наше последнее предложение, черновики, сводки
  const EXTRA = {
    t1: { ourLast: 1100 },
    t2: {
      ourLast: null, summary: 'Спрашивает про доставку в Польшу, по цене пока не торгуется.',
      draft: { lang: 'pl', suggestedPrice: 380,
        text: 'Dzień dobry, tak, wysyłka do Polski jest możliwa – koszt 45 €. Konsola kosztuje 380 €. Pozdrawiam',
        ru: 'Добрый день, да, доставка в Польшу возможна — 45 €. Консоль стоит 380 €. С уважением' },
    },
    t3: {
      ourLast: 620, summary: 'Готов купить сегодня за 600 €, это выше минимума.',
      gen: { lang: 'de', suggestedPrice: 600,
        text: 'Hallo Thomas, 600 € ist in Ordnung, wenn Sie heute abholen. Die Adresse schicke ich gleich. Viele Grüße',
        ru: 'Здравствуйте, Томас, 600 € подходит, если заберёте сегодня. Адрес пришлю сразу. С уважением' },
    },
    t4: { ourLast: 210, summary: 'Ждём ответа до завтра, торга не было.' },
    t5: { ourLast: 270, summary: 'Договорились на 270 €, согласовать время.' },
    t6: {
      ourLast: 760, summary: 'Давит низкой ценой — 500 € намного ниже минимума.',
      draft: { lang: 'tr', suggestedPrice: 720,
        text: 'Merhaba Mehmet, 500 € maalesef çok düşük. Son fiyatım 720 €. Saygılarımla',
        ru: 'Здравствуйте, Мехмет, 500 € — к сожалению, слишком мало. Моя последняя цена — 720 €. С уважением' },
    },
    t7: { ourLast: null, summary: 'Спросил про аварии, ждём реакцию на ответ.' },
  };
  const OUT_GENERIC = {
    de: { de: 'Gerne, melden Sie sich einfach. Viele Grüße', ru: 'Хорошо, пишите в любое время. С уважением' },
    nl: { de: 'Prima, laat het me weten. Groeten', ru: 'Хорошо, дайте знать. С уважением' },
    pl: { de: 'Dobrze, proszę dać znać. Pozdrawiam', ru: 'Хорошо, дайте знать. С уважением' },
    tr: { de: 'Tamam, haber bekliyorum. Saygılarımla', ru: 'Хорошо, жду ответа. С уважением' },
  };
  const REWRITE = {
    de: { text: 'Hallo Markus, die Sitzbank ist noch da. Mein bestes Angebot: 1.000 € bei Abholung am Samstag. Viele Grüße',
      ru: 'Здравствуйте, Маркус, сиденье ещё есть. Лучшее, что могу предложить: 1 000 € при самовывозе в субботу. С уважением', price: 1000 },
  };

  // Новое от владельца: досье, «возможно тот же покупатель», уроки, найденные продажи, пометки писем
  const CLIENT_X = {
    tags: ['наличные', 'самовывоз', 'торгуется ~10 %'],
    dossier: {
      summary: 'Пишет утром и вечером, отвечает за ~12 мин. Интересуется салоном T6 Multivan. Торгуется на 10–15 %, в итоге платит близко к цене.',
      items: [
        { when: 'сегодня', what: 'Sitzbank 3er T6', price: 'предлагает 900 при цене 1 200' },
        { when: '14.07', what: 'Tischplatte Multivan', price: 'просил 420 → купил за 480' },
        { when: '02.05', what: 'Kopfstützen T6', price: 'предложил 60 при 90 → не договорились' },
      ],
    },
    similar: { name: 'M. Weber', acc: 3, why: 'та же подпись «Gruß M.», тот же T6 Multivan, Kassel', ad: 'Kopfstützen T6', when: 'в мае' },
  };
  const LESSONS = [
    { bad: 'Der Preis ist fest.', good: 'Beim Preis kann ich Ihnen etwas entgegenkommen, aber nicht unter 1.000 €.', note: 'не отказывать в торге сразу — называть свою цену' },
    { bad: 'Mit freundlichen Grüßen', good: 'Viele Grüße', note: 'короткая дружелюбная подпись' },
    { bad: 'Versand ist nicht möglich.', good: 'Versand ist per Spedition möglich, Kosten ca. 45 €.', note: 'доставку предлагать, а не отказывать' },
  ];
  const FOUND_SALES = [
    { id: 'f1', ad: 'Sitzbank Vito 2+1', acc: 5, price: 720, why: 'объявление снято, клиент написал «abgeholt, danke»' },
    { id: 'f2', ad: 'Isofix Sitz T6.1', acc: 2, price: 340, why: 'объявление помечено «Reserviert»' },
  ];
  // пометки событий треда t1: пришло через сайт/приложение KZ; пришло с опозданием
  const EVENT_MARKS = { 't1:2': { app: true }, 't1:4': { app: true }, 't1:0': { late: '6 ч' } };
  const ROW_MARKS = { t1: { similar: true }, t6: { late: true }, t3: { app: true } };

  const threads = M.threads.map(t => ({ ...t, ourLast: (EXTRA[t.id] || {}).ourLast ?? null }));
  const byId = id => threads.find(t => t.id === id);
  const cache = {};
  function td(id) {
    if (cache[id]) return cache[id];
    const t = byId(id), x = EXTRA[id] || {};
    let events, draft, deal;
    if (id === M.thread.id) {
      events = M.thread.events.map(e => ({ ...e }));
      draft = { ...M.thread.draft };
      deal = { ...M.thread.deal };
    } else {
      events = [{ who: 'in', time: t.time, de: t.last_de, ru: t.last_ru }];
      if (t.kind === 'out') events.push({ who: 'out', time: t.time, ...(OUT_GENERIC[t.lang] || OUT_GENERIC.de) });
      draft = x.draft ? { ...x.draft } : null;
      deal = { price: t.price, floor: t.floor, offer: t.offer, ourLast: x.ourLast ?? null, summary: x.summary || '' };
    }
    if (draft) draft.orig = { text: draft.text, ru: draft.ru };
    return (cache[id] = { events, draft, deal });
  }

  /* ---------- состояние ---------- */
  const SCREENS = ['inbox', 'thread', 'client', 'sales'];
  const S = {
    screen: SCREENS.includes(qs.get('screen')) ? qs.get('screen') : 'inbox',
    threadId: 't1',
    back: 'inbox',
    filter: 'all',
    showTr: true,
    flipped: new Set(),
    draftMin: false,
    dealOpen: false,
    override: false,
    busy: false,
    vt: null,
    swipeOpen: SHOT && innerWidth < 760 ? 't2' : null,
    sold: new Set(),
    later: new Set(),
    pinned: new Set(['t1']),
    select: qs.has('select'),
    picked: new Set(qs.has('select') ? ['t2', 't6'] : []),
    found: FOUND_SALES.map(f => ({ ...f })),
    tags: CLIENT_X.tags.slice(),
  };
  if (S.screen === 'client') S.back = 'thread';

  /* ---------- утилиты ---------- */
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = n => n == null ? '—' : String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  const eur = n => n == null ? '—' : num(n) + ' €';
  const initials = name => name.replace(/[^\p{L}\s]/gu, ' ').trim().split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase();
  const hue = s => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  const CYR = /[Ѐ-ӿ]/;
  const langCheck = (text, lang) => CYR.test(text) ? { ok: false, code: 'RU' } : { ok: true, code: LANG[lang].code };
  function layout() {
    const w = innerWidth, h = innerHeight;
    if (w <= 440 && h <= 480) return 'cover';
    if (w >= 900) return 'trio';
    if (w >= 760) return 'duo';
    return 'phone';
  }

  /* ---------- иконки (inline SVG) ---------- */
  const P = {
    back: '<path d="M15 5l-7 7 7 7"/>',
    send: '<path d="M4.5 12.5 19 5l-4 15-3.5-6.2z"/><path d="M11.5 13.8 19 5"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    more: '<circle cx="5.5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="18.5" cy="12" r="1.3"/>',
    spark: '<path d="M12 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    sold: '<path d="M3.5 12.5V5.5a2 2 0 0 1 2-2h7l8 8-9 9z"/><circle cx="8.5" cy="8.5" r="1.5"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
    chats: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 3.5V17A2.5 2.5 0 0 1 4 14.5z"/>',
    user: '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c1.3-3.6 4.2-5.5 7.5-5.5s6.2 1.9 7.5 5.5"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    tr: '<path d="M3.5 5.5h9M8 3.5v2M10.5 5.5c-.8 3.6-3.2 6.4-6.5 8M6 9c1.2 2.2 3 3.8 5 4.6"/><path d="M12.5 20.5l4-10 4 10M14 17h5"/>',
    down: '<path d="M6 9.5l6 6 6-6"/>',
    up: '<path d="M6 14.5l6-6 6 6"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    mail: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M4 7l8 6 8-6"/>',
    bot: '<rect x="5" y="8" width="14" height="10" rx="3"/><path d="M12 4.5V8M9.5 13h0M14.5 13h0"/>',
    refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    pen: '<path d="M4 20h16"/><path d="M6 16l9.5-9.5 2.5 2.5L8.5 18.5H6z"/>',
    note: '<path d="M6 3.5h9l3.5 3.5v13.5H6z"/><path d="M9 11h6M9 15h6"/>',
    copy: '<rect x="8" y="8" width="11.5" height="11.5" rx="2.5"/><path d="M5 15.5V6.5A2 2 0 0 1 7 4.5h8.5"/>',
    pin: '<path d="M9 3.5h6l-1 6 3.5 3.5h-11L10 9.5z"/><path d="M12 13v7.5"/>',
    phone: '<rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 17.5h2"/>',
    late: '<circle cx="12" cy="13" r="7.5"/><path d="M12 9v4.2l2.5 1.5M9 2.5h6"/>',
    warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.5v.01"/>',
    book: '<path d="M4.5 5.5c2.5-1.3 5-1.3 7.5 0v14c-2.5-1.3-5-1.3-7.5 0zM12 5.5c2.5-1.3 5-1.3 7.5 0v14c-2.5-1.3-5-1.3-7.5 0z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    sel: '<rect x="4" y="4" width="16" height="16" rx="5"/><path d="M8.5 12.2l2.4 2.4 4.6-4.8"/>',
    shield: '<path d="M12 3.5l7 3v5c0 4.5-3 7.7-7 9-4-1.3-7-4.5-7-9v-5z"/><path d="M9 12l2.2 2.2L15.5 10"/>',
  };
  const ic = (n, cls = '') => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[n]}</svg>`;

  /* ---------- атомы ---------- */
  const flag = (lang, cls = '') => `<span class="flag f-${lang} ${cls}" title="${esc(LANG[lang].name)}"></span>`;
  function avatar(t, size = '', vt = false) {
    const h = hue(t.buyer), acc = ACC[t.acc];
    return `<span class="av ${size}" style="--h:${h};--acc:${acc.color};${vt ? 'view-transition-name:vt-av' : ''}">
      <span class="av-in">${esc(initials(t.buyer))}</span>${flag(t.lang, 'av-flag')}</span>`;
  }
  function accChip(id) {
    const a = ACC[id];
    return `<span class="acc" style="--acc:${a.color}"><i></i>${esc(a.name)}</span>`;
  }

  // Шкала торга: красная зона ниже минимума, зелёная — минимум…цена, маркеры предложений
  function scale(d, size = 'sm') {
    const vals = [d.price, d.floor, d.offer, d.ourLast, d.sugg].filter(v => v != null);
    const lo = Math.min(...vals) * 0.9, hi = Math.max(...vals) * 1.03;
    const p = v => Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100));
    const pf = p(d.floor), pp = p(d.price);
    const offBad = d.offer != null && d.offer < d.floor;
    const mk = (v, cls, label, row) => v == null ? '' : `<span class="mk ${cls}" style="left:${p(v).toFixed(1)}%"></span>${size === 'lg'
      ? `<span class="mk-l ${row} ${cls}" style="--x:${p(v).toFixed(1)}">${num(v)}</span>` : ''}`;
    const aria = `Цена ${eur(d.price)}, минимум ${eur(d.floor)}` + (d.offer != null ? `, клиент ${eur(d.offer)}` : '') + (d.ourLast != null ? `, мы ${eur(d.ourLast)}` : '');
    return `<div class="scale ${size}" role="img" aria-label="${esc(aria)}">
      <div class="sc-track">
        <span class="sc-bad" style="width:${pf.toFixed(1)}%"></span>
        <span class="sc-good" style="left:${pf.toFixed(1)}%;width:${(pp - pf).toFixed(1)}%"></span>
        <span class="sc-tick t-floor" style="left:${pf.toFixed(1)}%"></span>
        <span class="sc-tick t-price" style="left:${pp.toFixed(1)}%"></span>
      </div>
      ${mk(d.sugg, 'm-sugg', 'ИИ', 'bot')}${mk(d.ourLast, 'm-ours', 'Мы', 'top')}${mk(d.offer, offBad ? 'm-offer bad' : 'm-offer', 'Клиент', 'top')}
      ${size === 'lg' ? `<span class="mk-l bot m-floor" style="--x:${pf.toFixed(1)}">${num(d.floor)}</span><span class="mk-l bot m-price" style="--x:${pp.toFixed(1)}">${num(d.price)}</span>` : ''}
    </div>`;
  }

  /* ---------- INBOX ---------- */
  function visibleThreads() {
    return threads.filter(t => !S.sold.has(t.id)).filter(t => {
      if (S.filter === 'all') return true;
      if (S.filter === 'wait') return t.kind === 'in';
      if (S.filter === 'draft') return t.draft && t.kind === 'in';
      return String(t.acc) === S.filter;
    });
  }
  function row(t, L) {
    const sel = L !== 'phone' && S.threadId === t.id && S.screen !== 'sales';
    const offBad = t.offer != null && t.offer < t.floor;
    const vt = S.vt === t.id && L === 'phone';
    const drafted = t.draft && t.kind === 'in';
    const acts = `<div class="row-acts" aria-hidden="${S.swipeOpen === t.id ? 'false' : 'true'}">
        ${drafted ? `<button class="ra ra-send" data-act="swipe-send" data-id="${t.id}">${ic('send')}<span>Отправить<br>черновик</span></button>` : ''}
        <button class="ra ra-later" data-act="swipe-later" data-id="${t.id}">${ic('clock')}<span>Жду</span></button>
        <button class="ra ra-sold" data-act="swipe-sold" data-id="${t.id}">${ic('sold')}<span>Продано</span></button>
      </div>`;
    return `<div class="rw ${S.swipeOpen === t.id ? 'open' : ''}" data-id="${t.id}" style="view-transition-name:r-${t.id}">
      ${acts}
      <div class="row ${sel ? 'sel' : ''} ${t.unread ? 'unread' : ''} ${S.later.has(t.id) ? 'later' : ''} ${S.select ? 'selecting' : ''} ${S.picked.has(t.id) ? 'picked' : ''}" data-act="${S.select ? 'pick' : 'open'}" data-id="${t.id}" role="button" tabindex="0"
           aria-label="${esc(t.buyer + ', ' + t.ad)}" ${S.select ? `aria-pressed="${S.picked.has(t.id)}"` : ''}>
        <span class="av-wrap">${avatar(t, '', vt)}${S.select ? `<span class="pick-box">${ic('check')}</span>` : ''}</span>
        <div class="row-main">
          <div class="r1"><b class="r-name" ${vt ? 'style="view-transition-name:vt-title"' : ''}>${esc(t.buyer)}</b>${rowMarks(t)}<span class="r-time">${esc(t.time)}</span></div>
          <div class="r2"><span class="r-acc" style="--acc:${ACC[t.acc].color}"></span><span class="r-ad">${esc(t.ad)}</span></div>
          <div class="r3"><span class="r-msg">${esc(t.last_ru)}</span>${drafted ? `<span class="pill-draft">${ic('spark')}готов</span>` : t.unread ? '<span class="dot-unread"></span>' : ''}</div>
          <div class="r4">${scale(t, 'xs')}<span class="r-price">${t.offer != null ? `<em class="${offBad ? 'bad' : 'good'}">${num(t.offer)}</em> / ` : ''}${eur(t.price)}</span></div>
        </div>
      </div>
    </div>`;
  }
  function rowMarks(t) {
    const m = ROW_MARKS[t.id] || {};
    return (S.pinned.has(t.id) ? `<span class="rm rm-pin" title="Закреплено">${ic('pin')}</span>` : '')
      + (m.similar ? `<span class="rm rm-warn" title="Возможно тот же покупатель">${ic('warn')}</span>` : '')
      + (m.late ? `<span class="rm rm-late" title="Запоздалое письмо">${ic('late')}</span>` : '')
      + (m.app ? `<span class="rm rm-app" title="Из приложения">${ic('phone')}</span>` : '');
  }
  const pinSort = list => list.slice().sort((a, b) => (S.pinned.has(b.id) ? 1 : 0) - (S.pinned.has(a.id) ? 1 : 0));
  function inboxList(L) {
    const vis = visibleThreads();
    const wait = pinSort(vis.filter(t => t.kind === 'in' && !S.later.has(t.id)));
    const later = pinSort(vis.filter(t => S.later.has(t.id)));
    const out = pinSort(vis.filter(t => t.kind === 'out' && !S.later.has(t.id)));
    const sec = (title, list, cls = '') => list.length ? `<div class="sec ${cls}"><span>${title}</span><i>${list.length}</i></div>${list.map(t => row(t, L)).join('')}` : '';
    const body = sec('Ждут ответа', wait, 'hot') + sec('Жду · отложено', later) + sec('Ждём клиента', out);
    return body || `<div class="empty">${ic('check')}<p>Здесь пусто — все ответы отправлены.</p></div>`;
  }
  function filters() {
    const all = threads.filter(t => !S.sold.has(t.id));
    const f = [
      ['all', 'Все', all.length],
      ['wait', 'Ждут', all.filter(t => t.kind === 'in').length],
      ['draft', 'Черновики', all.filter(t => t.kind === 'in' && t.draft).length],
      ...M.accounts.map(a => [String(a.id), a.name, null, a.color]),
    ];
    return `<div class="chips" role="tablist">${f.map(([k, l, n, c]) => `<button class="chip ${S.filter === k ? 'on' : ''}" data-act="filter" data-f="${k}" role="tab" aria-selected="${S.filter === k}" ${c ? `style="--acc:${c}"` : ''}>
      ${c ? '<i class="chip-dot"></i>' : ''}${esc(l)}${n != null ? `<b>${n}</b>` : ''}</button>`).join('')}</div>`;
  }
  function inboxScreen(L) {
    const waiting = threads.filter(t => t.kind === 'in' && !S.sold.has(t.id));
    const drafts = waiting.filter(t => t.draft).length;
    const head = S.select
      ? `<div class="top-row selbar">
          <button class="ibtn" data-act="sel-off" aria-label="Отменить выбор">${ic('x')}</button>
          <div class="title-block"><h1 class="sel-n">${S.picked.size}</h1><p class="sub">выбрано</p></div>
          <button class="ibtn tonal" data-act="bulk" data-op="pin" aria-label="Закрепить">${ic('pin')}</button>
          <button class="ibtn tonal" data-act="bulk" data-op="wait" aria-label="Жду">${ic('clock')}</button>
          <button class="ibtn tonal" data-act="bulk" data-op="sold" aria-label="Продано">${ic('sold')}</button>
        </div>`
      : `<div class="top-row">
          <div class="title-block"><h1>Чаты</h1><p class="sub"><b class="hot-n">${waiting.length}</b> ждут · ${drafts} готовы</p></div>
          <button class="ibtn" data-act="sel-on" aria-label="Выбрать несколько">${ic('sel')}</button>
          <button class="ibtn" data-act="toast" data-msg="Поиск по клиентам и объявлениям" aria-label="Поиск">${ic('search')}</button>
        </div>`;
    return `<header class="top glass ${S.select ? 'selecting' : ''}">
        ${head}
        ${filters()}
      </header>
      <div class="list scroll" id="list">${inboxList(L)}<div class="list-pad"></div></div>`;
  }

  /* ---------- THREAD ---------- */
  function dayOf(time) {
    if (/^Вт/.test(time)) return 'Вторник';
    if (/^Ср/.test(time)) return 'Среда';
    if (/^Пн/.test(time)) return 'Понедельник';
    if (/вчера/i.test(time)) return 'Вчера';
    return 'Сегодня';
  }
  function bubbles(id) {
    const d = td(id), t = byId(id);
    let lastDay = '', html = '';
    d.events.forEach((e, i) => {
      const day = dayOf(e.time);
      if (day !== lastDay) { html += `<div class="day"><span>${day}</span></div>`; lastDay = day; }
      const key = id + ':' + i;
      if (i === d.events.length - 1 && e.who === 'in' && (ROW_MARKS[id] || {}).similar) html += similarCard();
      const flip = S.flipped.has(key);
      const orig = `<p class="b-orig" lang="${t.lang}">${esc(e.de)}</p>`;
      const ru = S.showTr || flip ? `<p class="b-ru" lang="ru"><span class="b-ru-l">RU</span>${esc(e.ru)}</p>` : '';
      const tm = e.time.split(' ').pop();
      html += `<div class="msg ${e.who} ${flip ? 'flip' : ''} ${e.fresh ? 'fresh' : ''}" ${e.fresh && !d.draft ? 'style="view-transition-name:vt-send"' : ''}>
        <div class="bubble" data-act="flip" data-key="${key}" role="button" tabindex="0" aria-label="Переключить язык">
          ${flip ? ru.replace('b-ru', 'b-ru main') + orig.replace('b-orig', 'b-orig small') : orig + ru}
          <span class="meta">${e.auto ? `<span class="auto">${ic('bot')}авто</span>` : ''}${(EVENT_MARKS[key] || {}).app ? `<span class="auto app">${ic('phone')}из приложения</span>` : ''}${(EVENT_MARKS[key] || {}).late ? `<span class="auto late">${ic('late')}запоздало · ${EVENT_MARKS[key].late}</span>` : ''}${esc(tm)}${e.who === 'out' ? ic('check', 'tick') : ''}</span>
        </div></div>`;
    });
    return html;
  }
  function similarCard() {
    const x = CLIENT_X.similar, a = ACC[x.acc];
    return `<div class="similar" role="note">
      <span class="sim-ic">${ic('warn')}</span>
      <div class="sim-m"><b>Возможно, тот же покупатель</b>
        <small>«${esc(x.name)}» писал на <span class="acc" style="--acc:${a.color}"><i></i>${esc(a.name)}</span> ${esc(x.when)}: ${esc(x.why)}</small></div>
      <button class="sim-btn" data-act="client">Досье</button>
    </div>`;
  }
  function dealNums(d, t) {
    const offBad = d.offer != null && d.offer < d.floor;
    return `<div class="nums">
      <div class="n n-price"><small>Цена</small><b>${num(d.price)}</b></div>
      <div class="n n-floor"><small>Минимум</small><b>${num(d.floor)}</b></div>
      <div class="n n-offer ${offBad ? 'bad' : d.offer != null ? 'good' : ''}"><small>Клиент</small><b>${num(d.offer)}</b></div>
      <div class="n n-ours"><small>Мы</small><b>${num(d.ourLast)}</b></div>
    </div>`;
  }
  function dealData(id) {
    const d = td(id).deal;
    return { ...d, sugg: td(id).draft ? td(id).draft.suggestedPrice : null };
  }
  function dealStrip(id) {
    const d = dealData(id), t = byId(id);
    const gap = d.offer != null ? d.offer - d.floor : null;
    return `<div class="deal glass ${S.dealOpen ? 'open' : ''}" data-act="deal" role="button" tabindex="0" aria-expanded="${S.dealOpen}">
      ${dealNums(d, t)}
      ${scale(d, 'sm')}
      <div class="deal-more"><div class="dm-in">
        <p>${esc(d.summary)}</p>
        <div class="deal-legend">
          ${gap != null ? `<span class="${gap < 0 ? 'bad' : 'good'}">${gap < 0 ? 'ниже минимума на ' + eur(-gap) : 'выше минимума на ' + eur(gap)}</span>` : '<span>клиент ещё не назвал цену</span>'}
          ${d.sugg ? `<span class="lg-sugg">ИИ предлагает ${eur(d.sugg)}</span>` : ''}
        </div>
      </div></div>
      <span class="deal-chev">${ic(S.dealOpen ? 'up' : 'down')}</span>
    </div>`;
  }
  function draftCard(id, L) {
    const d = td(id), t = byId(id), dr = d.draft;
    const chk = langCheck(dr.text, t.lang);
    const lang = LANG[t.lang];
    const canSend = chk.ok || S.override;
    if (S.draftMin) {
      return `<div class="draft min glass-strong" id="draft">
        <button class="dm-open" data-act="draft-max" aria-label="Развернуть черновик">${ic('spark')}<span class="dm-txt">${esc(dr.ru)}</span><span class="lang-badge ${chk.ok ? 'ok' : 'no'}">${chk.code} ${chk.ok ? '✓' : '✕'}</span></button>
        <button class="send-mini" data-act="send" ${canSend ? '' : 'disabled'} aria-label="Отправить ${lang.adv}">${ic('send')}</button>
      </div>`;
    }
    return `<div class="draft glass-strong ${S.busy ? 'busy' : ''}" id="draft">
      <div class="steps" aria-label="Этап 3 из 3: проверено"><i class="on"></i><i class="on"></i><i class="on"></i></div>
      <div class="dr-top">
        <span class="ai">${ic('spark')}<span class="ai-l">Черновик ИИ</span></span>
        <span class="lang-badge ${chk.ok ? 'ok' : 'no'}" title="${chk.ok ? 'Текст на языке клиента' : 'Текст не на языке клиента'}">${chk.code} ${chk.ok ? '✓' : '✕'}</span>
        <span class="dr-sp"></span>
        <button class="ibtn sm lessons" data-act="lessons" aria-label="Учтено уроков: ${LESSONS.length}">${ic('book')}<b>${LESSONS.length}</b></button>
        <button class="ibtn sm" data-act="edit" aria-label="Редактировать">${ic('edit')}</button>
        <button class="ibtn sm" data-act="more" aria-label="Другие варианты">${ic('more')}</button>
        <button class="ibtn sm" data-act="draft-min" aria-label="Свернуть черновик">${ic('down')}</button>
      </div>
      <div class="dr-text" lang="${t.lang}" style="view-transition-name:vt-send">${esc(dr.text)}</div>
      <div class="dr-ru"><span class="b-ru-l">RU</span>${dr.edited && !CYR.test(dr.text) ? '<i>зеркало обновится после проверки</i>' : esc(dr.ru)}</div>
      ${chk.ok ? '' : `<label class="override"><input type="checkbox" data-act="override" ${S.override ? 'checked' : ''}><span>Разрешаю отправить текст не на языке клиента</span></label>`}
      <div class="dr-acts">
        <button class="send-big" data-act="send" ${canSend ? '' : 'disabled'}>
          <span class="sb-txt"><b><span class="sbl">Отправить ${chk.ok ? lang.adv : 'как есть'}</span><span class="sbs">Отправить</span></b><small><span class="sbl">${chk.ok ? 'проверено: ' + lang.name + ' ✓' : 'текст не на языке клиента'}</span><span class="sbs">${chk.ok ? lang.adv + ' ✓' : 'не на языке клиента'}</span></small></span>${ic('send')}
        </button>
      </div>
    </div>`;
  }
  function composer(id) {
    const t = byId(id), d = td(id);
    const canSuggest = t.kind === 'in' && !d.draft;
    return `<div class="composer glass-strong">
      ${canSuggest ? `<button class="suggest" data-act="suggest">${ic('spark')}<span>ИИ подготовит ответ ${LANG[t.lang].adv}</span></button>` : ''}
      <div class="cmp-row">
        <button class="ibtn tonal" data-act="suggest" aria-label="Предложить текст ИИ">${ic('spark')}</button>
        <label class="cmp-in"><span class="sr">Сообщение</span><input type="text" placeholder="Написать ${esc(t.buyer.split(' ')[0])}…" data-act="noop"></label>
        <button class="ibtn accent" data-act="toast" data-msg="Свой текст пройдёт перевод и проверку языка" aria-label="Отправить">${ic('send')}</button>
      </div>
    </div>`;
  }
  function threadScreen(id, L) {
    const t = byId(id), d = td(id);
    const vt = S.vt === id && L === 'phone';
    const back = L === 'phone' ? `<button class="ibtn back" data-act="back" aria-label="Назад">${ic('back')}<span class="back-l">Назад</span></button>` : '';
    return `<header class="th-head glass">
        ${back}
        <button class="th-who" data-act="client" aria-label="Карточка клиента">
          ${avatar(t, 'md', vt)}
          <span class="th-names"><b ${vt ? 'style="view-transition-name:vt-title"' : ''}>${esc(t.buyer)}</b><small>${accChip(t.acc)}<span class="th-ad">${esc(t.ad)}</span></small></span>
        </button>
        <button class="ibtn ${S.showTr ? 'on' : ''}" data-act="tr" aria-pressed="${S.showTr}" aria-label="Перевод RU под оригиналом">${ic('tr')}</button>
      </header>
      ${L === 'trio' ? '' : dealStrip(id)}
      <div class="chat scroll" id="chat"><div class="chat-in">${bubbles(id)}</div></div>
      <div class="dock">${d.draft ? draftCard(id, L) : composer(id)}</div>`;
  }

  /* ---------- боковая панель сделки и клиента (Fold, 3 колонки) ---------- */
  function sidePanel(id) {
    const t = byId(id), d = dealData(id), c = M.client;
    const isMarkus = t.buyer === c.name;
    const gap = d.offer != null ? d.offer - d.floor : null;
    return `<div class="side scroll">
      <section class="card">
        <h3>Сделка</h3>
        ${dealNums(d, t)}
        ${scale(d, 'lg')}
        <div class="legend"><span class="lgd l-offer">клиент</span><span class="lgd l-ours">мы</span><span class="lgd l-sugg">ИИ</span><span class="lgd l-floor">минимум</span></div>
        ${gap != null ? `<p class="gap ${gap < 0 ? 'bad' : 'good'}">${gap < 0 ? 'Ниже минимума на ' + eur(-gap) : 'Выше минимума на ' + eur(gap)}</p>` : ''}
        <p class="summary">${esc(d.summary)}</p>
        <div class="tags">${t.tags.map(x => `<span class="tag">${esc(x)}</span>`).join('')}<span class="tag score">интерес ${t.score}</span></div>
      </section>
      <section class="card">
        <h3>Клиент</h3>
        <div class="mini-client">${avatar(t, 'md')}<div><b>${esc(t.buyer)}</b><small>${isMarkus ? 'впервые ' + esc(c.since) : 'новый клиент'} · ${accChip(t.acc)}</small></div></div>
        ${isMarkus ? `<div class="stats s2">
          <div class="stat"><b>${c.stats.inquiries}</b><small>обращения</small></div>
          <div class="stat"><b>${c.stats.bought}</b><small>покупка</small></div>
          <div class="stat"><b>${eur(c.stats.spent)}</b><small>потратил</small></div>
          <div class="stat"><b>${esc(c.stats.avgReply)}</b><small>отвечает</small></div>
        </div><p class="summary">${esc(CLIENT_X.dossier.summary)}</p>
        <div class="side-warn">${ic('warn')}<span>Возможно, тот же покупатель: «${esc(CLIENT_X.similar.name)}» на ${esc(ACC[CLIENT_X.similar.acc].name)}</span></div>` : '<p class="summary">Первое обращение. Истории покупок нет.</p>'}
        <button class="btn-tonal wide" data-act="client">${ic('user')}Карточка клиента</button>
      </section>
    </div>`;
  }

  /* ---------- CLIENT ---------- */
  function clientScreen(L) {
    const c = M.client, t = byId('t1');
    const st = { 'идёт торг': 'live', 'куплено': 'won', 'не договорились': 'lost' };
    return `<div class="client scroll">
      <header class="hero" style="--h:${hue(c.name)}">
        <div class="hero-bar">
          <button class="ibtn back glassy" data-act="back" aria-label="Назад">${ic('back')}<span class="back-l">Назад</span></button>
          <button class="ibtn glassy" data-act="toast" data-msg="Заметка сохранена" aria-label="Заметка">${ic('note')}</button>
        </div>
        ${avatar(t, 'xl', S.vt === 'client')}
        <h1>${esc(c.name)}</h1>
        <p class="hero-meta">${flag(c.lang)}<span>немецкий</span><span class="sep">·</span><span>впервые ${esc(c.since)}</span><span class="sep">·</span>${accChip(t.acc)}</p>
        <div class="hero-acts">
          <button class="btn-grad" data-act="open" data-id="t1">${ic('chats')}Открыть чат</button>
          <button class="btn-glass" data-act="toast" data-msg="Email скопирован" aria-label="Скопировать email">${ic('copy')}<span class="hide-xs">Email</span></button>
        </div>
      </header>
      <div class="client-body">
        <div class="stats s4">
          <div class="stat"><b>${c.stats.inquiries}</b><small>обращения</small></div>
          <div class="stat"><b>${c.stats.bought}</b><small>покупка</small></div>
          <div class="stat accent"><b>${eur(c.stats.spent)}</b><small>потратил</small></div>
          <div class="stat"><b>${esc(c.stats.avgReply)}</b><small>отвечает</small></div>
        </div>
        <section class="card note">${ic('note')}<div><p>${esc(c.note)}</p>
          <div class="tags edit">${S.tags.map(x => `<span class="tag">${esc(x)}</span>`).join('')}<button class="tag add" data-act="tag-add">${ic('plus')}тег</button></div></div></section>
        <section class="card warn-card">${ic('warn')}<div><b>Возможно, тот же покупатель</b>
          <p>«${esc(CLIENT_X.similar.name)}» · ${accChip(CLIENT_X.similar.acc)} · ${esc(CLIENT_X.similar.when)}, ${esc(CLIENT_X.similar.ad)}. ${esc(CLIENT_X.similar.why)}.</p>
          <div class="wc-acts"><button class="btn-tonal" data-act="toast" data-msg="Профили объединены">Это он</button><button class="btn-tonal" data-act="toast" data-msg="Отмечено: разные люди">Разные</button></div></div></section>
        <h2 class="h2">Досье</h2>
        <section class="card dossier"><p class="ds-sum">${esc(CLIENT_X.dossier.summary)}</p>
          ${CLIENT_X.dossier.items.map(x => `<div class="ds-row"><span class="ds-when">${esc(x.when)}</span><div><b>${esc(x.what)}</b><small>${esc(x.price)}</small></div></div>`).join('')}
        </section>
        <h2 class="h2">Сделки</h2>
        <ol class="timeline">
          ${c.history.map((h, i) => `<li class="tl ${st[h.status] || ''}">
            <span class="tl-dot"></span>
            <div class="tl-card" ${i === 0 ? 'data-act="open" data-id="t1" role="button" tabindex="0"' : ''}>
              <div class="tl-r1"><b>${esc(h.ad)}</b><span class="tl-price">${eur(h.price)}</span></div>
              <div class="tl-r2"><span class="st st-${st[h.status] || ''}">${esc(h.status)}</span><span class="tl-date">${esc(h.date)}</span></div>
            </div></li>`).join('')}
        </ol>
        <div class="list-pad"></div>
      </div>
    </div>`;
  }

  /* ---------- SALES ---------- */
  function spark(data, w = 320, h = 96) {
    const max = Math.max(...data), n = data.length, gap = 3;
    const bw = (w - gap * (n - 1)) / n;
    const maxI = data.indexOf(max);
    const bars = data.map((v, i) => {
      const bh = v ? Math.max(4, v / max * (h - 18)) : 2;
      const x = i * (bw + gap), y = h - bh;
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="${Math.min(3, bw / 2).toFixed(1)}" class="${v ? (i === maxI ? 'bar max' : 'bar') : 'bar zero'}"/>`;
    }).join('');
    const mx = maxI * (bw + gap) + bw / 2;
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Продажи по дням, максимум ${eur(max)}">
      <defs><linearGradient id="gbar" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="g1"/><stop offset="1" class="g2"/></linearGradient></defs>${bars}</svg>
      <div class="spark-max" style="left:${(mx / w * 100).toFixed(1)}%">${eur(max)}</div>`;
  }
  function salesScreen(L) {
    const s = M.sales;
    const tot = s.byAccount.reduce((a, b) => a + b.sum, 0);
    const maxA = Math.max(...s.byAccount.map(a => a.sum));
    return `<header class="top glass"><div class="top-row">
        <div class="title-block"><h1>Продажи</h1><p class="sub">${esc(s.period)}</p></div>
        <button class="btn-tonal add-sale" data-act="manual-sale">${ic('plus')}<span>Продажа</span></button>
      </div></header>
      <div class="sales scroll">
        <section class="card total">
          <small>Выручка за месяц</small>
          <div class="big">${num(s.total)}<span>€</span></div>
          <div class="kpis"><span><b>${s.count}</b> продаж</span><span><b>${String(s.avgDiscount).replace('.', ',')}%</b> ср. скидка</span><span><b>${esc(s.replyMedian)}</b> ответ</span></div>
          <div class="spark-wrap">${spark(s.byDay)}</div>
          <div class="axis"><span>1 окт</span><span>по дням</span><span>20 окт</span></div>
        </section>
        ${S.found.length ? `<section class="card found">
          <h3>${ic('spark')}Найденные продажи <i>${S.found.length}</i></h3>
          ${S.found.map(f => `<div class="fs" style="--acc:${ACC[f.acc].color}">
            <div class="fs-m"><div class="fs-r1"><b>${esc(f.ad)}</b><span>${eur(f.price)}</span></div>
              <small><span class="acc"><i></i>${esc(ACC[f.acc].name)}</span> · ${esc(f.why)}</small></div>
            <div class="fs-acts"><button class="btn-tonal" data-act="found-no" data-id="${f.id}">Отклонить</button><button class="btn-grad" data-act="found-yes" data-id="${f.id}">${ic('check')}Подтвердить</button></div>
          </div>`).join('')}
        </section>` : ''}
        <div class="sales-grid">
          <section class="card">
            <h3>По аккаунтам</h3>
            ${s.byAccount.map(a => { const acc = ACC[a.acc]; return `<div class="acc-row" style="--acc:${acc.color}">
              <div class="ar-1"><span class="acc"><i></i>${esc(acc.name)}</span><b>${eur(a.sum)}</b></div>
              <div class="ar-bar"><span style="width:${(a.sum / maxA * 100).toFixed(1)}%"></span></div>
              <small>${Math.round(a.sum / tot * 100)}% выручки</small></div>`; }).join('')}
          </section>
          <section class="card">
            <h3>Последние продажи</h3>
            ${s.recent.map(r => `<div class="sale" style="--acc:${ACC[r.acc].color}"><span class="sale-ic">${ic('check')}</span>
              <div class="sale-m"><b>${esc(r.ad)}</b><small>${esc(ACC[r.acc].name)} · ${esc(r.date)}</small></div><span class="sale-p">${eur(r.price)}</span></div>`).join('')}
          </section>
        </div>
        <div class="list-pad"></div>
      </div>`;
  }

  /* ---------- нижние вкладки ---------- */
  function tabs() {
    const waiting = threads.filter(t => t.kind === 'in' && !S.sold.has(t.id)).length;
    const cur = S.screen === 'thread' ? 'inbox' : S.screen;
    const T = [['inbox', 'chats', 'Чаты', waiting], ['client', 'user', 'Клиенты'], ['sales', 'chart', 'Продажи']];
    return `<nav class="tabs glass" aria-label="Разделы">${T.map(([k, i, l, n]) => `<button class="tab ${cur === k ? 'on' : ''}" data-act="tab" data-s="${k}" aria-current="${cur === k ? 'page' : 'false'}">
      <span class="tab-ic">${ic(i)}${n ? `<b>${n}</b>` : ''}</span><span>${l}</span></button>`).join('')}</nav>`;
  }

  /* ---------- Flex Window 306×340 ---------- */
  function coverScreen() {
    const s = S.screen;
    if (s === 'thread') {
      const t = byId(S.threadId), d = td(S.threadId), last = [...d.events].reverse().find(e => e.who === 'in');
      const dr = d.draft, chk = dr ? langCheck(dr.text, t.lang) : null;
      return `<div class="cv cv-thread">
        <header class="cv-head"><button class="cv-back" data-act="back">${ic('back')}Назад</button><b class="cv-name">${esc(t.buyer)}</b>${flag(t.lang)}</header>
        <div class="cv-msg"><small>${esc(last ? last.time.split(' ').pop() : '')} · клиент${t.offer ? ` · <em class="${t.offer < t.floor ? 'bad' : 'good'}">${eur(t.offer)}</em>` : ''}</small><p>${esc(last ? last.ru : '')}</p></div>
        ${dr ? `<div class="cv-draft"><small>${ic('spark')}Ответ <span class="lang-badge ${chk.ok ? 'ok' : 'no'}">${chk.code} ${chk.ok ? '✓' : '✕'}</span></small><p>${esc(dr.ru)}</p></div>
        <button class="cv-send" data-act="send" ${chk.ok ? '' : 'disabled'}>Отправить ${ic('send')}</button>`
        : `<div class="cv-draft empty"><p>${d.events[d.events.length - 1].who === 'out' ? 'Ответ отправлен. Ждём клиента.' : 'Черновика нет — откройте на большом экране.'}</p></div>
        <button class="cv-send ghost" data-act="back">${ic('back')}К списку</button>`}
      </div>`;
    }
    if (s === 'client') {
      const c = M.client, t = byId('t1');
      return `<div class="cv cv-client">
        <header class="cv-head"><button class="cv-back" data-act="back">${ic('back')}Назад</button></header>
        <div class="cv-hero">${avatar(t, 'md')}<div><b>${esc(c.name)}</b><small>впервые ${esc(c.since)}</small></div></div>
        <div class="cv-stats"><div><b>${c.stats.bought}</b><small>покупка</small></div><div><b>${eur(c.stats.spent)}</b><small>потратил</small></div></div>
        <p class="cv-note">${esc(c.note)}</p>
        <div class="side-warn">${ic('warn')}<span>Возможно, тот же: «${esc(CLIENT_X.similar.name)}» на ${esc(ACC[CLIENT_X.similar.acc].name)}</span></div>
      </div>`;
    }
    if (s === 'sales') {
      const sl = M.sales;
      return `<div class="cv cv-sales">
        <header class="cv-head"><button class="cv-back" data-act="tab" data-s="inbox">${ic('back')}Чаты</button><span class="cv-per">${esc(sl.period)}</span></header>
        <div class="big">${num(sl.total)}<span>€</span></div>
        <p class="cv-k">${sl.count} продаж · ответ ${esc(sl.replyMedian)}</p>
        <div class="spark-wrap">${spark(sl.byDay, 280, 70)}</div>
      </div>`;
    }
    const wait = threads.filter(t => t.kind === 'in' && !S.sold.has(t.id));
    return `<div class="cv cv-inbox">
      <header class="cv-head"><b class="cv-title">Ждут ответа</b><span class="cv-count">${wait.length}</span></header>
      <div class="cv-list scroll">${wait.map(t => `<button class="cv-row" data-act="open" data-id="${t.id}">
        <span class="cv-acc" style="--acc:${ACC[t.acc].color}"></span>
        <span class="cv-rm"><b>${esc(t.buyer)}</b><small>${esc(t.last_ru)}</small></span>
        ${t.draft ? `<span class="pill-draft">${ic('spark')}</span>` : ''}</button>`).join('')}</div>
    </div>`;
  }

  /* ---------- сборка ---------- */
  function render() {
    const L = layout();
    root.className = `shell L-${L} S-${S.screen}`;
    if (L === 'cover') root.innerHTML = coverScreen();
    else if (L === 'phone') {
      let body;
      if (S.screen === 'thread') body = threadScreen(S.threadId, L);
      else if (S.screen === 'client') body = clientScreen(L);
      else if (S.screen === 'sales') body = salesScreen(L);
      else body = inboxScreen(L);
      root.innerHTML = `<main class="view" data-screen="${S.screen}">${body}</main>${S.screen === 'thread' ? '' : tabs()}`;
    } else {
      // Fold внутренний: список | тред (или клиент/продажи) | сделка+клиент
      const main = S.screen === 'client' ? clientScreen(L) : S.screen === 'sales' ? salesScreen(L) : threadScreen(S.threadId, L);
      const side = L === 'trio' && (S.screen === 'inbox' || S.screen === 'thread');
      root.innerHTML = `<aside class="col-list">${inboxScreen(L)}${tabs()}</aside>
        <main class="col-main view" data-screen="${S.screen}">${main}</main>
        ${side ? `<aside class="col-side">${sidePanel(S.threadId)}</aside>` : ''}`;
    }
    after(L);
  }
  function after(L) {
    const chat = document.getElementById('chat');
    if (chat) chat.scrollTop = chat.scrollHeight;
    bindSwipes();
    if (TG && TG.BackButton) {
      try { (L === 'phone' || L === 'cover') && S.screen !== 'inbox' && S.screen !== 'sales' ? TG.BackButton.show() : TG.BackButton.hide(); } catch (e) { /* вне Telegram */ }
    }
  }

  // Переход с View Transitions (shared element: аватар и имя летят в шапку треда)
  function transition(update, dir = 'fwd') {
    if (SHOT || REDUCED || !document.startViewTransition) {
      update();
      if (!SHOT && !REDUCED) { const v = root.querySelector('.view'); v && v.classList.add('enter-' + dir); }
      return;
    }
    document.documentElement.dataset.dir = dir;
    try {
      const vt = document.startViewTransition(update);
      vt.ready.catch(() => {}); vt.finished.catch(() => {}).then(() => { delete document.documentElement.dataset.dir; });
      if (vt.updateCallbackDone) vt.updateCallbackDone.catch(() => {});
    } catch (e) { update(); }
  }
  function go(screen, opts = {}) {
    const L = layout();
    const dir = opts.dir || 'fwd';
    if (opts.threadId) S.threadId = opts.threadId;
    S.vt = L === 'phone' ? (opts.vt || null) : null;
    transition(() => {
      if (opts.back) S.back = opts.back;
      S.screen = screen; S.swipeOpen = null; S.override = false;
      render();
    }, dir);
  }

  /* ---------- тосты и шторки ---------- */
  let toastTimer;
  function toast(msg, undo) {
    clearTimeout(toastTimer);
    layer.querySelector('.toast')?.remove();
    const el = document.createElement('div');
    el.className = 'toast glass-strong';
    el.setAttribute('role', 'status');
    el.innerHTML = `<span>${esc(msg)}</span>${undo ? '<button>Отменить</button>' : ''}`;
    if (undo) el.querySelector('button').onclick = () => { undo(); el.remove(); };
    layer.appendChild(el);
    toastTimer = setTimeout(() => el.classList.add('out'), 3600);
    setTimeout(() => el.remove(), 4000);
  }
  function sheet(html, onClick) {
    closeSheet();
    const wrap = document.createElement('div');
    wrap.className = 'sheet-wrap';
    wrap.innerHTML = `<div class="scrim" data-x="1"></div><div class="sheet glass-strong" role="dialog" aria-modal="true"><div class="grab"></div>${html}</div>`;
    wrap.addEventListener('click', e => {
      const b = e.target.closest('[data-x],[data-op]');
      if (!b) return;
      if (b.dataset.x) return closeSheet();
      onClick && onClick(b.dataset.op, wrap);
    });
    layer.appendChild(wrap);
    requestAnimationFrame(() => wrap.classList.add('in'));
    return wrap;
  }
  function closeSheet() {
    const w = layer.querySelector('.sheet-wrap');
    if (!w) return;
    w.classList.remove('in');
    setTimeout(() => w.remove(), SHOT || REDUCED ? 0 : 220);
  }

  /* ---------- действия ---------- */
  function sendDraft(id) {
    const t = byId(id), d = td(id), dr = d.draft;
    if (!dr) return;
    const chk = langCheck(dr.text, t.lang);
    if (!chk.ok && !S.override) { toast('Текст не на языке клиента — нужна галочка-разрешение'); return; }
    const snapshot = { events: d.events.slice(), draft: dr, kind: t.kind, draftFlag: t.draft, unread: t.unread };
    try { TG && TG.HapticFeedback && TG.HapticFeedback.notificationOccurred('success'); } catch (e) { /* вне Telegram */ }
    const doit = () => {
      d.events.forEach(e => delete e.fresh);
      d.events.push({ who: 'out', time: 'сейчас', de: dr.text, ru: dr.edited && !CYR.test(dr.text) ? dr.ru : dr.ru, fresh: true });
      if (dr.suggestedPrice) d.deal.ourLast = dr.suggestedPrice;
      t.ourLast = d.deal.ourLast;
      d.draft = null; t.kind = 'out'; t.draft = false; t.unread = false; t.time = 'сейчас';
      S.override = false; S.draftMin = false; S.swipeOpen = null;
      render();
    };
    transition(doit, 'none');
    toast(chk.ok ? `Отправлено ${LANG[t.lang].adv} · ${t.buyer}` : `Отправлено как есть · ${t.buyer}`, () => {
      d.events = snapshot.events; d.draft = snapshot.draft; t.kind = snapshot.kind; t.draft = snapshot.draftFlag; t.unread = snapshot.unread;
      transition(render, 'none');
    });
  }
  function moreSheet(id) {
    const t = byId(id), lang = LANG[t.lang];
    sheet(`<h3>Как ответить</h3>
      <p class="sh-sub">Сейчас: этап 3 — текст готов и проверен (${lang.code} ✓)</p>
      <button class="op" data-op="translate">${ic('tr')}<span><b>Перевести на язык клиента</b><small>Ваш текст → ${lang.name}, RU-зеркало для проверки</small></span></button>
      <button class="op" data-op="rewrite">${ic('refresh')}<span><b>Переписать идею ИИ…</b><small>Жёстче, мягче, короче или своя инструкция</small></span></button>
      <button class="op" data-op="asis">${ic('send')}<span><b>Отправить как есть</b><small>Без перевода, с проверкой языка</small></span></button>
      <button class="op" data-op="own">${ic('pen')}<span><b>Написать своё</b><small>Скрыть черновик и открыть поле ввода</small></span></button>
      <div class="op-row"><button class="op sm" data-op="later">${ic('clock')}Позже</button><button class="op sm" data-op="sold">${ic('sold')}Продано</button></div>
      <button class="op cancel" data-x="1">Отменить</button>`, op => {
      const d = td(id);
      closeSheet();
      if (op === 'translate') {
        if (d.draft && CYR.test(d.draft.text)) { d.draft.text = d.draft.orig.text; d.draft.ru = d.draft.orig.ru; d.draft.edited = false; render(); toast('Переведено: ' + lang.name); }
        else toast('Текст уже ' + lang.adv + ' — переводить нечего');
      } else if (op === 'rewrite') rewriteSheet(id);
      else if (op === 'asis') sendDraft(id);
      else if (op === 'own') { const keep = d.draft; d.draft = null; render(); toast('Черновик скрыт', () => { d.draft = keep; render(); }); }
      else if (op === 'later') { S.later.add(id); toast('Напомню через 2 часа'); go('inbox', { dir: 'back', vt: id }); }
      else if (op === 'sold') markSold(id);
    });
  }
  function rewriteSheet(id) {
    const w = sheet(`<h3>Переписать идею ИИ</h3>
      <div class="presets">${['Жёстче', 'Мягче', 'Короче', 'Без торга', 'Предложить 1 000 €'].map(p => `<button class="chip" data-op="p:${p}">${p}</button>`).join('')}</div>
      <label class="field"><span>Своя инструкция по-русски</span><textarea rows="3" placeholder="Например: скажи, что в субботу только до 14:00"></textarea></label>
      <div class="sh-acts"><button class="btn-tonal" data-x="1">Отмена</button><button class="btn-grad" data-op="go">${ic('spark')}Переписать</button></div>`, op => {
      if (op.startsWith('p:')) { w.querySelector('textarea').value = op.slice(2); return; }
      if (op === 'go') { closeSheet(); regenerate(id); }
    });
  }
  function regenerate(id) {
    const d = td(id), t = byId(id);
    S.busy = true; S.draftMin = false; render();
    setTimeout(() => {
      S.busy = false;
      const alt = REWRITE[t.lang];
      if (d.draft && alt && id === 't1') { d.draft.text = alt.text; d.draft.ru = alt.ru; d.draft.suggestedPrice = alt.price; d.draft.edited = false; }
      transition(render, 'none');
      toast('Новый вариант готов · проверено ' + LANG[t.lang].code + ' ✓');
    }, SHOT ? 0 : 900);
  }
  function suggest(id) {
    const d = td(id), t = byId(id), x = EXTRA[id] || {};
    const gen = x.gen || { lang: t.lang, suggestedPrice: t.price,
      text: (OUT_GENERIC[t.lang] || OUT_GENERIC.de).de, ru: (OUT_GENERIC[t.lang] || OUT_GENERIC.de).ru };
    d.draft = { ...gen, orig: { text: gen.text, ru: gen.ru } }; t.draft = true;
    S.busy = true; render();
    setTimeout(() => { S.busy = false; transition(render, 'none'); }, SHOT ? 0 : 800);
  }
  function editSheet(id) {
    const t = byId(id), d = td(id), lang = LANG[t.lang];
    const w = sheet(`<h3>Редактировать</h3>
      <p class="sh-sub">Текст уйдёт клиенту ровно так. Язык клиента — ${lang.name}.</p>
      <label class="field"><span>Текст для клиента <b class="lang-badge ok" id="lb">${lang.code} ✓</b></span><textarea rows="5" lang="${t.lang}">${esc(d.draft.text)}</textarea></label>
      <p class="warn" id="lw" hidden>${ic('shield')}Кириллица: клиент не поймёт. Отправка будет заблокирована, пока вы явно не разрешите.</p>
      <div class="sh-acts"><button class="btn-tonal" data-x="1">Отмена</button><button class="btn-grad" data-op="save">${ic('check')}Сохранить</button></div>`, op => {
      if (op !== 'save') return;
      const v = w.querySelector('textarea').value.trim();
      if (v && v !== d.draft.text) { d.draft.text = v; d.draft.edited = true; S.override = false; }
      closeSheet(); render();
    });
    const ta = w.querySelector('textarea'), lb = w.querySelector('#lb'), lw = w.querySelector('#lw');
    const upd = () => { const c = langCheck(ta.value, t.lang); lb.className = 'lang-badge ' + (c.ok ? 'ok' : 'no'); lb.textContent = c.code + (c.ok ? ' ✓' : ' ✕'); lw.hidden = c.ok; };
    ta.addEventListener('input', upd);
    if (!SHOT) setTimeout(() => ta.focus(), 250);
  }
  function markSold(id) {
    const t = byId(id);
    S.sold.add(id); S.swipeOpen = null;
    const L = layout();
    if (S.screen === 'thread' && L === 'phone') go('inbox', { dir: 'back' });
    else {
      if (S.threadId === id) S.threadId = (threads.find(x => !S.sold.has(x.id)) || threads[0]).id;
      transition(render, 'none');
    }
    toast(`Продано · ${t.ad}`, () => { S.sold.delete(id); transition(render, 'none'); });
  }

  function bulk(op) {
    const ids = [...S.picked];
    if (!ids.length) { toast('Сначала выберите чаты'); return; }
    if (op === 'pin') { const all = ids.every(i => S.pinned.has(i)); ids.forEach(i => all ? S.pinned.delete(i) : S.pinned.add(i)); toast(all ? 'Откреплено' : `Закреплено: ${ids.length}`); }
    if (op === 'wait') { ids.forEach(i => S.later.add(i)); toast(`В «Жду»: ${ids.length}`, () => { ids.forEach(i => S.later.delete(i)); render(); }); }
    if (op === 'sold') { ids.forEach(i => S.sold.add(i)); toast(`Продано: ${ids.length}`, () => { ids.forEach(i => S.sold.delete(i)); render(); }); }
    S.select = false; S.picked.clear();
    transition(render, 'none');
  }
  function lessonsSheet() {
    sheet(`<h3>Уроки, которые учёл ИИ</h3>
      <p class="sh-sub">Ваши прошлые правки. ИИ берёт самые похожие в каждый черновик.</p>
      ${LESSONS.map(l => `<div class="lesson"><p class="ls-bad">${esc(l.bad)}</p><p class="ls-good">${esc(l.good)}</p><small>${esc(l.note)}</small></div>`).join('')}
      <button class="op cancel" data-x="1">Закрыть</button>`);
  }
  function manualSaleSheet() {
    const w = sheet(`<h3>Продажа вручную</h3>
      <p class="sh-sub">Если сделка прошла мимо переписки — по телефону или на месте.</p>
      <label class="field"><span>Объявление</span><textarea rows="1">Sitzbank 3er VW T6 Multivan</textarea></label>
      <label class="field"><span>Сумма, €</span><textarea rows="1" inputmode="numeric">1050</textarea></label>
      <div class="sh-acts"><button class="btn-tonal" data-x="1">Отмена</button><button class="btn-grad" data-op="save">${ic('check')}Записать</button></div>`, op => {
      if (op === 'save') { closeSheet(); toast('Продажа записана'); }
    });
    return w;
  }

  /* ---------- свайп строки влево ---------- */
  function bindSwipes() {
    root.querySelectorAll('.rw').forEach(rw => {
      const rowEl = rw.querySelector('.row'), acts = rw.querySelector('.row-acts');
      let x0 = 0, y0 = 0, dx = 0, mode = null, base = 0, pid = null;
      const width = () => acts.offsetWidth;
      let lp;
      rowEl.addEventListener('pointerdown', e => {
        if (e.button > 0) return;
        clearTimeout(lp);
        if (!S.select) lp = setTimeout(() => { if (!mode) { S.select = true; S.picked = new Set([rw.dataset.id]); S.lpAt = Date.now(); pid = null; rw.dataset.swiped = '1'; render(); } }, 550);
        x0 = e.clientX; y0 = e.clientY; dx = 0; mode = null; pid = e.pointerId;
        base = rw.classList.contains('open') ? -width() : 0;
      });
      rowEl.addEventListener('pointermove', e => {
        if (pid !== e.pointerId) return;
        if (S.select) return;
        const mx = e.clientX - x0, my = e.clientY - y0;
        if (!mode) {
          if (Math.abs(mx) > 10 && Math.abs(mx) > Math.abs(my) * 1.3) { mode = 'swipe'; rowEl.setPointerCapture(pid); rw.classList.add('drag'); }
          else if (Math.abs(my) > 10) { mode = 'scroll'; }
        }
        if (mode === 'swipe') {
          dx = Math.min(0, Math.max(-width() - 24, base + mx));
          rowEl.style.transform = `translateX(${dx}px)`;
          acts.style.setProperty('--reveal', Math.min(1, -dx / width()).toFixed(2));
        }
      });
      const end = () => {
        clearTimeout(lp);
        if (mode === 'swipe') {
          rw.classList.remove('drag'); rowEl.style.transform = ''; acts.style.removeProperty('--reveal');
          const open = dx < -width() * 0.35;
          root.querySelectorAll('.rw.open').forEach(o => o !== rw && o.classList.remove('open'));
          rw.classList.toggle('open', open);
          S.swipeOpen = open ? rw.dataset.id : null;
          rw.dataset.swiped = '1';
          setTimeout(() => delete rw.dataset.swiped, 50);
        }
        pid = null; mode = null;
      };
      rowEl.addEventListener('pointerup', end);
      rowEl.addEventListener('pointercancel', end);
    });
  }

  /* ---------- делегирование кликов ---------- */
  document.addEventListener('click', e => {
    const el = e.target.closest('[data-act]');
    if (!el || layer.contains(el)) return;
    const act = el.dataset.act, id = el.dataset.id;
    const L = layout();
    switch (act) {
      case 'open': {
        const rw = el.closest('.rw');
        if (rw && rw.dataset.swiped) return;
        if (rw && rw.classList.contains('open')) { rw.classList.remove('open'); S.swipeOpen = null; return; }
        S.draftMin = false; S.dealOpen = false;
        if (L === 'phone' || L === 'cover') go('thread', { threadId: id, vt: id, back: S.screen === 'client' ? 'client' : 'inbox' });
        else { S.threadId = id; const t = byId(id); t.unread = false; go(S.screen === 'sales' || S.screen === 'client' ? 'thread' : S.screen, { threadId: id, dir: 'none' }); }
        byId(id).unread = false;
        break;
      }
      case 'back':
        if (S.screen === 'client') go(S.back === 'thread' ? 'thread' : 'inbox', { dir: 'back', vt: 'client' });
        else go('inbox', { dir: 'back', vt: S.threadId });
        break;
      case 'tab': go(el.dataset.s, { dir: 'none', back: 'inbox' }); break;
      case 'client': go('client', { dir: 'fwd', vt: 'client', back: 'thread' }); break;
      case 'filter': S.filter = el.dataset.f; transition(render, 'none'); break;
      case 'tr': S.showTr = !S.showTr; S.flipped.clear(); render(); break;
      case 'flip': { const k = el.dataset.key; S.flipped.has(k) ? S.flipped.delete(k) : S.flipped.add(k); const chat = document.getElementById('chat'); const top = chat && chat.scrollTop; render(); const c2 = document.getElementById('chat'); if (c2 && top != null) c2.scrollTop = top; break; }
      case 'deal': S.dealOpen = !S.dealOpen; transition(render, 'none'); break;
      case 'draft-min': S.draftMin = true; transition(render, 'none'); break;
      case 'draft-max': S.draftMin = false; transition(render, 'none'); break;
      case 'override': S.override = el.checked; render(); break;
      case 'send': sendDraft(S.threadId); break;
      case 'edit': editSheet(S.threadId); break;
      case 'more': moreSheet(S.threadId); break;
      case 'suggest': suggest(S.threadId); break;
      case 'swipe-send': { const t = byId(id); if (td(id).draft) { const prev = S.threadId; S.threadId = id; sendDraft(id); if (L !== 'phone') S.threadId = prev; } else toast('У ' + t.buyer + ' нет черновика'); break; }
      case 'swipe-later': S.later.add(id); S.swipeOpen = null; transition(render, 'none'); toast('Напомню через 2 часа', () => { S.later.delete(id); transition(render, 'none'); }); break;
      case 'swipe-sold': markSold(id); break;
      case 'toast': toast(el.dataset.msg || ''); break;
      case 'sel-on': S.select = true; S.picked.clear(); S.swipeOpen = null; render(); break;
      case 'sel-off': S.select = false; S.picked.clear(); render(); break;
      case 'pick': if (Date.now() - (S.lpAt || 0) < 700) break; S.picked.has(id) ? S.picked.delete(id) : S.picked.add(id); render(); break;
      case 'bulk': bulk(el.dataset.op); break;
      case 'lessons': lessonsSheet(); break;
      case 'tag-add': { const v = (prompt('Новый тег') || '').trim(); if (v) { S.tags.push(v); render(); } break; }
      case 'manual-sale': manualSaleSheet(); break;
      case 'found-yes': case 'found-no': { const f = S.found.find(x => x.id === id); S.found = S.found.filter(x => x.id !== id); transition(render, 'none');
        toast(act === 'found-yes' ? `Продажа подтверждена · ${eur(f.price)}` : 'Отклонено — это не продажа', () => { S.found.push(f); render(); }); break; }
      default: break;
    }
  });
  document.addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[role="button"][data-act]')) { e.preventDefault(); e.target.click(); }
    if (e.key === 'Escape') closeSheet();
  });
  if (TG) {
    try { TG.ready(); TG.expand(); TG.BackButton && TG.BackButton.onClick(() => document.querySelector('[data-act="back"]')?.click()); } catch (e) { /* вне Telegram */ }
  }
  let rz;
  addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(render, 120); });

  // Синхронизация стартовой темы, если пользователь сменит системную (без ?theme=)
  render();
})();
