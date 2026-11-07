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
  const cont = document.getElementById('container');
  const top = cont ? [...cont.children].map(c => ({tag: c.tagName.toLowerCase(), id: c.id, dt: c.getAttribute('data-test-id'),
      cls: (c.className || '').toString().slice(0, 60), cards: c.querySelectorAll('[data-test-id="product-card"]').length,
      listing: !!c.querySelector('[data-test-id="product-listing-ref"]'), h: Math.round(c.getBoundingClientRect().height),
      text: (c.innerText || '').replace(/\s+/g, ' ').slice(0, 90),
      kids: [...c.children].slice(0, 6).map(k => k.tagName.toLowerCase() + (k.getAttribute('data-test-id') ? '[' + k.getAttribute('data-test-id') + ']' : '') + (k.id ? '#' + k.id.slice(0, 22) : ''))})) : [];
  const ref = document.querySelector('[data-test-id="product-listing-ref"]');
  const refInfo = ref ? {tag: ref.tagName, cardsInside: ref.querySelectorAll('[data-test-id="product-card"]').length,
      parentCards: ref.parentElement.querySelectorAll('[data-test-id="product-card"]').length} : null;
  const ul = cards.length ? cards[cards.length - 1].closest('ul') : null;
  const ulInfo = ul ? {lis: ul.children.length, cls: (ul.className || '').toString().slice(0, 80), parentDt: ul.parentElement.getAttribute('data-test-id'),
      chainDt: (() => { const a = []; let e = ul; while (e && e.id !== 'container') { if (e.getAttribute('data-test-id')) a.push(e.getAttribute('data-test-id')); e = e.parentElement; } return a; })()} : null;
  return {cards: cards.length, groups, test_ids: cnt, top, refInfo, ulInfo};
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
            ogoun = []
            pg.on("request", lambda r: ogoun.append(r.url[:160]) if "ogoun." in r.url else None)
            await pg.goto(url, wait_until="networkidle", timeout=90000)
            for y in range(0, 20000, 800):
                await pg.evaluate(f"window.scrollTo(0,{y})")
                await pg.wait_for_timeout(150)
            await pg.wait_for_timeout(2500)
            tree = await pg.evaluate(CARD_TREE_JS)
            out[url] = {"hosts": hosts.most_common(), "tracking_paths": paths.most_common(40), **tree}
            out[url]["ogoun"] = [r for r in ogoun][:5]
            await ctx.close()
        await b.close()
    print("PROBE_JSON_START"); print(json.dumps(out)); print("PROBE_JSON_END")

asyncio.run(main(sys.argv[1:]))
