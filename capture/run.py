"""Capture startpages and store them as snapshots.

  python capture/run.py --markets sv-SE --devices desktop,mobile              # Supabase (needs env)
  python capture/run.py --markets all --store memory:/tmp/hs.pkl --rebuild-dir out  # local test

Env for Supabase: SUPABASE_URL, SUPABASE_SECRET_KEY. Optional: HINDSIGHT_BYPASS_HEADER.
"""
import argparse
import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from playwright.async_api import async_playwright  # noqa: E402

from browser import capture_page  # noqa: E402
from rebuild import data_uri, rebuild  # noqa: E402
from snapshot import build, diff_modules  # noqa: E402
from store import MemoryStore, SupabaseStore  # noqa: E402

FALLBACK_MARKETS = {loc: f"https://www.nordicnest.{tld}/" for loc, tld in [
    ("sv-SE", "se"), ("en-US", "com"), ("de-DE", "de"), ("nb-NO", "no"), ("da-DK", "dk"), ("fi-FI", "fi"),
    ("nl-NL", "nl"), ("fr-FR", "fr"), ("es-ES", "es"), ("it-IT", "it"), ("pl-PL", "pl"), ("pt-PT", "pt"),
    ("el-GR", "gr"), ("ja-JP", "jp"), ("ko-KR", "kr"), ("zh-CN", "cn")]}


def make_store(spec: str):
    if spec.startswith("memory:"):
        return MemoryStore(spec.split(":", 1)[1])
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY must be set (or use --store memory:PATH).")
    return SupabaseStore(url, key)


async def capture_one(browser, store, market, url, device, reason, rebuild_dir, slots):
    async with slots:  # a few pages at a time; saving runs in a thread so pages keep loading meanwhile
        t0 = time.monotonic()
        rid = await asyncio.to_thread(store.open_request, market, device, reason)
        try:
            frozen = await capture_page(browser, url, device, market, os.environ.get("HINDSIGHT_BYPASS_HEADER"))
            if frozen.get("placeholders"):  # product data never arrived: try the page once more
                print(f"{market} {device}: {frozen['placeholders']} placeholders left, capturing again", flush=True)
                frozen = await capture_page(browser, url, device, market, os.environ.get("HINDSIGHT_BYPASS_HEADER"))
            return await asyncio.to_thread(finish, store, market, url, device, rid, frozen, t0, rebuild_dir)
        except Exception as e:
            traceback.print_exc()
            try:
                await asyncio.to_thread(store.update_request, rid, status="failed", finished_at="now()", error=str(e)[:500])
            except Exception:
                pass
            return {"market": market, "device": device, "ok": False, "error": str(e)[:300],
                    "seconds": round(time.monotonic() - t0, 1)}


def finish(store, market, url, device, rid, frozen, t0, rebuild_dir):
    """Split, store and diff one captured page. Runs in a worker thread."""
    try:
        manifest, modules, P = build(frozen, url)

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

        t_store = time.monotonic()
        blob_stats = store.save_blobs(P.blobs)
        asset_bytes = store.save_assets(P.assets)
        if new_prices:
            store.save_card_prices(market, new_prices)

        prev_id, prev_mods = store.latest_snapshot(market, device)
        changes = diff_modules(prev_mods, modules)
        stats = {
            "seconds": round(time.monotonic() - t0, 1), "blocks": len(modules), "cards": len(manifest["cards"]),
            "images": len(frozen["imgs"]), "images_not_loaded": frozen["images_not_loaded"],
            "placeholders": frozen.get("placeholders", 0),
            "pieces": len(P.blobs), "assets": len(P.assets),
            "new_bytes": {**blob_stats, "assets": asset_bytes}, "title": frozen["title"], "lang": frozen["lang"],
            "store_seconds": round(time.monotonic() - t_store, 1),
        }
        sid = store.save_snapshot({
            "market": market, "device": device, "url": url, "manifest": manifest,
            "page_height": frozen["height"], "stats": stats,
            "capture_request_id": rid if isinstance(rid, int) else None, "previous_snapshot_id": prev_id,
        }, modules, changes)
        store.update_request(rid, status="done", finished_at="now()")

        if rebuild_dir:
            if isinstance(store, MemoryStore):
                src = lambda h, mime: data_uri(*store.asset_bytes(h)) if store.asset_bytes(h)[0] else ""
            else:
                src = lambda h, mime: store.asset_url(h)
            html = rebuild(manifest, store.get_blobs, src)
            Path(rebuild_dir).mkdir(parents=True, exist_ok=True)
            Path(rebuild_dir, f"{market}-{device}.html").write_text(html, encoding="utf-8")

        new_total = sum(stats["new_bytes"].values())
        return {"market": market, "device": device, "ok": True, "snapshot": sid, "changes": len(changes),
                "new_kb": round(new_total / 1024, 1), **{k: stats[k] for k in ("seconds", "blocks", "cards", "images_not_loaded")}}
    except Exception as e:
        traceback.print_exc()
        try:
            store.update_request(rid, status="failed", finished_at="now()", error=str(e)[:500])
        except Exception:
            pass
        return {"market": market, "device": device, "ok": False, "error": str(e)[:300],
                "seconds": round(time.monotonic() - t0, 1)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="sv-SE", help="'all' or comma-separated locales")
    ap.add_argument("--devices", default="desktop,mobile")
    ap.add_argument("--store", default="supabase")
    ap.add_argument("--reason", default="manual")
    ap.add_argument("--rebuild-dir", default=None)
    ap.add_argument("--parallel", type=int, default=4, help="pages captured at the same time")
    args = ap.parse_args()

    store = make_store(args.store)
    # The markets table is the source of truth; local tests use the same list hard-coded
    all_markets = {m["locale"]: m["url"] for m in store.markets()} if hasattr(store, "markets") else FALLBACK_MARKETS
    chosen = list(all_markets) if args.markets == "all" else [m.strip() for m in args.markets.split(",")]
    devices = [d.strip() for d in args.devices.split(",")]

    results = [{"market": m, "device": "-", "ok": False, "error": "unknown market"} for m in chosen if m not in all_markets]
    slots = asyncio.Semaphore(max(1, args.parallel))
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def one(market, device):
            r = await capture_one(browser, store, market, all_markets[market], device, args.reason, args.rebuild_dir, slots)
            print(r, flush=True)
            return r

        jobs = [one(m, d) for m in chosen if m in all_markets for d in devices]
        results += await asyncio.gather(*jobs)
        await browser.close()
    order = {m: i for i, m in enumerate(chosen)}
    results.sort(key=lambda r: (order.get(r["market"], 99), r["device"]))

    lines = ["## Hindsight captures", "",
             "| Market | Device | Result | Seconds | Blocks | Cards | New KB | Changes | Images not loaded |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        if r["ok"]:
            lines.append(f"| {r['market']} | {r['device']} | OK | {r['seconds']} | {r['blocks']} | {r['cards']} | "
                         f"{r['new_kb']} | {r['changes']} | {r['images_not_loaded']} |")
        else:
            lines.append(f"| {r['market']} | {r['device']} | **FAILED**: {r.get('error', '')} | {r.get('seconds', '')} | | | | | |")
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
