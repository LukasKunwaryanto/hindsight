"""Which requests does a category page make to load its products, and how fast are they?"""
import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright


async def main(url):
    out = []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        starts = {}
        pg.on("request", lambda r: starts.__setitem__(r.url, time.monotonic()))

        async def on_resp(r):
            ct = r.headers.get("content-type", "")
            if r.request.resource_type in ("xhr", "fetch") or "json" in ct:
                try:
                    body = await r.body()
                except Exception:
                    body = b""
                out.append({"url": r.url[:300], "method": r.request.method, "status": r.status, "type": ct[:40],
                            "ms": round((time.monotonic() - starts.get(r.url, time.monotonic())) * 1000),
                            "bytes": len(body), "post": (r.request.post_data or "")[:300],
                            "sample": body[:400].decode("utf-8", "replace")})
        pg.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        await pg.goto(url, wait_until="networkidle", timeout=90000)
        await pg.wait_for_timeout(3000)
        skus = await pg.evaluate("""[...document.querySelectorAll('[data-test-id=product-card] a[data-test-id=product-link]')].slice(0,5).map(a=>a.href)""")
        await b.close()
    print(json.dumps({"page": url, "product_links": skus, "requests": out}, indent=1))

for u in sys.argv[1:]:
    asyncio.run(main(u))
