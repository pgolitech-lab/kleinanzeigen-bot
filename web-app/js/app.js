// Mini App entry point. Bootstrap → router.start.

import { ready, startParam } from "./tg.js?v=20260722-3";
import { start as startRouter } from "./router.js?v=20260722-3";
import { mountTabbar } from "./components/tabbar.js?v=20260722-3";
import { mountBackbar } from "./components/backbar.js?v=20260722-3";

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

function main() {
  ready();
  applyStartParam();
  const mount = document.getElementById("app");
  mountBackbar();
  startRouter(mount);
  mountTabbar();
}

main();
