// 📮 Аккаунты: 5 почт Kleinanzeigen. Вкл/выкл, авто-приветствие, проверка
// входа в Gmail (IMAP), смена паролей (только запись — сервер их не отдаёт),
// ручной бэкап со статусом. Пароли в логи и на экран не попадают.
import { api, LLM_TIMEOUT_MS } from "../api.js";
import { el, setLoading, setError, accountColor, berlinTime } from "../utils.js";
import { openSheet, closeSheet } from "../components/sheet.js";
import { toast } from "../components/kit.js";

function ago(sec) {
  if (sec == null) return "ни разу";
  if (sec < 3600) return `${Math.round(sec / 60)} мин назад`;
  if (sec < 86400) return `${Math.round(sec / 3600)} ч назад`;
  return `${Math.round(sec / 86400)} дн назад`;
}

function toggle(label, on, onChange) {
  const t = el(`<label class="tgl"><span class="tgl-l"></span><input type="checkbox" role="switch"><span class="tgl-s"></span></label>`);
  t.querySelector(".tgl-l").textContent = label;
  const cb = t.querySelector("input");
  cb.checked = !!on;
  cb.addEventListener("change", async () => {
    cb.disabled = true;
    try { await onChange(cb.checked); }
    catch (e) { cb.checked = !cb.checked; toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 }); }
    finally { cb.disabled = false; }
  });
  return t;
}

function passwordSheet(a, onDone) {
  const box = el(`
    <div class="pw-form">
      <label class="form-label small">Пароль приложения Gmail (16 букв)</label>
      <input type="password" class="form-control pw-g" autocomplete="new-password" placeholder="оставьте пустым — не менять">
      <button class="btn pw-test" type="button">Проверить вход</button>
      <div class="pw-res small"></div>
      <label class="form-label small mt-2">Почта Kleinanzeigen</label>
      <input type="email" class="form-control pw-ke" autocomplete="off">
      <label class="form-label small">Пароль Kleinanzeigen</label>
      <input type="password" class="form-control pw-kp" autocomplete="new-password" placeholder="оставьте пустым — не менять">
      <div class="form-error text-danger small" hidden></div>
      <div class="sheet-actions"><button class="btn cancel-btn" type="button">Отмена</button><button class="btn btn-primary save-btn" type="button">💾 Сохранить</button></div>
    </div>`);
  box.querySelector(".pw-ke").value = a.kleinanzeigen_email ?? "";
  const res = box.querySelector(".pw-res"), err = box.querySelector(".form-error");
  box.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  box.querySelector(".pw-test").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    res.textContent = "Проверяю…";
    try {
      const r = await api(`/api/ma/accounts/${a.id}/test-imap`, { method: "POST", body: { gmail_app_password: box.querySelector(".pw-g").value || null }, timeoutMs: LLM_TIMEOUT_MS });
      res.textContent = r.ok ? "✅ Вход в Gmail работает" : `❌ ${r.message ?? "не вошли"}`;
      res.className = `pw-res small ${r.ok ? "ok" : "bad"}`;
    } catch (e) {
      res.textContent = `❌ ${e.message ?? e}`;
    } finally { ev.currentTarget.disabled = false; }
  });
  box.querySelector(".save-btn").addEventListener("click", async ev => {
    const body = {};
    const g = box.querySelector(".pw-g").value, kp = box.querySelector(".pw-kp").value, ke = box.querySelector(".pw-ke").value.trim();
    if (g) body.gmail_app_password = g;
    if (kp) body.kleinanzeigen_password = kp;
    if (ke !== (a.kleinanzeigen_email ?? "")) body.kleinanzeigen_email = ke;
    if (!Object.keys(body).length) { closeSheet(); return; }
    ev.currentTarget.disabled = true;
    try {
      await api(`/api/ma/accounts/${a.id}`, { method: "POST", body });
      closeSheet();
      toast("✅ Сохранено");
      onDone();
    } catch (e) {
      err.textContent = e.message ?? String(e);
      err.hidden = false;
      ev.currentTarget.disabled = false;
    }
  });
  openSheet(box, `🔑 ${a.name}: доступы`);
}

