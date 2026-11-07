"""Check a stored snapshot once, right after it is saved: rebuild it and compare with the live page it was
captured from (visible images, page height, first screen). Stored pieces never change, so a snapshot that
passes here stays correct until the rebuild code changes (tested separately) or source images disappear."""
import asyncio
import io

from PIL import Image, ImageChops, ImageFilter

from browser import DEVICES, VISIBLE_JS, WAIT_IMAGES_JS

# A first screen differing in more than this share of pixels is flagged (carousels and countdowns move a little)
MAX_DIFF = 0.25


def _img(b: bytes, size=None):
    im = Image.open(io.BytesIO(b)).convert("RGB")
    return im.resize(size) if size and im.size != size else im


def _changed(a, b):
    return ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 40 else 0)


def pixel_diff(live: bytes, stored: bytes, stored_later: bytes | None = None) -> float:
    """Share of still pixels that differ clearly between the live and stored first screen. Pixels that move on
    the stored page by themselves (video, animated images, carousels, countdowns) are left out."""
    a = _img(live)
    b = _img(stored, a.size)
    d = _changed(a, b)
    still = a.size[0] * a.size[1]
    if stored_later:
        moving = _changed(b, _img(stored_later, a.size)).filter(ImageFilter.MaxFilter(15))
        d = ImageChops.subtract(d, moving)
        still -= moving.histogram()[255]
    return round(d.histogram()[255] / max(still, 1), 3)


async def verify(browser, html: str, device: str, live: dict) -> dict:
    ctx = await browser.new_context(device_scale_factor=1, **DEVICES[device])
    try:
        page = await ctx.new_page()
        await page.set_content(html, wait_until="load", timeout=90_000)
        await page.evaluate(WAIT_IMAGES_JS, 10_000)
        await page.wait_for_timeout(500)
        got = await page.evaluate(VISIBLE_JS)
        shot = await page.screenshot(type="png")
        await page.wait_for_timeout(1200)
        later = await page.screenshot(type="png")
    finally:
        await ctx.close()
    diff = pixel_diff(live["shot"], shot, later) if live.get("shot") else None
    problems = []
    if got["images"] < live["images"] * 0.9 - 1:
        problems.append(f"{live['images'] - got['images']} images missing or invisible")
    if live["height"] and abs(got["height"] - live["height"]) > 0.15 * live["height"]:
        problems.append(f"height {got['height']} vs {live['height']}")
    if diff is not None and diff > MAX_DIFF:
        problems.append(f"first screen {round(diff * 100)}% different")
    return {"_shots": (live.get("shot"), shot), "ok": not problems, "problems": problems, "images_live": live["images"], "images_stored": got["images"],
            "height_live": live["height"], "height_stored": got["height"], "first_screen_diff": diff}
