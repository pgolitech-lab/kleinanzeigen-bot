#!/usr/bin/env python3
"""QA-прогон Mini App в headless Chromium 375x800 (только чтение).

Использование:
  python3 qa_ma.py <папка web-app> [--out report.json] [--port 8799] [--latency-ms 300]

Режимы API:
  * proxy — если заданы env KAB_BOT_TOKEN и KAB_TG_USER_ID: GET проксируется на
    http://127.0.0.1:8080 с валидным X-Telegram-Init-Data (алгоритм modules/tg_init_data.py),
    без cf-*/Host-заголовков туннеля. Токен никуда не пишется.
  * mock — иначе: GET отвечают синтетическими фикстурами по форме web/api_ma.py.
Любой не-GET к API -> фейк 200 {"ok":true} + запись в отчёт. sendBeacon перехвачен.
Внешние запросы (кроме telegram-web-app.js, подменённого заглушкой) абортятся и пишутся.
"""
import argparse, asyncio, hashlib, hmac, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from playwright.async_api import async_playwright

ap = argparse.ArgumentParser()
ap.add_argument("webapp")
ap.add_argument("--out", default=None)
ap.add_argument("--port", type=int, default=8799)
ap.add_argument("--latency-ms", type=int, default=300, help="искусственная задержка ответов API (имитация туннеля)")
ap.add_argument("--race-wait", type=int, default=25)
args = ap.parse_args()

TOKEN = os.environ.get("KAB_BOT_TOKEN", "")
TG_USER = os.environ.get("KAB_TG_USER_ID", "")
MODE = "proxy" if (TOKEN and TG_USER) else "mock"
BASE = f"http://127.0.0.1:{args.port}/"
LAT = args.latency_ms / 1000.0


