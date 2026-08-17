# -*- coding: utf-8 -*-
"""Печать document.html в PDF через Chrome DevTools Protocol с ожиданием paged.js."""
import asyncio, base64, json, subprocess, sys, time, urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "out" / "document.html"
PDF = ROOT / "out" / "document.pdf"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9333


async def main():
    proc = subprocess.Popen([
        CHROME, "--headless", "--disable-gpu", f"--remote-debugging-port={PORT}",
        "--no-first-run", "--user-data-dir=" + str(ROOT / "out" / "chrome-profile"),
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json") as r:
                    targets = json.load(r)
                page = next(t for t in targets if t["type"] == "page")
                ws_url = page["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.3)
        if not ws_url:
            sys.exit("no debug target")

        async with websockets.connect(ws_url, max_size=200 * 1024 * 1024) as ws:
            mid = 0

            async def call(method, **params):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        return msg.get("result", {})

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Page.navigate", url=HTML.as_uri())

            # ждём сигнал PAGED_DONE из документа (console.log)
            deadline = time.time() + 120
            done = False
            while time.time() < deadline and not done:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except asyncio.TimeoutError:
                    # опрос напрямую
                    r = await call("Runtime.evaluate", expression="window.__pagedDone === true")
                    done = r.get("result", {}).get("value") is True
                    continue
                if msg.get("method") == "Runtime.consoleAPICalled":
                    args = msg["params"].get("args", [])
                    if args and args[0].get("value") == "PAGED_DONE":
                        done = True
            if not done:
                sys.exit("paged.js did not finish in 120s")

            pages = await call("Runtime.evaluate",
                               expression="document.querySelectorAll('.pagedjs_page').length")
            n = pages.get("result", {}).get("value")
            print(f"paged.js done, pages: {n}")

            r = await call("Page.printToPDF", printBackground=True,
                           preferCSSPageSize=True, marginTop=0, marginBottom=0,
                           marginLeft=0, marginRight=0, transferMode="ReturnAsBase64")
            PDF.write_bytes(base64.b64decode(r["data"]))
            print(f"PDF: {PDF} ({PDF.stat().st_size/1024:.0f} KB)")
    finally:
        proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
