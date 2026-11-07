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

# Captures must not count as visits: block tag managers, analytics and ad pixels. The site's own
# server-side tagging endpoint is ogoun.<site> (GA4 /g/collect). The site still fills window.dataLayer
# itself, so promotion names keep working.
TRACKING_HOSTS = ("googletagmanager.com", "google-analytics.com", "analytics.google.com", "doubleclick.net",
                  "googleadservices.com", "bat.bing.com", "bat.bing.net", "clarity.ms", "facebook.net",
                  "facebook.com", "connect.facebook", "tiktok.com", "analytics.tiktok", "contentsquare.net",
                  "hotjar.com", "criteo.com", "criteo.net", "pinterest.com", "pinimg.com/ct", "snapchat.com",
                  "adform.net", "adnxs.com", "linkedin.com/px", "ads.linkedin")


def is_tracking(url: str) -> bool:
    host = url.split("/", 3)[2] if "://" in url else ""
    return host.startswith("ogoun.") or any(t in url for t in TRACKING_HOSTS)

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

PLACEHOLDER_SEL = 'img[src^="data:image/svg+xml"][src*="viewBox=%220%200%20400%20400%22"]'
PLACEHOLDERS_JS = """async (maxMs) => {
  const sel = '""" + PLACEHOLDER_SEL.replace("'", "\\'") + """';
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs && document.querySelectorAll(sel).length) await new Promise(r => setTimeout(r, 500));
  return document.querySelectorAll(sel).length;
}"""

SETTLE_JS = """async (maxMs) => {
  const t0 = Date.now(); let last = -1, same = 0;
  while (Date.now() - t0 < maxMs && same < 3) {
    const n = document.getElementsByTagName('*').length;
    same = n === last ? same + 1 : 0; last = n;
    await new Promise(r => setTimeout(r, 500));
  }
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
    blocked = []

    async def block(route):
        blocked.append(route.request.url[:120])
        await route.abort()
    await ctx.route(lambda u: is_tracking(u), block)
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
        # Content loaded after scrolling (product carousels) can arrive late when the machine is busy:
        # wait for the network to go quiet and the page to stop growing before freezing it.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await page.evaluate(SETTLE_JS, 15_000)
        # Product carousels show grey placeholders until their data arrives: wait for them
        for _ in range(2):
            left = await page.evaluate(PLACEHOLDERS_JS, 20_000)
            if not left:
                break
            await page.evaluate(f"document.querySelector('{PLACEHOLDER_SEL}')?.scrollIntoView({{block:'center'}})")
            await page.wait_for_timeout(1500)
            await page.evaluate("window.scrollTo(0,0)")
        placeholders = await page.evaluate(f"document.querySelectorAll('{PLACEHOLDER_SEL}').length")
        not_loaded = await page.evaluate(WAIT_IMAGES_JS, 10_000)
        await page.wait_for_timeout(1500)
        await page.evaluate("document.querySelectorAll('[id^=ccc]').forEach(e => e.remove())")
        # The site reports every promotion it shows (view_promotion): keep its exact IDs and names
        promotions = await page.evaluate("""() => (window.dataLayer || [])
          .filter(e => e && e.event === 'view_promotion' && e.ecommerce)
          .map(e => ({id: String(e.ecommerce.promotion_id || ''), name: String(e.ecommerce.promotion_name || ''),
                      creative: String(e.ecommerce.creative_name || '')}))""")
        frozen = await page.evaluate(FREEZE_JS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        frozen.update(status=status, resources=resources, images_not_loaded=not_loaded, placeholders=placeholders,
                      promotions=promotions, tracking_blocked=len(blocked))
        return frozen
    finally:
        await ctx.close()


def sha16(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()[:16]
