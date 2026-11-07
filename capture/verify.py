"""Check a stored snapshot once, right after it is saved: rebuild it and compare with the live page it was
captured from (visible images, page height, first screen). Stored pieces never change, so a snapshot that
passes here stays correct until the rebuild code changes (tested separately) or source images disappear."""
import asyncio
import io

from PIL import Image, ImageChops

from browser import DEVICES, VISIBLE_JS, WAIT_IMAGES_JS

# A first screen differing in more than this share of pixels is flagged (carousels and countdowns move a little)
MAX_DIFF = 0.25


def pixel_diff(a: bytes, b: bytes) -> float:
    """Share of pixels that differ clearly between two screenshots of the same size."""
    ia, ib = Image.open(io.BytesIO(a)).convert("RGB"), Image.open(io.BytesIO(b)).convert("RGB")
    if ia.size != ib.size:
        ib = ib.resize(ia.size)
    d = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 40 else 0)
    return round(d.histogram()[255] / (ia.size[0] * ia.size[1]), 3)


async def verify(browser, html: str, device: str, live: dict) -> dict:
    ctx = await browser.new_context(device_scale_factor=1, **DEVICES[device])
    try:
        page = await ctx.new_page()
        await page.set_content(html, wait_until="load", timeout=90_000)
        await page.evaluate(WAIT_IMAGES_JS, 10_000)
        await page.wait_for_timeout(500)
        got = await page.evaluate(VISIBLE_JS)
        shot = await page.screenshot(type="png")
    finally:
        await ctx.close()
    diff = pixel_diff(live["shot"], shot) if live.get("shot") else None
    problems = []
    if got["images"] < live["images"] * 0.9 - 1:
        problems.append(f"{live['images'] - got['images']} images missing or invisible")
    if live["height"] and abs(got["height"] - live["height"]) > 0.15 * live["height"]:
        problems.append(f"height {got['height']} vs {live['height']}")
    if diff is not None and diff > MAX_DIFF:
        problems.append(f"first screen {round(diff * 100)}% different")
    return {"ok": not problems, "problems": problems, "images_live": live["images"], "images_stored": got["images"],
            "height_live": live["height"], "height_stored": got["height"], "first_screen_diff": diff}