def build_init_data() -> str:
    if MODE != "proxy":
        return "query_id=MOCK&user=%7B%22id%22%3A1%7D&auth_date=0&hash=mock"
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAqa_test",
        "user": json.dumps({"id": int(TG_USER), "first_name": "QA", "username": "qa_headless"},
                           separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


INIT_DATA = build_init_data()

# ---------------- фикстуры (mock) ----------------
NOW = "2026-10-20T10:00:00"
ACCS = [{"id": 1, "name": "osman"}, {"id": 2, "name": "anna"}]


def _pi(tid, mid, title, kind, pend, pinned=False, ap=False, acc=1):
    return {"thread_id": tid, "msg_id": mid, "ad_title": title, "ad_price": "1000 €", "ad_url": None,
            "buyer_display_name": "Max M.", "deal_brief_json": json.dumps({"summary_ru": "Клиент спрашивает цену"}),
            "last_event_at": NOW, "last_event_kind": kind, "pending_drafts_count": pend,
            "is_autopilot": ap, "account_id": acc, "ru_client": "Здравствуйте, ещё актуально?",
            "is_pinned": pinned, "operator_unread": kind == "in"}


PIPE = {"pinned": [_pi("T3", 301, "Pinned Sitzbank", "out", 0, pinned=True)],
        "red": [_pi("T1", 101, "Sitzbank Peugeot", "in", 1)],
        "green": [_pi("T2", 201, "Tisch Eiche", "out", 0, ap=True, acc=2)], "accounts": ACCS}


def _thread(tid, pending_mid):
    ev = [{"ts": "2026-10-19T09:00:00", "kind": "in", "text": "Hallo, noch da?", "ru_text": "Привет, ещё есть?",
           "is_auto_ack": False, "msg_id": pending_mid - 1, "status": "sent"},
          {"ts": "2026-10-19T09:30:00", "kind": "out", "text": "Ja. MfG", "ru_text": "Да.",
           "is_auto_ack": False, "msg_id": pending_mid - 1, "status": "sent"}]
    if pending_mid:
        ev.append({"ts": NOW, "kind": "in", "text": "Preis?", "ru_text": "Цена?", "is_auto_ack": False,
                   "msg_id": pending_mid, "status": "pending"})
    return {"header": {"thread_id": tid, "ad_title": f"Thread {tid} title", "ad_price": "1000 €", "ad_url": "https://example.invalid/ad",
                       "buyer_display_name": "Max M.", "buyer_email": "max@example.invalid", "account_name": "osman",
                       "account_email": "osman@example.invalid", "is_autopilot": False},
            "events": ev, "related": {"buyer_display_name": "Max M.", "matches": []}}


def _msg(mid, tid, status="pending"):
    return {"msg_id": mid, "thread_id": tid, "status": status,
            "ad": {"title": "Sitzbank Peugeot", "price": "1000 €", "url": None, "id": "123",
                   "buyer_display_name": "Max M.", "buyer_email": "max@example.invalid"},
            "client_lang": "de", "client_message": {"raw": "Preis?", "ru": "Цена?"},
            "draft": {"ru_answer": "Цена 1000", "de_answer": "Der Preis ist 1000 €. MfG", "ru_translation": "Цена 1000 €."},
            "deal_brief": {"summary_ru": "торг"}, "related": {"buyer_display_name": "Max M.", "matches": []},
            "lock": {"holder": None, "remaining_min": 0},
            "autopilot": {"active": False, "messages_sent": 0, "floor_eur": None, "notify_mode": None},
            "extra_notes": None, "is_auto_ack": False}


STOCK_PRODUCTS = [
    {"product_id": 1, "title": "Sitzbank 3er VW T6 Multivan", "category": "sitzbank", "condition": "gebraucht", "archived": False,
     "stock_tracked": True, "qty": 3, "unit_cost_eur": 450, "list_price_eur": 1200, "min_price_eur": 950, "listings": 2,
     "listed_days": 23, "stale": False, "negative_stock": False},
    {"product_id": 2, "title": "Tisch Schienensystem T5", "category": "tisch", "condition": None, "archived": False,
     "stock_tracked": True, "qty": 1, "unit_cost_eur": 60, "list_price_eur": 220, "min_price_eur": 180, "listings": 1,
     "listed_days": 71, "stale": True, "negative_stock": False},
    {"product_id": 3, "title": "Drehkonsole Fahrersitz Sprinter", "category": "konsole", "condition": None, "archived": False,
     "stock_tracked": False, "qty": None, "unit_cost_eur": None, "list_price_eur": 380, "min_price_eur": None, "listings": 1,
     "listed_days": 9, "stale": False, "negative_stock": False},
]


def mock_get(path: str):
    p = urllib.parse.urlparse(path)
    route = p.path
    if route == "/api/ma/stock/report":
        return 200, {"totals": {"products": 3, "tracked": 2, "qty": 4, "stock_cost": 1410, "expected_revenue_list": 3820,
                                "expected_profit_list": 2410, "open_questions": 2, "unlinked_sales": 1, "stale": 1},
                     "products": STOCK_PRODUCTS}
    m = re.match(r"^/api/ma/stock/products/(\d+)$", route)
    if m:
        pr = next((x for x in STOCK_PRODUCTS if x["product_id"] == int(m.group(1))), STOCK_PRODUCTS[0])
        return 200, {"product": pr, "listings": [{"ad_id": "3391", "title": pr["title"], "price_eur": pr["list_price_eur"], "state": "active",
                                                  "account_id": 1, "first_seen_at": "2026-09-27T10:00:00", "url": "https://example.invalid/ad"}],
                     "movements": [{"kind": "receipt", "qty": 4, "unit_cost_eur": 450, "occurred_at": "2026-09-20T10:00:00"},
                                   {"kind": "sale", "qty": -1, "unit_price_eur": 1050, "occurred_at": "2026-10-12T10:00:00"}]}
    if route == "/api/ma/stock/questions":
        return 200, {"items": [{"id": 7, "ad_id": "3402", "ad_title": "Sitzbank 2+1 Traveller", "ad_price_eur": 900,
                                "suggested_title": "Sitzbank 2+1 SpaceTourer", "suggested_price_eur": 950, "reason": "тот же заголовок без модели, цена близко"}]}
    if route == "/api/ma/stock/unlinked-sales":
        return 200, {"items": [{"thread_id": "T9", "ad_title": "2+1 старые 3 шт", "price_eur": 600}]}
    if route == "/api/ma/stock/month":
        return 200, {"profit": 1340, "revenue": 2210, "cogs": 870, "invested": 1800, "sold_qty": 4,
                     "stock_end": {"qty": 4, "list_value": 3820},
                     "lots": [{"title": "Sitzbank T6", "qty_in": 4, "cost": 1800, "date": "2026-10-02T10:00:00", "payback": "not_yet", "payback_pct": 58}],
                     "products": [{"title": "Sitzbank 3er VW T6 Multivan", "sold": 2, "profit": 1100}, {"title": "Tisch T5", "sold": 2, "profit": 240}]}
    if route == "/api/ma/pipeline":
        return 200, PIPE
    m = re.match(r"^/api/ma/threads/([^/]+)$", route)
    if m:
        tid = urllib.parse.unquote(m.group(1))
        return 200, _thread(tid, 101 if tid == "T1" else 0)
    m = re.match(r"^/api/ma/messages/(\d+)$", route)
    if m:
        mid = int(m.group(1))
        return 200, _msg(mid, "T1", "pending" if mid == 101 else "sent")
    if route == "/api/ma/clients":
        return 200, {"clients": [{"email": "max@example.invalid", "display_name": "Max M.", "ad_count": 1,
                                  "thread_count": 1, "msg_count": 3, "last_at": NOW, "last_status": "pending"}]}
    if re.match(r"^/api/ma/clients/.+/history$", route):
        return 200, {"buyer_email": "max@example.invalid", "display_name": "Max M.", "total_cost_usd": 0.01, "tags": [],
                     "note": "", "sold_count": 0, "total_negotiated_eur": 0, "last_active_thread_id": "T1",
                     "threads": [{"thread_id": "T1", "ad_title": "Sitzbank Peugeot", "ad_id": "123", "ad_price": "1000 €",
                                  "msg_count": 3, "last_at": NOW, "last_status": "pending", "deal_brief": None}]}
    if route == "/api/ma/sales":
        sale = {"thread_id": "T2", "ad_id": "9", "ad_title": "Tisch Eiche", "ad_url": None, "ad_price_listed": "300 €",
                "ad_price_listed_eur": 300.0, "buyer_display_name": "Eva", "account_id": 2, "account_name": "anna",
                "sold_at": "2026-10-01T10:00:00", "sold_price_eur": 280.0, "discount_eur": 20.0, "discount_pct": 6.7}
        return 200, {"sales": [sale], "summary": {"count": 1, "total_eur": 280.0, "avg_eur": 280.0, "max_eur": 280.0, "min_eur": 280.0},
                     "breakdown": [{"period_label": "2026-10", "count": 1, "total_eur": 280.0,
                                    "items": [{"ad_title": "Tisch Eiche", "sold_price_eur": 280.0, "thread_id": "T2"}]}],
                     "accounts": ACCS, "filters": {"period": "all", "from": None, "to": None, "account_id": None, "q": None, "group_by": "month"}}
    if route == "/api/ma/detected-sales":
        return 200, {"items": [{"id": 5, "thread_id": "T2", "confidence": 0.8, "sold_price_eur": 250.0,
                                "detected_ad_title": "Tisch", "detected_sold_at": NOW, "evidence": "verkauft",
                                "thread_ad_title": "Tisch Eiche", "thread_ad_price": "300 €", "thread_ad_price_eur": 300.0,
                                "discount_eur": 50.0, "buyer_display_name": "Eva", "account_name": "anna"}], "count": 1}
    if route == "/api/ma/settings":
        return 200, {"send_mode": "disabled", "debug_email": "d@example.invalid", "gmail_poll_interval_sec": "60",
                     "claude_model": "claude-sonnet-4-6", "reminders_enabled": "1", "reminder_after_days": "1",
                     "polling_paused": "0", "scout_auto_enabled": "1", "scout_interval_hours": "6",
                     "anthropic_api_key": "sk-…xxxx", "telegram_bot_token": "…xxxx"}
    if route == "/api/ma/dashboard":
        return 200, {"pipeline": {"red": 1, "green": 1, "drafts": 1, "autopilot_active": 1},
                     "today": {"new": 2, "sent": 1, "sold": 0},
                     "api_balance": {"snapshot_usd": None, "remaining_usd": None, "burn_per_day_usd": 0, "days_remaining": None},
                     "sales_7d": {"count": 0, "total_eur": 0.0}, "sales_30d": {"count": 1, "total_eur": 280.0}}
    counts = {"cars": 1, "parts": 1, "other": 0, "unverified": 0, "removed": 0}
    if route == "/api/ma/scout/overview":
        return 200, {"counts": counts, "auto_enabled": True, "interval_hours": 6, "running": False, "started_at": None,
                     "summary": None, "queries": [{"id": 1, "kind": "car", "label": "Boxer", "keywords": "peugeot boxer",
                                                   "category": None, "enabled": True, "max_pages": 2, "source": "manual",
                                                   "last_run_at": NOW, "last_count": 5}],
                     "car_regions": [{"bundesland": "Bayern", "cnt": 1, "min_price": 9000, "avg_price": 9000, "max_price": 9000}],
                     "part_regions": [{"bundesland": "Berlin", "cnt": 1, "min_price": 100, "avg_price": 100, "max_price": 100}]}
    if route == "/api/ma/scout/listings":
        kind = urllib.parse.parse_qs(p.query).get("kind", ["car"])[0]
        return 200, {"kind": kind, "removed_count": 0, "listings": [{
            "ad_id": "55", "kind": kind, "title": "Peugeot Boxer" if kind == "car" else "Sitzbank", "url": "https://example.invalid/55",
            "price_eur": 9000 if kind == "car" else 100, "negotiable": True, "plz": "80331", "city": "München",
            "bundesland": "Bayern", "year": 2015, "mileage_km": 150000, "fuel": "Diesel", "gearbox": "manual",
            "model_family": "Boxer", "part_type": "seat", "condition": "used", "posted_raw": "heute",
            "removed": False, "days_unseen": 0}]}
    if route == "/api/ma/scout/status":
        return 200, {"running": False, "started_at": None, "summary": None, "counts": counts}
    return 404, {"detail": f"mock: no fixture for {route}"}


def proxy_get(path: str):
    req = urllib.request.Request("http://127.0.0.1:8080" + path, headers={"X-Telegram-Init-Data": INIT_DATA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


TG_STUB = """
window.__qa = {beacons: [], back: [], alerts: []};
window.Telegram = {WebApp: {
  initData: %s, initDataUnsafe: {user: {id: 1, username: 'qa'}},
  ready(){}, expand(){}, close(){ window.__qa.closed = true; },
  showConfirm(m, cb){ window.__qa.alerts.push('confirm:' + m); cb(false); },
  openLink(u){ window.__qa.alerts.push('openLink:' + u); },
  BackButton: {show(){ window.__qa.back.push('show'); }, hide(){ window.__qa.back.push('hide'); },
               onClick(){}, offClick(){}},
  themeParams: {}, colorScheme: 'dark', platform: 'qa', version: '7.0'
}};
"""
BEACON_HOOK = """
(() => {
  const orig = navigator.sendBeacon ? navigator.sendBeacon.bind(navigator) : null;
  navigator.sendBeacon = function(url, data) {
    let abs = url; try { abs = new URL(url, location.href).href; } catch (e) {}
    window.__qa.beacons.push({url: String(url), resolved: abs, bodyType: data && data.constructor && data.constructor.name});
    return true;  // реально не шлём
  };
  window.alert = (m) => window.__qa.alerts.push('alert:' + m);
  window.confirm = (m) => { window.__qa.alerts.push('confirm:' + m); return false; };
})();
"""

R = {"mode": MODE, "latency_ms": args.latency_ms, "screens": [], "back": [], "races": {}, "nonget": [],
     "external": [], "http_errors": [], "console_errors": [], "page_errors": []}
CUR = {"label": "init"}
NET = {"api": 0, "all": 0}


async def handle_api(route, request):
    url = urllib.parse.urlparse(request.url)
    path = url.path + (("?" + url.query) if url.query else "")
    if request.method == "OPTIONS":
        await route.fulfill(status=204, headers={"access-control-allow-origin": "*",
                                                 "access-control-allow-headers": "*", "access-control-allow-methods": "*"})
        return
    NET["api"] += 1
    # POST /threads/{id}/open — чтение (тред + черновик + лок); мок отвечает данными
    mo = re.match(r"^/api/ma/threads/([^/]+)/open$", url.path)
    if request.method == "POST" and mo and MODE == "mock":
        R["nonget"].append({"screen": CUR["label"], "method": "POST", "path": path, "body": None})
        tid = urllib.parse.unquote(mo.group(1))
        pend = {"T1": 101, "T2": 0, "T3": 0}.get(tid, 0)
        data = _thread(tid, pend)
        data["header"]["account_id"] = 1
        data["review"] = _msg(pend, tid) if pend else None
        data["lock"] = {"acquired": True, "holder": "qa", "remaining_min": 5} if pend else None
        await asyncio.sleep(LAT)
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(data),
                            headers={"access-control-allow-origin": "*"})
        return
    # правки черновика — мок отвечает review с изменённым текстом (как настоящий api)
    me = re.match(r"^/api/ma/messages/(\d+)/(edit-de|edit-ru|price|regenerate|translate-draft|instruction)$", url.path)
    if request.method == "POST" and me and MODE == "mock":
        R["nonget"].append({"screen": CUR["label"], "method": "POST", "path": path, "body": (request.post_data or "")[:300]})
        msg = _msg(int(me.group(1)), "T1")
        try:
            b = json.loads(request.post_data or "{}")
        except ValueError:
            b = {}
        kind = me.group(2)
        if kind == "edit-de" and b.get("text"):
            msg["draft"]["de_answer"] = b["text"]
        elif kind == "price":
            msg["draft"]["de_answer"] = f"Für {int(b.get('eur', 0))} € gehört sie Ihnen. MfG"
        elif kind == "regenerate":
            msg["draft"]["de_answer"] = f"Variante ({b.get('strategy')}): Der Preis ist fest. MfG"
        elif kind == "translate-draft":
            msg["draft"]["de_answer"] = "Übersetzt: Der Preis ist 1000 €. MfG"
        await asyncio.sleep(LAT)
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(msg),
                            headers={"access-control-allow-origin": "*"})
        return
    if request.method != "GET":
        body = request.post_data
        R["nonget"].append({"screen": CUR["label"], "method": request.method, "path": path,
                            "body": (body[:300] if body else None)})
        await asyncio.sleep(LAT)
        await route.fulfill(status=200, content_type="application/json", body='{"ok":true}',
                            headers={"access-control-allow-origin": "*"})
        return
    await asyncio.sleep(LAT)
    if MODE == "proxy":
        status, body = await asyncio.to_thread(proxy_get, path)
    else:
        status, obj = mock_get(path)
        body = json.dumps(obj).encode()
    await route.fulfill(status=status, content_type="application/json", body=body,
                        headers={"access-control-allow-origin": "*"})


async def handle_any(route, request):
    u = request.url
    NET["all"] += 1
    if "trycloudflare.com" in u:
        return await handle_api(route, request)
    if u.startswith(BASE):
        if urllib.parse.urlparse(u).path.startswith("/api/"):
            # запрос к API относительно origin страницы (github.io в проде) — баг адресации
            R["nonget"].append({"screen": CUR["label"], "method": request.method, "path": u,
                                "note": "запрос к /api на origin страницы, не на API_BASE"})
            return await route.fulfill(status=404, body="")
        return await route.continue_()
    if "telegram.org/js/telegram-web-app.js" in u:
        return await route.fulfill(status=200, content_type="application/javascript", body="/* stub */")
    R["external"].append({"screen": CUR["label"], "url": u})
    await route.abort()


READY_JS = "() => { const a = document.querySelector('#app'); return a && !a.querySelector(':scope > p.text-muted.py-3') && !/^\\s*Загрузка/.test(a.innerText); }"


async def snapshot(page):
    return await page.evaluate("""() => ({
      hash: location.hash, title: document.getElementById('htitle')?.textContent,
      back_visible: !document.querySelector('#hd .hback')?.classList.contains('d-none'),
      error: document.querySelector('#app .alert-danger')?.innerText || null,
      has_tcard: !!document.querySelector('#app .tcard'), has_dock: !!document.querySelector('#app .dock'),
      has_sec: !!document.querySelector('#app .sec'),
      text: (document.querySelector('#app')?.innerText || '').replace(/\\s+/g,' ').slice(0, 160),
      active_tab: document.querySelector('#tabbar .tab.active .tl')?.textContent || null,
      bulkbar: !!document.querySelector('.bulkbar')
    })""")


async def open_screen(page, label, hash_, wait_extra=1.5):
    CUR["label"] = label
    NET["api"] = NET["all"] = 0
    await page.goto("about:blank")
    NET["api"] = NET["all"] = 0
    t0 = time.perf_counter()
    await page.goto(BASE + "index.html" + hash_, wait_until="domcontentloaded")
    ttfc = None
    try:
        await page.wait_for_function(READY_JS, timeout=20000)
        ttfc = round((time.perf_counter() - t0) * 1000)
    except Exception:
        pass
    await page.wait_for_timeout(int(wait_extra * 1000))
    snap = await snapshot(page)
    rec = {"label": label, "hash": hash_, "ttfc_ms": ttfc, "api_requests": NET["api"], "all_requests": NET["all"], **snap}
    R["screens"].append(rec)
    return rec


async def main():
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1",
                            "-d", os.path.abspath(args.webapp)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    try:
        async with async_playwright() as p:
            br = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            ctx = await br.new_context(viewport={"width": 375, "height": 800}, service_workers="block")
            await ctx.add_init_script(TG_STUB % json.dumps(INIT_DATA))
            await ctx.add_init_script(BEACON_HOOK)
            await ctx.route("**/*", handle_any)
            page = await ctx.new_page()
            page.on("pageerror", lambda e: R["page_errors"].append({"screen": CUR["label"], "msg": str(e)}))
            page.on("console", lambda m: m.type == "error" and R["console_errors"].append(
                {"screen": CUR["label"], "msg": m.text, "loc": m.location}))
            page.on("response", lambda r: r.status >= 400 and R["http_errors"].append(
                {"screen": CUR["label"], "status": r.status, "url": r.url}))

            # --- 1. экраны ---
            pipe = await open_screen(page, "Входящие #/", "#/")
            await open_screen(page, "Клиенты", "#/clients")
            await open_screen(page, "Продажи", "#/sales")
            await open_screen(page, "Обзор", "#/dashboard")
            await open_screen(page, "Рынок (Машины)", "#/scout")
            for tab, lbl in (("part", "Запчасти"), ("queries", "Запросы"), ("car", "Машины")):
                CUR["label"] = f"Рынок/{lbl}"
                NET["api"] = 0
                ok = True
                try:
                    await page.click(f"a[data-tab={tab}]", timeout=5000)
                    await page.wait_for_timeout(1500)
                except Exception as e:
                    ok = f"click fail: {e}"
                snap = await snapshot(page)
                R["screens"].append({"label": f"Рынок/{lbl}", "hash": "#/scout", "ttfc_ms": None, "click_ok": ok,
                                     "api_requests": NET["api"], "all_requests": None, **snap})
            await open_screen(page, "Проверка продаж", "#/detected")
            await open_screen(page, "Настройки", "#/settings")

            # первый тред из pipeline: с pending (red) приоритетно
            tid = None
            await page.goto(BASE + "index.html#/", wait_until="domcontentloaded")
            await page.wait_for_function(READY_JS, timeout=20000)
            first = await page.query_selector("#app .tcard")
            pipe_api = None
            if MODE == "mock":
                tid, pend_mid = "T1", 101
            else:
                st, body = await asyncio.to_thread(proxy_get, "/api/ma/pipeline")
                pipe_api = json.loads(body) if st == 200 else {}
                cand = [t for t in (pipe_api.get("red", []) + pipe_api.get("pinned", []) + pipe_api.get("green", []))]
                withp = [t for t in cand if t.get("pending_drafts_count")]
                pick = (withp or cand or [None])[0]
                tid = pick["thread_id"] if pick else None
                pend_mid = None
                if withp:
                    st2, b2 = await asyncio.to_thread(proxy_get, f"/api/ma/threads/{urllib.parse.quote(tid, safe='')}")
                    evs = json.loads(b2).get("events", []) if st2 == 200 else []
                    for ev in evs:
                        if ev.get("kind") == "in" and ev.get("status") in ("pending", "new", "edited", "approved"):
                            pend_mid = ev.get("msg_id")
            R["picked"] = {"thread_id_present": bool(tid), "pending_msg": pend_mid}
            th_hash = f"#/thread/{urllib.parse.quote(tid, safe='')}" if tid else None
            if th_hash:
                await open_screen(page, "Тред", th_hash, wait_extra=2)
            # клиент
            email = "max@example.invalid"
            if MODE == "proxy":
                st, body = await asyncio.to_thread(proxy_get, "/api/ma/clients")
                cl = json.loads(body).get("clients", []) if st == 200 else []
                email = cl[0]["email"] if cl else None
            if email:
                await open_screen(page, "Клиент", f"#/client/{urllib.parse.quote(email, safe='')}")
            if pend_mid:
                rec = await open_screen(page, "Review → тред", f"#/review/{pend_mid}", wait_extra=2)

            # --- 2. кнопка «Назад» ---
            async def back_test(label, from_hash, to_hash, expect):
                CUR["label"] = f"Назад: {label}"
                await page.goto("about:blank")
                await page.goto(BASE + "index.html" + from_hash, wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)
                await page.evaluate(f"location.hash = {json.dumps(to_hash)}")
                await page.wait_for_timeout(1500)
                before = await snapshot(page)
                res = {"label": label, "from": from_hash, "deep": to_hash, "back_visible": before["back_visible"],
                       "deep_hash_before_click": before["hash"]}
                if before["back_visible"]:
                    await page.click("#hd .hback")
                    await page.wait_for_timeout(1800)
                    after = await snapshot(page)
                    res.update({"after_hash": after["hash"], "after_title": after["title"],
                                "ok": after["hash"] in expect})
                else:
                    res["ok"] = False
                R["back"].append(res)

            if th_hash:
                await back_test("тред (из Входящих)", "#/", th_hash, ["#/", "#/pipeline", ""])
            if email:
                ch = f"#/client/{urllib.parse.quote(email, safe='')}"
                await back_test("клиент (из Клиентов)", "#/clients", ch, ["#/clients"])
            await back_test("настройки (из Обзора)", "#/dashboard", "#/settings", ["#/dashboard"])
            await back_test("проверка продаж (из Продаж)", "#/sales", "#/detected", ["#/sales"])
            if pend_mid:
                await back_test("review→тред (из Входящих)", "#/", f"#/review/{pend_mid}", ["#/", "#/pipeline", ""])
            # прямой заход на глубокий экран (истории нет)
            CUR["label"] = "Назад: прямой заход в #/settings"
            pg2 = await ctx.new_page()
            await pg2.goto(BASE + "index.html#/settings", wait_until="domcontentloaded")
            await pg2.wait_for_timeout(1200)
            vis = await pg2.evaluate("!document.querySelector('#hd .hback').classList.contains('d-none')")
            if vis:
                await pg2.click("#hd .hback"); await pg2.wait_for_timeout(1500)
            h = await pg2.evaluate("location.href.startsWith('about:') ? 'about:blank (ушли из приложения)' : location.hash")
            R["back"].append({"label": "прямой заход #/settings (history.length=1)", "back_visible": vis,
                              "after_hash": h, "ok": h in ("#/pipeline", "#/")})
            await pg2.close()

            # --- 3. гонки ---
            if th_hash:
                # (а) #/ -> через 50 мс тред, ждём N с
                CUR["label"] = "Гонка (а)"
                NET["api"] = 0
                pipe_calls = []
                def _cnt(req):
                    if "/api/ma/pipeline" in req.url:
                        pipe_calls.append(round(time.perf_counter() - t0, 2))
                page.on("request", _cnt)
                await page.goto("about:blank")
                t0 = time.perf_counter()
                await page.goto(BASE + "index.html#/", wait_until="domcontentloaded")
                await page.wait_for_timeout(50)
                await page.evaluate(f"location.hash = {json.dumps(th_hash)}")
                samples = []
                for sec in (1, 3, args.race_wait):
                    await page.wait_for_timeout(int((sec - (samples[-1]['t'] if samples else 0.05)) * 1000))
                    s = await snapshot(page)
                    samples.append({"t": sec, "hash": s["hash"], "title": s["title"], "has_dock": s["has_dock"],
                                    "has_tcard_or_sec": s["has_tcard"] or s["has_sec"], "text": s["text"][:100]})
                page.remove_listener("request", _cnt)
                last = samples[-1]
                R["races"]["a"] = {"samples": samples, "pipeline_GET_times_s": pipe_calls,
                                   "bug": last["hash"].startswith("#/thread/") and (not last["has_dock"] or last["has_tcard_or_sec"])}
                # (б) тред -> через 100 мс #/clients
                CUR["label"] = "Гонка (б)"
                await page.goto("about:blank")
                await page.goto(BASE + "index.html" + th_hash, wait_until="domcontentloaded")
                await page.wait_for_timeout(100)
                await page.evaluate("location.hash = '#/clients'")
                await page.wait_for_timeout(4000)
                s = await snapshot(page)
                R["races"]["b"] = {"hash": s["hash"], "title": s["title"], "has_dock": s["has_dock"], "text": s["text"][:120],
                                   "bug": s["hash"] == "#/clients" and s["has_dock"]}
                # (в) pagehide в треде с pending
                CUR["label"] = "Гонка (в) pagehide"
                await page.goto("about:blank")
                await page.goto(BASE + "index.html" + th_hash, wait_until="domcontentloaded")
                await page.wait_for_function(READY_JS, timeout=20000)
                await page.wait_for_timeout(1500)
                await page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                await page.wait_for_timeout(300)
                beacons = await page.evaluate("window.__qa.beacons")
                R["races"]["c"] = {"pending_msg": pend_mid, "beacons": beacons}
            await br.close()
    finally:
        srv.terminate(); srv.wait(timeout=5)
    out = json.dumps(R, ensure_ascii=False, indent=1)
    if args.out:
        open(args.out, "w").write(out)
    print(out)


if __name__ == "__main__":
    asyncio.run(main())
