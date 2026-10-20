# Жёсткий UI-прогон: все экраны + стресс (обрыв сети, 500, медленный ответ, двойной тап, XSS).
# QA_THEME=dark|light python3 run_hard.py <out_dir> <api_port> <static_port> <devices,csv>
import json, re, sys, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import run as R  # noqa: E402  (читает argv[1..4])
from playwright.sync_api import sync_playwright  # noqa: E402
import initdata  # noqa: E402

OUT, API, STATIC = R.OUT, R.API_PORT, R.STATIC
SCREENS = [("inbox", "#/pipeline"), ("triage", "#/triage"), ("thread-draft", "#/thread/qa-t1"), ("thread-hot", "#/thread/qa-h2"),
           ("thread-overdue", "#/thread/qa-h1"), ("thread-en", "#/thread/qa-h3"), ("thread-xss", "#/thread/qa-h4"),
           ("thread-longname", "#/thread/qa-h5"), ("thread-pl", "#/thread/qa-h6"), ("thread-negprice", "#/thread/qa-h7"),
           ("clients", "#/clients"), ("sales", "#/sales"), ("stock", "#/stock"), ("stock-1", "#/stock/1"), ("stock-kit", "#/stock/4"),
           ("stock-xss", "#/stock/5"), ("stock-zero", "#/stock/7"), ("stock-types", "#/stock/types"), ("stock-month", "#/stock/month"),
           ("stock-questions", "#/stock/questions"), ("market", "#/stock/market"), ("accounts", "#/accounts"),
           ("settings", "#/settings"), ("more", "#/more"), ("review", "#/review/1")]
cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}


def run_device(br, dev, d):
    shots = OUT / "shots" / dev; shots.mkdir(parents=True, exist_ok=True)
    ctx = br.new_context(viewport={"width": d["w"], "height": d["h"]}, device_scale_factor=1, is_mobile=True, has_touch=True,
                         locale="ru-RU", timezone_id="Europe/Berlin", color_scheme=R.THEME)
    stub = R.stub_js()
    ctx.route("https://telegram.org/js/telegram-web-app.js*", lambda r: r.fulfill(status=200, content_type="application/javascript", body=stub))
    mode = {"m": "ok"}; ev = {"errors": [], "http": [], "posts": []}; cur = {"s": "boot"}

    def api_route(route):
        req = route.request; path = re.sub(r"^https://[^/]+", "", req.url)
        if req.method == "OPTIONS":
            return route.fulfill(status=204, headers=cors)
        if mode["m"] == "abort":
            return route.abort()
        if mode["m"] == "500":
            return route.fulfill(status=500, headers=cors, content_type="text/plain", body="Internal Server Error")
        if mode["m"] == "slow":
            import time; time.sleep(4)
        r = urllib.request.Request(f"http://127.0.0.1:{API}{path}", method=req.method, data=req.post_data_buffer if req.method != "GET" else None,
                                   headers={"X-Telegram-Init-Data": initdata.make(), "Content-Type": req.headers.get("content-type", "application/json")})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                body, st = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            body, st = e.read(), e.code
        if req.method != "GET":
            ev["posts"].append({"path": path, "status": st, "screen": cur["s"]})
        if st >= 400:
            ev["http"].append({"status": st, "path": path, "screen": cur["s"]})
        route.fulfill(status=st, headers=cors, content_type="application/json", body=body)

    ctx.route(re.compile(r"https://[^/]+\.trycloudflare\.com/.*"), api_route)
    page = ctx.new_page(); page.set_default_timeout(8000)
    page.on("pageerror", lambda e: ev["errors"].append({"screen": cur["s"], "type": "pageerror", "msg": str(e)[:200]}))
    page.on("console", lambda m: m.type == "error" and ev["errors"].append({"screen": cur["s"], "type": "console", "msg": m.text[:200]}))
    page.on("dialog", lambda dl: dl.accept())
    res = {}

    def open_(h, n):
        page.goto(f"{STATIC}/index.html?r={n}#/pipeline", wait_until="networkidle")
        page.evaluate("h => { location.hash = h; }", h); page.wait_for_timeout(1500)

    def snap(name):
        cur["s"] = name
        try: page.screenshot(path=str(shots / f"{name}.png"), full_page=True)
        except Exception as e: ev["errors"].append({"screen": name, "type": "shot", "msg": str(e)[:100]})
        c = page.evaluate(R.CHECK_JS)
        c["xss"] = page.evaluate("[window.__xss, window.__xss2, window.__xss3].filter(Boolean).length")
        c["rawtag"] = page.evaluate("document.querySelector('#app').innerHTML.includes('<script>window')")
        c["empty"] = len(c["text"].strip()) < 5
        res[name] = c
    for i, (name, h) in enumerate(SCREENS):
        cur["s"] = name; open_(h, i); snap(name)
    # --- стресс на ключевых экранах
    for mname in ("abort", "500", "slow"):
        for name, h in (("market", "#/stock/market"), ("stock", "#/stock"), ("inbox", "#/pipeline"), ("thread-hot", "#/thread/qa-h2")):
            mode["m"] = "ok"; open_(h, 100); mode["m"] = mname
            cur["s"] = f"{name}@{mname}"
            page.reload(); page.wait_for_timeout(2500 if mname != "slow" else 1200)
            snap(f"{name}@{mname}")
            res[f"{name}@{mname}"]["txt_head"] = res[f"{name}@{mname}"]["text"][:140]
    mode["m"] = "ok"
    # --- двойной тап «Приход» на карточке товара: ищем кнопку и жмём 2 раза подряд
    open_("#/stock/7", 200); cur["s"] = "dbltap"
    before = len(ev["posts"])
    try:
        btn = page.get_by_text(re.compile("Приход|Принять", re.I)).first
        btn.click(); page.wait_for_timeout(600)
        inp = page.locator("input[type=number]").first
        inp.fill("1"); ok = page.get_by_role("button", name=re.compile("Принять|Записать|Сохранить|Оприходовать", re.I)).last
        ok.dblclick(); page.wait_for_timeout(1500)
        res["dbltap"] = {"posts": [p for p in ev["posts"][before:]], "text": page.evaluate("document.querySelector('#app').innerText.slice(0,160)")}
        page.screenshot(path=str(shots / "dbltap.png"), full_page=True)
    except Exception as e:  # noqa: BLE001
        res["dbltap"] = {"err": str(e)[:200]}
    ctx.close()
    return {"screens": res, "errors": ev["errors"], "http": ev["http"], "posts": ev["posts"]}


def main():
    OUT.mkdir(parents=True, exist_ok=True); out = {}
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        for dev, d in R.DEVICES.items():
            if R.ONLY and dev not in R.ONLY: continue
            print("==", dev, R.THEME, flush=True); out[dev] = run_device(br, dev, d)
            (OUT / f"results-{R.THEME}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        br.close()


main()
