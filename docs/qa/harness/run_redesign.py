# Прогон редизайна Mini App (opt/webapp) по матрице устройств × тем, на СИНТЕТИЧЕСКОМ бэкенде.
# Перед каждым устройством БД пересоздаётся (seed.py) и бэкенд перезапускается — сценарии меняют состояние.
# Не-GET идут в синтетический бэкенд (send_mode=disabled, SMTP/IMAP/Telegram заглушены в serve.py),
# кроме /scout/* (живой скрапинг) — там фейк 200.
# python3 run_redesign.py <out_dir> <backend_repo> <static_dir> [devices=all] [themes=dark,light]
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
OUT, BACKEND, STATIC_DIR = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
ONLY = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] != "all" else None
THEMES = sys.argv[5].split(",") if len(sys.argv) > 5 else ["dark", "light"]
_argv = sys.argv
sys.argv = ["run.py", "/tmp/x", "8790", "8803"]
import run  # noqa: E402
sys.argv = _argv
import initdata  # noqa: E402

API_PORT = int(os.environ.get("QA_API_PORT", "8811"))
STATIC_PORT = int(os.environ.get("QA_STATIC_PORT", "8812"))
STATIC = f"http://127.0.0.1:{STATIC_PORT}"
DB = OUT / "qa-redesign.db"
DEVICES = {k: v for k, v in run.DEVICES.items()}
if os.environ.get("QA_ECO") == "1":  # режим экономии: 3 размера
    DEVICES = {"flip5-main": dict(w=412, h=915, dpr=2.625), "flip5-flex": dict(w=306, h=340, dpr=2.35),
               "fold-inner": dict(w=910, h=820, dpr=2.625)}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}

SCREENS = [("inbox", "#/pipeline"), ("triage", "#/triage"), ("dashboard", "#/dashboard"), ("clients", "#/clients"),
           ("client", "#/client/klausm.%40example.invalid"), ("sales", "#/sales"), ("detected", "#/detected"),
           ("listings", "#/listings"), ("more", "#/more"), ("accounts", "#/accounts"), ("settings", "#/settings"),
           ("thread-draft", "#/thread/qa-t1"), ("thread-long", "#/thread/qa-t2"), ("thread-pl", "#/thread/qa-t3"),
           ("thread-answered", "#/thread/qa-t4"), ("thread-sold", "#/thread/qa-t5"), ("thread-fr", "#/thread/qa-t6"),
           ("review", "#/review/1")]

_srv = {"p": None}


