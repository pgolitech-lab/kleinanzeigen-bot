// 🪑 Рынок → Сиденья: рынок по НАШИМ сиденьям без ручного ввода.
// Сервер сам строит запросы «<тип> <модель>» по нашим живым товарам-сиденьям и отдаёт
// накопленный рынок (scout + живая выдача из кэша). Живую выдачу он обновляет в фоне
// (по одному запросу, не чаще раза в 12 ч) — экран просто перечитывает сводку, пока идёт обновление.
// Данные: GET /api/ma/market/seats?refresh=1 (контракт — market_advisor.seat_market).
import { api } from "../api.js";
import { el, eur } from "../utils.js";
import { openLink } from "../tg.js";
import { heatGrid } from "../components/heatmap.js";

// неразрывные пробелы: «370 €» и диапазон «370 €–610 €» не рвутся по строкам
const money = v => (v == null ? "—" : eur(Math.round(v)).replace(/\s/g, "\u00a0"));
const adsWord = n => { const m = n % 10, h = n % 100; return h >= 11 && h <= 14 ? "объявлений" : m === 1 ? "объявление" : m >= 2 && m <= 4 ? "объявления" : "объявлений"; };
const ageRu = d => (d == null ? "" : d < 1 ? "сегодня" : d < 2 ? "вчера" : d < 30 ? `${Math.floor(d)} дн. назад` : `${Math.round(d / 30)} мес. назад`);
const SHOW_ADS = 5;           // объявлений на товар сразу, остальные — по кнопке
const POLL_MS = 20000;        // перечитать сводку, пока сервер обновляет живую выдачу
const POLL_MAX = 9;           // ≈ 3 минуты, дальше — кнопка «Обновить»

// Сводка вверху: сколько конкурентов, свежесть данных, фоновое обновление.
function summary(d, onReload) {
  const box = el(`<section class="ms-main sm-sum"><h2 class="ms-h"></h2><p class="sm-sub"></p><small class="ms-age"></small></section>`);
  const n = d.n_active ?? d.total ?? 0;
  box.querySelector(".ms-h").textContent = `Сиденья: ${n} ${adsWord(n)} конкурентов`;
  const groups = d.groups ?? [];
  box.querySelector(".sm-sub").textContent = groups.length
    ? (d.status === "no_items" ? "Типовые запросы: " : "По нашим товарам: ") + groups.map(g => g.kind_ru).filter((v, i, a) => a.indexOf(v) === i).join(", ").toLowerCase()
    : "";
  const r = d.refresh ?? {}, h = d.data_health ?? {};
  const age = box.querySelector(".ms-age");
  const parts = [];
  if (r.running) parts.push(`🔄 Обновляю с Kleinanzeigen в фоне (${r.done ?? 0} из ${r.total ?? "?"})…`);
  else if (r.reason === "scout_running") parts.push("Сейчас идёт разведка scout — живую выдачу обновлю позже");
  if (h.stale && h.reason) parts.push(`Данные scout: ${h.reason}`);
  else if (h.age_days != null) parts.push(`Данные scout: ${ageRu(h.age_days)}`);
  if (r.errors?.length && !r.running) parts.push("Kleinanzeigen ответил не на все запросы — показаны накопленные данные");
  age.textContent = parts.join(" · ");
  if (!parts.length) age.remove();
  if (!r.running) {
    const b = el(`<button type="button" class="btn ms-act sm-reload">🔄 Обновить</button>`);
    b.addEventListener("click", onReload);
    box.appendChild(b);
  }
  return box;
}

// Тепловая карта всех конкурентов по сиденьям; нажатие на землю — фильтр объявлений ниже.
function heatSection(d, land, onPick) {
  const sec = el(`<section class="c-sec heat-sec"><h3>Где продают сиденья <small class="hs-sel"></small></h3><div class="hs-grid"></div><p class="hs-top"></p></section>`);
  const counts = new Map((d.lands ?? []).map(x => [x.bundesland, x.count]));
  sec.querySelector(".hs-grid").appendChild(heatGrid(counts, l => onPick(l === land ? null : l), land));
  const top = (d.lands ?? []).slice(0, 3).map(x => `${x.bundesland} ${x.count}${x.median_eur != null ? ` (≈${money(x.median_eur)})` : ""}`);
  sec.querySelector(".hs-top").textContent = top.length ? `Больше всего: ${top.join(" · ")}` : "Земли у объявлений не указаны.";
  const sel = sec.querySelector(".hs-sel");
  if (land) {
    sel.textContent = `${land} ✕`;
    sel.setAttribute("role", "button");
    sel.addEventListener("click", () => onPick(null));
  } else sel.remove();
  return sec;
}

