# QA-прогон Mini App по матрице устройств (headless Chromium, один браузер).
# - Telegram SDK подменён заглушкой с валидным initData (фейковый токен, см. seed.py).
# - GET к API → прокси на локальный QA-бэкенд (serve.py), любой не-GET → фейк 200 {"ok":true} + запись в лог.
# Запуск: python3 run.py <out_dir> [api_port=8798] [static_port=8799] [devices=all]
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import initdata  # noqa: E402

OUT = Path(sys.argv[1])
API_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8798
STATIC = f"http://127.0.0.1:{int(sys.argv[3]) if len(sys.argv) > 3 else 8799}"
ONLY = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] != "all" else None

# Размеры: CSS-пиксели вьюпорта Mini App (экран / DPR минус статус-бар и шапка Telegram).
DEVICES = {
    "fold-cover-412":   dict(w=412, h=860, dpr=2.625),   # Fold8 Ultra внешний 1080×2520
    "fold-cover-344":   dict(w=344, h=760, dpr=3.14),    # тот же экран при крупном масштабе интерфейса
    "fold-cover-280":   dict(w=280, h=620, dpr=3.86),    # худший случай
    "fold-inner-port":  dict(w=860, h=880, dpr=2.625),   # Fold8 Ultra внутренний 2256×2504
    "fold-inner-land":  dict(w=954, h=780, dpr=2.625),
    "flip5-main":       dict(w=412, h=915, dpr=2.625),   # Flip5 основной 1080×2640
    "flip5-flex":       dict(w=306, h=318, dpr=2.35),    # Flip5 Flex Window 720×748
}

TG_STUB = """
(() => {
  const log = (k, a) => { (window.__tg_log = window.__tg_log || []).push([k, a]); };
  const backHandlers = [];
  const BackButton = { isVisible: false,
    show() { this.isVisible = true; log('BackButton.show'); }, hide() { this.isVisible = false; },
    onClick(h) { backHandlers.push(h); }, offClick(h) { const i = backHandlers.indexOf(h); if (i >= 0) backHandlers.splice(i, 1); } };
  window.__tgBack = () => backHandlers.slice().forEach(h => h());
  const noopObj = new Proxy({}, { get: (t, p) => (...a) => { log('noop.' + String(p), a); return noopObj; } });
  const base = {
    initData: %INITDATA%, initDataUnsafe: { user: %USER%, start_param: null },
    version: '8.0', platform: 'android', colorScheme: 'dark', themeParams: {}, isExpanded: true,
    viewportHeight: innerHeight, viewportStableHeight: innerHeight,
    ready() {}, expand() {}, close() { log('close'); },
    BackButton, MainButton: noopObj, SecondaryButton: noopObj, SettingsButton: noopObj,
    HapticFeedback: noopObj, CloudStorage: noopObj,
    showConfirm(m, cb) { log('showConfirm', m); cb && cb(true); },
    showAlert(m, cb) { log('showAlert', m); cb && cb(); },
    showPopup(p, cb) { log('showPopup', p); cb && cb((p.buttons && p.buttons[0] && p.buttons[0].id) || 'ok'); },
    openLink(u) { log('openLink', u); }, openTelegramLink(u) { log('openTelegramLink', u); },
    onEvent() {}, offEvent() {},
  };
  window.Telegram = { WebApp: new Proxy(base, { get: (t, p) => (p in t ? t[p] : (log('missing.' + String(p)), (...a) => undefined)) }) };
})();
"""

