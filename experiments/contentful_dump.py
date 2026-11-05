"""Experiment: everything Contentful knows about the sv-SE startpage right now, plus the
real capture rendered to an image, for comparing a Contentful-only rebuild with reality.
Writes out/startpage_sv.json, out/images/*.webp, out/real_*.png."""
import asyncio
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
SPACE, START = "p7vzp7ftmsr1", "6q7zaSZcLXtokLbBepLSgo"
TOKEN = os.environ["CONTENTFUL_DELIVERY_TOKEN"].strip()
OUT = Path("out"); (OUT / "images").mkdir(parents=True, exist_ok=True)


def cda(path, **params):
    url = f"https://cdn.contentful.com/spaces/{SPACE}/environments/master{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"}), timeout=60) as r:
        return json.loads(r.read())


# 1. Walk the tree breadth-first in sv-SE (include=0 per batch keeps responses small)
entries, assets, todo, seen = {}, {}, [START], set()
def links(v):
    if isinstance(v, dict):
        s = v.get("sys", {})
        if s.get("type") == "Link":
            yield s.get("linkType"), s.get("id"); return
        for x in v.values(): yield from links(x)
    elif isinstance(v, list):
        for x in v: yield from links(x)
PAGE_TYPES = {"productListingPage", "collection", "brand", "designer", "editorial", "room", "rootCategory", "inspiration",
              "inspirationArticle", "inspirationCategory", "recipe", "storeListing", "store", "search", "checkout"}
depth = 0
while todo and depth < 8:
    ids = [i for i in dict.fromkeys(todo) if i not in seen]; todo = []
    for i in range(0, len(ids), 40):
        res = cda("/entries", **{"sys.id[in]": ",".join(ids[i:i + 40]), "locale": "sv-SE", "include": 0, "limit": 40})
        for e in res["items"]:
            entries[e["sys"]["id"]] = e
    seen |= set(ids)
    for eid in ids:
        e = entries.get(eid)
        if not e or (eid != START and e["sys"]["contentType"]["sys"]["id"] in PAGE_TYPES):
            continue
        fields = e["fields"] if eid != START else {"content": e["fields"].get("content")}
        for t, c in links(fields):
            if t == "Entry": todo.append(c)
            elif t == "Asset": assets[c] = None
    depth += 1
ids = list(assets)
for i in range(0, len(ids), 100):
    for a in cda("/assets", **{"sys.id[in]": ",".join(ids[i:i + 100]), "locale": "sv-SE", "limit": 100})["items"]:
        assets[a["sys"]["id"]] = a
print(f"{len(entries)} entries, {len(assets)} assets, depth {depth}")

# 2. Download the images (Contentful's image API, resized), as files next to the JSON
for aid, a in assets.items():
    f = (a or {}).get("fields", {}).get("file") or {}
    if not f.get("contentType", "").startswith("image/"):
        continue
    w = min(1440, (f.get("details", {}).get("image") or {}).get("width") or 1440)
    url = "https:" + urllib.parse.quote(f["url"], safe="/:%") + ("" if f["contentType"] == "image/svg+xml" else f"?w={w}&fm=webp&q=60")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            ext = "svg" if f["contentType"] == "image/svg+xml" else "webp"
            (OUT / "images" / f"{aid}.{ext}").write_bytes(r.read())
    except Exception as ex:
        print("image failed", aid, ex)
json.dump({"entries": entries, "assets": assets}, open(OUT / "startpage_sv.json", "w"), ensure_ascii=False)

# 3. The real capture (newest sv-SE desktop snapshot) rendered to an image, as ground truth
from rebuild import rebuild  # noqa: E402
from store import SupabaseStore  # noqa: E402
db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
row = db._req("GET", "/rest/v1/snapshots", params={"select": "id,captured_at,manifest,page_height", "market": "eq.sv-SE",
                                                   "device": "eq.desktop", "order": "captured_at.desc", "limit": "1"})[0]
html = rebuild(row["manifest"], db.get_blobs, lambda h, mime: f"{os.environ['SUPABASE_URL']}/storage/v1/object/public/assets/{h[:2]}/{h}.{mime.split('/')[-1].replace('svg+xml', 'svg')}")
(OUT / "real_sv_desktop.html").write_text(html, encoding="utf-8")
json.dump({"captured_at": row["captured_at"], "id": row["id"]}, open(OUT / "real_meta.json", "w"))


async def shot():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={"width": 1440, "height": 900})
        await pg.set_content(html, wait_until="networkidle"); await pg.wait_for_timeout(1500)
        await pg.screenshot(path=str(OUT / "real_sv_desktop.png"), full_page=True); await b.close()
asyncio.run(shot())
print("done")
