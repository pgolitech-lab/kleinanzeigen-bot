// 🔎 Рынок: ищем ТОВАР, а не город. Сверху поле «Какой товар?» + подсказки (наши товары, недавние запросы),
// под ним главное одним взглядом, затем «Где продаётся» (города) и список объявлений.
// Данные: GET /api/ma/market/suggest, GET /api/ma/market/search?q=…&product_id=… (контракт — сессия market).
import { api, LLM_TIMEOUT_MS } from "../api.js";
import { el, eur } from "../utils.js";
import { openLink } from "../tg.js";

const money = v => (v == null ? "—" : eur(Math.round(v)));
const RECENT_KEY = "kab:market:recent";
const loadRecent = () => { try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").filter(x => typeof x === "string").slice(0, 8); } catch (e) { return []; } };
const saveRecent = q => {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify([q, ...loadRecent().filter(x => x.toLowerCase() !== q.toLowerCase())].slice(0, 8))); } catch (e) { /* без хранилища — не страшно */ }
};

const ageRu = d => (d == null ? "" : d <= 0 ? "сегодня" : d === 1 ? "вчера" : d < 30 ? `${d} дн. назад` : `${Math.round(d / 30)} мес. назад`);
const adsWord = n => { const m = n % 10, h = n % 100; return h >= 11 && h <= 14 ? "объявлений" : m === 1 ? "объявление" : m >= 2 && m <= 4 ? "объявления" : "объявлений"; };

function note(cls, msg, action) {
  const b = el(`<div class="ms-note"><p></p></div>`);
  b.classList.add(cls);
  b.querySelector("p").textContent = msg;
  if (action) {
    const r = el(`<button class="btn" type="button"></button>`);
    r.textContent = action.label;
    r.addEventListener("click", action.fn);
    r.classList.add("ms-act");
    b.appendChild(r);
  }
  return b;
}

function headline(d) {
  const box = el(`<section class="ms-main"><h2 class="ms-h"></h2><p class="ms-range"></p><p class="ms-ours"></p><small class="ms-age"></small></section>`);
  const n = d.n_active ?? d.total ?? 0;
  box.querySelector(".ms-h").textContent = `Продаётся ${n} ${adsWord(n)}`;
  const range = box.querySelector(".ms-range"), p = d.price ?? {};
  if (p.min != null && p.max != null) {
    range.textContent = `Цена от ${money(p.min)} до ${money(p.max)}${p.median != null ? `, обычно ${money(p.median)}` : ""}`;
  } else range.remove();
  const ours = box.querySelector(".ms-ours"), it = d.ours?.items?.[0];
  if (d.ours?.position_ru) {
    ours.textContent = d.ours.position_ru;
    if (it?.verdict === "pricier") ours.classList.add("ms-up");
    else if (it?.verdict === "cheaper") ours.classList.add("ms-down");
  } else if (it?.price_eur != null) {
    const dp = it.vs_median_pct;
    const rel = dp == null ? "" : Math.abs(dp) < 1 ? " — как на рынке" : ` — ${dp > 0 ? "дороже" : "дешевле"} на ${Math.round(Math.abs(dp))} %`;
    ours.textContent = `У нас ${money(it.price_eur)}${rel}`;
  } else ours.remove();
  const age = box.querySelector(".ms-age"), h = d.data_health;
  if (h?.stale) age.textContent = h.reason || `Данные рынка ${ageRu(h.age_days)} — могли устареть`;
  else age.remove();
  return box;
}

function citiesBlock(cities) {
  const sec = el(`<section class="c-sec ms-cities"><h3>Где продаётся</h3><div class="ms-clist"></div></section>`);
  const list = sec.querySelector(".ms-clist");
  cities.forEach(c => {
    const r = el(`<div class="ms-city"><b></b><span></span></div>`);
    r.querySelector("b").textContent = c.city || (c.plz_area ? `район ${c.plz_area}` : "город не указан");
    r.querySelector("span").textContent = `${c.count} ${adsWord(c.count)}${c.median_eur != null ? ` · обычно ${money(c.median_eur)}` : ""}`;
    list.appendChild(r);
  });
  return sec;
}

function adsBlock(ads) {
  const sec = el(`<section class="c-sec ms-ads"><h3>Объявления</h3><div class="ms-alist"></div></section>`);
  const list = sec.querySelector(".ms-alist");
  ads.forEach(a => {
    const r = el(`<a class="ms-ad" role="link"><b class="ms-at"></b><span class="ms-ap"></span><small class="ms-am"></small></a>`);
    r.querySelector(".ms-at").textContent = (a.ours ? "⭐ " : "") + (a.title ?? "Без названия");
    r.querySelector(".ms-ap").textContent = money(a.price_eur);
    r.querySelector(".ms-am").textContent = [a.city, a.age_days != null ? ageRu(a.age_days) : a.posted_raw, a.active === false ? "снято" : ""].filter(Boolean).join(" · ");
    if (/^https:\/\//i.test(a.url ?? "")) {
      r.href = a.url;
      r.target = "_blank"; r.rel = "noopener";
      r.addEventListener("click", ev => { ev.preventDefault(); openLink(a.url); });
      r.insertAdjacentHTML("beforeend", `<span class="ms-open">Открыть ›</span>`);
    }
    list.appendChild(r);
  });
  return sec;
}

