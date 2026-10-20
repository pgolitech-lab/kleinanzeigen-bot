#!/usr/bin/env python3
"""Скриншоты реального Mini App (web-app/) на матрице Fold/Flip, API — моки из qa_ma.py.

  python3 docs/optimization/ui/shoot_app.py <папка web-app> <выходная папка> [--port 8841]
Не-GET к API отвечают фейком 200 (ничего не отправляется).
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

webapp, outdir = sys.argv[1], Path(sys.argv[2])
port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else "8841"
# qa_ma разбирает argv при импорте — подсовываем ему свои параметры
sys.argv = ["qa_ma.py", webapp, "--port", port, "--latency-ms", "50"]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qa_ma  # noqa: E402
from shoot import DEVICES  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

SCREENS = {
    "inbox": "#/", "thread": "#/thread/T1", "clients": "#/clients",
    "client": "#/client/max%40example.invalid", "sales": "#/sales", "dashboard": "#/dashboard",
    "scout": "#/scout", "settings": "#/settings",
}


async def main():
    outdir.mkdir(parents=True, exist_ok=True)
    problems = []
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        for dev, (w, h, dpr) in DEVICES.items():
            ctx = await br.new_context(viewport={"width": w, "height": h}, device_scale_factor=dpr,
                                       is_mobile=True, has_touch=True, service_workers="block")
            await ctx.add_init_script(qa_ma.TG_STUB % json.dumps(qa_ma.INIT_DATA))
            await ctx.add_init_script(qa_ma.BEACON_HOOK)
            await ctx.route("**/*", qa_ma.handle_any)
            page = await ctx.new_page()
            page.on("pageerror", lambda e: problems.append(f"{dev}: JS {e}"))
            for name, h_ in SCREENS.items():
                await page.goto("about:blank")
                await page.goto(qa_ma.BASE + "index.html" + h_)
                await page.wait_for_timeout(1500)
                ov = await page.evaluate("document.documentElement.scrollWidth - innerWidth")
                if ov > 1:
                    problems.append(f"{dev} {name}: горизонтальная прокрутка {ov}px")
                await page.screenshot(path=str(outdir / f"{dev}-{name}.png"))
            await ctx.close()
        await br.close()
    print(json.dumps(problems, ensure_ascii=False, indent=1))


srv = subprocess.Popen([sys.executable, "-m", "http.server", port, "--bind", "127.0.0.1", "-d", webapp],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.8)
try:
    asyncio.run(main())
finally:
    srv.terminate()
