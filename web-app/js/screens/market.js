// 🧭 Рынок-советник (над складом): дубли объявлений и перевыкладывание.
// Совет по цене и заметности одного товара — листы adviceSheet/tipsSheet (кнопки в карточке товара).
// Сервер только советует: снимает и выкладывает человек.
import { api } from "../api.js";
import { el, setLoading, setError, eur } from "../utils.js";
import { openSheet } from "../components/sheet.js";
import { toast } from "../components/kit.js";

const money = v => (v == null ? "—" : eur(Math.round(v)));
const text = (node, sel, t) => { node.querySelector(sel).textContent = t ?? ""; };

function adLine(ad, tag) {
  const a = el(`<a class="mk-ad" target="_blank" rel="noopener"><b class="mk-t"></b><small class="mk-m"></small></a>`);
  if (/^https:\/\//i.test(ad.url ?? "")) a.href = ad.url;
  text(a, ".mk-t", `${tag ? tag + " " : ""}${ad.title ?? ad.ad_id}`);
  text(a, ".mk-m", [ad.price_eur != null ? money(ad.price_eur) : "", ad.account, ad.city, ad.inquiries ? `писем: ${ad.inquiries}` : ""].filter(Boolean).join(" · "));
  return a;
}

// ---- совет по цене ----
export async function adviceSheet(productId) {
  let d;
  try { d = await api(`/api/ma/market/advice/${productId}`); }
  catch (e) { toast(`Совет: ${e.message ?? e}`, { level: "bad", ms: 5000 }); return; }
  const box = el(`<div class="mk-advice">
    <p class="mk-verdict"></p>
    <div class="c-tiles mk-tiles"></div>
    <div class="mk-sim"></div>
    <small class="mk-note"></small></div>`);
  text(box, ".mk-verdict", d.status === "few_data" ? "Мало данных по рынку" : (d.verdict_ru || ""));
  const tiles = box.querySelector(".mk-tiles");
  [["У нас", d.item?.price_eur], ["Выставить за", d.recommended_eur], ["Минимум", d.min_eur], ["Рынок (медиана)", d.median]]
    .filter(([, v]) => v != null).forEach(([k, v]) => {
      const t = el(`<div class="c-tile"><b></b><small></small></div>`);
      text(t, "small", k); text(t, "b", money(v));
      tiles.appendChild(t);
    });
  (d.similar ?? []).forEach(s => box.querySelector(".mk-sim").appendChild(adLine(s, s.active ? "" : "(ушло)")));
  const basis = { cost: "минимум = себестоимость + маржа", p25: "минимум = нижняя четверть рынка", cost_only: "минимум по себестоимости" }[d.min_basis];
  text(box, ".mk-note", [d.n != null ? `Сопоставимых: ${d.n}` : "", basis, d.excluded_own ? `наших объявлений не считали: ${d.excluded_own}` : ""].filter(Boolean).join(" · "));
  openSheet(box, "💶 Цена и рынок");
}

// ---- как стать заметнее ----
export async function tipsSheet(productId) {
  let d;
  try { d = await api(`/api/ma/market/tips/${productId}`); }
  catch (e) { toast(`Советы: ${e.message ?? e}`, { level: "bad", ms: 5000 }); return; }
  const box = el(`<div class="mk-tips"><div class="mk-miss"></div><div class="mk-titles"></div><div class="mk-first"></div><small class="mk-note"></small></div>`);
  const miss = box.querySelector(".mk-miss");
  const terms = d.missing_terms ?? [];
  if (terms.length) {
    miss.appendChild(el(`<h4>Есть у лидеров, нет у нас</h4>`));
    const row = el(`<div class="fchips"></div>`);
    terms.forEach(t => {
      const c = el(`<span class="fchip"></span>`);
      c.textContent = `${t.term} · ${Math.round(t.share * 100)} %`;
      row.appendChild(c);
    });
    miss.appendChild(row);
  } else miss.remove();
  const copyRow = (sel, head, arr) => {
    const host = box.querySelector(sel);
    if (!arr?.length) { host.remove(); return; }
    host.appendChild(el(`<h4></h4>`)).textContent = head;
    arr.forEach(t => {
      const b = el(`<button class="mk-copy" type="button"></button>`);
      b.textContent = t;
      b.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(t); toast("Скопировано"); } catch (e) { toast("Не удалось скопировать — выделите вручную", { level: "bad" }); }
      });
      host.appendChild(b);
    });
  };
  const sg = d.suggestions ?? {};
  copyRow(".mk-titles", "Варианты заголовка (тап — копировать)", sg.titles);
  copyRow(".mk-first", "Первая строка описания", sg.first_lines);
  const tl = d.title_len;
  text(box, ".mk-note", [tl ? `Длина заголовка: у нас ${tl.ours}${tl.leaders_median != null ? `, у лидеров ~${tl.leaders_median}` : ""}` : "", ...(sg.notes_ru ?? []), d.warning].filter(Boolean).join(" · "));
  openSheet(box, "👀 Как стать заметнее");
}

