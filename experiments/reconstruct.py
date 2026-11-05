"""Experiment: rebuild the sv-SE startpage as it was at a past moment, from Contentful history alone,
and compare it with Hindsight's real capture from that moment.

Visibility rule (how the site works): an entry shows only if it and every entry above it, up to the
page, are published and linked. At time T an entry counts as published if its latest publish
(snapshot) is at or before T and no known scheduled unpublish came after that publish.
Manual unpublishes leave no record; those cases are reported as "uncertain".

Env: CONTENTFUL_MANAGEMENT_TOKEN, SUPABASE_URL, SUPABASE_SECRET_KEY.
Usage: python experiments/reconstruct.py 2026-10-28T14:12 2026-10-30T15:16 2026-11-02T15:36
Writes out/recon/<time>/ (entries, images, borrowed parts, screenshots, compare.json).
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from rebuild import rebuild  # noqa: E402
from store import SupabaseStore  # noqa: E402

SPACE, ENV, START, LOCALE = "p7vzp7ftmsr1", "master", "6q7zaSZcLXtokLbBepLSgo", "sv-SE"
CMA = f"https://api.contentful.com/spaces/{SPACE}"
TOKEN = os.environ["CONTENTFUL_MANAGEMENT_TOKEN"].strip()
OUT = Path("out/recon")
ENTRY_ID_RE = re.compile(r'id="([A-Za-z0-9]{20,24})"')
PAGE_TYPES = {"productListingPage", "collection", "brand", "designer", "editorial", "room", "rootCategory", "inspiration",
              "inspirationArticle", "inspirationCategory", "recipe", "storeListing", "store", "search", "checkout"}
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))


class TooBig(Exception):
    pass


def cma(path_or_url, **params):
    url = path_or_url if path_or_url.startswith("http") else CMA + path_or_url
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("X-Contentful-RateLimit-Reset", 1)) + 0.5); continue
            if e.code == 404:
                return None
            body = e.read()
            if e.code == 400 and b"Response size too big" in body:
                raise TooBig()
            raise RuntimeError(f"CMA {e.code} {url.split('?')[0]}: {body[:200]}")
    raise RuntimeError("rate limited")


# --- Contentful history ------------------------------------------------------------
locales = {l["code"]: l for l in cma(f"/environments/{ENV}/locales")["items"]}
DEFAULT = next(c for c, l in locales.items() if l.get("default"))


def chain(code):
    out = []
    while code and code not in out:
        out.append(code); code = locales.get(code, {}).get("fallbackCode")
    if DEFAULT not in out:
        out.append(DEFAULT)
    return out


CHAIN = chain(LOCALE)


def localize(fields):
    out = {}
    for k, v in (fields or {}).items():
        for loc in CHAIN:
            if isinstance(v, dict) and loc in v:
                out[k] = v[loc]; break
    return out


_snaps = {}


def snapshots(eid):
    """All publish snapshots of an entry, oldest first (None if the entry no longer exists)."""
    if eid not in _snaps:
        items, skip, limit = [], 0, 100
        while True:
            try:
                res = cma(f"/environments/{ENV}/entries/{eid}/snapshots", limit=limit, skip=skip)
            except TooBig:
                if limit == 1:
                    raise
                limit = max(1, limit // 2); continue
            if res is None:
                _snaps[eid] = None; break
            items += res["items"]; skip += len(res["items"])
            if skip >= res.get("total", 0):
                _snaps[eid] = sorted(items, key=lambda s: s["sys"]["createdAt"]); break
    return _snaps[eid]


def scheduled_unpublishes():
    """entity id -> sorted list of times it was unpublished by a scheduled action."""
    out, url = {}, f"/scheduled_actions?environment.sys.id={ENV}&sys.status[in]=succeeded&limit=100"
    while url:
        res = cma(url)
        if res is None:
            raise RuntimeError(f"scheduled actions not found: {url}")
        for a in res["items"]:
            if a["action"] == "unpublish":
                out.setdefault(a["entity"]["sys"]["id"], []).append(ts(a["scheduledFor"]["datetime"]))
        nxt = (res.get("pages") or {}).get("next")
        url = ("https://api.contentful.com" + nxt if nxt and nxt.startswith("/spaces") else nxt) if nxt and res["items"] else None
    return {k: sorted(v) for k, v in out.items()}


UNPUB = scheduled_unpublishes()
print(f"{sum(len(v) for v in UNPUB.values())} scheduled unpublishes on record", flush=True)


def state_at(eid, T):
    """(fields of the version live at T or None, status: live | not_live | uncertain | gone)."""
    snaps = snapshots(eid)
    if snaps is None:
        return None, "gone"
    before = [s for s in snaps if ts(s["sys"]["createdAt"]) <= T]
    if not before:
        return None, "not_live"
    last = before[-1]
    t_pub = ts(last["sys"]["createdAt"])
    if any(t_pub < u <= T for u in UNPUB.get(eid, [])):
        return None, "not_live"
    # published, and no known unpublish since. A later publish or a still-published entry makes it near certain.
    later = [s for s in snaps if ts(s["sys"]["createdAt"]) > T]
    status = "live" if later or eid in CURRENT_PUBLISHED else "uncertain"
    return last["snapshot"], status


CURRENT_PUBLISHED = set()


def links(v):
    if isinstance(v, dict):
        s = v.get("sys", {})
        if s.get("type") == "Link":
            yield s.get("linkType"), s.get("id"); return
        for x in v.values(): yield from links(x)
    elif isinstance(v, list):
        for x in v: yield from links(x)


def reconstruct(T):
    entries, assets, statuses = {}, set(), {}
    todo, seen = [START], set()
    while todo:
        eid = todo.pop(0)
        if eid in seen:
            continue
        seen.add(eid)
        snap, st = state_at(eid, T)
        statuses[eid] = st
        if snap is None:
            continue  # not live at T: it and everything below it don't show
        ctid = snap["sys"]["contentType"]["sys"]["id"]
        fields = localize(snap["fields"])
        entries[eid] = {"sys": {"id": eid, "contentType": {"sys": {"id": ctid}}}, "fields": fields}
        if eid != START and ctid in PAGE_TYPES:
            continue
        walk = {"content": fields.get("content")} if eid == START else fields
        for typ, child in links(walk):
            if typ == "Entry":
                todo.append(child)
            elif typ == "Asset":
                assets.add(child)
    return entries, assets, statuses


def fetch_assets(ids, folder):
    out = {}
    (folder / "images").mkdir(parents=True, exist_ok=True)
    for aid in ids:
        a = cma(f"/environments/{ENV}/assets/{aid}")
        if not a:
            continue
        f = localize(a.get("fields", {})).get("file") or {}
        out[aid] = {"fields": {"file": f}}
        if not (f.get("contentType") or "").startswith("image/"):
            continue
        w = min(1440, (f.get("details", {}).get("image") or {}).get("width") or 1440)
        url = "https:" + urllib.parse.quote(f["url"], safe="/:%") + ("" if f["contentType"] == "image/svg+xml" else f"?w={w}&fm=webp&q=60")
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                ext = "svg" if f["contentType"] == "image/svg+xml" else "webp"
                (folder / "images" / f"{aid}.{ext}").write_bytes(r.read())
        except Exception as ex:
            print("image failed", aid, ex)
    return out


# --- Real captures (answer key and borrowed site parts) ------------------------------
db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
asset_src = lambda h, mime: f"{os.environ['SUPABASE_URL']}/storage/v1/object/public/assets/{h[:2]}/{h}.{mime.split('/')[-1].replace('svg+xml', 'svg')}"


def snapshot_near(T=None):
    params = {"select": "id,captured_at,manifest", "market": f"eq.{LOCALE}", "device": "eq.desktop",
              "order": "captured_at.desc", "limit": "1"}
    if T:
        params["captured_at"] = f"lte.{T.isoformat()}"
    return db._req("GET", "/rest/v1/snapshots", params=params)[0]


def real_entry_ids(manifest):
    """Entry ids actually on the page: every Contentful id inside the main content blocks."""
    texts = db.get_blobs([m["ref"] for m in manifest["modules"] if m["label"].startswith("block:")])
    top, every = [], set()
    for m in manifest["modules"]:
        if m["label"].startswith("block:") and m["label"] != "block:anon":
            top.append(m["label"][6:])
            every |= set(ENTRY_ID_RE.findall(texts.get(m["ref"], "")))
    return top, every


async def screenshot(html_path, png_path, crops=None):
    """Full-page screenshot; crops: {name: css selector or 'header'/'footer'} cut out as separate PNGs."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={"width": 1440, "height": 900})
        await pg.goto(f"file://{html_path.resolve()}", wait_until="networkidle"); await pg.wait_for_timeout(1500)
        await pg.screenshot(path=str(png_path), full_page=True)
        for name, rect_js in (crops or {}).items():
            r = await pg.evaluate(rect_js)
            if r and r["height"] > 4:
                await pg.screenshot(path=str(png_path.parent / "borrowed" / f"{name}.png"), full_page=True,
                                    clip={"x": 0, "y": r["y"], "width": 1440, "height": r["height"]})
        await b.close()


