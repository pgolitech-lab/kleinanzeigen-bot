// Атомы дизайна v3: SVG-иконки, аватар-инициалы, флаг языка, бейдж языка,
// тост. Разметка статичная; данные — только через textContent.
import { el, accountColor } from "../utils.js";

// Пути иконок (stroke, 24×24)
const P = {
  send: '<path d="M4 12 20 4l-6 16-3-7z"/><path d="M11 13l9-9"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  sold: '<path d="M20 6 9 17l-5-5"/>',
  pin: '<path d="M9 4h6l-1 6 4 3v1H6v-1l4-3z"/><path d="M12 14v6"/>',
  warn: '<path d="M12 3 2 20h20z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
  late: '<circle cx="12" cy="13" r="7.5"/><path d="M12 9.5V13l2.5 1.5"/><path d="M5 4 3 6"/><path d="M19 4l2 2"/>',
  phone: '<rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 18h2"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M6 6l12 12"/><path d="M18 6 6 18"/>',
  sel: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  spark: '<path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/><path d="m6 6 2.5 2.5"/><path d="M15.5 15.5 18 18"/><path d="m18 6-2.5 2.5"/><path d="M8.5 15.5 6 18"/>',
  cards: '<rect x="6" y="3" width="13" height="16" rx="3"/><path d="M4 7v11a3 3 0 0 0 3 3h9"/>',
  fire: '<path d="M12 21c4 0 7-2.7 7-7 0-3-2-5.5-3.5-7-.3 2-1.5 3-2.5 3 0-3-1.5-5.5-4-7 .2 3-1.5 5-3 7C4.8 11.6 5 13 5 14c0 4.3 3 7 7 7z"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  money: '<rect x="3" y="6" width="18" height="12" rx="2.5"/><circle cx="12" cy="12" r="2.5"/>',
  bot: '<rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 4v4"/><path d="M9 13h.01"/><path d="M15 13h.01"/>',
};

export function icon(name, cls = "") {
  const s = el(`<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"></svg>`);
  if (cls) s.classList.add(...cls.split(" ").filter(Boolean));
  s.innerHTML = P[name] ?? "";  // статичная разметка из словаря выше
  return s;
}

// Устойчивый оттенок по имени покупателя
function hue(name) {
  let h = 0;
  for (const ch of String(name ?? "")) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

export function initials(name) {
  const parts = String(name ?? "?").replace(/[^\p{L}\p{N}\s.-]/gu, "").trim().split(/[\s.]+/).filter(Boolean);
  if (!parts.length) return "?";
  return ((parts[0][0] ?? "") + (parts.length > 1 ? parts[parts.length - 1][0] : (parts[0][1] ?? ""))).toUpperCase();
}

// Аватар: инициалы на градиенте, кольцо — цвет аккаунта, флажок языка снизу
export function avatar(name, accountId, lang, size = "") {
  const a = el(`<span class="av"><span class="av-in"></span></span>`);
  if (size) a.classList.add(size);
  a.style.setProperty("--h", String(hue(name)));
  a.style.setProperty("--acc", accountId != null ? accountColor(accountId) : "var(--line2)");
  a.querySelector(".av-in").textContent = initials(name);
  const f = flag(lang);
  if (f) { f.classList.add("av-flag"); a.appendChild(f); }
  return a;
}

const FLAGS = new Set(["de", "pl", "nl", "tr", "fr", "en", "ru", "uk", "it", "es", "ro", "cs"]);
const LANG_NAMES = { de: "немецкий", pl: "польский", nl: "нидерландский", tr: "турецкий", fr: "французский",
  en: "английский", ru: "русский", uk: "украинский", it: "итальянский", es: "испанский", ro: "румынский", cs: "чешский" };

export function langName(code) {
  return LANG_NAMES[String(code ?? "").toLowerCase()] ?? String(code ?? "").toUpperCase();
}

// Флаг языка (CSS-полосы); неизвестный язык — текстовый код
export function flag(lang) {
  const code = String(lang ?? "").toLowerCase().slice(0, 2);
  if (!code) return null;
  const f = el(`<span class="flag"></span>`);
  if (FLAGS.has(code)) f.classList.add(`f-${code}`);
  else { f.classList.add("f-txt"); f.textContent = code.toUpperCase(); }
  f.title = langName(code);
  return f;
}

// Бейдж языка: ok → «DE ✓» зелёный, иначе «RU ✕» красный, null → «DE …» (проверяется)
export function langBadge(code, ok) {
  const b = el(`<span class="lang-badge"></span>`);
  b.textContent = String(code ?? "?").toUpperCase() + (ok === true ? " ✓" : ok === false ? " ✕" : " …");
  b.classList.add(ok === true ? "ok" : ok === false ? "no" : "wait");
  return b;
}

// Тост внизу экрана; action = {label, onClick} — кнопка (например «Отменить»)
let _toastTimer = null;
export function toast(text, { action = null, ms = 3200, level = "" } = {}) {
  document.querySelector(".toast")?.remove();
  clearTimeout(_toastTimer);
  const t = el(`<div class="toast" role="status"><span class="tt"></span></div>`);
  if (level) t.classList.add(level);
  t.querySelector(".tt").textContent = text;
  if (action) {
    const b = el(`<button class="ta"></button>`);
    b.textContent = action.label;
    b.addEventListener("click", () => { t.remove(); action.onClick(); });
    t.appendChild(b);
  }
  (document.getElementById("shell") ?? document.body).appendChild(t);
  requestAnimationFrame(() => t.classList.add("on"));
  _toastTimer = setTimeout(() => { t.classList.remove("on"); setTimeout(() => t.remove(), 300); }, ms);
}
