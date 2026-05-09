// Mini App entry point. Phase 1: только bootstrap + /api/ma/health.

import { ready, user, startParam } from "./tg.js";
import { api, API_BASE } from "./api.js";

function el(html) {
  const tmpl = document.createElement("template");
  tmpl.innerHTML = html.trim();
  return tmpl.content.firstChild;
}

async function renderHealth(mount) {
  if (!API_BASE) {
    mount.replaceChildren(el(`
      <div>
        <h5>⚠️ API_BASE не настроен</h5>
        <p class="muted">Это ожидаемо в Phase 1 локально. После Task 5 (cloudflared) обновится.</p>
        <p>TG user (из initDataUnsafe):</p>
        <pre class="payload">${JSON.stringify(user(), null, 2)}</pre>
      </div>
    `));
    return;
  }
  try {
    mount.replaceChildren(el(`<p class="muted">Запрашиваю /api/ma/health…</p>`));
    const data = await api("/api/ma/health");
    mount.replaceChildren(el(`
      <div>
        <h5 class="ok">✅ Authenticated</h5>
        <p>${data.first_name ?? "?"} ${data.username ? `(@${data.username})` : ""}</p>
        <p class="muted">user_id: ${data.user_id}</p>
        <pre class="payload">${JSON.stringify(data, null, 2)}</pre>
      </div>
    `));
  } catch (e) {
    mount.replaceChildren(el(`
      <div>
        <h5 class="error">❌ Ошибка</h5>
        <pre class="payload">${e.message}</pre>
      </div>
    `));
  }
}

function main() {
  ready();
  const mount = document.getElementById("app");
  const sp = startParam();
  if (sp) {
    console.log("[ma] start_param:", sp);
  }
  renderHealth(mount);
}

main();
