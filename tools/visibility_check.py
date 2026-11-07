"""Visibility check: rebuild the newest stored snapshot of every startpage and inner page (desktop) as the app
shows it, and count images that are broken or invisible. The structure check (check_rebuild.py) passed while a
fade-in rule left every image at opacity 0; this one looks at what a person would see.
Exits non-zero when a page has more than ALLOWED problem images. Env: SUPABASE_URL, SUPABASE_SECRET_KEY."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from playwright.async_api import async_playwright  # noqa: E402
from rebuild import rebuild  # noqa: E402
from store import ASSET_BUCKET, EXT, SupabaseStore  # noqa: E402

ALLOWED = int(os.environ.get("HINDSIGHT_ALLOWED_PROBLEMS", "2"))
db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
# stored files live at <bucket>/<first 2 of hash>/<hash>.<ext>: no lookup needed
url_of = lambda h, mime: f"{db.url}/storage/v1/object/public/{ASSET_BUCKET}/{h[:2]}/{h}.{EXT.get(mime, 'bin')}"
_blobs: dict = {}


def get_blobs(hashes):
    """Pieces are shared between pages: fetch each once."""
    missing = [h for h in hashes if h not in _blobs]
    if missing:
        _blobs.update(db.get_blobs(missing))
    return {h: _blobs[h] for h in hashes if h in _blobs}

CHECK_JS = """async () => {
  const settle = imgs => Promise.all(imgs.map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; setTimeout(r, 8000); })));
  await settle([...document.images]);
  // retry images that failed once: the storage CDN throttles a check that loads thousands of images quickly
  const failed = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && i.currentSrc && !i.currentSrc.startsWith('data:'));
  if (failed.length) {
    await new Promise(r => setTimeout(r, 3000));
    failed.forEach(i => { const u = i.currentSrc; i.removeAttribute('loading'); i.src = u + (u.includes('?') ? '&' : '?') + 'retry=1'; });
    await settle(failed);
  }
  const W = document.documentElement.clientWidth, out = {broken: [], invisible: [], images: 0};
  for (const i of document.images) {
    const r = i.getBoundingClientRect();
    if (r.width < 40 || r.height < 40 || r.right < 0 || r.left > W) continue;   // icons, closed carousel slides
    out.images++;
    if (i.complete && i.naturalWidth === 0) { out.broken.push(i.currentSrc.slice(0, 120)); continue; }
    // the image itself transparent or hidden (as with the fade-in bug of 5 Nov). Containers hidden on purpose
    // (hover popups of "shop the look" hotspots, closed menus) don't count.
    const cs = getComputedStyle(i);
    const op = parseFloat(cs.opacity || '1'), hidden = cs.visibility === 'hidden';
    if (hidden || op < 0.1) {
      let z = i; while (z && z.nodeType === 1 && parseFloat(getComputedStyle(z).opacity || '1') >= 0.1 && getComputedStyle(z).visibility !== 'hidden') z = z.parentElement;
      out.invisible.push(i.currentSrc.slice(0, 120));
      (out.why = out.why || []).push({y: Math.round(r.top + scrollY), w: Math.round(r.width), el: z ? z.outerHTML.slice(0, 260) : '',
        sib: z && z.parentElement ? z.parentElement.children.length : 0});
    }
  }
  return out;
}"""


async def main():
    markets = [m["locale"] for m in db.markets()]
    pages = [("/", m) for m in markets] + [(p["path"], p["market"]) for p in db.pages() if p["market"] in markets]
    only = os.environ.get("HINDSIGHT_ONLY")  # e.g. "en-US@/tableware/" to look at one page
    if only:
        pages = [(pg, m) for pg, m in pages if f"{m}@{pg}" == only]
    lines, failed = ["## Visibility check", "", "| Market | Page | Images | Broken | Invisible |", "| --- | --- | --- | --- | --- |"], []
    slots = asyncio.Semaphore(2)
    lock = asyncio.Lock()

    async def check(b, path, m):
        async with slots:
            rows = await asyncio.to_thread(db._req, "GET", "/rest/v1/snapshots", None, None, False, {
                "select": "id,manifest", "market": f"eq.{m}", "page": f"eq.{path}", "device": "eq.desktop",
                "order": "captured_at.desc", "limit": "1"})
            if not rows:
                return
            async with lock:  # the piece cache is shared
                html = await asyncio.to_thread(rebuild, rows[0]["manifest"], get_blobs, url_of, db.product_images)
            pg = await b.new_page(viewport={"width": 1440, "height": 900})
            try:
                await pg.set_content(html, wait_until="load", timeout=90000)
                res = await pg.evaluate(CHECK_JS)
            except Exception as e:
                res = {"images": 0, "broken": [f"page failed: {e}"[:120]], "invisible": []}
            await pg.close()
            bad = len(res["broken"]) + len(res["invisible"])
            lines.append(f"| {m} | {path} | {res['images']} | {len(res['broken'])} | {len(res['invisible'])} |")
            if os.environ.get("HINDSIGHT_ONLY"):
                print(__import__("json").dumps(res.get("why", [])[:6], indent=1))
            if bad > ALLOWED:
                failed.append(f"{m} {path}: {res['broken'][:2]} {res['invisible'][:2]}")

    async with async_playwright() as p:
        b = await p.chromium.launch()
        await asyncio.gather(*[check(b, path, m) for path, m in pages])
        await b.close()
    lines.sort()
    lines += ["", f"{len(failed)} of {len(pages)} pages have more than {ALLOWED} problem images."] + [f"- {f}" for f in failed]
    md = "\n".join(lines)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
    if failed:
        raise SystemExit(1)

asyncio.run(main())