// ---- экран: дубли + перевыкладывание ----
function dupCard(g) {
  const c = el(`<article class="mk-card"><b class="mk-h"></b><p class="mk-msg"></p><div class="mk-ads"></div></article>`);
  c.classList.add(g.level === "danger" ? "bad" : "warn");
  text(c, ".mk-h", `${g.level === "danger" ? "🔴 Опасно" : "🟡 Проверьте"}: ${g.title}`);
  text(c, ".mk-msg", g.message_ru);
  const ads = c.querySelector(".mk-ads");
  if (g.keep) ads.appendChild(adLine(g.keep, "✅ оставить:"));
  (g.remove ?? []).forEach(a => ads.appendChild(adLine(a, "❌ снять:")));
  return c;
}

function repostCard(it) {
  const c = el(`<article class="mk-card"><b class="mk-h"></b><ul class="mk-why"></ul><small class="mk-win"></small><div class="mk-ads"></div><div class="mk-txt" hidden></div><ol class="mk-steps"></ol></article>`);
  text(c, ".mk-h", `${it.action === "repost" ? "🔁 Перевыложить" : "🧹 Сначала снять лишние"}: ${it.title}`);
  [].concat(it.reasons_ru ?? []).forEach(r => c.querySelector(".mk-why").appendChild(el(`<li></li>`)).textContent = r);
  if (it.window?.label_ru) text(c, ".mk-win", `Когда: ${it.window.label_ru}`);
  if (it.old_ad) c.querySelector(".mk-ads").appendChild(adLine(it.old_ad, "удалить старое:"));
  const t = it.text;
  if (t && it.action === "repost") {
    const box = c.querySelector(".mk-txt");
    box.hidden = false;
    box.appendChild(el(`<h4>Готовый текст</h4>`));
    // без ИИ текст собран по шаблону — честно говорим об этом
    const warn = t.warning || it.warning || (t.source && t.source !== "llm" ? "Текст собран по шаблону, без ИИ — проверьте заголовок и допишите описание." : "");
    if (warn) box.appendChild(el(`<p class="mk-warn small"></p>`)).textContent = `⚠️ ${warn}`;
    const parts = [["Заголовок", t.title], ["Описание", t.description]];
    parts.forEach(([k, v]) => {
      if (!v) return;
      const b = el(`<button class="mk-copy" type="button"><small></small><span></span></button>`);
      text(b, "small", `${k} — копировать`); text(b, "span", v);
      b.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(v); toast(`${k} скопирован`); } catch (e) { toast("Не удалось скопировать", { level: "bad" }); }
      });
      box.appendChild(b);
    });
    const pr = it.price?.suggested ?? t.price_eur;
    if (pr != null) box.appendChild(el(`<small></small>`)).textContent = `Цена: ${money(pr)}${it.price?.advice_ru ? " — " + it.price.advice_ru : ""}`;
    // контракт гибкий: строка → [строка], не-массив/пусто → []
    const chg = Array.isArray(t.changes_ru) ? t.changes_ru : (t.changes_ru ? [String(t.changes_ru)] : []);
    chg.forEach(x => box.appendChild(el(`<small class="mk-ch"></small>`)).textContent = `• ${x}`);
  }
  [].concat(it.steps_ru ?? []).forEach(s => c.querySelector(".mk-steps").appendChild(el(`<li></li>`)).textContent = s);
  if (!(it.reasons_ru ?? []).length) c.querySelector(".mk-why").remove();
  if (!(it.steps_ru ?? []).length) c.querySelector(".mk-steps").remove();
  return c;
}

export async function render(mount) {
  setLoading(mount, "Смотрю рынок…");
  let dup, rep;
  try {
    [dup, rep] = await Promise.all([api("/api/ma/market/duplicates"), api("/api/ma/market/repost?limit=5")]);
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const root = el(`<div class="market"><section class="c-sec mk-dup"><h3>Дубли объявлений</h3><p class="mk-sum"></p><div class="mk-list"></div></section><section class="c-sec mk-rep"><h3>Пора перевыложить</h3><div class="mk-list"></div></section></div>`);
  const groups = dup.groups ?? [];
  text(root, ".mk-dup .mk-sum", groups.length ? `Опасных: ${dup.danger ?? 0} · проверить: ${dup.warn ?? 0} · снять объявлений: ${dup.ads_to_remove ?? 0}` : "Дублей нет");
  groups.forEach(g => root.querySelector(".mk-dup .mk-list").appendChild(dupCard(g)));
  const items = rep.items ?? [];
  if (!items.length) root.querySelector(".mk-rep .mk-list").appendChild(el(`<div class="empty"><p>Перевыкладывать пока нечего</p></div>`));
  items.forEach(i => root.querySelector(".mk-rep .mk-list").appendChild(repostCard(i)));
  mount.replaceChildren(root);
}
