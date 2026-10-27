"""Open a page the way a customer sees it and freeze it for storage."""
import asyncio
import hashlib

DEVICES = {
    "desktop": dict(viewport={"width": 1440, "height": 900}),
    "mobile": dict(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    ),
}

CHALLENGE_MARKERS = ["Just a moment", "challenges.cloudflare.com", "cf-chl-", "Verify you are human"]

# Runs in the page after everything has loaded. Records each image's displayed size,
# pins its chosen source, and reads every stylesheet from the browser (most CSS is
# injected by JavaScript and is not in the HTML).
FREEZE_JS = r"""() => {
  document.querySelectorAll('picture source').forEach(s => s.remove());
  const imgs = [];
  document.querySelectorAll('img').forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const src = el.currentSrc || el.src;
    el.setAttribute('data-hs-id', i);
    el.removeAttribute('srcset'); el.removeAttribute('srcSet'); el.removeAttribute('sizes');
    el.setAttribute('loading', 'eager'); el.setAttribute('src', src);
    imgs.push({id: i, src, w: r.width, h: r.height});
  });
  const sheets = [];
  for (const sh of document.styleSheets) {
    let css = '';
    try { css = [...sh.cssRules].map(r => r.cssText).join('\n'); } catch (e) { css = ''; }
    sheets.push({href: sh.href, media: sh.media.mediaText, css});
  }
  return {imgs, sheets, html: '<!DOCTYPE html>\n' + document.documentElement.outerHTML,
          title: document.title, lang: document.documentElement.lang,
          height: document.documentElement.scrollHeight};
}"""

WAIT_IMAGES_JS = """async (ms) => {
  const imgs = [...document.images].filter(i => !i.complete);
  await Promise.race([
    Promise.all(imgs.map(i => new Promise(r => { i.onload = i.onerror = r; }))),
    new Promise(r => setTimeout(r, ms)),
  ]);
  return [...document.images].filter(i => !i.complete).length;
}"""


async def capture_page(browser, url: str, device: str, locale: str, extra_header: str | None = None) -> dict:
    """Load, scroll and freeze a page. Returns frozen HTML, image/sheet info and the
    bytes of every image, font and stylesheet the page loaded."""
    ctx = await browser.new_context(locale=locale, device_scale_factor=1, **DEVICES[device])
    if extra_header and ":" in extra_header:
        name, value = extra_header.split(":", 1)
        await ctx.set_extra_http_headers({name.strip(): value.strip()})
    page = await ctx.new_page()
    resources: dict[str, tuple[bytes, str]] = {}
    pending = []

    async def on_response(resp):
        try:
            if resp.request.resource_type in ("image", "font", "stylesheet"):
                body = await resp.body()
                if body:
                    resources[resp.url] = (body, resp.headers.get("content-type", ""))
        except Exception:
            pass

    page.on("response", lambda r: pending.append(asyncio.ensure_future(on_response(r))))
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        status = resp.status if resp else None
        await page.wait_for_timeout(2500)
        html0 = await page.content()
        challenged = [m for m in CHALLENGE_MARKERS if m in html0]
        if (status or 0) >= 400 or challenged:
            raise RuntimeError(f"Page blocked or failed: HTTP {status}, markers {challenged}")

        await page.evaluate("document.querySelectorAll('[id^=ccc]').forEach(e => e.remove())")
        await page.add_style_tag(content="*{content-visibility:visible!important}")
        height, y = await page.evaluate("document.body.scrollHeight"), 0
        while y < height:
            y += 600
            await page.evaluate(f"window.scrollTo(0,{y})")
            await page.wait_for_timeout(300)
            height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0,0)")
        not_loaded = await page.evaluate(WAIT_IMAGES_JS, 10_000)
        await page.wait_for_timeout(1500)
        await page.evaluate("document.querySelectorAll('[id^=ccc]').forEach(e => e.remove())")
        frozen = await page.evaluate(FREEZE_JS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        frozen.update(status=status, resources=resources, images_not_loaded=not_loaded)
        return frozen
    finally:
        await ctx.close()


def sha16(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()[:16]
