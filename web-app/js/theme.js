// Тема и окружение Telegram WebApp: светлая/тёмная по tg.colorScheme,
// цвета шапки/фона клиента Telegram, отключение вертикального свайпа-закрытия
// (иначе свайпы карточек закрывают Mini App), флаг открытой клавиатуры.
import { tg } from "./tg.js";

function applyScheme() {
  let scheme = tg?.colorScheme;
  if (scheme !== "light" && scheme !== "dark") {
    // вне Telegram (браузер, QA) — по системной теме
    scheme = window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  // ?theme=light|dark — для проверки вёрстки (скриншоты)
  try {
    const forced = new URL(location.href).searchParams.get("theme");
    if (forced === "light" || forced === "dark") scheme = forced;
  } catch (e) { /* адрес не разобрать — оставляем тему клиента */ }
  document.documentElement.dataset.theme = scheme;
  try {
    // тёмная: свой мягкий цвет вместо bg Telegram (может быть #000)
    const c = scheme === "dark" ? "#1c2128" : "bg_color";
    tg?.setHeaderColor?.(c);
    tg?.setBackgroundColor?.(c);
    tg?.setBottomBarColor?.(c);
  } catch (e) { /* старый клиент Telegram — без раскраски шапки */ }
}

// Клавиатура: поле ввода в фокусе → body.kb (таб-бар прячется, места больше).
function isField(el) {
  return el && (el.tagName === "TEXTAREA" ||
    (el.tagName === "INPUT" && !["checkbox", "radio", "button", "submit"].includes(el.type)) ||
    el.isContentEditable);
}

// Экранная клавиатура: Telegram/браузер не всегда сжимают окно (iOS, Fold).
// visualViewport даёт настоящую видимую высоту: поднимаем шторки (--kb) и ужимаем оболочку (--app-h).
function syncViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const root = document.documentElement;
  const kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
  if (kb > 80) {
    root.style.setProperty("--kb", `${kb}px`);          // шторки поднимаем над клавиатурой
    root.style.setProperty("--app-h", `${Math.round(vv.height)}px`);
    root.style.setProperty("--kb-open", "1");
    if (window.scrollY || vv.offsetTop) window.scrollTo(0, 0);
  } else {
    root.style.removeProperty("--kb");
    root.style.removeProperty("--app-h");
    root.style.removeProperty("--kb-open");
  }
}

export function initTheme() {
  applyScheme();
  window.visualViewport?.addEventListener("resize", syncViewport);
  window.visualViewport?.addEventListener("scroll", syncViewport);
  tg?.onEvent?.("themeChanged", applyScheme);
  try { tg?.disableVerticalSwipes?.(); } catch (e) { /* Bot API < 7.7 */ }
  document.addEventListener("focusin", e => {
    if (!isField(e.target)) return;
    document.body.classList.add("kb");
    // после того как клавиатура выехала — показать поле над ней
    const t = e.target;
    [250, 600].forEach(ms => setTimeout(() => {
      if (document.activeElement === t) t.scrollIntoView({ block: "center", behavior: "smooth" });
    }, ms));
  });
  document.addEventListener("focusout", e => {
    if (!isField(e.target)) return;
    // фокус может сразу перейти в соседнее поле — проверяем на следующем тике
    setTimeout(() => {
      if (!isField(document.activeElement)) document.body.classList.remove("kb");
    }, 0);
  });
}