// Полоса цен: P25–P75 залита, медиана — риска, наша цена — точка.
function priceBar(p, our) {
  if (p?.min == null || p?.max == null || p.max <= p.min) return null;
  const lo = Math.min(p.min, our ?? p.min), hi = Math.max(p.max, our ?? p.max);
  const pct = v => `${(((v - lo) / (hi - lo)) * 100).toFixed(1)}%`;
  const bar = el(`<div class="sm-bar" aria-hidden="true"><i class="sm-iqr"></i><i class="sm-med"></i></div>`);
  const iqr = bar.querySelector(".sm-iqr");
  iqr.style.left = pct(p.p25 ?? p.min);
  iqr.style.width = `calc(${pct(p.p75 ?? p.max)} - ${pct(p.p25 ?? p.min)})`;
  bar.querySelector(".sm-med").style.left = pct(p.median ?? p.min);
  if (our != null) {
    const dot = el(`<i class="sm-our"></i>`);
    dot.style.left = pct(our);
    bar.appendChild(dot);
  }
  const lab = el(`<div class="sm-barlab"><span></span><span></span></div>`);
  lab.children[0].textContent = money(lo);
  lab.children[1].textContent = money(hi);
  const w = el(`<div class="sm-barw"></div>`);
  w.append(bar, lab);
  return w;
}

function adRow(a) {
  const r = el(`<a class="ms-ad" role="link"><b class="ms-at"></b><span class="ms-ap"></span><small class="ms-am"></small></a>`);
  r.querySelector(".ms-at").textContent = a.title ?? "Без названия";
  r.querySelector(".ms-ap").textContent = money(a.price_eur);
  r.querySelector(".ms-am").textContent = [a.city, a.age_days != null ? ageRu(a.age_days) : a.posted_raw, a.active === false ? "снято" : "", a.source === "live" ? "свежая выдача" : ""].filter(Boolean).join(" · ");
  if (/^https:\/\//i.test(a.url ?? "")) {
    r.href = a.url;
    r.target = "_blank"; r.rel = "noopener";
    r.addEventListener("click", ev => { ev.preventDefault(); openLink(a.url); });
    r.insertAdjacentHTML("beforeend", `<span class="ms-open">Открыть ›</span>`);
  }
  return r;
}

