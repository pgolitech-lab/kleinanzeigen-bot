// 🔍 Найденные продажи (v3, итог QA «ленивый продажник»): ИИ нашёл в переписке
// похожие на продажу сделки — оператор подтверждает или отклоняет.
// «Точно / вероятно / сомнительно» вместо high/medium/low, дата по Берлину,
// «Принять все уверенные» одной кнопкой (по одному запросу на продажу).
import { api } from "../api.js";
import { el, setLoading, setError, eur, berlinTime, invalidateSoldToday } from "../utils.js";
import { confirmSheet } from "../components/sheet.js";
import { toast } from "../components/kit.js";

const CONF = {
  high: ["good", "точно"],
  medium: ["warn", "вероятно"],
  low: ["mut", "сомнительно"],
};

const apply = (id, price) => api(`/api/ma/detected-sales/${id}/apply`, { method: "POST", body: {price_eur: price} });
const reject = id => api(`/api/ma/detected-sales/${id}/reject`, { method: "POST" });

function card(d, onDone) {
  const c = el(`
    <article class="det">
      <div class="det-1"><b class="det-t"></b><span class="det-c"></span></div>
      <div class="det-meta"></div>
      <blockquote class="det-q"></blockquote>
      <div class="det-price">
        <label>Цена продажи, €</label>
        <input type="number" inputmode="decimal" min="0" step="1" class="form-control">
        <span class="det-disc"></span>
      </div>
      <div class="det-acts">
        <a class="btn det-open">Переписка</a>
        <button class="btn det-no" type="button">Не продано</button>
        <button class="btn btn-primary det-yes" type="button">✅ Продано</button>
      </div>
    </article>`);
  c.querySelector(".det-t").textContent = d.thread_ad_title || d.detected_ad_title || "(без названия)";
  const [cls, label] = CONF[d.confidence] ?? ["mut", "сомнительно"];
  const conf = c.querySelector(".det-c");
  conf.textContent = label;
  conf.classList.add(cls);
  const meta = [];
  if (d.detected_sold_at) meta.push(berlinTime(d.detected_sold_at) || d.detected_sold_at);
  if (d.buyer_display_name) meta.push(d.buyer_display_name);
  if (d.account_name) meta.push(d.account_name);
  if (d.thread_ad_price) meta.push(`в объявлении ${d.thread_ad_price}`);
  c.querySelector(".det-meta").textContent = meta.join(" · ");
  const q = c.querySelector(".det-q");
  const quote = d.evidence_ru || d.evidence;
  if (quote) q.textContent = `«${quote}»`; else q.remove();

  const input = c.querySelector("input");
  if (d.sold_price_eur != null) input.value = String(d.sold_price_eur);
  const disc = c.querySelector(".det-disc");
  const upd = () => {
    const p = Number(input.value);
    if (!input.value || !Number.isFinite(p) || !d.thread_ad_price_eur) { disc.textContent = ""; return; }
    const diff = d.thread_ad_price_eur - p;
    disc.textContent = diff > 0 ? `скидка ${eur(diff)}` : diff < 0 ? `+${eur(-diff)}` : "без скидки";
  };
  input.addEventListener("input", upd);
  upd();
  c.querySelector(".det-open").href = `#/thread/${encodeURIComponent(d.thread_id)}`;

  const btns = () => c.querySelectorAll("button");
  c.querySelector(".det-yes").addEventListener("click", async () => {
    const p = Number(input.value);
    if (!input.value || !Number.isFinite(p) || p < 0) { input.classList.add("is-invalid"); return; }
    btns().forEach(b => { b.disabled = true; });
    try {
      await apply(d.id, p);
      invalidateSoldToday();
      toast(`✅ Продажа ${eur(p)} записана`);
      onDone(d.id);
    } catch (e) {
      toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 });
      btns().forEach(b => { b.disabled = false; });
    }
  });
  c.querySelector(".det-no").addEventListener("click", async () => {
    btns().forEach(b => { b.disabled = true; });
    try {
      await reject(d.id);
      onDone(d.id);
    } catch (e) {
      toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 });
      btns().forEach(b => { b.disabled = false; });
    }
  });
  return c;
}

