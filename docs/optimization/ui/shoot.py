#!/usr/bin/env python3
"""Скриншоты концепций (и Mini App) на матрице устройств Fold/Flip.

  python3 docs/optimization/ui/shoot.py concepts            # все концепции
  python3 docs/optimization/ui/shoot.py concepts b-stack    # одна
  python3 docs/optimization/ui/shoot.py --devices flip-cover,fold-outer-min concepts

Прототип открывается как <concept>/index.html?screen=<s>&theme=<t>; кадр = видимая
область (как на телефоне), без full-page. Итог: <concept>/shots/<device>-<screen>[-light].png
и отчёт о горизонтальной прокрутке / JS-ошибках в stdout.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent

# CSS-px × DPR. Fold 8 Ultra: внешний 2520×1080 (21:9), внутренний 2504×2256 (10:9).
# Flip 5: основной 1080×2640, внешний Flex Window 720×748.
DEVICES = {
    "fold-outer":      (412, 960, 2.625),
    "fold-outer-min":  (280, 653, 2.625),   # худший случай по ширине
    "fold-inner-port": (820, 910, 2.75),
    "fold-inner-land": (910, 820, 2.75),
    "flip-main":       (412, 915, 2.625),
    "flip-cover":      (306, 340, 2.35),
}
SCREENS = ["inbox", "thread", "client", "sales"]
LIGHT_ON = {"flip-main", "fold-inner-land"}   # светлая тема — на двух устройствах


async def shoot(base_url, concepts, devices):
    problems = []
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        for c in concepts:
            out = HERE / "concepts" / c / "shots"
            out.mkdir(parents=True, exist_ok=True)
            for dev in devices:
                w, h, dpr = DEVICES[dev]
                ctx = await br.new_context(viewport={"width": w, "height": h}, device_scale_factor=dpr,
                                           is_mobile=True, has_touch=True)
                page = await ctx.new_page()
                errs = []
                page.on("pageerror", lambda e: errs.append(str(e)))
                page.on("console", lambda m: m.type == "error" and errs.append(m.text))
                themes = ["dark", "light"] if dev in LIGHT_ON else ["dark"]
                for s in SCREENS:
                    for t in themes:
                        await page.goto(f"{base_url}/concepts/{c}/index.html?screen={s}&theme={t}&shot=1")
                        await page.wait_for_timeout(700)
                        ov = await page.evaluate("document.documentElement.scrollWidth - innerWidth")
                        if ov > 1:
                            problems.append(f"{c} {dev} {s}: горизонтальная прокрутка {ov}px")
                        name = f"{dev}-{s}" + ("-light" if t == "light" else "") + ".png"
                        await page.screenshot(path=str(out / name))
                for e in errs:
                    problems.append(f"{c} {dev}: JS {e}")
                await ctx.close()
        await br.close()
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", choices=["concepts"])
    ap.add_argument("only", nargs="*")
    ap.add_argument("--devices", default=",".join(DEVICES))
    ap.add_argument("--port", type=int, default=8821)
    a = ap.parse_args()
    concepts = a.only or sorted(d.name for d in (HERE / "concepts").iterdir()
                                if d.is_dir() and not d.name.startswith("_") and (d / "index.html").exists())
    devices = [d for d in a.devices.split(",") if d]
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(a.port), "--bind", "127.0.0.1", "-d", str(HERE)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    try:
        problems = asyncio.run(shoot(f"http://127.0.0.1:{a.port}", concepts, devices))
    finally:
        srv.terminate()
    print(json.dumps({"concepts": concepts, "devices": devices, "problems": problems}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
