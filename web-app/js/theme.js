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
    tg?.setHeaderColor?.("bg_color");
    tg?.setBackgroundColor?.("bg_color");
    tg?.setBottomBarColor?.("bg_color");
  } catch (e) { /* старый клиент Telegram — без раскраски шапки */ }
}

// Клавиатура: поле ввода в фокусе → body.kb (таб-бар прячется, места больше).
function isField(el) {
  return el && (el.tagName === "TEXTAREA" ||
    (el.tagName === "INPUT" && !["checkbox", "radio", "button", "submit"].includes(el.type)) ||
    el.isContentEditable);
}

export function initTheme() {
  applyScheme();
  tg?.onEvent?.("themeChanged", applyScheme);
  try { tg?.disableVerticalSwipes?.(); } catch (e) { /* Bot API < 7.7 */ }
  document.addEventListener("focusin", e => {
    if (isField(e.target)) document.body.classList.add("kb");
  });
  document.addEventListener("focusout", e => {
    if (!isField(e.target)) return;
    // фокус может сразу перейти в соседнее поле — проверяем на следующем тике
    setTimeout(() => {
      if (!isField(document.activeElement)) document.body.classList.remove("kb");
    }, 0);
  });
}
