# Экран «Рынок» (#/scout): 2 размера, состояния, клавиатура (эмуляция урезанием высоты). python3 run_msearch.py <out_dir> <api_port> <static_port>
import sys, re, json, urllib.request, urllib.error
from pathlib import Path
sys.argv = [sys.argv[0], sys.argv[1], sys.argv[2], sys.argv[3]]
sys.path.insert(0, str(Path(__file__).parent))
import run as R, initdata  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
OUT, API, STATIC = R.OUT, R.API_PORT, R.STATIC
cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}
SIZES = {"flip5": (412, 915), "fold-inner": (860, 880)}
res = {}
with sync_playwright() as p:
    br = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
    for name, (w, h) in SIZES.items():
        ctx = br.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, is_mobile=True, has_touch=True, locale="ru-RU", color_scheme="light")
        stub = R.stub_js()
        ctx.route("https://telegram.org/js/telegram-web-app.js*", lambda r: r.fulfill(status=200, content_type="application/javascript", body=stub))
        mode = {"m": "ok"}; calls = []
        def api(route):
            rq = route.request; path = re.sub(r"^https://[^/]+", "", rq.url); calls.append(path)
            if rq.method == "OPTIONS": return route.fulfill(status=204, headers=cors)
            if mode["m"] == "abort": return route.abort()
            r = urllib.request.Request(f"http://127.0.0.1:{API}{path}", headers={"X-Telegram-Init-Data": initdata.make()})
            try:
                with urllib.request.urlopen(r, timeout=60) as x: body, st = x.read(), x.status
            except urllib.error.HTTPError as e: body, st = e.read(), e.code
            route.fulfill(status=st, headers=cors, content_type="application/json", body=body)
        ctx.route(re.compile(r"https://[^/]+\.trycloudflare\.com/.*"), api)
        pg = ctx.new_page(); pg.set_default_timeout(8000)
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)[:150])); pg.on("console", lambda m: m.type == "error" and errs.append(m.text[:150]))
        pg.on("dialog", lambda d: d.accept())
        def shot(n): pg.screenshot(path=f"{OUT}/{name}-{n}.png", full_page=True)
        def metrics(n):
            res[f"{name}/{n}"] = pg.evaluate("""() => ({hs: document.scrollingElement.scrollWidth - innerWidth,
              over: [...document.querySelectorAll('#app *')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > innerWidth + 2; }).length,
              small: [...document.querySelectorAll('#app button, #app a, #app input')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && (r.height < 44 || r.width < 44); }).map(e => (e.innerText || e.placeholder || e.className).slice(0, 25)),
              text: document.querySelector('#app').innerText.slice(0, 500)})""")
        def fresh(n=[0]):
            n[0] += 1; pg.goto(f"{STATIC}/index.html?r={n[0]}#/scout", wait_until="networkidle"); pg.wait_for_timeout(1200)
        def ask(q, n, wait=1800):
            pg.fill("#ms-q", q); pg.click(".ms-go"); pg.wait_for_timeout(wait); shot(n); metrics(n)
        fresh(); shot("0-empty"); metrics("0-empty")
        pg.click(".ms-go"); pg.wait_for_timeout(400); shot("0b-empty-click"); metrics("0b-empty-click")
        ask("Sitzbank", "1-sitzbank"); ask("Werkbank", "2-werkbank-nodata"); ask("Tisch", "3-tisch-few")
        ask("<img src=x onerror=window.__xss=1>", "4-xss"); res[f"{name}/xss"] = pg.evaluate("!!window.__xss")
        ask("x" * 81, "5-longquery")
        fresh(); pg.fill("#ms-q", "Sitzbank"); pg.click(".ms-go"); pg.wait_for_timeout(1500)
        pg.click(".ms-ad >> nth=0") if pg.locator(".ms-ad").count() else None
        # двойной тап «Найти»
        fresh(); n0 = len(calls); pg.fill("#ms-q", "Sitzbank"); pg.dblclick(".ms-go"); pg.wait_for_timeout(1800)
        res[f"{name}/dbl-calls"] = len([c for c in calls[n0:] if "market/search?q=Sitzbank" in c])
        # сеть оборвана → «Повторить» → сеть вернулась
        fresh(); mode["m"] = "abort"; pg.fill("#ms-q", "Sitzbank"); pg.click(".ms-go"); pg.wait_for_timeout(1500); shot("6-neterr"); metrics("6-neterr")
        mode["m"] = "ok"
        try: pg.get_by_text("Повторить").first.click(); pg.wait_for_timeout(1800); shot("7-retry-ok"); metrics("7-retry-ok")
        except Exception as e: res[f"{name}/retry"] = f"НЕТ кнопки: {str(e)[:80]}"
        # живой поиск (реальный Kleinanzeigen, только чтение)
        fresh(); ask("Werkbank", "8a-nodata2", 1500)
        try:
            pg.get_by_text("Искать на Kleinanzeigen").first.click(); pg.wait_for_timeout(500); shot("8b-live-busy"); pg.wait_for_selector(".ms-err, .ms-h, .ms-warn, .ms-empty", timeout=90000); pg.wait_for_timeout(800); shot("8c-live-result"); metrics("8c-live-result")
        except Exception as e: res[f"{name}/live"] = f"ERR {str(e)[:120]}"
        # клавиатура: высота вьюпорта 915 → 420, поле в фокусе
        fresh(); pg.set_viewport_size({"width": w, "height": 420}); pg.focus("#ms-q"); pg.wait_for_timeout(300)
        res[f"{name}/kbd-input-visible"] = pg.evaluate("(() => { const r = document.querySelector('#ms-q').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; })()")
        pg.fill("#ms-q", "Sitzbank"); pg.keyboard.press("Enter"); pg.wait_for_timeout(1800); shot("9-kbd-result")
        res[f"{name}/kbd-blur"] = pg.evaluate("document.activeElement.id !== 'ms-q'")
        res[f"{name}/kbd-headline-in-view"] = pg.evaluate("(() => { const e = document.querySelector('.ms-h'); if (!e) return null; const r = e.getBoundingClientRect(); return r.bottom <= innerHeight; })()")
        res[f"{name}/errors"] = sorted(set(errs))[:6]
        ctx.close()
    br.close()
json.dump(res, open(f"{OUT}/results.json", "w"), ensure_ascii=False, indent=1)
for k, v in res.items():
    if isinstance(v, dict): print(k, {a: v[a] for a in ("hs", "over", "small") if v.get(a)}, "|", v["text"][:110].replace("\n", " / "))
    else: print(k, v)
