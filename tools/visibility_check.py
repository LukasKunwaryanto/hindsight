"""Visibility check: rebuild the newest stored snapshot of every startpage and inner page (desktop) as the app
shows it, and count images that are broken or invisible. The structure check (check_rebuild.py) passed while a
fade-in rule left every image at opacity 0; this one looks at what a person would see.
Exits non-zero when a page has more than ALLOWED problem images. Env: SUPABASE_URL, SUPABASE_SECRET_KEY."""
import asyncio
import functools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from playwright.async_api import async_playwright  # noqa: E402
from rebuild import rebuild  # noqa: E402
from store import SupabaseStore  # noqa: E402

ALLOWED = int(os.environ.get("HINDSIGHT_ALLOWED_PROBLEMS", "2"))
db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
url_of = functools.lru_cache(maxsize=None)(db.asset_url)

CHECK_JS = """async () => {
  await Promise.all([...document.images].map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; setTimeout(r, 8000); })));
  const W = document.documentElement.clientWidth, out = {broken: [], invisible: [], images: 0};
  for (const i of document.images) {
    const r = i.getBoundingClientRect();
    if (r.width < 40 || r.height < 40 || r.right < 0 || r.left > W) continue;   // icons, closed carousel slides
    out.images++;
    if (i.complete && i.naturalWidth === 0) { out.broken.push(i.currentSrc.slice(0, 120)); continue; }
    let op = 1, e = i, hidden = false;
    while (e && e.nodeType === 1) {
      const cs = getComputedStyle(e);
      op *= parseFloat(cs.opacity || '1');
      if (cs.visibility === 'hidden') hidden = true;
      e = e.parentElement;
    }
    if (hidden || op < 0.1) out.invisible.push(i.currentSrc.slice(0, 120));
  }
  return out;
}"""


async def main():
    markets = [m["locale"] for m in db.markets()]
    pages = [("/", m) for m in markets] + [(p["path"], p["market"]) for p in db.pages() if p["market"] in markets]
    lines, failed = ["## Visibility check", "", "| Market | Page | Images | Broken | Invisible |", "| --- | --- | --- | --- | --- |"], []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for path, m in pages:
            rows = db._req("GET", "/rest/v1/snapshots", params={"select": "id,manifest", "market": f"eq.{m}", "page": f"eq.{path}",
                                                                 "device": "eq.desktop", "order": "captured_at.desc", "limit": "1"})
            if not rows:
                continue
            html = rebuild(rows[0]["manifest"], db.get_blobs, lambda h, mime: url_of(h) or "", db.product_images)
            pg = await b.new_page(viewport={"width": 1440, "height": 900})
            try:
                await pg.set_content(html, wait_until="load", timeout=90000)
                res = await pg.evaluate(CHECK_JS)
            except Exception as e:
                res = {"images": 0, "broken": [f"page failed: {e}"[:120]], "invisible": []}
            await pg.close()
            bad = len(res["broken"]) + len(res["invisible"])
            lines.append(f"| {m} | {path} | {res['images']} | {len(res['broken'])} | {len(res['invisible'])} |")
            if bad > ALLOWED:
                failed.append(f"{m} {path}: {res['broken'][:2]} {res['invisible'][:2]}")
        await b.close()
    lines += ["", f"{len(failed)} of {len(pages)} pages have more than {ALLOWED} problem images."] + [f"- {f}" for f in failed]
    md = "\n".join(lines)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
    if failed:
        raise SystemExit(1)

asyncio.run(main())
