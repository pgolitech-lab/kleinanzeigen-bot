// Термометр торга (из концепции C, вид A): дорожка цены, красная зона ниже
// минимума, зелёная — минимум…цена объявления; маркеры: предложение клиента,
// наше последнее, цена из черновика ИИ (пунктир).
// d = {price, floor, offer, ours, sugg} — числа € (любые могут быть null).
// size: xs (строка списка) | lg (полоса сделки, с подписями).
import { el } from "../utils.js";

const fmt = n => Math.round(n).toLocaleString("de-DE");

export function hasThermo(d) {
  return d && d.price != null && (d.floor != null || d.offer != null || d.ours != null || d.sugg != null);
}

export function thermo(d, size = "xs") {
  const vals = [d.price, d.floor, d.offer, d.ours, d.sugg].filter(v => v != null);
  const lo = Math.min(...vals) * 0.9, hi = Math.max(...vals) * 1.03;
  const p = v => Math.max(0, Math.min(100, ((v - lo) / ((hi - lo) || 1)) * 100));
  const box = el(`<div class="scale" role="img"><div class="sc-track"></div></div>`);
  box.classList.add(size);
  const track = box.querySelector(".sc-track");
  const pp = p(d.price);
  const pf = d.floor != null ? p(d.floor) : null;
  if (pf != null) {
    const bad = el(`<span class="sc-bad"></span>`);
    bad.style.width = `${pf.toFixed(1)}%`;
    const good = el(`<span class="sc-good"></span>`);
    good.style.left = `${pf.toFixed(1)}%`;
    good.style.width = `${Math.max(0, pp - pf).toFixed(1)}%`;
    const tf = el(`<span class="sc-tick t-floor"></span>`);
    tf.style.left = `${pf.toFixed(1)}%`;
    track.append(bad, good, tf);
  }
  const tp = el(`<span class="sc-tick t-price"></span>`);
  tp.style.left = `${pp.toFixed(1)}%`;
  track.appendChild(tp);

  const offerBad = d.offer != null && d.floor != null && d.offer < d.floor;
  const mark = (v, cls) => {
    if (v == null) return;
    const m = el(`<span class="mk"></span>`);
    m.classList.add(...cls.split(" "));
    m.style.left = `${p(v).toFixed(1)}%`;
    box.appendChild(m);
  };
  mark(d.sugg, "m-sugg");
  mark(d.ours, "m-ours");
  mark(d.offer, offerBad ? "m-offer bad" : "m-offer");

  if (size === "lg") {
    const label = (v, cls, row) => {
      if (v == null) return;
      const l = el(`<span class="mk-l"></span>`);
      l.classList.add(row, ...cls.split(" "));
      l.style.setProperty("--x", p(v).toFixed(1));
      l.textContent = fmt(v);
      box.appendChild(l);
    };
    label(d.offer, offerBad ? "m-offer bad" : "m-offer", "top");
    label(d.ours, "m-ours", "top");
    label(d.floor, "m-floor", "bot");
    label(d.price, "m-price", "bot");
    label(d.sugg, "m-sugg", "bot");
  }
  const parts = [`цена ${fmt(d.price)} €`];
  if (d.floor != null) parts.push(`минимум ${fmt(d.floor)} €`);
  if (d.offer != null) parts.push(`клиент ${fmt(d.offer)} €`);
  if (d.ours != null) parts.push(`мы ${fmt(d.ours)} €`);
  box.setAttribute("aria-label", parts.join(", "));
  return box;
}
