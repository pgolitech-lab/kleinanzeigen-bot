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
