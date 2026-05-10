// Settings screen — KV editor с per-field save.

import { api } from "../api.js?v=20260510-13";
import { el, esc, setLoading, setError } from "../utils.js?v=20260510-13";


const FIELDS = [
  // [key, label, kind, options?]
  ["send_mode", "Send mode", "radio", ["disabled", "redirect", "production"]],
  ["polling_paused", "Polling paused", "checkbox"],
  ["debug_email", "Debug email (для redirect mode)", "text"],
  ["gmail_poll_interval_sec", "Gmail poll interval (сек)", "number"],
  ["gmail_from_filter", "Gmail from filter", "text"],
  ["inquiry_max_age_days", "Inquiry max age (дней)", "number"],
  ["reminders_enabled", "Reminders enabled", "checkbox"],
  ["reminder_after_days", "Reminder after (дней)", "number"],
  ["telegram_authorized", "Telegram authorized IDs (CSV)", "text"],
  ["telegram_operator_dm_ids", "Telegram DM IDs (CSV)", "text"],
  ["max_discount_percent", "Max discount %", "number"],
  ["claude_model", "Claude model", "text"],
  ["chat_font_em", "Chat font em", "text"],
  ["api_balance_snapshot_usd", "API balance snapshot $", "text"],
  ["api_balance_snapshot_at", "API balance snapshot at (ISO)", "text"],
  ["anthropic_api_key", "Anthropic API key", "secret"],
  ["telegram_bot_token", "Telegram bot token", "secret"],
];


function fieldRow(key, label, kind, value, options) {
  const wrap = el(`<div class="mb-3"></div>`);

  const labelEl = el(`<label class="form-label small fw-semibold mb-1"></label>`);
  labelEl.textContent = label;
  wrap.appendChild(labelEl);

  let input;
  if (kind === "radio") {
    const group = el(`<div></div>`);
    options.forEach(opt => {
      const choice = el(`
        <div class="form-check form-check-inline">
          <input class="form-check-input" type="radio" name="${key}" value="${opt}" id="${key}_${opt}">
          <label class="form-check-label small" for="${key}_${opt}">${opt}</label>
        </div>
      `);
      const radio = choice.querySelector("input");
      if (value === opt) radio.checked = true;
      group.appendChild(choice);
    });
    input = group;
  } else if (kind === "checkbox") {
    input = el(`<div class="form-check"><input class="form-check-input" type="checkbox" id="${key}"></div>`);
    input.querySelector("input").checked = (value === "1");
  } else if (kind === "secret") {
    const masked = el(`
      <div class="d-flex gap-2 align-items-center">
        <span class="masked-display"></span>
        <button type="button" class="btn btn-sm btn-outline-secondary replace-btn">Заменить</button>
        <input type="password" class="form-control secret-input d-none" />
      </div>
    `);
    masked.querySelector(".masked-display").textContent = value || "(не задано)";
    masked.querySelector(".replace-btn").addEventListener("click", () => {
      masked.querySelector(".masked-display").classList.add("d-none");
      masked.querySelector(".replace-btn").classList.add("d-none");
      masked.querySelector(".secret-input").classList.remove("d-none");
    });
    input = masked;
  } else {
    input = el(`<input class="form-control" type="${kind === "number" ? "number" : "text"}" />`);
    input.value = value || "";
  }
  wrap.appendChild(input);

  const saveBar = el(`
    <div class="d-flex gap-2 mt-1 align-items-center">
      <button class="btn btn-sm btn-primary save-btn">💾 Сохранить</button>
      <span class="status-text small text-muted"></span>
    </div>
  `);
  wrap.appendChild(saveBar);

  saveBar.querySelector(".save-btn").addEventListener("click", async () => {
    const status = saveBar.querySelector(".status-text");
    status.textContent = "Сохраняю…";
    status.className = "status-text small text-muted";

    let valueToSend;
    if (kind === "radio") {
      valueToSend = input.querySelector("input:checked")?.value || "";
    } else if (kind === "checkbox") {
      valueToSend = input.querySelector("input").checked ? "1" : "0";
    } else if (kind === "secret") {
      valueToSend = input.querySelector(".secret-input").value;
      if (!valueToSend) {
        status.textContent = "Поле пустое — пропустил";
        status.className = "status-text small text-warning";
        return;
      }
    } else {
      valueToSend = input.value;
    }

    try {
      await api("/api/ma/settings", {
        method: "POST",
        body: {key, value: valueToSend},
      });
      status.textContent = "✅ Сохранено";
      status.className = "status-text small text-success";
    } catch (e) {
      status.textContent = `❌ ${e.message ?? "ошибка"}`;
      status.className = "status-text small text-danger";
    }
  });

  return wrap;
}


export async function render(mount, params) {
  setLoading(mount, "Загружаю настройки…");
  try {
    const data = await api("/api/ma/settings");
    const container = el(`
      <div>
        <h5 class="mb-3">⚙ Настройки</h5>
        <div class="fields-list"></div>
        <a class="btn btn-sm btn-outline-secondary mt-3" href="#/pipeline">↩ К pipeline</a>
      </div>
    `);
    const list = container.querySelector(".fields-list");
    FIELDS.forEach(([key, label, kind, options]) => {
      list.appendChild(fieldRow(key, label, kind, data[key] ?? "", options));
    });
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
