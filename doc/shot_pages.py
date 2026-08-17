# -*- coding: utf-8 -*-
"""Скриншоты выбранных страниц пагинированного документа через CDP."""
import asyncio, base64, json, subprocess, sys, time, urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "out" / "document.html"
SHOTS = ROOT / "out" / "shots"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9334
PAGES = [int(x) for x in sys.argv[1:]] or [1, 2, 3, 4, 6, 10, 14, 18, 22, 27, 31, 34]


async def main():
    SHOTS.mkdir(exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless", "--disable-gpu", f"--remote-debugging-port={PORT}",
        "--no-first-run", "--window-size=1400,1200",
        "--user-data-dir=" + str(ROOT / "out" / "chrome-profile2"), "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json") as r:
                    page = next(t for t in json.load(r) if t["type"] == "page")
                ws_url = page["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.3)

        async with websockets.connect(ws_url, max_size=200 * 1024 * 1024) as ws:
            mid = 0

            async def call(method, **params):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})

            await call("Page.enable")
            await call("Page.navigate", url=HTML.as_uri())
            deadline = time.time() + 120
            while time.time() < deadline:
                r = await call("Runtime.evaluate", expression="window.__pagedDone === true")
                if r.get("result", {}).get("value") is True:
                    break
                await asyncio.sleep(1)

            for p in PAGES:
                r = await call("Runtime.evaluate", expression=f"""
                    (() => {{ const el = document.querySelectorAll('.pagedjs_page')[{p-1}];
                       if (!el) return null; const b = el.getBoundingClientRect();
                       return JSON.stringify({{x:b.x+scrollX, y:b.y+scrollY, w:b.width, h:b.height}}); }})()""")
                v = r.get("result", {}).get("value")
                if not v:
                    print(f"page {p}: not found"); continue
                b = json.loads(v)
                shot = await call("Page.captureScreenshot", format="png",
                                  clip={"x": b["x"], "y": b["y"], "width": b["w"],
                                        "height": b["h"], "scale": 1.3},
                                  captureBeyondViewport=True)
                f = SHOTS / f"p{p:02d}.png"
                f.write_bytes(base64.b64decode(shot["data"]))
                print(f"page {p}: {f.name} {f.stat().st_size//1024} KB")
    finally:
        proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
