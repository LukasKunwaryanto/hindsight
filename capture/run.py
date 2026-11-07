"""Capture startpages and store them as snapshots.

  python capture/run.py --markets sv-SE --devices desktop,mobile              # Supabase (needs env)
  python capture/run.py --markets all --store memory:/tmp/hs.pkl --rebuild-dir out  # local test

Env for Supabase: SUPABASE_URL, SUPABASE_SECRET_KEY. Optional: HINDSIGHT_BYPASS_HEADER.
"""
import argparse
import asyncio
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from playwright.async_api import async_playwright  # noqa: E402

from browser import capture_page  # noqa: E402
from rebuild import data_uri, rebuild  # noqa: E402
from describe import ASSET_RE, NOISE, SITE_PARTS, _kind_of_edit, describe_changes, module_info
from snapshot import build, source_path  # noqa: E402
from store import MemoryStore, SupabaseStore  # noqa: E402

FALLBACK_MARKETS = {loc: f"https://www.nordicnest.{tld}/" for loc, tld in [
    ("sv-SE", "se"), ("en-US", "com"), ("de-DE", "de"), ("nb-NO", "no"), ("da-DK", "dk"), ("fi-FI", "fi"),
    ("nl-NL", "nl"), ("fr-FR", "fr"), ("es-ES", "es"), ("it-IT", "it"), ("pl-PL", "pl"), ("pt-PT", "pt"),
    ("el-GR", "gr"), ("ja-JP", "jp"), ("ko-KR", "kr"), ("zh-CN", "cn")]}
FALLBACK_MARKETS.update({"kitchentime:sv-SE": "https://www.kitchentime.se/", "svenssons:sv-SE": "https://www.svenssons.se/"})


def make_store(spec: str):
    if spec.startswith("memory:"):
        return MemoryStore(spec.split(":", 1)[1])
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY must be set (or use --store memory:PATH).")
    return SupabaseStore(url, key)


async def capture_one(browser, store, market, url, device, reason, rebuild_dir, slots, lang=None, page="/", kind=None):
    async with slots:  # a few pages at a time; saving runs in a thread so pages keep loading meanwhile
        t0 = time.monotonic()
        rid = await asyncio.to_thread(store.open_request, market, device, reason, page)
        try:
            lang = lang or market.split(":")[-1]  # 'kitchentime:de-DE' -> 'de-DE'
            frozen = await capture_page(browser, url, device, lang, os.environ.get("HINDSIGHT_BYPASS_HEADER"))
            if frozen.get("placeholders"):  # product data never arrived: try the page once more
                print(f"{market} {device}: {frozen['placeholders']} placeholders left, capturing again", flush=True)
                frozen = await capture_page(browser, url, device, lang, os.environ.get("HINDSIGHT_BYPASS_HEADER"))
            return await asyncio.to_thread(finish, store, market, url, device, rid, frozen, t0, rebuild_dir, page, kind, reason)
        except Exception as e:
            traceback.print_exc()
            try:
                await asyncio.to_thread(store.update_request, rid, status="failed", finished_at="now()", error=str(e)[:500])
            except Exception:
                pass
            return {"market": market, "page": page, "device": device, "ok": False, "error": str(e)[:300],
                    "seconds": round(time.monotonic() - t0, 1)}


_site_lead: dict = {}


def site_lead(store, market, device):
    """How many #container children before the content are site bars: as on the market's startpage."""
    if (market, device) not in _site_lead:
        _, mods, _ = store.latest_snapshot(market, device, "/")
        n = next((i for i, m in enumerate(mods or []) if m["label"].startswith("block:")), None)
        _site_lead[(market, device)] = n
    return _site_lead[(market, device)]


