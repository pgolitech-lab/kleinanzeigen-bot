// Общие хелперы для всех экранов.

export function el(html) {
  // ВАЖНО: html здесь должен быть СТАТИЧЕСКИМ. Динамические данные —
  // через .textContent на named-узлах после el().
  const tmpl = document.createElement("template");
  tmpl.innerHTML = html.trim();
  return tmpl.content.firstChild;
}

export function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function berlinTime(iso, fmt = "short") {
  // UTC ISO → "DD.MM HH:MM" (Europe/Berlin) для оператора.
  if (!iso) return "";
  try {
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(d.getTime())) return iso;
    const opts = fmt === "full"
      ? { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }
      : { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" };
    return new Intl.DateTimeFormat("ru-RU", { ...opts, timeZone: "Europe/Berlin" }).format(d);
  } catch (e) {
    return iso;
  }
}

export function setLoading(mount, message = "Загрузка…") {
  mount.replaceChildren(el(`<p class="text-muted py-3">${esc(message)}</p>`));
}

export function setError(mount, message) {
  const card = el(`<div class="alert alert-danger" role="alert"><strong>Ошибка:</strong> <span class="msg"></span></div>`);
  card.querySelector(".msg").textContent = String(message ?? "");
  mount.replaceChildren(card);
}


const _ACCT_COLORS = ["#0d6efd", "#198754", "#dc3545", "#fd7e14", "#6f42c1", "#20c997", "#d63384"];

export function accountColor(id) {
  const n = parseInt(id, 10);
  return _ACCT_COLORS[(isNaN(n) ? 0 : n) % _ACCT_COLORS.length];
}


// Чип состояния: kind ∈ red|grn|amb|blu|mut (см. .chip в app.css)
export function chip(kind, label) {
  const c = el(`<span class="chip"></span>`);
  c.classList.add(`c-${kind}`);
  c.textContent = label;
  return c;
}

// € с немецкой локалью; null → «—»
export function eur(n) {
  if (n == null) return "—";
  return n.toLocaleString("de-DE", { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + " €";
}

// Расшифровка ответа /send и /compose: сервер отвечает 200 и когда письмо НЕ
// ушло (режим выключен, защита от повтора, redirect на тестовый адрес).
// res: {status?, kind?, mode?}. Без status/kind честно говорим «проверьте ленту».
// level: ok | warn | bad.
export function sendOutcome(res) {
  const st = res?.status, kind = res?.kind, mode = res?.mode;
  if (kind === "skipped" && mode !== "disabled" && st !== "not_sent_disabled") {
    return { level: "warn", text: "⚠️ Уже было отправлено — повторная отправка заблокирована" };
  }
  if (st === "not_sent_disabled" || mode === "disabled") {
    return { level: "bad", text: "⛔ Не отправлено — режим отправки выключен" };
  }
  if (st === "sent_debug" || mode === "redirect") {
    return { level: "warn", text: "🧪 Ушло на тестовый адрес (режим redirect) — клиенту НЕ отправлено" };
  }
  if (st === "sent" || (kind === "sent" && mode === "production")) {
    return { level: "ok", text: "✅ Сообщение отправлено клиенту" };
  }
  if (st === "sending") {
    return { level: "warn", text: "⏳ Письмо уже отправляется (защита от повтора) — проверьте ленту через минуту" };
  }
  if (kind === "error") {
    return { level: "bad", text: "❌ Ошибка отправки — проверьте ленту" };
  }
  if (st == null && kind == null) {
    return { level: "warn", text: "📨 Сервер принял письмо — итог отправки смотрите в ленте" };
  }
  return { level: "bad", text: `⚠️ Отправка не подтверждена (статус: ${st ?? kind}) — проверьте ленту` };
}

// «1.200 €», «1 200,50 €», «VB 950» → число евро; не разобрать → null
export function parseEur(v) {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (v == null) return null;
  let s = String(v).replace(/[^\d.,]/g, "");
  if (!s) return null;
  // немецкий формат: точка — тысячи, запятая — дробь
  if (/,\d{1,2}$/.test(s)) s = s.replace(/\./g, "").replace(",", ".");
  else s = s.replace(/[.,](?=\d{3}(\D|$))/g, "").replace(",", ".");
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : null;
}
