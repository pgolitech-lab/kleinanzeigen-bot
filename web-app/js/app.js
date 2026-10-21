// Mini App entry point. Bootstrap → router.start.

import { ready, startParam, openLink } from "./tg.js";
import { start as startRouter } from "./router.js";
import { mountTabbar } from "./components/tabbar.js";
import { mountBackbar } from "./components/backbar.js";
import { initTheme } from "./theme.js";
import { initLayout } from "./layout.js";

function applyStartParam() {
  // First try Telegram SDK (works for /start commands, startapp deep-links, etc.)
  let sp = startParam();
  // Fallback: read from URL search params (web_app button with ?tgWebAppStartParam=X)
  if (!sp) {
    try {
      const url = new URL(location.href);
      sp = url.searchParams.get("tgWebAppStartParam");
    } catch (e) {
      // ignore
    }
  }
  if (!sp) return;
  const m = sp.match(/^([a-z]+)(?:_(.+))?$/);
  if (m) {
    const [, screen, id] = m;
    location.hash = id ? `#/${screen}/${encodeURIComponent(id)}` : `#/${screen}`;
  }
}

// Внешние ссылки (<a target="_blank"> на объявления в треде, складе, рынке) — только через
// openLink. Без перехвата WebView Telegram открывал ссылку сам, а Android ещё раз
// перебрасывал её в приложение Kleinanzeigen — объявление открывалось дважды с задержкой.
// Фаза всплытия: экраны, которые сами зовут openLink с preventDefault, сюда не дублируются.
function interceptExternalLinks() {
  document.addEventListener("click", e => {
    const a = e.target.closest?.("a[href]");
    if (!a || e.defaultPrevented) return;
    if (!/^https?:\/\//i.test(a.getAttribute("href") || "")) return;   // #/… — навигация роутера
    e.preventDefault();
    openLink(a.href);
  });
}

function main() {
  ready();
  interceptExternalLinks();
  initTheme();
  initLayout();
  applyStartParam();
  const mount = document.getElementById("app");
  mountBackbar();
  startRouter(mount);
  mountTabbar();
}

main();
