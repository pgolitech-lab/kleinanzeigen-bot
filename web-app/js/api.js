import { initData } from "./tg.js";

// Адрес бэкенда (cloudflared Quick Tunnel → FastAPI :8080). URL туннеля
// ротируется при рестарте cloudflared. Источники по приоритету:
//   1. ?api=<url> в адресе Mini App — бот подставляет актуальный туннель в
//      Menu Button и web_app-кнопки (свежий при каждом открытии);
//   2. sessionStorage — тот же ?api= в рамках открытого окна;
//   3. FALLBACK_API_BASE — последний известный адрес туннеля (запасной, если ?api= нет).
// TUNNEL_RE (только https://<имя>.trycloudflare.com) — фильтр от мусора, НЕ
// защита: такой поддомен анонимно получит кто угодно. Защищает то, что initData
// подписана токеном нашего бота и выдаётся только его кнопками. Остаточный
// риск — старый ?api= в старой кнопке, если имя туннеля достанется чужому;
// полностью снимает named tunnel со своим доменом (тогда ?api= не нужен).
//
// NB: backend должен отдавать Access-Control-Allow-Origin для github.io
// (CORSMiddleware). X-Telegram-Init-Data — non-simple header → всегда preflight.
const FALLBACK_API_BASE = "https://contrast-push-eligible-neighbor.trycloudflare.com";
const TUNNEL_RE = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/;
const API_BASE_KEY = "kab_api_base";

function resolveApiBase() {
  let fromUrl = null;
  try {
    fromUrl = new URL(location.href).searchParams.get("api");
  } catch (e) {
    console.warn("[api] не разобрать location.href:", e);
  }
  if (fromUrl) {
    if (TUNNEL_RE.test(fromUrl)) {
      try { sessionStorage.setItem(API_BASE_KEY, fromUrl); } catch (e) { /* приватный режим — ок, URL уже есть */ }
      return fromUrl;
    }
    console.warn("[api] ?api= отклонён (не *.trycloudflare.com):", fromUrl);
  }
  try {
    const saved = sessionStorage.getItem(API_BASE_KEY);
    if (saved && TUNNEL_RE.test(saved)) return saved;
  } catch (e) { /* storage недоступен — берём зашитый */ }
  return FALLBACK_API_BASE;
}

export const API_BASE = resolveApiBase();

// Понятная инструкция вместо технической ошибки/вечной загрузки, когда
// Telegram не передал сессию (initData пустой) или сервер её отверг.
const SESSION_LOST_MSG =
  "Сессия Telegram потеряна. Закройте мини-приложение и откройте заново " +
  "через кнопку меню в боте.";

// Таймаут по умолчанию. Покрывает и загрузку экранов (GET, <1с при живой сети),
// и LLM-вызовы (Haiku/Sonnet, обычно <25с). Без него запрос, не дошедший до
// сервера (сеть клиента моргнула / старый кэш стучится в никуда), висел вечно —
// экран залипал на «Загрузка…»/«Открываю карточку…».
const DEFAULT_TIMEOUT_MS = 30000;
// Для LLM-вызовов (генерация ответа Sonnet, автопилот-превью с веб-поиском) —
// они легитимно идут дольше 30с. Передаётся явно в такие call-sites.
export const LLM_TIMEOUT_MS = 120000;

export async function api(path, { method = "GET", body = null, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS, signal = null } = {}) {
  const url = `${API_BASE}${path}`;
  const id = initData();
  // initData пустой — Telegram потерял контекст WebApp (обычно при смене сети
  // или пробуждении устройства). Запрос ушёл бы с пустым заголовком, Cloudflare
  // его отбрасывает, сервер отвечает 422 → экран вис на «Загрузка». Не шлём.
  if (!id) {
    throw new Error(SESSION_LOST_MSG);
  }
  const reqHeaders = {
    "X-Telegram-Init-Data": id,
    ...headers,
  };
  // Content-Type только когда есть body (RFC 7231: GET без body не должен иметь
  // Content-Type — некоторые строгие сервера ругаются, плюс лишний preflight).
  if (body !== null) {
    reqHeaders["Content-Type"] = "application/json";
  }
  const opts = { method, headers: reqHeaders };
  if (body !== null) opts.body = JSON.stringify(body);

  // Таймаут: без него мёртвый/недоступный запрос висит бесконечно.
  const ctrl = new AbortController();
  opts.signal = ctrl.signal;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  if (signal) signal.aborted ? ctrl.abort() : signal.addEventListener("abort", () => ctrl.abort(), { once: true });
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    if (e.name === "AbortError") {
      if (signal?.aborted) { const ae = new Error("Отменено"); ae.aborted = true; throw ae; }
      throw new Error(
        "Сервер не ответил вовремя. Проверьте связь и переоткройте " +
        "мини-приложение через кнопку меню в боте.");
    }
    // Сетевая ошибка (CORS, offline, мёртвый тоннель) — fetch реджектит TypeError.
    // Чаще всего это сменившийся адрес туннеля: свежий ?api= приходит только
    // с кнопкой меню бота.
    const ne = new Error(
      "Нет связи с сервером. Проверьте интернет и нажмите «Повторить». " +
      "Если не помогает — закройте мини-приложение и откройте его кнопкой меню бота (адрес мог смениться).");
    ne.network = true;
    throw ne;
  } finally {
    clearTimeout(timer);
  }
  // 401 — initData истекла/плохой hash; 422 — заголовок не дошёл (пустой →
  // срезан прокси). Оба = проблема сессии: показываем инструкцию, а НЕ
  // закрываем МА резко (иначе на гонке при старте окно мигает и закрывается).
  if (res.status === 401) {
    throw new Error(SESSION_LOST_MSG);
  }
  if (res.status === 422) {
    // 422 от pydantic (detail — массив ошибок полей) — это ошибка данных, а не сессии;
    // 422 без тела/заголовка — initData не дошла
    const body = await res.clone().json().catch(() => null);
    if (!Array.isArray(body?.detail)) throw new Error(SESSION_LOST_MSG);
    const err = new Error("Проверьте данные: " + body.detail.map(d => d.msg || d.type).join("; "));
    err.status = 422;
    err.detail = body.detail;
    throw err;
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    // FastAPI кладёт причину в {"detail": "..."} — показываем её, а не сырой JSON.
    // В Error сохраняем status/detail: экраны различают 409 и прочее по e.status.
    let detail = null;
    try {
      const j = JSON.parse(text);
      if (typeof j?.detail === "string") detail = j.detail;
      else if (j?.detail && typeof j.detail === "object") detail = j.detail.message || j.detail.msg || JSON.stringify(j.detail);
    } catch (e) { /* не JSON — оставляем text */ }
    // 409 без причины — состояние за это время изменилось (ушло, в очереди, чужой лок)
    const fallback = res.status === 409 ? "Состояние изменилось, пока вы смотрели: письмо уже ушло, стоит в очереди или занято другим оператором. Обновите экран." : `HTTP ${res.status}: ${text}`;
    const human = res.status >= 500 ? "Сервер сейчас не справился. Нажмите «Повторить» через минуту." : null;
    const err = new Error(detail ?? human ?? fallback);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  // 204 No Content (например /lock/release, /reject) — нет JSON body
  if (res.status === 204 || res.headers.get("content-length") === "0") {
    return null;
  }
  return res.json();
}
