// Mini App entry point. Bootstrap → router.start.

import { ready, startParam } from "./tg.js";
import { start as startRouter } from "./router.js";

function applyStartParam() {
  const sp = startParam();
  if (!sp) return;
  const m = sp.match(/^([a-z]+)_(.+)$/);
  if (m) {
    const [, screen, id] = m;
    location.hash = `#/${screen}/${encodeURIComponent(id)}`;
  }
}

function main() {
  ready();
  applyStartParam();
  const mount = document.getElementById("app");
  startRouter(mount);
}

main();