# Чек-скрипт: горизонтальная прокрутка, вылезающие элементы, мелкие тап-цели, обрезанный текст.
CHECK_JS = """
() => {
  const W = innerWidth, H = innerHeight;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const label = e => (e.getAttribute('aria-label') || e.innerText || e.value || e.placeholder || e.className || e.tagName).toString().trim().replace(/\\s+/g,' ').slice(0, 40);
  const hscroll = document.scrollingElement.scrollWidth - W;
  const overflow = [];
  document.querySelectorAll('body *').forEach(e => { if (!vis(e)) return; const r = e.getBoundingClientRect();
    if (r.right > W + 2 && r.width < W * 3) {
      // элемент внутри горизонтально-скроллящегося контейнера — не баг
      let p = e.parentElement, inScroller = false;
      while (p && p !== document.body) { const s = getComputedStyle(p).overflowX; if ((s === 'auto' || s === 'scroll') && p.scrollWidth > p.clientWidth) { inScroller = true; break; } p = p.parentElement; }
      if (!inScroller) overflow.push(`${e.tagName.toLowerCase()}.${(e.className||'').toString().split(' ')[0]} «${label(e)}» right=${Math.round(r.right)}`); } });
  const small = [];
  document.querySelectorAll('button, a[href], [role=button], input:not([type=hidden]), select, textarea, .chip[onclick], [data-dt], [data-tab]').forEach(e => {
    if (!vis(e)) return; const r = e.getBoundingClientRect();
    if (e.type === 'checkbox' || e.type === 'radio') { const lab = e.id && document.querySelector(`label[for="${e.id}"]`); if (lab && lab.getBoundingClientRect().height >= 44) return; }
    if (r.height < 44 || r.width < 44) small.push(`${e.tagName.toLowerCase()} «${label(e)}» ${Math.round(r.width)}×${Math.round(r.height)}`); });
  const trunc = [];
  document.querySelectorAll('body *').forEach(e => { if (!vis(e) || e.children.length > 2) return; const s = getComputedStyle(e);
    if ((s.textOverflow === 'ellipsis' || s.overflow === 'hidden' || s.webkitLineClamp !== 'none') && (e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 2) && e.innerText.trim())
      trunc.push(`${e.tagName.toLowerCase()}.${(e.className||'').toString().split(' ')[0]} «${label(e)}»`); });
  const back = document.querySelector('header .hback');
  return { W, H, hscroll, overflow: [...new Set(overflow)].slice(0, 12), overflowN: overflow.length,
           small: [...new Set(small)].slice(0, 20), smallN: small.length, trunc: [...new Set(trunc)].slice(0, 12), truncN: trunc.length,
           backVisible: !!(back && vis(back)), tgBack: window.Telegram.WebApp.BackButton.isVisible,
           title: (document.querySelector('#htitle')||{}).innerText, text: document.querySelector('#app').innerText.slice(0, 200) };
}
"""