// Факты продавца для «⚡ Быстрых ответов» в переписке (по-русски; ИИ напишет на языке покупателя)
async function quickInfoSheet(a) {
  const box = el(`
    <div class="pw-form">
      <label class="form-label small">Доставка</label>
      <input class="form-control qi-delivery" maxlength="500" placeholder="DHL 10 €, 2–3 дня">
      <label class="form-label small">Самовывоз</label>
      <input class="form-control qi-pickup" maxlength="300" placeholder="Köln, вечером после 18">
      <label class="form-label small">Оплата</label>
      <input class="form-control qi-payment" maxlength="300" placeholder="наличными или PayPal">
      <div class="form-error text-danger small" hidden></div>
      <div class="sheet-actions"><button class="btn cancel-btn" type="button">Отмена</button><button class="btn btn-primary save-btn" type="button">💾 Сохранить</button></div>
    </div>`);
  const err = box.querySelector(".form-error");
  const f = k => box.querySelector(`.qi-${k}`);
  box.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  try {
    const cur = await api(`/api/ma/accounts/${a.id}/quick-info`);
    ["delivery", "pickup", "payment"].forEach(k => { f(k).value = cur?.[k] ?? ""; });
  } catch (e) {
    err.textContent = `Не удалось загрузить: ${e.message ?? e}`;
    err.hidden = false;
    box.querySelector(".save-btn").disabled = true;  // иначе пустые поля затрут сохранённое
  }
  box.querySelector(".save-btn").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    try {
      await api(`/api/ma/accounts/${a.id}/quick-info`, { method: "POST", body: { delivery: f("delivery").value, pickup: f("pickup").value, payment: f("payment").value } });
      closeSheet();
      toast("✅ Сохранено");
    } catch (e) {
      err.textContent = e.message ?? String(e);
      err.hidden = false;
      ev.currentTarget.disabled = false;
    }
  });
  openSheet(box, `⚡ ${a.name}: быстрые ответы`);
}