def record_site_parts(store, market, device, manifest, P):
    """Keep the site bars (header, bars, footer) as their own timeline per market and device."""
    parts = [{"label": m["label"], "ref": m["ref"]} for m in manifest["modules"] if m["label"] in SITE_PARTS]
    prev = store.latest_site_parts(market, device)
    if not parts or prev == parts:
        return False
    if prev and len(prev) == len(parts):
        changed = [(a["ref"], b["ref"]) for a, b in zip(prev, parts) if a["ref"] != b["ref"]]
        texts = store.get_blobs(sorted({h for pair in changed for h in pair} - set(P.blobs)))
        texts.update({h: P.blobs[h][1] for pair in changed for h in pair if h in P.blobs})
        urls = {h: a["url"] for h, a in P.assets.items()}
        if all(_kind_of_edit(texts.get(a, ""), texts.get(b, ""), urls, "chrome") in NOISE for a, b in changed):
            return False
    # the parts' pieces must exist even if the page itself isn't stored this time
    need = {p["ref"] for p in parts}
    for _ in range(3):  # bars hold nested pieces (e.g. card parts) and images
        more = {h for r in need if r in P.blobs for h in re.findall(r'ref="([0-9a-f]{16})"', P.blobs[r][1])}
        if more <= need:
            break
        need |= more
    store.save_blobs({h: P.blobs[h] for h in need if h in P.blobs})
    used = {a for h in need if h in P.blobs for a in re.findall(r"hs-asset:([0-9a-f]{16})", P.blobs[h][1])}
    store.save_assets({h: P.assets[h] for h in used if h in P.assets})
    store.save_site_parts(market, device, parts)
    return True


