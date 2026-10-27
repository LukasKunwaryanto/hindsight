"""One-time (and safe to re-run) setup for automatic captures.

1. Runs Contentful's full initial sync once to get a sync position (the data itself
   is thrown away; only the position is kept).
2. Walks the startpage tree and stores its ~800 entries with the markets each one
   appears in, so the poller can tell which markets an edit affects.

Env: CONTENTFUL_DELIVERY_TOKEN, SUPABASE_URL, SUPABASE_SECRET_KEY.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402
from sync_test import PAGE_TYPES, SPACE, START_ID, TOKEN, entry_name, full_initial_sync, links_in, walk_tree  # noqa: E402


def main():
    if not TOKEN:
        raise SystemExit("CONTENTFUL_DELIVERY_TOKEN is not set.")
    db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    # Position first, tree second: anything published in between shows up again
    # in the first poll, which is harmless.
    t0 = time.monotonic()
    s = full_initial_sync(time_budget_s=1500)
    if not s["complete"] or not s["next_sync"]:
        raise SystemExit(f"Initial sync did not finish: {s}")
    print(f"Initial sync: {s['pages']} pages, {s['mb']:.0f} MB, {s['seconds']} s")

    entries, _assets, markets_of, depth = walk_tree()
    start_markets = sorted(entries[START_ID]["fields"].get("content", {}).keys())
    rows = []
    for eid, e in entries.items():
        links, assets = set(), set()
        for typ, child in links_in(e.get("fields", {})):
            (links if typ == "Entry" else assets if typ == "Asset" else set()).add(child)
        ct = e["sys"]["contentType"]["sys"]["id"]
        rows.append({
            "entry_id": eid, "content_type": ct, "name": entry_name(e), "is_page": ct in PAGE_TYPES and eid != START_ID,
            "markets": start_markets if eid == START_ID else sorted(markets_of.get(eid, set())),
            "fields": e.get("fields", {}), "links": sorted(links), "asset_ids": sorted(assets),
            "revision": e["sys"].get("revision"), "published_at": e["sys"].get("updatedAt"),
        })

    db._req("DELETE", "/rest/v1/tree_entries", headers={"Prefer": "return=minimal"}, params={"entry_id": "not.is.null"})
    for i in range(0, len(rows), 50):
        db._insert("tree_entries", rows[i:i + 50])
    db._insert("sync_state", [{"space_id": SPACE, "next_sync_url": s["next_sync"], "last_error": None,
                               "last_polled_at": None}], on_conflict="space_id")
    # on_conflict above ignores duplicates; make sure an existing row gets the new position too
    db._req("PATCH", "/rest/v1/sync_state", {"next_sync_url": s["next_sync"], "last_error": None},
            {"Prefer": "return=minimal"}, params={"space_id": f"eq.{SPACE}"})

    md = (f"## Contentful setup\n\nStored {len(rows)} startpage entries ({depth} levels) for "
          f"{len(start_markets)} markets and a sync position. Took {time.monotonic() - t0:.0f} s.\n"
          f"Automatic captures are now active.\n")
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md)


if __name__ == "__main__":
    main()
