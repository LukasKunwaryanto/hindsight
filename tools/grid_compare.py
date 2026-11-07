"""Side by side: a listing grid on the live site and the same grid as stored (slim placeholder cards).
Usage: python tools/grid_compare.py <store.pkl> <rebuilt.html> <live url> <device> <out prefix>"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from playwright.async_api import async_playwright  # noqa: E402
from browser import DEVICES, is_tracking  # noqa: E402

html_path, url, device, out = sys.argv[2:6]
GRID_LIVE = """() => { const c = document.querySelector('li [data-test-id="product-card"]'); const ul = c && c.closest('ul');
  if (!ul) return null; ul.scrollIntoView(); const r = ul.getBoundingClientRect();
  return {x: r.left, y: r.top + scrollY, w: r.width, h: Math.min(r.height, 1100)}; }"""
GRID_STORED = """() => { const c = document.querySelector('.hs-gcard'); const ul = c && c.closest('ul');
  if (!ul) return null; const r = ul.getBoundingClientRect();
  return {x: r.left, y: r.top + scrollY, w: r.width, h: Math.min(r.height, 1100)}; }"""


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for name, target, js in (("live", url, GRID_LIVE), ("stored", "file://" + os.path.abspath(html_path), GRID_STORED)):
            ctx = await b.new_context(**DEVICES[device])
            await ctx.route(lambda u: is_tracking(u), lambda r: r.abort())
            pg = await ctx.new_page()
            await pg.goto(target, wait_until="networkidle", timeout=90000)
            await pg.evaluate("document.querySelectorAll('[id^=ccc]').forEach(e => e.remove())")
            for y in range(0, 6000, 600):
                await pg.evaluate(f"window.scrollTo(0,{y})"); await pg.wait_for_timeout(200)
            box = await pg.evaluate(js)
            await pg.wait_for_timeout(2000)
            if box:
                await pg.screenshot(path=f"{out}-{name}.png", full_page=True,
                                    clip={"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"]})
            print(name, box)
            await ctx.close()
        await b.close()

asyncio.run(main())
