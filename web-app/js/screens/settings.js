// ⚙️ Настройки (v3): группы с русскими подписями, одна кнопка «Сохранить»
// для изменённых полей (POST /settings по ключу). Режим отправки «production»
// — с явным подтверждением (письма пойдут реальным покупателям).
// Секреты (токен бота) сервер отдаёт маской; пустое поле — не менять.
import { api } from "../api.js";
import { el, setLoading, setError } from "../utils.js";
import { confirmSheet } from "../components/sheet.js";
import { toast } from "../components/kit.js";

const SEND_MODES = [
  ["disabled", "Выключено", "ничего не отправляется — безопасно"],
  ["redirect", "На тестовый адрес", "письма уходят на адрес ниже, не покупателям"],
  ["production", "Покупателям", "письма уходят реальным покупателям"],
];

// [key, подпись, тип]
const GROUPS = [
  ["Отправка", [
    ["send_mode", "Режим отправки", "mode"],
    ["debug_email", "Тестовый адрес (для режима «на тестовый адрес»)", "text"],
  ]],
  ["Почта", [
    ["polling_paused", "Пауза приёма писем", "check"],
    ["gmail_poll_interval_sec", "Проверять почту каждые, сек", "number"],
    ["inquiry_max_age_days", "Не отвечать на письма старше, дней", "number"],
    ["gmail_from_filter", "Фильтр отправителя (служебное)", "text"],
  ]],
  ["Напоминания и торг", [
    ["reminders_enabled", "Предлагать напомнить молчащим покупателям", "check"],
    ["reminder_after_days", "Напоминать через, дней (0.5 = 12 ч)", "number"],
    ["max_discount_percent", "Максимальная скидка ИИ, %", "number"],
  ]],
  ["Telegram и служебное", [
    ["telegram_operator_dm_ids", "Операторы (Telegram ID через запятую)", "text"],
    ["telegram_admin_chat_id", "Служебный чат (Telegram ID — сюда идут ошибки и поломки)", "text"],
    ["telegram_authorized", "Кому можно открывать MA (ID через запятую)", "text"],
    ["claude_model", "Модель ИИ для черновиков", "text"],
    ["chat_font_em", "Размер шрифта чата в веб-морде, em", "text"],
    ["telegram_bot_token", "Токен бота", "secret"],
  ]],
];

export async function render(mount) {
  setLoading(mount, "Загружаю настройки…");
  let data;
  try {
    data = await api("/api/ma/settings");
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const initial = {}, current = {};
  const root = el(`<div class="settings"><div class="set-groups"></div>
    <div class="set-save"><span class="set-n"></span><button class="btn btn-primary" type="button" disabled>💾 Сохранить изменения</button></div></div>`);
  const groups = root.querySelector(".set-groups");
  const saveBtn = root.querySelector(".set-save button"), nEl = root.querySelector(".set-n");
  const changed = () => Object.keys(current).filter(k => current[k] !== initial[k]);
  const sync = () => {
    const n = changed().length;
    saveBtn.disabled = !n;
    nEl.textContent = n ? `изменено: ${n}` : "";
    root.querySelector(".set-save").classList.toggle("on", !!n);
  };
  const set = (k, v) => { current[k] = v; sync(); };

  GROUPS.forEach(([title, fields]) => {
    const sec = el(`<section class="c-sec"><h3></h3></section>`);
    sec.querySelector("h3").textContent = title;
    fields.forEach(([key, label, kind]) => {
      const raw = data[key] ?? "";
      initial[key] = kind === "secret" ? "" : String(raw);
      current[key] = initial[key];
      if (kind === "mode") {
        const box = el(`<div class="set-mode" role="radiogroup"></div>`);
        box.setAttribute("aria-label", label);
        SEND_MODES.forEach(([v, name, hint]) => {
          const opt = el(`<label class="sm-opt"><input type="radio" name="send_mode"><span class="sm-t"><b></b><small></small></span></label>`);
          opt.classList.add(`sm-${v}`);
          const r = opt.querySelector("input");
          r.value = v;
          r.checked = raw === v;
          opt.querySelector("b").textContent = name;
          opt.querySelector("small").textContent = hint;
          r.addEventListener("change", () => set(key, v));
          box.appendChild(opt);
        });
        sec.appendChild(box);
        return;
      }
      if (kind === "check") {
        const t = el(`<label class="tgl"><span class="tgl-l"></span><input type="checkbox" role="switch"><span class="tgl-s"></span></label>`);
        t.querySelector(".tgl-l").textContent = label;
        const cb = t.querySelector("input");
        cb.checked = raw === "1";
        cb.addEventListener("change", () => set(key, cb.checked ? "1" : "0"));
        sec.appendChild(t);
        return;
      }
      const l = el(`<label class="form-label small"></label>`);
      l.textContent = label;
      const inp = el(`<input class="form-control">`);
      inp.type = kind === "number" ? "number" : kind === "secret" ? "password" : "text";
      if (kind === "number") inp.step = "any";
      if (kind === "secret") {
        inp.placeholder = raw ? "задан — оставьте пустым, чтобы не менять" : "не задан";
        inp.autocomplete = "new-password";
      } else inp.value = String(raw);
      inp.addEventListener("input", () => set(key, inp.value));
      sec.append(l, inp);
    });
    groups.appendChild(sec);
  });

  saveBtn.addEventListener("click", async () => {
    const keys = changed();
    if (current.send_mode === "production" && keys.includes("send_mode") &&
        !(await confirmSheet("Включить отправку РЕАЛЬНЫМ покупателям?\nВсе одобренные ответы начнут уходить клиентам.", "Да, покупателям"))) return;
    saveBtn.disabled = true;
    let ok = 0;
    const failed = [];
    for (const k of keys) {
      // пустой секрет = «не менять»
      if (k === "telegram_bot_token" && !current[k]) { initial[k] = current[k]; continue; }
      try {
        await api("/api/ma/settings", { method: "POST", body: { key: k, value: current[k] } });
        initial[k] = current[k];
        ok++;
      } catch (e) {
        failed.push(`«${k}»: ${e.message ?? e}`);
      }
    }
    if (failed.length) toast(`Сохранено ${ok}, не сохранилось: ${failed.join("; ")}`, { level: "bad", ms: 8000 });
    else if (ok) toast(`✅ Сохранено: ${ok}`);
    sync();
  });
  sync();
  mount.replaceChildren(root);
}
