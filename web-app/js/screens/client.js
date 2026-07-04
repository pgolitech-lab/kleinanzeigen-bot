// 👤 Профиль клиента — CRM карточка: шапка, теги, заметка, треды с deal_brief.
import { api } from "../api.js?v=20260704-203337";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260704-203337";

const ALLOWED_TAGS = ["Серьёзный", "Торгуется", "Тянет время", "Мошенник"];

const STATUS_LABELS = {
  sent: "отправлен",
  sent_debug: "отправлен",
  pending: "ждёт",
  new: "ждёт",
  edited: "ждёт",
  approved: "ждёт",
  skipped: "пропущен",
  skipped_sold: "продан",
  archived: "архив",
};

function statusLabel(raw) {
  return STATUS_LABELS[raw] ?? raw ?? "статус";
}

function threadCard(t) {
  const card = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1 me-2">
          <div class="title fw-semibold"></div>
          <div class="brief text-muted small mt-1 fst-italic"></div>
        </div>
        <div class="text-end small">
          <div class="when text-muted"></div>
          <span class="badge bg-secondary status mt-1"></span>
        </div>
      </div>
    </a>
  `);
  card.href = `#/thread/${encodeURIComponent(t.thread_id)}`;
  card.querySelector(".title").textContent =
    `${t.ad_title ?? "(без названия)"} · ${t.ad_price ?? "?"}`;
  card.querySelector(".when").textContent = berlinTime(t.last_at);
  card.querySelector(".status").textContent = statusLabel(t.last_status);

  if (t.deal_brief) {
    const b = t.deal_brief;
    const parts = [];
    if (b.summary_ru) parts.push(b.summary_ru);
    if (b.client_assessment) parts.push(b.client_assessment);
    if (b.negotiated_price_eur) parts.push(`${b.negotiated_price_eur}€`);
    if (parts.length) card.querySelector(".brief").textContent = parts.join(" · ");
  }
  return card;
}

export async function render(mount, params) {
  const email = params.email;
  setLoading(mount, `Загружаю профиль ${email}…`);
  let data;
  try {
    data = await api(`/api/ma/clients/${encodeURIComponent(email)}/history`);
  } catch (e) { setError(mount, e.message ?? String(e)); return; }

  // Локальное состояние тегов (мутируется при тогглах)
  let currentTags = Array.isArray(data.tags) ? [...data.tags] : [];

  const root = el(`
    <div>
      <div class="mb-3">
        <div class="fw-bold fs-5 name"></div>
        <div class="text-muted small email-line"></div>
        <div class="d-flex gap-3 mt-2 stats text-muted small"></div>
      </div>

      <div class="mb-2 tag-buttons d-flex flex-wrap gap-1"></div>

      <textarea class="form-control form-control-sm mb-1 note-area" rows="2"
        placeholder="Заметка оператора…"></textarea>
      <div class="d-flex justify-content-end mb-3">
        <button class="btn btn-sm btn-outline-primary save-btn">💾 Сохранить</button>
      </div>

      <div class="write-wrap mb-3 d-none">
        <a class="btn btn-sm btn-outline-success write-btn">✉️ Написать</a>
      </div>

      <div class="text-muted small fw-semibold mb-1">──── Переписки ────</div>
      <div class="list-group list-group-flush thread-list"></div>
    </div>
  `);

  root.querySelector(".name").textContent = `👤 ${esc(data.display_name || email)}`;
  root.querySelector(".email-line").textContent = email;

  // Статистика
  const stats = root.querySelector(".stats");
  stats.innerHTML = `
    <span>${data.threads.length} обращ.</span>
    <span>${data.sold_count} продажа</span>
    ${data.total_negotiated_eur ? `<span>${data.total_negotiated_eur}€ итог</span>` : ""}
  `;

  // Кнопки тегов
  const tagWrap = root.querySelector(".tag-buttons");
  ALLOWED_TAGS.forEach(tag => {
    const btn = el(`<button class="btn btn-sm"></button>`);
    btn.textContent = tag;
    const active = currentTags.includes(tag);
    btn.className = `btn btn-sm ${active ? "btn-secondary" : "btn-outline-secondary"}`;
    btn.addEventListener("click", () => {
      if (currentTags.includes(tag)) {
        currentTags = currentTags.filter(t => t !== tag);
        btn.className = "btn btn-sm btn-outline-secondary";
      } else {
        currentTags.push(tag);
        btn.className = "btn btn-sm btn-secondary";
      }
    });
    tagWrap.appendChild(btn);
  });

  // Заметка
  const noteArea = root.querySelector(".note-area");
  noteArea.value = data.note || "";

  // Кнопка Сохранить
  const saveBtn = root.querySelector(".save-btn");
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "⏳…";
    try {
      await api(`/api/ma/clients/${encodeURIComponent(email)}/profile`, {
        method: "POST",
        body: { tags: currentTags, note: noteArea.value },
      });
      saveBtn.textContent = "✅ Сохранено";
    } catch (e) {
      saveBtn.textContent = "❌ Ошибка";
    } finally {
      setTimeout(() => { saveBtn.disabled = false; saveBtn.textContent = "💾 Сохранить"; }, 1500);
    }
  });

  // Кнопка Написать
  if (data.last_active_thread_id) {
    const writeWrap = root.querySelector(".write-wrap");
    writeWrap.classList.remove("d-none");
    root.querySelector(".write-btn").href =
      `#/thread/${encodeURIComponent(data.last_active_thread_id)}`;
  }

  // Список тредов
  const list = root.querySelector(".thread-list");
  if (data.threads.length === 0) {
    list.appendChild(el(`<div class="text-muted fst-italic px-2 py-2">Нет переписок.</div>`));
  } else {
    data.threads.forEach(t => list.appendChild(threadCard(t)));
  }

  mount.replaceChildren(root);
}
