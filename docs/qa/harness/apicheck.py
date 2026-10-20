# Проверка выбора адреса API в Mini App (?api= / sessionStorage / зашитый FALLBACK).
# python3 apicheck.py <static_url> <api_port>
# Любой запрос к *.trycloudflare.com перехватывается: GET → прокси на QA-бэкенд, не-GET → фейк 200.
# Запросы на НЕ-trycloudflare хосты (кроме статики/CDN) — блокируются и записываются (утечка initData).
import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import initdata  # noqa: E402
from run import TG_STUB  # noqa: E402

STATIC, API_PORT = sys.argv[1].rstrip("/"), int(sys.argv[2])
ALLOWED = ("127.0.0.1", "cdn.jsdelivr.net", "telegram.org", "fonts.googleapis.com", "fonts.gstatic.com",
           "cdnjs.cloudflare.com", "unpkg.com")
cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}

CASES = [
    ("без ?api=", "index.html#/pipeline", "contrast-push-eligible-neighbor.trycloudflare.com"),
    ("?api= валидный туннель", "index.html?api=https://qa-new-tunnel.trycloudflare.com#/pipeline", "qa-new-tunnel.trycloudflare.com"),
    ("перезагрузка без ?api= в том же окне", "index.html?r=2#/pipeline", "qa-new-tunnel.trycloudflare.com"),
    ("?api= чужой хост", "index.html?api=https://evil.example.com#/pipeline", None),
    ("?api= http (не https)", "index.html?api=http://qa-x.trycloudflare.com#/pipeline", None),
    ("?api= + deep-link review_1", "index.html?api=https://qa-dl.trycloudflare.com&tgWebAppStartParam=review_1", "qa-dl.trycloudflare.com"),
]


def main():
    out = []
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        ctx = br.new_context(viewport={"width": 412, "height": 860}, is_mobile=True, has_touch=True)
        stub = TG_STUB.replace("%INITDATA%", json.dumps(initdata.make())).replace("%USER%", json.dumps(initdata.USER))
        ctx.route("https://telegram.org/js/telegram-web-app.js*",
                  lambda r: r.fulfill(status=200, content_type="application/javascript", body=stub))
        log = {"hosts": [], "blocked": [], "console": []}

        def api_route(route):
            req = route.request
            host = re.match(r"https://([^/]+)", req.url).group(1)
            path = re.sub(r"^https://[^/]+", "", req.url)
            log["hosts"].append(host)
            if req.method == "OPTIONS":
                return route.fulfill(status=204, headers=cors)
            if req.method != "GET":
                return route.fulfill(status=200, headers=cors, content_type="application/json", body='{"ok":true}')
            r = urllib.request.Request(f"http://127.0.0.1:{API_PORT}{path}",
                                       headers={"X-Telegram-Init-Data": req.headers.get("x-telegram-init-data", "")})
            try:
                with urllib.request.urlopen(r, timeout=20) as resp:
                    body, st = resp.read(), resp.status
            except urllib.error.HTTPError as e:
                body, st = e.read(), e.code
            route.fulfill(status=st, headers=cors, content_type="application/json", body=body)

        def other(route):
            host = re.match(r"https?://([^/:]+)", route.request.url).group(1)
            if host.endswith(".trycloudflare.com"):
                return api_route(route)
            if host in ALLOWED:
                return route.continue_()
            log["blocked"].append(route.request.url[:120])
            return route.abort()

        ctx.route(re.compile(r"https?://.*"), other)
        page = ctx.new_page()
        page.on("console", lambda m: m.type in ("warning", "error") and log["console"].append(m.text[:160]))
        for name, url, expect in CASES:
            log["hosts"].clear(); log["blocked"].clear(); log["console"].clear()
            page.goto(f"{STATIC}/{url}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            hosts = sorted(set(log["hosts"]))
            ok = (hosts == [expect]) if expect else all(h == "contrast-push-eligible-neighbor.trycloudflare.com"
                                                         or h.startswith("qa-") for h in hosts) and not log["blocked"]
            out.append({"case": name, "expect": expect or "отклонить, взять сохранённый/зашитый", "hosts": hosts,
                        "blocked": list(log["blocked"]), "hash": page.evaluate("location.hash"),
                        "warn": [c for c in log["console"] if "api" in c.lower()][:2], "ok": ok,
                        "screen": page.evaluate("document.querySelector('#app').innerText.slice(0,60)")})
        br.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
