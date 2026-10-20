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

function note(cls, msg, retry) {
  const b = el(`<div class="ms-note"><p></p></div>`);
  b.classList.add(cls);
  b.querySelector("p").textContent = msg;
  if (retry) {
    const r = el(`<button class="btn" type="button">🔄 Повторить</button>`);
    r.addEventListener("click", retry);
    b.appendChild(r);
  }
  return b;
}

function headline(d) {
  const box = el(`<section class="ms-main"><h2 class="ms-h"></h2><p class="ms-range"></p><p class="ms-ours"></p><small class="ms-age"></small></section>`);
  box.querySelector(".ms-h").textContent = `Продаётся ${d.n} ${adsWord(d.n)}`;
  const range = box.querySelector(".ms-range");
  if (d.price_min != null && d.price_max != null) {
    range.textContent = `Цена от ${money(d.price_min)} до ${money(d.price_max)}${d.median != null ? `, обычно ${money(d.median)}` : ""}`;
  } else range.remove();
  const ours = box.querySelector(".ms-ours");
  if (d.ours?.price_eur != null) {
    const dp = d.delta_pct;
    const rel = dp == null ? "" : Math.abs(dp) < 1 ? " — как на рынке" : ` — ${dp > 0 ? "дороже" : "дешевле"} на ${Math.round(Math.abs(dp))} %`;
    ours.textContent = `У нас ${money(d.ours.price_eur)}${rel}`;
    if (dp != null && Math.abs(dp) >= 1) ours.classList.add(dp > 0 ? "ms-up" : "ms-down");
  } else if (d.verdict_ru) ours.textContent = d.verdict_ru;
  else ours.remove();
  const age = box.querySelector(".ms-age");
  if (d.data_age_days != null && d.data_age_days >= 3) age.textContent = `Данные рынка ${ageRu(d.data_age_days)} — могли устареть`;
  else age.remove();
  return box;
}

function citiesBlock(cities) {
  const sec = el(`<section class="c-sec ms-cities"><h3>Где продаётся</h3><div class="ms-clist"></div></section>`);
  const list = sec.querySelector(".ms-clist");
  cities.forEach(c => {
    const r = el(`<div class="ms-city"><b></b><span></span></div>`);
    r.querySelector("b").textContent = c.city || "город не указан";
    r.querySelector("span").textContent = `${c.n} ${adsWord(c.n)}${c.median != null ? ` · обычно ${money(c.median)}` : c.min != null ? ` · от ${money(c.min)}` : ""}`;
    list.appendChild(r);
  });
  return sec;
}

function adsBlock(ads) {
  const sec = el(`<section class="c-sec ms-ads"><h3>Объявления</h3><div class="ms-alist"></div></section>`);
  const list = sec.querySelector(".ms-alist");
  ads.forEach(a => {
    const r = el(`<a class="ms-ad" role="link"><b class="ms-at"></b><span class="ms-ap"></span><small class="ms-am"></small></a>`);
    r.querySelector(".ms-at").textContent = a.title ?? "Без названия";
    r.querySelector(".ms-ap").textContent = money(a.price_eur);
    r.querySelector(".ms-am").textContent = [a.city, ageRu(a.age_days)].filter(Boolean).join(" · ");
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
        <div class="ms-row"><input id="ms-q" class="form-control ms-q" type="search" enterkeyhint="search" autocomplete="off" placeholder="например: сиденья Peugeot"><button class="btn btn-primary ms-go" type="submit">Найти</button></div>
      </form>
      <div class="ms-sug"></div>
      <div class="ms-res"></div>
      <a class="ms-legacy" href="#/scout/legacy">Разведка машин и запчастей по городам ›</a>
    </div>`);
  mount.replaceChildren(root);
  const input = root.querySelector(".ms-q"), sug = root.querySelector(".ms-sug"), res = root.querySelector(".ms-res");
  let seq = 0;

  const chip = (label, onClick, cls) => {
    const c = el(`<button type="button" class="qchip"></button>`);
    c.textContent = label;
    if (cls) c.classList.add(cls);
    c.addEventListener("click", onClick);
    sug.appendChild(c);
  };
  const paintSuggest = data => {
    sug.replaceChildren();
    (data?.products ?? []).slice(0, 8).forEach(p => chip(p.title, () => { input.value = p.title; search(p.title, p.product_id); }, "ms-prod"));
    const seen = new Set((data?.products ?? []).map(p => p.title.toLowerCase()));
    [...(data?.recent ?? []), ...loadRecent()].filter((q, i, a) => !seen.has(q.toLowerCase()) && a.findIndex(x => x.toLowerCase() === q.toLowerCase()) === i).slice(0, 6)
      .forEach(q => chip(`🕘 ${q}`, () => { input.value = q; search(q); }));
    sug.hidden = !sug.children.length;
  };
  paintSuggest(null);
  api("/api/ma/market/suggest").then(d => { if (root.isConnected) paintSuggest(d); }).catch(() => {});

  async function search(q, productId) {
    q = (q ?? "").trim();
    if (!q) { res.replaceChildren(note("ms-empty", "Введите товар — например «сиденья Peugeot» или «скамья». Или нажмите на подсказку выше.")); return; }
    const my = ++seq;
    res.replaceChildren(el(`<div class="ms-busy">⏳ Смотрю, как это продаётся…</div>`));
    input.blur();  // клавиатура не должна закрывать результат
    let d;
    try {
      d = await api(`/api/ma/market/search?q=${encodeURIComponent(q)}${productId ? `&product_id=${encodeURIComponent(productId)}` : ""}`, { timeoutMs: LLM_TIMEOUT_MS });
    } catch (e) {
      if (my !== seq || !root.isConnected) return;
      const nf = e.status === 404 && (!e.detail || e.detail === "Not Found");
      res.replaceChildren(note("ms-err", nf ? "Поиск по рынку ещё не подключён на сервере." : (e.network ? e.message : "Не получилось посмотреть рынок. Попробуйте ещё раз."), nf ? null : () => search(q, productId)));
      return;
    }
    if (my !== seq || !root.isConnected) return;
    saveRecent(q);
    res.replaceChildren();
    if (d?.status === "error") { res.appendChild(note("ms-err", d.message_ru || "Kleinanzeigen не ответил.", () => search(q, productId))); return; }
    if (!d || d.status === "no_data" || !(d.n > 0)) {
      res.appendChild(note("ms-empty", d?.message_ru || `По запросу «${q}» объявлений не нашли. Попробуйте короче: «скамья», «сиденья».`));
      return;
    }
    res.appendChild(headline(d));
    if (d.status === "few_data") res.appendChild(note("ms-warn", d.message_ru || "Мало данных — цены ориентировочные."));
    if ((d.cities ?? []).length) res.appendChild(citiesBlock(d.cities));
    if ((d.ads ?? []).length) res.appendChild(adsBlock(d.ads));
  }

  root.querySelector(".ms-form").addEventListener("submit", ev => { ev.preventDefault(); search(input.value); });
}
