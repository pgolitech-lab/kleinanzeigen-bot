// Раскладка по размеру окна → html[data-layout]:
//   flex   — Flip 5 Flex Window (~306×340): «карман», одна карточка, без таб-бара;
//   phone  — обычный телефон / Fold внешний (280–412);
//   split2 — Fold внутренний ~820: список | открытое;
//   split3 — ≥ 900 (Fold внутренний ландшафт): список | тред | панель сделки.
// Клавиатура уменьшает высоту окна — пока поле в фокусе, раскладку не меняем,
// иначе на 280 px при наборе прыгали бы в «карман».
import { tg } from "./tg.js";

const listeners = new Set();
let current = null;

function compute() {
  const w = window.innerWidth;
  const h = Math.max(window.innerHeight, tg?.viewportStableHeight || 0);
  if (w >= 900) return "split3";
  if (w >= 760) return "split2";
  if (w <= 360 && h <= 440) return "flex";
  return "phone";
}

function update() {
  if (document.body.classList.contains("kb")) return;
  const next = compute();
  if (next === current) return;
  const prev = current;
  current = next;
  document.documentElement.dataset.layout = next;
  if (prev !== null) listeners.forEach(fn => fn(next, prev));
}

export function layout() {
  return current;
}

export function isSplit() {
  return current === "split2" || current === "split3";
}

// fn(next, prev) — при смене раскладки (складывание/раскладывание, поворот)
export function onLayoutChange(fn) {
  listeners.add(fn);
}

export function initLayout() {
  update();
  window.addEventListener("resize", update);
  tg?.onEvent?.("viewportChanged", e => { if (!e || e.isStateStable) update(); });
  document.addEventListener("focusout", () => setTimeout(update, 50));
}