TAB_SCREENS = ["pipeline", "dashboard", "clients", "sales", "scout", "detected", "settings"]
DEEP_SCREENS = [("thread-draft", "#/thread/qa-t1"), ("thread-long", "#/thread/qa-t2"), ("thread-pl", "#/thread/qa-t3"),
                ("thread-new", "#/thread/qa-t6"), ("thread-answered", "#/thread/qa-t4"), ("client", "#/client/klausm.%40example.invalid"), ("review", "#/review/1")]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        for dev, d in DEVICES.items():
            if ONLY and dev not in ONLY:
                continue
            print("==", dev, flush=True)
            results[dev] = run_device(browser, dev, d)
            (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
        browser.close()
    print("done", OUT / "results.json")


def run_device(browser, dev, d):
    shots = OUT / "shots" / dev
    shots.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": d["w"], "height": d["h"]}, device_scale_factor=1,
                              is_mobile=True, has_touch=True, locale="ru-RU", timezone_id="Europe/Berlin",
                              color_scheme="dark")
    ev = {"errors": [], "http": [], "nonget": [], "dialogs": []}
    stub = TG_STUB.replace("%INITDATA%", json.dumps(initdata.make())).replace("%USER%", json.dumps(initdata.USER))
    ctx.route("https://telegram.org/js/telegram-web-app.js*",
              lambda r: r.fulfill(status=200, content_type="application/javascript", body=stub))
    cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}

    def api_route(route):
        req = route.request
        path = re.sub(r"^https://[^/]+", "", req.url)
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=cors)
        if req.method != "GET":
            ev["nonget"].append({"method": req.method, "path": path, "body": (req.post_data or "")[:300], "screen": cur["s"]})
            return route.fulfill(status=200, headers=cors, content_type="application/json", body='{"ok":true}')
        r = urllib.request.Request(f"http://127.0.0.1:{API_PORT}{path}", headers={"X-Telegram-Init-Data": initdata.make()})
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                body, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            body, status = e.read(), e.code
        if status >= 400:
            ev["http"].append({"status": status, "path": path, "body": body[:200].decode("utf-8", "replace"), "screen": cur["s"]})
        route.fulfill(status=status, headers=cors, content_type="application/json", body=body)

    ctx.route(re.compile(r"https://[^/]+\.trycloudflare\.com/.*"), api_route)
    # Всё прочее внешнее (кроме CDN) — не пускаем, логируем
    page = ctx.new_page()
    page.set_default_timeout(8000)
    cur = {"s": "boot"}
    page.on("pageerror", lambda e: ev["errors"].append({"screen": cur["s"], "type": "pageerror", "msg": str(e)[:300]}))
    page.on("console", lambda m: m.type == "error" and ev["errors"].append({"screen": cur["s"], "type": "console", "msg": m.text[:300]}))
    page.on("response", lambda r: (r.status >= 400 and "trycloudflare" not in r.url) and ev["http"].append(
        {"status": r.status, "path": r.url, "screen": cur["s"]}))
    page.on("requestfailed", lambda r: ev["http"].append({"status": "failed", "path": r.url, "err": r.failure, "screen": cur["s"]}))
    page.on("dialog", lambda dl: (ev["dialogs"].append({"screen": cur["s"], "type": dl.type, "msg": dl.message[:200]}), dl.accept()))

    screens = {}

    def snap(name, full=True):
        cur["s"] = name
        page.wait_for_timeout(700)
        try:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=full)
        except Exception as e:  # noqa: BLE001
            ev["errors"].append({"screen": name, "type": "screenshot", "msg": str(e)[:200]})
        screens[name] = page.evaluate(CHECK_JS)

    def fresh(hash_):
        cur["n"] = cur.get("n", 0) + 1  # новый query → настоящая перезагрузка, а не смена hash
        page.goto(f"{STATIC}/index.html?r={cur['n']}#/pipeline", wait_until="networkidle")
        page.wait_for_timeout(800)
        go(hash_)

    def go(hash_):
        page.evaluate("h => { location.hash = h; }", hash_)
        page.wait_for_timeout(1200)

    cur["s"] = "boot"
    page.goto(f"{STATIC}/index.html#/pipeline", wait_until="networkidle")
    page.wait_for_timeout(1500)
    for s in TAB_SCREENS:
        cur["s"] = s
        go(f"#/{s}")
        snap(s)
    for name, h in DEEP_SCREENS:
        cur["s"] = name
        go("#/pipeline")
        go(h)
        snap(name)
        # «Назад»: кнопка в шапке, иначе Telegram BackButton
        before = page.evaluate("location.hash")
        back = page.locator("header .hback")
        how = None
        if back.count() and back.is_visible():
            back.click(); how = "header"
        elif page.evaluate("window.Telegram.WebApp.BackButton.isVisible"):
            page.evaluate("window.__tgBack()"); how = "tg"
        page.wait_for_timeout(500)
        screens[name]["back"] = {"how": how, "from": before, "to": page.evaluate("location.hash")}

    # Таб-бар: все ли вкладки видны и кликабельны
    cur["s"] = "tabbar"
    go("#/pipeline")
    screens["tabbar"] = page.evaluate("""() => [...document.querySelectorAll('nav a, .tabbar a, .tabbar button, nav button')].map(a => {
        const r = a.getBoundingClientRect(); return {t: a.innerText.trim().replace(/\\s+/g,' '), w: Math.round(r.width), h: Math.round(r.height), bottom: Math.round(r.bottom), H: innerHeight}; })""")

    flows = {}
    # Сценарий 1: тред с черновиком → «Отправить» → шиты
    try:
        cur["s"] = "send-flow"
        go("#/thread/qa-t1")
        page.get_by_role("button", name="📨 Отправить").first.click()
        page.wait_for_timeout(800)
        snap("send-sheet-1", full=False)
        flows["send_sheet_buttons"] = page.evaluate("() => [...document.querySelectorAll('#sheet.on button')].filter(b=>b.offsetParent).map(b=>b.innerText.trim())")
        btn = page.get_by_role("button", name=re.compile("Отправить как есть"))
        if btn.count():
            btn.first.click(); page.wait_for_timeout(1500)
            snap("send-sheet-final", full=False)
            flows["final_sheet_text"] = page.evaluate("() => (document.querySelector('#sheet.on')||document.body).innerText.slice(0,600)")
        # Шит остаётся открытым при уходе с экрана?
        go("#/pipeline")
        flows["sheet_open_after_nav"] = page.evaluate("!!document.querySelector('#sheet.on')")
        snap("sheet-after-nav", full=False)
    except Exception as e:  # noqa: BLE001
        flows["send_error"] = str(e)[:300]

    # Меню «⋯»
    try:
        cur["s"] = "more-menu"
        fresh("#/thread/qa-t1")
        page.locator("button.more").click(); page.wait_for_timeout(700)
        snap("more-menu", full=False)
    except Exception as e:  # noqa: BLE001
        flows["more_error"] = str(e)[:300]

    # Несохранённый текст: правим черновик → «Назад» → возвращаемся
    try:
        cur["s"] = "unsaved"
        fresh("#/thread/qa-t1")
        ta = page.locator("textarea.de-input")
        ta.fill("QA-ПРАВКА оператора, не отправлена")
        dialogs_before = len(ev["dialogs"])
        page.locator("header .hback").click() if page.locator("header .hback").is_visible() else page.evaluate("window.__tgBack()")
        page.wait_for_timeout(800)
        left_to = page.evaluate("location.hash")
        go("#/thread/qa-t1")
        val = page.locator("textarea.de-input").input_value() if page.locator("textarea.de-input").count() else None
        flows["unsaved"] = {"left_to": left_to, "warned": len(ev["dialogs"]) > dialogs_before,
                            "tg_confirm": [x for x in page.evaluate("window.__tg_log||[]") if x[0] in ("showConfirm", "showPopup")][-1:],
                            "text_after_return": val, "kept": val == "QA-ПРАВКА оператора, не отправлена"}
    except Exception as e:  # noqa: BLE001
        flows["unsaved_error"] = str(e)[:300]

    # Клавиатура: фокус в поле + урезанный вьюпорт (экранная клавиатура ≈ 45% высоты)
    try:
        cur["s"] = "keyboard"
        fresh("#/thread/qa-t1")
        page.locator("textarea.de-input").focus()
        kb_h = int(d["h"] * 0.55)
        page.set_viewport_size({"width": d["w"], "height": kb_h})
        page.wait_for_timeout(600)
        page.locator("textarea.de-input").scroll_into_view_if_needed()
        flows["keyboard"] = page.evaluate("""() => { const t = document.querySelector('textarea.de-input').getBoundingClientRect();
            const dock = document.querySelector('.dock'); const dr = dock && dock.getBoundingClientRect();
            const send = [...document.querySelectorAll('.dock button')].find(b => b.innerText.includes('Отправить'));
            const sr = send && send.getBoundingClientRect();
            return { H: innerHeight, textarea_top: Math.round(t.top), textarea_bottom: Math.round(t.bottom), textarea_visible: t.top >= 0 && t.top < innerHeight,
                     dock_top: dr && Math.round(dr.top), dock_covers_textarea: !!(dr && dr.top < t.bottom && dr.bottom > t.top),
                     send_visible: !!(sr && sr.bottom <= innerHeight && sr.top >= 0) }; }""")
        snap("keyboard-thread", full=False)
        page.set_viewport_size({"width": d["w"], "height": d["h"]})
        # Compose в треде без черновика
        fresh("#/thread/qa-t4")  # тред без черновика (ответ отправлен) → в доке «Написать»
        page.get_by_role("button", name=re.compile("Написать")).first.click(); page.wait_for_timeout(800)
        snap("compose-sheet", full=False)
        txt = page.locator("#sheet.on textarea").first
        if txt.count():
            txt.fill("Hallo, der Preis ist fest. Abholung in Berlin möglich.")
            txt.focus()
            page.set_viewport_size({"width": d["w"], "height": kb_h})
            page.wait_for_timeout(600)
            flows["keyboard_compose"] = page.evaluate("""() => { const t = document.querySelector('#sheet.on textarea').getBoundingClientRect();
                const btns = [...document.querySelectorAll('#sheet.on button')].filter(b => b.offsetParent);
                const prim = btns.find(b => b.classList.contains('btn-primary')) || btns[btns.length-1];
                const pr = prim && prim.getBoundingClientRect();
                return { H: innerHeight, textarea_visible: t.top >= 0 && t.bottom <= innerHeight + 4, primary: prim && prim.innerText.trim(),
                         primary_visible: !!(pr && pr.bottom <= innerHeight && pr.top >= 0) }; }""")
            snap("keyboard-compose", full=False)
            page.set_viewport_size({"width": d["w"], "height": d["h"]})
            # уйти с открытым compose-шитом — потеряется ли текст?
            page.evaluate("window.__tgBack ? (document.querySelector('header .hback')?.offsetParent ? document.querySelector('header .hback').click() : window.__tgBack()) : null")
            page.wait_for_timeout(600)
            go("#/thread/qa-t4")
            page.get_by_role("button", name=re.compile("Написать")).first.click(); page.wait_for_timeout(800)
            t2 = page.locator("#sheet.on textarea").first
            flows["compose_kept_after_back"] = t2.input_value() if t2.count() else None
    except Exception as e:  # noqa: BLE001
        flows["keyboard_error"] = str(e)[:300]
        page.set_viewport_size({"width": d["w"], "height": d["h"]})

    # Продано-шит (цена) — проверка числового поля
    try:
        cur["s"] = "sold-sheet"
        fresh("#/thread/qa-t1")
        page.locator("button.more").click(); page.wait_for_timeout(500)
        page.get_by_text(re.compile("Продано")).first.click(); page.wait_for_timeout(700)
        snap("sold-sheet", full=False)
    except Exception as e:  # noqa: BLE001
        flows["sold_error"] = str(e)[:300]

    # Deep-link из уведомления бота (web_app-кнопка ?tgWebAppStartParam=review_<id>) → «Назад»
    for sp in ("review_1", "thread_qa-t1", "dashboard"):
        try:
            cur["s"] = f"deeplink-{sp}"
            page.goto("about:blank")
            page.goto(f"{STATIC}/index.html?tgWebAppStartParam={sp}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            opened = page.evaluate("location.hash")
            hops = [opened]
            for _ in range(3):
                b = page.locator("header .hback")
                if not (b.count() and b.is_visible()):
                    break
                b.click(); page.wait_for_timeout(1500)
                hops.append(page.evaluate("location.hash"))
            flows[f"deeplink_{sp}"] = {"opened": opened, "back_hops": hops}
        except Exception as e:  # noqa: BLE001
            flows[f"deeplink_{sp}_error"] = str(e)[:300]

    ctx.close()
    return {"device": d, "screens": screens, "flows": flows, "events": ev}


if __name__ == "__main__":
    main()