def finish(store, market, url, device, rid, frozen, t0, rebuild_dir, page="/", kind=None, reason="manual"):
    """Split, store and diff one captured page. Runs in a worker thread."""
    try:
        # Images stored before (same picture, same displayed size) are reused, not re-encoded; also images
        # the page shows but hadn't loaded yet (an icon in a closed menu)
        paths = [source_path(u) for u, (_, ct) in frozen["resources"].items() if "image" in ct or "font" in ct]
        paths += [source_path(i["src"]) for i in frozen["imgs"] if i["src"] and not i["src"].startswith("data:")]
        key_page = page == "/" or kind == "menu"
        manifest, modules, P = build(frozen, url, store.known_assets(paths),
                                     site_lead=None if page == "/" else site_lead(store, market, device), key_page=key_page)
        bars_changed = record_site_parts(store, market, device, manifest, P)

        # Prices: keep the first price seen per SKU and market (accuracy not needed)
        skus = [c["sku"] for c in manifest["cards"] if c.get("price")]
        known = store.card_prices(market, skus) if skus else {}
        new_prices = {}
        for c in manifest["cards"]:
            if not c.get("price"):
                continue
            if c["sku"] in known:
                c["price"] = known[c["sku"]]
            else:
                new_prices.setdefault(c["sku"], c["price"])
        used = {c["price"] for c in manifest["cards"] if c.get("price")}
        for h in [h for h, (k, _) in P.blobs.items() if k == "price" and h not in used]:
            del P.blobs[h]

        # Readable info per block: headline, and promotion names as the site reports them to GA
        # (fallback: the Contentful internal name, which follows the same naming convention)
        reported = {}
        for pr in frozen.get("promotions") or []:
            if pr["id"] and pr["name"]:
                reported.setdefault(pr["id"], pr["name"])
        texts = {m["blob_hash"]: P.blobs[m["blob_hash"]][1] for m in modules}
        infos = {m["blob_hash"]: module_info(texts[m["blob_hash"]]) for m in modules}
        all_ids = sorted({i for inf in infos.values() for i in inf["ids"]})
        tree = store.tree_names(all_ids) if all_ids else {}
        for m in modules:
            inf = infos[m["blob_hash"]]
            m["title"] = inf["title"]
            names = [reported[i] for i in inf["ids"] if i in reported]
            if not names and m.get("entry_id") in tree:
                names = [tree[m["entry_id"]]]
            m["promotion_names"] = list(dict.fromkeys(names))

        prev_id, prev_mods, prev_manifest = store.latest_snapshot(market, device, page)
        if prev_mods:
            missing = [p["blob_hash"] for p in prev_mods if p["blob_hash"] not in texts]
            texts.update(store.get_blobs(missing) if missing else {})
            for p in prev_mods:
                if not p.get("title") and p["blob_hash"] in texts:
                    p["title"] = module_info(texts[p["blob_hash"]])["title"]
        hashes = sorted({a for t in texts.values() for a, _ in ASSET_RE.findall(t) if a})
        urls = {h: a["url"] for h, a in P.assets.items()}
        urls.update(store.asset_urls([h for h in hashes if h not in urls]))
        # Content changes only: products in grids and badges are product data, not page content. A carousel
        # showing other products counts only when a Contentful publish caused this capture. Inner pages keep
        # their site bars in their own timeline (site_parts), so a bar change doesn't store the page again.
        changes, summary = describe_changes(prev_mods, modules, texts, urls, products_are_noise=reason != "contentful",
                                            ignore_site_parts=page != "/")
        if prev_manifest and summary["change_size"] == "none":
            # Nothing visible changed: keep the previous snapshot, store nothing new
            store.update_request(rid, status="done", finished_at="now()")
            return {"market": market, "page": page, "device": device, "ok": True, "snapshot": prev_id, "changes": 0,
                    "new_kb": 0, "summary": "unchanged (not stored)" + (", bars updated" if bars_changed else ""),
                    "seconds": round(time.monotonic() - t0, 1), "blocks": len(modules), "cards": len(manifest["cards"]),
                    "images_not_loaded": frozen["images_not_loaded"]}

        t_store = time.monotonic()
        blob_stats = store.save_blobs(P.blobs)
        asset_bytes = store.save_assets(P.assets)
        if new_prices:
            store.save_card_prices(market, new_prices)
        if P.grid_images:
            store.save_product_images(P.grid_images)
        stats = {
            "seconds": round(time.monotonic() - t0, 1), "blocks": len(modules), "cards": len(manifest["cards"]),
            "images": len(frozen["imgs"]), "images_not_loaded": frozen["images_not_loaded"],
            "grid": len(manifest.get("grid", [])), "tracking_blocked": frozen.get("tracking_blocked", 0),
            "placeholders": frozen.get("placeholders", 0),
            "pieces": len(P.blobs), "assets": len(P.assets),
            "new_bytes": {**blob_stats, "assets": asset_bytes}, "title": frozen["title"], "lang": frozen["lang"],
            "store_seconds": round(time.monotonic() - t_store, 1),
        }
        sid = store.save_snapshot({
            "market": market, "device": device, "page": page, "url": url, "manifest": manifest,
            "page_height": frozen["height"], "stats": stats, **summary,
            "capture_request_id": rid if isinstance(rid, int) else None, "previous_snapshot_id": prev_id,
        }, modules, changes)
        store.update_request(rid, status="done", finished_at="now()")

        if rebuild_dir:
            if isinstance(store, MemoryStore):
                src = lambda h, mime: data_uri(*store.asset_bytes(h)) if store.asset_bytes(h)[0] else ""
            else:
                src = lambda h, mime: store.asset_url(h)
            html = rebuild(manifest, store.get_blobs, src, store.product_images)
            Path(rebuild_dir).mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", page.lower()).strip("-") or "start"
            Path(rebuild_dir, f"{market.replace(':', '_')}-{slug}-{device}.html").write_text(html, encoding="utf-8")

        new_total = sum(stats["new_bytes"].values())
        return {"market": market, "page": page, "device": device, "ok": True, "snapshot": sid, "changes": len(changes),
                "new_kb": round(new_total / 1024, 1), "summary": f"{summary['change_size']}: {summary['change_title']}", **{k: stats[k] for k in ("seconds", "blocks", "cards", "images_not_loaded")}}
    except Exception as e:
        traceback.print_exc()
        try:
            store.update_request(rid, status="failed", finished_at="now()", error=str(e)[:500])
        except Exception:
            pass
        return {"market": market, "page": page, "device": device, "ok": False, "error": str(e)[:300],
                "seconds": round(time.monotonic() - t0, 1)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="sv-SE", help=(
        "Comma separated: 'all' (every startpage), 'site:kitchentime' (a site's startpages), market ids "
        "('sv-SE'), 'pages' or 'pages:menu' (enabled inner pages, optionally of one kind), 'pages:site:svenssons', "
        "or one page: 'sv-SE@/nyheter/'"))
    ap.add_argument("--devices", default="desktop,mobile")
    ap.add_argument("--store", default="supabase")
    ap.add_argument("--reason", default="manual")
    ap.add_argument("--rebuild-dir", default=None)
    ap.add_argument("--parallel", type=int, default=4, help="pages captured at the same time")
    args = ap.parse_args()

    store = make_store(args.store)
    # The markets table is the source of truth; local tests use the same list hard-coded
    rows = store.markets() if hasattr(store, "markets") else [{"locale": k, "url": v} for k, v in FALLBACK_MARKETS.items()]
    all_markets = {m["locale"]: m["url"] for m in rows}
    langs = {m["locale"]: m.get("lang") for m in rows}
    sites = {m["locale"]: m.get("site") or "nordicnest" for m in rows}
    page_rows = store.pages() if any(x.strip().startswith("pages") for x in args.markets.split(",")) else []
    kinds = {(p["market"], p["path"]): p["kind"] for p in page_rows}
    # targets: (market, page path)
    chosen = []
    for m in (x.strip() for x in args.markets.split(",")):
        if m == "all":
            chosen += [(k, "/") for k in all_markets]
        elif m.startswith("site:"):
            chosen += [(k, "/") for k in all_markets if sites[k] == m[5:]]
        elif m == "pages" or m.startswith("pages:"):
            f = m[6:]
            chosen += [(p["market"], p["path"]) for p in page_rows if p["market"] in all_markets and (
                not f or p["kind"] == f or (f.startswith("site:") and sites[p["market"]] == f[5:]))]
        elif "@" in m:
            mk, path = m.split("@", 1)
            chosen.append((mk, path if path.startswith("/") else "/" + path))
        elif m:
            chosen.append((m, "/"))
    chosen = list(dict.fromkeys(chosen))
    devices = [d.strip() for d in args.devices.split(",")]

    results = [{"market": m, "page": pg, "device": "-", "ok": False, "error": "unknown market"}
               for m, pg in chosen if m not in all_markets]
    slots = asyncio.Semaphore(max(1, args.parallel))
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def one(market, page, device):
            url = all_markets[market].rstrip("/") + page
            r = await capture_one(browser, store, market, url, device, args.reason, args.rebuild_dir, slots,
                                  langs.get(market), page, kinds.get((market, page)))
            print(r, flush=True)
            return r

        # startpages first: inner pages take their site-bar layout from the market's startpage
        jobs = [one(m, pg, d) for m, pg in sorted(chosen, key=lambda t: t[1] != "/") if m in all_markets for d in devices]
        results += await asyncio.gather(*jobs)
        await browser.close()
    order = {t: i for i, t in enumerate(chosen)}
    results.sort(key=lambda r: (order.get((r["market"], r.get("page", "/")), 99), r["device"]))

    lines = ["## Hindsight captures", "",
             "| Market | Page | Device | Result | Seconds | Blocks | Cards | New KB | Changes | Images not loaded |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        if r["ok"]:
            lines.append(f"| {r['market']} | {r.get('page', '/')} | {r['device']} | {r.get('summary', 'OK')} | {r['seconds']} | "
                         f"{r['blocks']} | {r['cards']} | {r['new_kb']} | {r['changes']} | {r['images_not_loaded']} |")
        else:
            lines.append(f"| {r['market']} | {r.get('page', '/')} | {r['device']} | **FAILED**: {r.get('error', '')} | "
                         f"{r.get('seconds', '')} | | | | | |")
    ok = sum(r["ok"] for r in results)
    lines += ["", f"{ok} of {len(results)} captures succeeded."]
    md = "\n".join(lines)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")
    if ok < len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
