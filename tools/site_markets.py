"""Print a site's markets as the site itself lists them (hreflang alternate links)."""
import asyncio
import json
import sys

from playwright.async_api import async_playwright


async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for url in sys.argv[1:]:
            pg = await b.new_page()
            try:
                resp = await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                await pg.wait_for_timeout(2000)
                links = await pg.evaluate("""[...document.querySelectorAll('link[rel=alternate][hreflang]')]
                    .map(l => [l.hreflang, l.href])""")
                out[url] = {"status": resp.status if resp else None, "final_url": pg.url,
                            "lang": await pg.evaluate("document.documentElement.lang"), "hreflang": links,
                            "has_container": await pg.evaluate("!!document.getElementById('container')")}
            except Exception as e:
                out[url] = {"error": str(e).splitlines()[0][:120]}
            await pg.close()
        await b.close()
    print(json.dumps(out, indent=1))

asyncio.run(main())
