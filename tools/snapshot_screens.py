"""Screenshot the newest stored snapshot of some markets, rebuilt as the app shows it, and list images
that don't load in it. Output: screens/<market>-<device>.png and screens/report.txt"""
import asyncio
import functools
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from playwright.async_api import async_playwright  # noqa: E402
from rebuild import rebuild  # noqa: E402
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
markets = sys.argv[1].split(",")
devices = (sys.argv[2] if len(sys.argv) > 2 else "desktop").split(",")
out = Path("screens")
out.mkdir(exist_ok=True)
url_of = functools.lru_cache(maxsize=None)(db.asset_url)
BROKEN_JS = """() => [...document.images].filter(i => i.complete && i.naturalWidth === 0 && i.currentSrc)
                     .map(i => i.currentSrc.slice(0, 160))"""


async def main():
    lines = []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for m in markets:
            for dev in devices:
                rows = db._req("GET", "/rest/v1/snapshots", params={"select": "id,captured_at,manifest,page_height",
                               "market": f"eq.{m}", "device": f"eq.{dev}", "order": "captured_at.desc", "limit": "1"})
                if not rows:
                    lines.append(f"{m} {dev}: no snapshot")
                    continue
                html = rebuild(rows[0]["manifest"], db.get_blobs, lambda h, mime: url_of(h) or "")
                w = 1440 if dev == "desktop" else 390
                pg = await b.new_page(viewport={"width": w, "height": 900})
                await pg.set_content(html, wait_until="load", timeout=90000)
                await pg.wait_for_timeout(3000)
                broken = await pg.evaluate(BROKEN_JS)
                await pg.screenshot(path=str(out / f"{m.replace(':', '_')}-{dev}.png"), full_page=True)
                await pg.close()
                lines.append(f"{m} {dev} {rows[0]['captured_at'][:16]} height={rows[0]['page_height']} broken_images={len(broken)}")
                lines += [f"   {u}" for u in broken[:8]]
        await b.close()
    (out / "report.txt").write_text("\n".join(lines))
    print("\n".join(lines))

asyncio.run(main())