def backend_restart():
    if _srv["p"]:
        _srv["p"].send_signal(signal.SIGTERM)
        _srv["p"].wait(10)
    for suf in ("", "-wal", "-shm"):
        Path(f"{DB}{suf}").unlink(missing_ok=True)
    subprocess.run([sys.executable, str(HERE / "seed.py"), BACKEND, str(DB)], check=True, capture_output=True)
    _srv["p"] = subprocess.Popen(env={**os.environ, "QA_LLM": "fake"}, args=[sys.executable, str(HERE / "serve.py"), BACKEND, str(DB), str(API_PORT)],
                                 stdout=open(OUT / "serve.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/health", timeout=2)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError("QA-бэкенд не поднялся")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    static = subprocess.Popen([sys.executable, "-m", "http.server", str(STATIC_PORT), "--bind", "127.0.0.1",
                               "-d", STATIC_DIR], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            for theme in THEMES:
                for dev, d in DEVICES.items():
                    if ONLY and dev not in ONLY:
                        continue
                    key = f"{dev}/{theme}"
                    print("==", key, flush=True)
                    backend_restart()
                    results[key] = run_device(browser, dev, d, theme, flows=(theme == "dark"))
                    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
            browser.close()
    finally:
        static.terminate()
        if _srv["p"]:
            _srv["p"].terminate()
    print("done", OUT / "results.json")


def run_device(browser, dev, d, theme, flows):
    run.THEME = theme
    shots = OUT / "shots" / f"{dev}-{theme}"
    shots.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": d["w"], "height": d["h"]}, device_scale_factor=1, is_mobile=True,
                              has_touch=True, locale="ru-RU", timezone_id="Europe/Berlin", color_scheme=theme)
    ev = {"errors": [], "http": [], "nonget": [], "dialogs": []}
    cur = {"s": "boot", "n": 0}
    ctx.route("https://telegram.org/js/telegram-web-app.js*",
              lambda r: r.fulfill(status=200, content_type="application/javascript", body=run.stub_js()))

    def api_route(route):
        req = route.request
        path = re.sub(r"^https://[^/]+", "", req.url)
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=CORS)
        if req.method != "GET" and "/scout/" in path:
            ev["nonget"].append({"method": req.method, "path": path, "status": "fake", "screen": cur["s"]})
            return route.fulfill(status=200, headers=CORS, content_type="application/json", body='{"ok":true}')
        r = urllib.request.Request(f"http://127.0.0.1:{API_PORT}{path}", method=req.method,
                                   data=req.post_data_buffer if req.method != "GET" else None,
                                   headers={"X-Telegram-Init-Data": initdata.make(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                body, st = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            body, st = e.read(), e.code
        if req.method != "GET":
            ev["nonget"].append({"method": req.method, "path": path, "body": (req.post_data or "")[:200], "status": st,
                                 "resp": body[:240].decode("utf-8", "replace"), "screen": cur["s"]})
        if st >= 400:
            ev["http"].append({"status": st, "method": req.method, "path": path,
                               "body": body[:160].decode("utf-8", "replace"), "screen": cur["s"]})
        route.fulfill(status=st, headers=CORS, content_type="application/json", body=body)

    ctx.route(re.compile(r"https://[^/]+\.trycloudflare\.com/.*"), api_route)
    page = ctx.new_page()
    page.set_default_timeout(6000)
    page.on("pageerror", lambda e: ev["errors"].append({"screen": cur["s"], "type": "pageerror", "msg": str(e)[:300]}))
    page.on("console", lambda m: m.type == "error" and ev["errors"].append(
        {"screen": cur["s"], "type": "console", "msg": m.text[:300]}))
    page.on("dialog", lambda dl: (ev["dialogs"].append({"screen": cur["s"], "msg": dl.message[:200]}), dl.accept()))
    screens, fl = {}, {}

    def fresh(h):
        cur["n"] += 1
        page.goto(f"{STATIC}/index.html?r={cur['n']}#{h.lstrip('#')}", wait_until="networkidle")
        page.wait_for_timeout(1200)

    def snap(name, full=False):
        cur["s"] = name
        page.wait_for_timeout(500)
        try:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=full)
        except Exception as e:  # noqa: BLE001
            ev["errors"].append({"screen": name, "type": "screenshot", "msg": str(e)[:200]})
        s = page.evaluate(run.CHECK_JS)
        s["layout"] = page.evaluate("document.documentElement.dataset.layout || document.body.dataset.layout || ''")
        s["theme"] = page.evaluate("document.documentElement.dataset.theme || ''")
        s["unknown_route"] = "Неизвестный путь" in page.evaluate("document.body.innerText")
        screens[name] = s

    def tap(locator, taps):
        locator.first.click()
        taps.append(1)
        page.wait_for_timeout(700)

    def sheet_open():
        return page.evaluate("!!document.querySelector('#sheet.on')")

    for name, h in SCREENS:
        cur["s"] = name
        fresh(h)
        snap(name)
        if name in ("thread-draft", "client", "settings", "detected"):
            screens[name]["tg_back_visible"] = page.evaluate("window.Telegram.WebApp.BackButton.isVisible")

    if not flows:
        ctx.close()
        return {"device": d, "theme": theme, "screens": screens, "flows": fl, "events": ev}
    flexmode = d["w"] <= 360 and d["h"] <= 440

    # F1. Ответ Klaus (черновик готов): Входящие → тред → «Отправить» → «✅ Отправить». Честный исход /send.
    try:
        cur["s"] = "F1-send"
        fresh("#/pipeline")
        taps = []
        if flexmode:  # на Flex вместо Входящих — «Разбор»: одна карточка
            tap(page.locator(".ta-send"), taps)
        else:
            tap(page.locator(".row", has_text="Klaus"), taps)
            tap(page.locator(".btn-send"), taps)
        snap("F1-final-sheet")
        fl["F1_final_text"] = page.evaluate("(document.querySelector('#sheet.on')||{}).innerText||''")[:400]
        page.wait_for_timeout(1500)  # lang-check
        btn = page.locator("#sheet.on .send-btn, #sheet.on .btn-primary:has-text('Отправить')")
        fl["F1_send_enabled"] = btn.count() > 0 and btn.first.is_enabled()
        if fl["F1_send_enabled"]:
            tap(btn, taps)
            page.wait_for_timeout(1500)
        snap("F1-after-send")
        sends = [x for x in ev["nonget"] if x["path"].endswith("/send")]
        fl["F1_taps"] = len(taps)
        fl["F1_send_resp"] = sends[-1]["resp"] if sends else None
        fl["F1_toast"] = page.evaluate("[...document.querySelectorAll('.toast,[class*=toast],.alert,.banner')].map(e=>e.innerText.trim()).filter(Boolean).slice(0,4)")
        fl["F1_says_sent"] = any(re.search(r"(?<!не )отправлено клиенту|ушло клиенту", t, re.I) for t in fl["F1_toast"])
    except Exception as e:  # noqa: BLE001
        fl["F1_error"] = str(e)[:300]

    # F2. Языковой guard: FR-клиенту кириллица → блок; /send только с галочкой и force:true.
    if not flexmode:
        try:
            cur["s"] = "F2-guard"
            fresh("#/thread/qa-t6")
            ta = page.locator("textarea.d2-de")
            ta.fill("Здравствуйте, цена окончательная, забрать можно сегодня.")
            n_send0 = len([x for x in ev["nonget"] if x["path"].endswith("/send")])
            page.locator(".btn-send").first.click()
            page.wait_for_timeout(2000)
            snap("F2-guard-sheet")
            sb = page.locator("#sheet.on .send-btn")
            fl["F2_mismatch_shown"] = page.evaluate("!!document.querySelector('#sheet.on .mismatch:not([hidden])')")
            fl["F2_send_disabled_before_cb"] = sb.count() > 0 and not sb.first.is_enabled()
            if sb.count() and sb.first.is_enabled():
                sb.first.click(); page.wait_for_timeout(1500)  # не должно было быть доступно
            cb = page.locator("#sheet.on .allow-lang")
            if cb.count():
                cb.first.check(); page.wait_for_timeout(300)
                fl["F2_send_enabled_after_cb"] = sb.first.is_enabled()
                sb.first.click(); page.wait_for_timeout(1500)
            sends = [x for x in ev["nonget"] if x["path"].endswith("/send")][n_send0:]
            fl["F2_sends"] = [(x["body"], x["status"]) for x in sends]
            fl["F2_ok"] = (fl.get("F2_mismatch_shown") and fl.get("F2_send_disabled_before_cb")
                           and len(sends) == 1 and '"force":true' in sends[0]["body"].replace(" ", ""))
            snap("F2-after")
        except Exception as e:  # noqa: BLE001
            fl["F2_error"] = str(e)[:300]

    # F3. «Продано» в треде БЕЗ черновика (qa-t4), цена предзаполнена?
    if not flexmode:
        try:
            cur["s"] = "F3-sold"
            fresh("#/thread/qa-t4")
            taps = []
            tap(page.locator(".d-sold, button:has-text('Продано')"), taps)
            snap("F3-sold-sheet")
            inp = page.locator("#sheet.on .sold-price")
            fl["F3_price_prefilled"] = inp.input_value() if inp.count() else None
            if inp.count() and not inp.input_value():
                inp.fill("580")
            tap(page.locator("#sheet.on .save-btn"), taps)
            page.wait_for_timeout(1000)
            sold = [x for x in ev["nonget"] if x["path"].endswith("/sold")]
            fl["F3_sold_req"] = [(x["path"], x["status"], x["resp"][:120]) for x in sold]
            fl["F3_taps"] = len(taps)
            fl["F3_toast"] = page.evaluate("[...document.querySelectorAll('.toast,[class*=toast]')].map(e=>e.innerText.trim()).filter(Boolean)")
            snap("F3-after")
        except Exception as e:  # noqa: BLE001
            fl["F3_error"] = str(e)[:300]

    # F4. Чипы торга (LLM в стенде выключен → ожидаем понятную ошибку, без JS-исключений)
    if not flexmode:
        try:
            cur["s"] = "F4-chip"
            fresh("#/thread/qa-t3")
            page.locator(".pchip").first.click()
            page.wait_for_timeout(2500)
            reqs = [x for x in ev["nonget"] if x["screen"] == "F4-chip" and "lock" not in x["path"]]
            fl["F4_chip_req"] = [(x["path"], x["body"][:60], x["status"]) for x in reqs]
            fl["F4_visible_error"] = page.evaluate("[...document.querySelectorAll('.text-danger,.toast,[class*=err]')].map(e=>e.innerText.trim()).filter(Boolean).slice(0,3)")
            snap("F4-chip")
        except Exception as e:  # noqa: BLE001
            fl["F4_error"] = str(e)[:300]

    # F5. «Назад» и набранный текст: правка черновика → «←» → вернуться; нативная BackButton Telegram
    if not flexmode:
        try:
            cur["s"] = "F5-back"
            fresh("#/pipeline")  # в тред — внутри того же документа, чтобы «Назад» вёл во Входящие
            page.evaluate("location.hash = '#/thread/qa-t3'")
            page.wait_for_timeout(1500)
            ta = page.locator("textarea.d2-de")
            ta.fill("QA-ПРАВКА оператора, не отправлена")
            fl["F5_tg_back_visible"] = page.evaluate("window.Telegram.WebApp.BackButton.isVisible")
            hb = page.locator("#hd .hback")
            if hb.is_visible():
                hb.click()
            else:
                page.evaluate("window.__tgBack()")
            page.wait_for_timeout(1000)
            fl["F5_after_back_hash"] = page.evaluate("location.hash")
            fl["F5_sheet_or_confirm"] = sheet_open() or bool(ev["dialogs"]) or bool(
                [x for x in page.evaluate("window.__tg_log||[]") if x[0] in ("showConfirm", "showPopup")])
            if sheet_open():
                snap("F5-leave-confirm")
                page.locator("#sheet.on button").last.click(); page.wait_for_timeout(800)
                fl["F5_after_confirm_hash"] = page.evaluate("location.hash")
            page.evaluate("location.hash = '#/thread/qa-t3'")
            page.wait_for_timeout(1500)
            v = page.locator("textarea.d2-de")
            fl["F5_text_after_return"] = v.input_value() if v.count() else None
            fl["F5_kept"] = fl["F5_text_after_return"] == "QA-ПРАВКА оператора, не отправлена"
            # нативная кнопка Telegram ведёт туда же
            page.evaluate("window.__tgBack()")
            page.wait_for_timeout(1000)
            fl["F5_tg_back_hash"] = page.evaluate("location.hash")
        except Exception as e:  # noqa: BLE001
            fl["F5_error"] = str(e)[:300]

    # F6. Шит не висит после смены экрана (B-01)
    if not flexmode:
        try:
            cur["s"] = "F6-sheet-nav"
            fresh("#/thread/qa-t3")
            page.locator(".btn-send").first.click(); page.wait_for_timeout(1500)
            opened = sheet_open()
            page.evaluate("location.hash = '#/pipeline'"); page.wait_for_timeout(1000)
            fl["F6_sheet_opened"] = opened
            fl["F6_sheet_after_nav"] = sheet_open()
        except Exception as e:  # noqa: BLE001
            fl["F6_error"] = str(e)[:300]

    # F7. «Разбор»: «Позже» листает, счётчик меняется
    try:
        cur["s"] = "F7-triage"
        fresh("#/triage")
        c0 = page.evaluate("document.body.innerText.match(/(\\d+) из (\\d+)/)?.[0] || ''")
        later = page.locator(".ta-later")
        if later.count():
            later.first.click(); page.wait_for_timeout(1000)
        c1 = page.evaluate("document.body.innerText.match(/(\\d+) из (\\d+)/)?.[0] || ''")
        fl["F7_counter"] = [c0, c1]
        snap("F7-triage-next")
    except Exception as e:  # noqa: BLE001
        fl["F7_error"] = str(e)[:300]

    # F8. Клавиатура: фокус в поле черновика, вьюпорт −45 %
    if not flexmode:
        try:
            cur["s"] = "F8-kb"
            fresh("#/thread/qa-t3")
            page.locator("textarea.d2-de").focus()
            page.set_viewport_size({"width": d["w"], "height": int(d["h"] * 0.55)})
            page.wait_for_timeout(700)
            # браузер телефона при появлении клавиатуры сам докручивает поле в фокусе в видимую область
            page.evaluate("document.activeElement && document.activeElement.scrollIntoView({block: 'nearest'})")
            page.wait_for_timeout(300)
            fl["F8_kb"] = page.evaluate("""() => { const t = document.querySelector('textarea.d2-de').getBoundingClientRect();
                const s = document.querySelector('.btn-send'); const r = s && s.getBoundingClientRect();
                const tab = document.querySelector('.tabbar, nav'); const tr = tab && tab.getBoundingClientRect();
                return {H: innerHeight, ta_top: Math.round(t.top), ta_bottom: Math.round(t.bottom),
                        ta_visible: t.top >= 0 && t.bottom <= innerHeight + 2, send_visible: !!(r && r.bottom <= innerHeight && r.top >= 0),
                        tabbar_visible: !!(tr && tr.height > 0 && tr.top < innerHeight)}; }""")
            snap("F8-kb")
            page.set_viewport_size({"width": d["w"], "height": d["h"]})
        except Exception as e:  # noqa: BLE001
            fl["F8_error"] = str(e)[:300]
            page.set_viewport_size({"width": d["w"], "height": d["h"]})

    ctx.close()
    return {"device": d, "theme": theme, "screens": screens, "flows": fl, "events": ev}


if __name__ == "__main__":
    main()