// Карточка одного нашего товара-сиденья: вердикт по цене, полоса, города, конкуренты.
function groupCard(g, land) {
  const sec = el(`
    <section class="c-sec sm-g">
      <h3><span class="sm-kind"></span><small class="sm-n"></small></h3>
      <p class="sm-title"></p>
      <p class="sm-range"></p>
      <p class="ms-ours"></p>
      <div class="sm-cities"></div>
      <div class="ms-alist"></div>
    </section>`);
  sec.querySelector(".sm-kind").textContent = g.kind_ru;
  sec.querySelector(".sm-n").textContent = `${g.n_active ?? g.total} ${adsWord(g.n_active ?? g.total)}`;
  const title = sec.querySelector(".sm-title");
  title.textContent = g.default ? `Запрос «${g.phrases?.[0] ?? ""}»` : (g.price_eur != null ? `${g.title} · у нас ${money(g.price_eur)}` : g.title);
  const p = g.price ?? {};
  const range = sec.querySelector(".sm-range");
  if (p.median != null) range.textContent = `Обычно ${money(p.median)} · большинство ${money(p.p25)}–${money(p.p75)}`;
  else if (g.total) range.textContent = "Мало объявлений с ценой — цифры не считаю";
  else range.textContent = "Конкурентов по этим запросам не нашли";
  const bar = priceBar(p, g.default ? null : g.price_eur);
  if (bar) range.after(bar);
  const ours = sec.querySelector(".ms-ours");
  if (g.ours?.position_ru) {
    ours.textContent = g.ours.position_ru[0].toUpperCase() + g.ours.position_ru.slice(1);
    if (g.ours.verdict === "pricier") ours.classList.add("ms-up");
    else if (g.ours.verdict === "cheaper") ours.classList.add("ms-down");
  } else ours.remove();
  const cities = sec.querySelector(".sm-cities");
  (g.cities ?? []).slice(0, 3).forEach(c => {
    const r = el(`<div class="ms-city"><b></b><span></span></div>`);
    r.querySelector("b").textContent = c.city || (c.plz_area ? `район ${c.plz_area}` : "город не указан");
    r.querySelector("span").textContent = `${c.count} · ${c.median_eur != null ? `обычно ${money(c.median_eur)}` : "без цены"}`;
    cities.appendChild(r);
  });
  if (!cities.children.length) cities.remove();
  const list = sec.querySelector(".ms-alist");
  const ads = (g.listings ?? []).filter(a => !land || a.bundesland === land);
  const paint = all => {
    list.replaceChildren(...(all ? ads : ads.slice(0, SHOW_ADS)).map(adRow));
    if (!all && ads.length > SHOW_ADS) {
      const more = el(`<button type="button" class="btn ms-act sm-more"></button>`);
      more.textContent = `Показать все ${ads.length}`;
      more.addEventListener("click", () => paint(true));
      list.appendChild(more);
    }
  };
  if (ads.length) paint(false);
  else if (land && (g.listings ?? []).length) list.appendChild(el(`<p class="sm-none">В этой земле объявлений нет.</p>`));
  else list.remove();
  return sec;
}

export async function render(mount) {
  const root = el(`<div class="mseats"><div class="ms-busy"><p>⏳ Смотрю рынок по нашим сиденьям…</p></div></div>`);
  mount.replaceChildren(root);
  let land = null, data = null, polls = 0, timer = null;
  const alive = () => root.isConnected;

  const paint = () => {
    if (!alive() || !data) return;
    const out = [];
    out.push(summary(data, () => load(true)));
    if (data.message_ru) {
      const n = el(`<div class="ms-note"><p></p></div>`);
      n.classList.add(data.status === "no_data" ? "ms-warn" : "ms-empty");
      n.querySelector("p").textContent = data.message_ru;
      out.push(n);
    }
    if ((data.total ?? 0) > 0) out.push(heatSection(data, land, l => { land = l; paint(); }));
    (data.groups ?? []).forEach(g => out.push(groupCard(g, land)));
    const hint = el(`<a class="sm-search" href="#/scout/search">🔎 Найти другой товар</a>`);
    hint.addEventListener("click", ev => { ev.preventDefault(); root.closest(".mhub")?.querySelector('.mh-tab[data-key="search"]')?.click() ?? (location.hash = "#/scout/search"); });
    out.push(hint);
    root.replaceChildren(...out);
  };

  async function load(userAsked) {
    clearTimeout(timer);
    if (userAsked) { polls = 0; root.querySelector(".sm-reload")?.setAttribute("disabled", ""); }
    let d;
    try {
      // refresh=1 только на первом заходе и по кнопке: перечитывание сводки фон не перезапускает
      d = await api(`/api/ma/market/seats?refresh=${polls === 0 ? 1 : 0}&limit=30`);
    } catch (e) {
      if (!alive()) return;
      const nf = e.status === 404 && (!e.detail || e.detail === "Not Found");
      const n = el(`<div class="ms-note ms-err"><p></p><button type="button" class="btn ms-act">🔄 Повторить</button></div>`);
      n.querySelector("p").textContent = nf ? "Сводка по сиденьям ещё не подключена на сервере." : (e.network ? e.message : "Не получилось посмотреть рынок.");
      n.querySelector("button").addEventListener("click", () => load(true));
      if (data) { paint(); root.prepend(n); } else root.replaceChildren(n);
      return;
    }
    if (!alive()) return;
    data = d;
    paint();
    // фон ещё обновляет живую выдачу — перечитаем позже (только пока экран открыт)
    if (d?.refresh?.running && ++polls <= POLL_MAX) {
      timer = setTimeout(() => { if (alive()) load(false); }, POLL_MS);
    }
  }

  await load(false);
}

