"""Storage experiment: capture a sample of pages (tools/sample_pages.json) into a local test store and
record what each capture adds. Nothing is written to Supabase.

  python tools/sample_storage.py --store store.pkl --label pass1     # first captures
  python tools/sample_storage.py --store store.pkl --label pass2     # same pages again (unchanged = not stored)

Appends one JSON line per capture to results.jsonl."""
import argparse
import asyncio
import functools
import gzip
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from playwright.async_api import async_playwright  # noqa: E402
from run import capture_one  # noqa: E402
from store import MemoryStore  # noqa: E402

LOCK = threading.RLock()


def locked(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        with LOCK:
            return fn(*a, **k)
    return w


for name in ("flush", "save_blobs", "save_assets", "card_prices", "save_card_prices", "asset_urls", "latest_snapshot",
             "known_assets", "save_snapshot", "open_request", "get_blobs"):
    setattr(MemoryStore, name, locked(getattr(MemoryStore, name)))
MemoryStore.flush = locked(lambda self: None)  # saved once at the end instead of after every capture


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="store.pkl")
    ap.add_argument("--label", default="pass1")
    ap.add_argument("--devices", default="desktop,mobile")
    ap.add_argument("--parallel", type=int, default=4)
    args = ap.parse_args()
    pages = json.load(open(os.path.join(os.path.dirname(__file__), "sample_pages.json")))
    store = MemoryStore(args.store)
    slots = asyncio.Semaphore(args.parallel)
    out = open("results.jsonl", "a")
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def one(pg, dev):
            key = f"{pg['site']}|{pg['type']}|{pg['url']}"
            n_before = len(store.d["snapshots"])
            r = await capture_one(browser, store, key, pg["url"], dev, "experiment", None, slots, pg["lang"])
            rec = {**pg, "device": dev, "label": args.label, "ok": r["ok"], "error": r.get("error"),
                   "summary": r.get("summary"), "seconds": r.get("seconds"), "blocks": r.get("blocks"),
                   "cards": r.get("cards"), "stored": False, "t": time.time()}
            with LOCK:
                snap = next((s for s in store.d["snapshots"][n_before:] if s["row"]["market"] == key
                             and s["row"]["device"] == dev), None)
            if snap:
                m = json.dumps(snap["row"]["manifest"], separators=(",", ":"))
                rec.update(stored=True, new_bytes=snap["row"]["stats"]["new_bytes"], manifest_raw=len(m),
                           manifest_gz=len(gzip.compress(m.encode(), 6)), modules=len(snap["modules"]),
                           entry_modules=sum(1 for x in snap["modules"] if x.get("entry_id")),
                           changes=len(snap["changes"]), page_height=snap["row"]["page_height"])
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(rec["site"], rec["type"], dev, rec["ok"], rec.get("summary"), rec.get("new_bytes"), flush=True)

        await asyncio.gather(*[one(pg, d) for pg in pages for d in args.devices.split(",")])
        await browser.close()
    pickle_path = args.store
    import pickle
    with LOCK:
        pickle.dump(store.d, open(pickle_path, "wb"))


asyncio.run(main())
