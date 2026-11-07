"""Probe pages: third-party hosts contacted, and the structure around product cards (grid vs carousel)."""
import asyncio
import collections
import json
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright

CARD_TREE_JS = r"""() => {
  const cards = [...document.querySelectorAll('[data-test-id="product-card"]')];
  const desc = el => el.tagName.toLowerCase() + (el.getAttribute('data-test-id') ? `[dt=${el.getAttribute('data-test-id')}]` : '')
     + (el.id ? `#${el.id.slice(0,24)}` : '') + (el.getAttribute('role') ? `[role=${el.getAttribute('role')}]` : '')
     + (el.getAttribute('aria-roledescription') ? `[ard=${el.getAttribute('aria-roledescription')}]` : '');
  const groups = {};
  for (const c of cards) {
    const chain = []; let e = c.parentElement;
    while (e && e.tagName !== 'MAIN' && chain.length < 8) { chain.push(desc(e)); e = e.parentElement; }
    const k = chain.join(' < ');
    groups[k] = (groups[k] || 0) + 1;
  }
  const lists = [...document.querySelectorAll('[data-test-id]')].map(e => e.getAttribute('data-test-id'))
     .filter(x => /list|grid|carousel|slider|product/i.test(x));
  const cnt = {}; lists.forEach(x => cnt[x] = (cnt[x] || 0) + 1);
  return {cards: cards.length, groups, test_ids: cnt};
}"""


async def main(urls):
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for url in urls:
            ctx = await b.new_context(viewport={"width": 1440, "height": 900})
            pg = await ctx.new_page()
            hosts = collections.Counter()
            paths = collections.Counter()
            own = urlparse(url).netloc

            def on_req(r):
                u = urlparse(r.url)
                if u.netloc and u.netloc != own:
                    hosts[u.netloc] += 1
                    if any(k in u.netloc for k in ("google", "analytics", "contentsquare", "facebook", "tiktok", "clarity", "bing", "criteo", "pinterest", "snap", "hotjar", "adform", "doubleclick", "voyado", "klaviyo", "trustpilot", "abtasty", "optimizely", "vwo")):
                        paths[f"{u.netloc}{u.path[:40]}"] += 1
            pg.on("request", on_req)
            await pg.goto(url, wait_until="networkidle", timeout=90000)
            for y in range(0, 20000, 800):
                await pg.evaluate(f"window.scrollTo(0,{y})")
                await pg.wait_for_timeout(150)
            await pg.wait_for_timeout(2500)
            tree = await pg.evaluate(CARD_TREE_JS)
            out[url] = {"hosts": hosts.most_common(), "tracking_paths": paths.most_common(40), **tree}
            await ctx.close()
        await b.close()
    print("PROBE_JSON_START"); print(json.dumps(out)); print("PROBE_JSON_END")

asyncio.run(main(sys.argv[1:]))
