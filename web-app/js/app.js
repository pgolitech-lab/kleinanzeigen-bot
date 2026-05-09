// Mini App entry point. Phase 1: только bootstrap + /api/ma/health.

import { ready, user, startParam } from "./tg.js";
import { api, API_BASE } from "./api.js";

function el(html) {
  // Возвращает корневой элемент шаблона. ВАЖНО: html здесь должен быть
  // СТАТИЧЕСКИМ — все динамические данные подставляем через textContent
  // на named-узлах после построения, чтобы избежать XSS.
  const tmpl = document.createElement("template");
  tmpl.innerHTML = html.trim();
  return tmpl.content.firstChild;
}

function renderApiBaseMissing(mount) {
  const card = el(`
    <div>
      <h5>⚠️ API_BASE не настроен</h5>
      <p class="muted">Это ожидаемо в Phase 1 локально. После Task 5 (cloudflared) обновится.</p>
      <p>TG user (из initDataUnsafe):</p>
      <pre class="payload"></pre>
    </div>
  `);
  card.querySelector(".payload").textContent = JSON.stringify(user(), null, 2);
  mount.replaceChildren(card);
}

function renderLoading(mount) {
  mount.replaceChildren(el(`<p class="muted">Запрашиваю /api/ma/health…</p>`));
}

function renderAuthenticated(mount, data) {
  const card = el(`
    <div>
      <h5 class="ok">✅ Authenticated</h5>
      <p class="who"></p>
      <p class="muted">user_id: <span class="uid"></span></p>
      <pre class="payload"></pre>
    </div>
  `);
  const who = `${data.first_name ?? "?"}${data.username ? ` (@${data.username})` : ""}`;
  card.querySelector(".who").textContent = who;
  card.querySelector(".uid").textContent = String(data.user_id ?? "");
  card.querySelector(".payload").textContent = JSON.stringify(data, null, 2);
  mount.replaceChildren(card);
}

function renderError(mount, message) {
  const card = el(`
    <div>
      <h5 class="error">❌ Ошибка</h5>
      <pre class="payload"></pre>
    </div>
  `);
  card.querySelector(".payload").textContent = String(message ?? "");
  mount.replaceChildren(card);
}

async function renderHealth(mount) {
  if (!API_BASE) {
    renderApiBaseMissing(mount);
    return;
  }
  try {
    renderLoading(mount);
    const data = await api("/api/ma/health");
    renderAuthenticated(mount, data);
  } catch (e) {
    renderError(mount, e.message);
  }
}

function main() {
  ready();
  const mount = document.getElementById("app");
  const sp = startParam();
  if (sp) {
    console.log("[ma] start_param:", sp);
  }
  renderHealth(mount).catch(e => {
    console.error("[ma] unhandled:", e);
    renderError(mount, `Внутренняя ошибка: ${e.message ?? e}`);
  });
}

main();