export async function render(mount) {
  setLoading(mount, "Загружаю аккаунты…");
  let data, backup = null;
  try {
    [data, backup] = await Promise.all([
      api("/api/ma/accounts"),
      api("/api/ma/backup/status").catch(() => null),
    ]);
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  const root = el(`<div class="accounts"><div class="acc-list"></div><section class="c-sec bk"><h3>Резервная копия</h3><div class="bk-s"></div><button class="btn bk-run" type="button">💾 Сделать копию сейчас</button></section></div>`);
  const list = root.querySelector(".acc-list");
  (data.accounts ?? []).forEach(a => {
    const c = el(`
      <article class="acc-card">
        <div class="ac-head"><i class="ac-dot"></i><b class="ac-name"></b><span class="ac-st"></span></div>
        <div class="ac-mail"></div>
        <div class="ac-tgls"></div>
        <div class="ac-flags"></div>
        <div class="ac-acts"><button class="btn ac-test" type="button">Проверить вход</button><button class="btn ac-pw" type="button">🔑 Доступы</button><button class="btn ac-qi" type="button">⚡ Быстрые ответы</button></div>
        <div class="ac-res small"></div>
      </article>`);
    c.querySelector(".ac-dot").style.background = accountColor(a.id);
    c.querySelector(".ac-name").textContent = a.name;
    const st = c.querySelector(".ac-st");
    st.textContent = a.active ? "работает" : "выключен";
    st.classList.add(a.active ? "good" : "mut");
    c.querySelector(".ac-mail").textContent = a.gmail_email ?? "";
    const tg = c.querySelector(".ac-tgls");
    tg.appendChild(toggle("Принимать письма", a.active, async v => {
      await api(`/api/ma/accounts/${a.id}`, { method: "POST", body: { is_active: v } });
      st.textContent = v ? "работает" : "выключен";
      st.className = `ac-st ${v ? "good" : "mut"}`;
    }));
    tg.appendChild(toggle("Авто-приветствие новым покупателям", a.auto_ack_enabled, v =>
      api(`/api/ma/accounts/${a.id}`, { method: "POST", body: { auto_ack_enabled: v } })));
    const flags = c.querySelector(".ac-flags");
    flags.textContent = [
      a.gmail_app_password_set ? "пароль Gmail задан" : "⚠️ нет пароля Gmail",
      a.kleinanzeigen_email ? `Kleinanzeigen: ${a.kleinanzeigen_email}` : "почта Kleinanzeigen не задана",
    ].join(" · ");
    const res = c.querySelector(".ac-res");
    c.querySelector(".ac-test").addEventListener("click", async ev => {
      ev.currentTarget.disabled = true;
      res.textContent = "Проверяю вход в Gmail…";
      res.className = "ac-res small";
      try {
        const r = await api(`/api/ma/accounts/${a.id}/test-imap`, { method: "POST", body: {}, timeoutMs: LLM_TIMEOUT_MS });
        res.textContent = r.ok ? "✅ Вход в Gmail работает" : `❌ ${r.message ?? "не вошли"}`;
        res.classList.add(r.ok ? "ok" : "bad");
      } catch (e) {
        res.textContent = `❌ ${e.message ?? e}`;
        res.classList.add("bad");
      } finally { ev.currentTarget.disabled = false; }
    });
    c.querySelector(".ac-qi").addEventListener("click", () => quickInfoSheet(a));
    c.querySelector(".ac-pw").addEventListener("click", () => passwordSheet(a, () => render(mount)));
    list.appendChild(c);
  });
  if (!list.children.length) list.appendChild(el(`<div class="empty"><p>Аккаунтов нет</p></div>`));

  // ---- бэкап ----
  const bkS = root.querySelector(".bk-s"), bkBtn = root.querySelector(".bk-run");
  const paintBackup = st => {
    if (!st) { bkS.textContent = "Статус резервной копии недоступен"; return; }
    const parts = [`последняя полная копия: ${ago(st.backup_age_sec)}`];
    if (st.running) parts.push("⏳ идёт копирование…");
    else if (st.ok === false) parts.push(`❌ последний запуск с ошибкой (код ${st.exit_code})`);
    else if (st.ok === true && st.finished_at) parts.push(`✅ ручная копия ${berlinTime(st.finished_at)}`);
    bkS.textContent = parts.join(" · ");
    bkS.classList.toggle("bad", st.backup_age_sec == null || st.backup_age_sec > 2 * 86400 || st.ok === false);
    bkBtn.disabled = !!st.running;
  };
  paintBackup(backup);
  let poll = null;
  const pollBackup = () => {
    clearTimeout(poll);
    poll = setTimeout(async () => {
      if (!root.isConnected) return;
      try {
        const st = await api("/api/ma/backup/status");
        paintBackup(st);
        if (st.running) pollBackup();
      } catch (e) {
        bkS.textContent = `Статус недоступен: ${e.message ?? e} — откройте экран заново, чтобы проверить`;
        bkS.classList.add("bad");
        bkBtn.disabled = false;
      }
    }, 4000);
  };
  bkBtn.addEventListener("click", async () => {
    bkBtn.disabled = true;
    try {
      const st = await api("/api/ma/backup/run", { method: "POST" });
      paintBackup(st);
      if (!st.started) toast("Копирование уже идёт");
      pollBackup();
    } catch (e) {
      toast(`Ошибка: ${e.message ?? e}`, { level: "bad", ms: 5000 });
      bkBtn.disabled = false;
    }
  });
  if (backup?.running) pollBackup();
  mount.replaceChildren(root);
}