async def main():
    targets = [ts(a if "+" in a or a.endswith("Z") else a + ":00+00:00") for a in sys.argv[1:]]
    # which entries are published right now (to tell "still live" from "uncertain"): the Delivery API
    skip = 0
    while True:
        res = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"https://cdn.contentful.com/spaces/{SPACE}/environments/{ENV}/entries?select=sys.id&limit=1000&skip={skip}",
            headers={"Authorization": f"Bearer {os.environ['CONTENTFUL_DELIVERY_TOKEN'].strip()}"}), timeout=120).read())
        CURRENT_PUBLISHED.update(i["sys"]["id"] for i in res["items"]); skip += 1000
        if skip >= res["total"]:
            break
    print(f"{len(CURRENT_PUBLISHED)} entries published now", flush=True)

    # The site's fonts (design system, not page content), from our stored files
    Path("fonts").mkdir(exist_ok=True)
    for key, file in (("gthaptik", "haptik.woff2"), ("intervariable", "inter.woff2")):
        rows = db._req("GET", "/rest/v1/assets", params={"select": "hash", "source_path": f"like.*{key}*", "limit": "1"}) or []
        if rows:
            with urllib.request.urlopen(asset_src(rows[0]["hash"], "font/woff2"), timeout=60) as r:
                (Path("fonts") / file).write_bytes(r.read())

    # Borrowed site parts come from today's capture (a backfill only has later captures)
    latest = snapshot_near()
    borrow_dir = OUT / "borrow_source"; borrow_dir.mkdir(parents=True, exist_ok=True)
    (borrow_dir / "page.html").write_text(rebuild(latest["manifest"], db.get_blobs, asset_src), encoding="utf-8")
    (borrow_dir / "borrowed").mkdir(exist_ok=True)
    carousel_ids = [m["label"][6:] for m in latest["manifest"]["modules"] if m["label"].startswith("block:")]
    crops = {
        "header": "(() => { const m = document.querySelector('main'); return m ? {y: 0, height: m.getBoundingClientRect().top + scrollY} : null })()",
        "footer": "(() => { const f = document.querySelector('footer'); if (!f) return null; const r = f.getBoundingClientRect();"
                  " return {y: r.top + scrollY, height: document.documentElement.scrollHeight - r.top - scrollY} })()",
    }
    for cid in carousel_ids:  # product carousels: cut out by their entry id
        crops[cid] = (f"(() => {{ const e = document.getElementById('{cid}'); if (!e || !e.querySelector('[data-test-id=\"product-card\"]')) return null;"
                      f" const r = e.getBoundingClientRect(); return {{y: r.top + scrollY, height: r.height}} }})()")
    await screenshot(borrow_dir / "page.html", borrow_dir / "page.png", crops)
    borrowed = list((borrow_dir / "borrowed").glob("*.png"))
    print("borrowed parts:", [p.stem for p in borrowed], flush=True)

    summary = []
    for T in targets:
        name = T.strftime("%Y-%m-%dT%H%M")
        folder = OUT / name; (folder / "borrowed").mkdir(parents=True, exist_ok=True)
        for p in borrowed:
            (folder / "borrowed" / p.name).write_bytes(p.read_bytes())
        entries, asset_ids, statuses = reconstruct(T)
        assets = fetch_assets(asset_ids, folder)
        (folder / "startpage_sv.json").write_text(json.dumps({"entries": entries, "assets": assets}, ensure_ascii=False))
        subprocess.run([sys.executable, str(Path(__file__).parent / "render_from_contentful.py"), str(folder), "fonts"], check=True)
        await screenshot(folder / "from_contentful.html", folder / "reconstructed.png")

        real = snapshot_near(T)
        (folder / "real.html").write_text(rebuild(real["manifest"], db.get_blobs, asset_src), encoding="utf-8")
        await screenshot(folder / "real.html", folder / "real.png")
        real_top, real_all = real_entry_ids(real["manifest"])
        start_fields = entries.get(START, {}).get("fields", {})
        recon_top = [l["sys"]["id"] for l in start_fields.get("content") or [] if l["sys"]["id"] in entries]
        stats = json.loads((folder / "render_stats.json").read_text())
        recon_all = set(stats.get("rendered_ids", []))
        both = real_all & recon_all
        res = {
            "time": T.isoformat(), "real_snapshot": real["id"], "real_captured_at": real["captured_at"],
            "top_level_real": real_top, "top_level_reconstructed": recon_top, "top_level_same_order": real_top == recon_top,
            "entries_real": len(real_all), "entries_reconstructed": len(recon_all), "entries_in_both": len(both),
            "missed": sorted(real_all - recon_all), "extra": sorted(recon_all - real_all),
            "uncertain": sorted(e for e, s in statuses.items() if s == "uncertain" and e in recon_all),
            "gone": sorted(e for e, s in statuses.items() if s == "gone"),
            "names": {e: (entries.get(e, {}).get("fields", {}).get("name") or "") for e in (real_all | recon_all)},
            "borrowed": stats.get("borrowed", []),
        }
        (folder / "compare.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
        summary.append({k: res[k] for k in ("time", "real_captured_at", "top_level_same_order", "entries_real",
                                             "entries_reconstructed", "entries_in_both")} | {"missed": len(res["missed"]), "extra": len(res["extra"]), "uncertain": len(res["uncertain"])})
        print(json.dumps(summary[-1]), flush=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))


asyncio.run(main())