export async function render(mount) {
  const root = el(`
    <div class="msearch">
      <form class="ms-form" role="search">
        <label class="ms-lbl" for="ms-q">Какой товар?</label>
        <div class="ms-row"><input id="ms-q" class="form-control ms-q" type="search" maxlength="80" enterkeyhint="search" autocomplete="off" placeholder="например: сиденья Peugeot"><button class="btn btn-primary ms-go" type="submit">Найти</button></div>
      </form>
      <div class="ms-sug"></div>
      <div class="ms-res"></div>
    </div>`);
  mount.replaceChildren(root);
  const input = root.querySelector(".ms-q"), sug = root.querySelector(".ms-sug"), res = root.querySelector(".ms-res");
  let seq = 0, ctrl = null;
  const go = root.querySelector(".ms-go");
  // уход с экрана обрывает запрос: роутер заменяет mount, ждём hashchange
  window.addEventListener("hashchange", () => { if (!root.isConnected) ctrl?.abort(); });

  const chip = (label, onClick, cls) => {
    const c = el(`<button type="button" class="qchip"></button>`);
    c.textContent = label;
    if (cls) c.classList.add(cls);
    c.addEventListener("click", onClick);
    sug.appendChild(c);
  };
  const paintSuggest = items => {
    sug.replaceChildren();
    (items ?? []).slice(0, 10).forEach(x => chip(x.ours ? `⭐ ${x.label ?? x.query}` : (x.label ?? x.query), () => { input.value = x.query; search(x.query); }, x.ours ? "ms-prod" : ""));
    const seen = new Set((items ?? []).map(x => (x.query ?? "").toLowerCase()));
    loadRecent().filter(q => !seen.has(q.toLowerCase())).slice(0, 6).forEach(q => chip(`🕘 ${q}`, () => { input.value = q; search(q); }));
    sug.hidden = !sug.children.length;
  };
  paintSuggest(null);
  api("/api/ma/market/search?q=").then(d => { if (root.isConnected) paintSuggest(d?.suggestions); }).catch(() => {});

  async function search(q, live) {
    q = (q ?? "").trim();
    if (!q) { res.replaceChildren(note("ms-empty", "Введите товар — например «сиденья Peugeot» или «скамья». Или нажмите на подсказку выше.")); return; }
    if (q.length > 80) q = q.slice(0, 80);
    ctrl?.abort();
    const my = ++seq, mine = ctrl = new AbortController();
    go.disabled = true;
    const busy = el(`<div class="ms-busy"><p></p><button class="btn ms-act" type="button">Отмена</button></div>`);
    busy.querySelector("p").textContent = `⏳ ${live ? "Ищу на Kleinanzeigen (до минуты)…" : "Смотрю, как это продаётся…"}`;
    busy.querySelector("button").addEventListener("click", () => mine.abort());
    res.replaceChildren(busy);
    input.blur();  // клавиатура не должна закрывать результат
    let d;
    try {
      d = await api(`/api/ma/market/search?q=${encodeURIComponent(q)}&live=${live ? 1 : 0}&limit=40`, { timeoutMs: LLM_TIMEOUT_MS, signal: mine.signal });
    } catch (e) {
      if (my !== seq || !root.isConnected) return;
      go.disabled = false;
      if (e.aborted) { res.replaceChildren(note("ms-empty", "Поиск отменён.", { label: "🔄 Искать снова", fn: () => search(q, live) })); return; }
      const nf = e.status === 404 && (!e.detail || e.detail === "Not Found");
      res.replaceChildren(note("ms-err", nf ? "Поиск по рынку ещё не подключён на сервере." : (e.network ? e.message : "Не получилось посмотреть рынок."), nf ? null : { label: "🔄 Повторить", fn: () => search(q, live) }));
      return;
    }
    if (my !== seq || !root.isConnected) return;
    go.disabled = false;
    res.replaceChildren();
    const st = d?.status, msg = d?.message_ru;
    if (st === "source_unavailable") { res.appendChild(note("ms-err", msg || "Kleinanzeigen не ответил.", { label: "🔄 Повторить", fn: () => search(q, live) })); return; }
    if (st === "empty_query") { res.appendChild(note("ms-empty", msg || "Введите товар.")); return; }
    saveRecent(q);
    if (st === "no_data" || !((d?.n_active ?? d?.total ?? 0) > 0)) {
      res.appendChild(note("ms-empty", msg || `По запросу «${q}» объявлений не нашли.`, live ? null : { label: "🔎 Искать на Kleinanzeigen", fn: () => search(q, true) }));
      return;
    }
    res.appendChild(headline(d));
    if (msg) res.appendChild(note(st === "few_data" ? "ms-warn" : "ms-empty", msg, !live && st === "few_data" ? { label: "🔎 Поискать на Kleinanzeigen", fn: () => search(q, true) } : null));
    if (d.sources?.live_error) res.appendChild(note("ms-warn", "Kleinanzeigen сейчас не ответил — показаны только наши данные.", { label: "🔄 Повторить", fn: () => search(q, true) }));
    if ((d.cities ?? []).length) res.appendChild(citiesBlock(d.cities));
    if ((d.listings ?? []).length) res.appendChild(adsBlock(d.listings));
  }

  root.querySelector(".ms-form").addEventListener("submit", ev => { ev.preventDefault(); search(input.value); });
}
