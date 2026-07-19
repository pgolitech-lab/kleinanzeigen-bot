// 👤 Профиль клиента — CRM карточка: шапка, теги, заметка, треды с deal_brief.
import { api } from "../api.js?v=20260719-1";
import { el, berlinTime, setLoading, setError, chip } from "../utils.js?v=20260719-1";
import { setTitle } from "../components/backbar.js?v=20260719-1";

const ALLOWED_TAGS = ["Серьёзный", "Торгуется", "Тянет время", "Мошенник"];

const STATUS_LABELS = {
  sent: ["grn", "отправлен"],
  sent_debug: ["grn", "отправлен"],
  pending: ["amb", "ждёт"],
  new: ["amb", "ждёт"],
  edited: ["amb", "ждёт"],
  approved: ["amb", "ждёт"],
  skipped: ["mut", "пропущен"],
  skipped_sold: ["grn", "продан"],
  archived: ["mut", "архив"],
};

function statusChip(raw) {
  const [kind, label] = STATUS_LABELS[raw] ?? ["mut", raw ?? "статус"];
  return chip(kind, label);
}

function threadCard(t) {
  const card = el(`
    <a class="tcard">
      <div class="tc-m">
        <div class="tc-t"><b class="title"></b><span class="price"></span></div>
        <div class="tc-sub brief" style="white-space:normal"></div>
      </div>
      <div class="tc-side">
        <time class="when"></time>
        <span class="status-slot"></span>
      </div>
    </a>
  `);
  card.href = `#/thread/${encodeURIComponent(t.thread_id)}`;
  card.querySelector(".title").textContent = t.ad_title ?? "(без названия)";
  card.querySelector(".price").textContent = t.ad_price ?? "";
  card.querySelector(".when").textContent = berlinTime(t.last_at);
  card.querySelector(".status-slot").replaceWith(statusChip(t.last_status));

  if (t.deal_brief) {
    const b = t.deal_brief;
    const parts = [];
    if (b.summary_ru) parts.push(b.summary_ru);
    if (b.client_assessment) parts.push(b.client_assessment);
    if (b.negotiated_price_eur) parts.push(`${b.negotiated_price_eur} €`);
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

  if (data.display_name) setTitle(data.display_name);

  // Локальное состояние тегов (мутируется при тогглах)
  let currentTags = Array.isArray(data.tags) ? [...data.tags] : [];

  const root = el(`
    <div>
      <div class="tcard" style="cursor:default">
        <div class="tc-m">
          <div class="tc-t"><b class="name"></b></div>
          <div class="tc-sub email-line"></div>
          <div class="tc-sub stats" style="margin-top:5px"></div>
        </div>
      </div>

      <div class="d-flex flex-wrap gap-2 mb-2 mt-2 tag-buttons"></div>

      <textarea class="form-control mb-2 note-area" rows="2"
        placeholder="Заметка оператора…"></textarea>
      <div class="d-flex gap-2 mb-3">
        <button class="btn btn-sm save-btn">💾 Сохранить</button>
        <a class="btn btn-sm write-btn d-none">✉️ Написать</a>
      </div>

      <div class="sec">Переписки</div>
      <div class="thread-list"></div>
    </div>
  `);

  root.querySelector(".name").textContent = data.display_name || email;
  root.querySelector(".email-line").textContent = email;

  // Статистика
  const statParts = [`${data.threads.length} обращ.`, `${data.sold_count} продажа`];
  if (data.total_negotiated_eur) statParts.push(`${data.total_negotiated_eur} € итог`);
  root.querySelector(".stats").textContent = statParts.join(" · ");

  // Кнопки тегов
  const tagWrap = root.querySelector(".tag-buttons");
  ALLOWED_TAGS.forEach(tag => {
    const btn = el(`<button type="button" class="tagbtn"></button>`);
    btn.textContent = tag;
    if (currentTags.includes(tag)) btn.classList.add("on");
    btn.addEventListener("click", () => {
      if (currentTags.includes(tag)) {
        currentTags = currentTags.filter(t => t !== tag);
        btn.classList.remove("on");
      } else {
        currentTags.push(tag);
        btn.classList.add("on");
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
    const writeBtn = root.querySelector(".write-btn");
    writeBtn.classList.remove("d-none");
    writeBtn.href = `#/thread/${encodeURIComponent(data.last_active_thread_id)}`;
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