export async function render(mount) {
  setLoading(mount, "Ищу найденные продажи…");
  let data;
  try {
    data = await api("/api/ma/detected-sales");
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  let items = data.items ?? [];
  let bulkRunning = false;  // повторный тап во время пачки не повторяет apply

  const root = el(`<div class="detected"><div class="det-head"></div><div class="det-list"></div></div>`);
  const head = root.querySelector(".det-head");
  const list = root.querySelector(".det-list");

  function onDone(id) {
    items = items.filter(x => x.id !== id);
    list.querySelector(`[data-id="${CSS.escape(String(id))}"]`)?.remove();
    paintHead();
    if (!items.length) paintEmpty();
  }

  function paintEmpty() {
    list.replaceChildren(el(`<div class="empty"><p>Всё проверено — новых находок нет.</p></div>`));
  }

  function paintHead() {
    head.replaceChildren();
    const sure = items.filter(x => x.confidence === "high" && x.sold_price_eur != null);
    const t = el(`<div class="lsec"><span>На проверку</span><i></i></div>`);
    t.querySelector("i").textContent = String(items.length);
    head.appendChild(t);
    if (sure.length && !bulkRunning) {
      const sum = sure.reduce((a, x) => a + Number(x.sold_price_eur || 0), 0);
      const b = el(`<button class="btn btn-primary det-all" type="button"></button>`);
      b.textContent = `✅ Принять все уверенные: ${sure.length} · ${eur(sum)}`;
      b.addEventListener("click", async () => {
        if (!(await confirmSheet(`Записать ${sure.length} продаж на ${eur(sum)}?`, "Записать"))) return;
        if (bulkRunning) return;
        bulkRunning = true;
        b.disabled = true;
        // атомарно на сервере (одна транзакция); нет эндпоинта — по одной
        try {
          const r = await api("/api/ma/detected-sales/accept-confident", { method: "POST", body: { min_confidence: "high" } });
          const applied = r.applied ?? [], skipped = r.skipped ?? [];
          applied.forEach(a => onDone(a.id ?? a.detection_id ?? a));
          invalidateSoldToday();
          if (skipped.length) toast(`Записано ${applied.length}, пропущено ${skipped.length}: ${skipped.map(s => s.reason ?? s).join("; ")}`, { level: "bad", ms: 8000 });
          else toast(`✅ Записано продаж: ${applied.length}`);
          bulkRunning = false;
          return render(mount);
        } catch (e) {
          if (!(e.status === 404 && (!e.detail || e.detail === "Not Found"))) {
            toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 6000 });
            bulkRunning = false;
            b.disabled = false;
            return;
          }
        }
        let ok = 0;
        const failed = [];
        for (const d of sure) {
          try {
            await apply(d.id, Number(d.sold_price_eur));
            ok++;
            onDone(d.id);
          } catch (e) {
            failed.push(`«${d.thread_ad_title || "?"}»: ${e.message ?? e}`);
          }
        }
        if (ok) invalidateSoldToday();
        // один итоговый тост: иначе красные ошибки затирались зелёным «записано»
        if (failed.length) toast(`Записано ${ok}, не прошло ${failed.length}: ${failed.join("; ")}`, { level: "bad", ms: 8000 });
        else toast(`✅ Записано продаж: ${ok}`);
        bulkRunning = false;
      });
      head.appendChild(b);
    }
  }

  paintHead();
  if (!items.length) paintEmpty();
  items.forEach(d => {
    const c = card(d, onDone);
    c.dataset.id = String(d.id);
    list.appendChild(c);
  });
  mount.replaceChildren(root);
}
