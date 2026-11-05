"""Storage breakdown and growth, from the live database. Env: SUPABASE_URL, SUPABASE_SECRET_KEY."""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])


def fetch_all(table, **params):
    rows, off = [], 0
    while True:
        page = db._req("GET", f"/rest/v1/{table}", params={**params, "limit": "1000", "offset": str(off)}) or []
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


out = {"tables": db._req("POST", "/rest/v1/rpc/hindsight_storage_report", {})}
blobs = fetch_all("blobs", select="kind,size_bytes,created_at")
assets = fetch_all("assets", select="mime,size_bytes,width,created_at")
snaps = fetch_all("snapshots", select="market,device,captured_at,stats,change_size")
k = defaultdict(lambda: [0, 0])
for b in blobs:
    k[b["kind"]][0] += 1; k[b["kind"]][1] += b["size_bytes"]
m = defaultdict(lambda: [0, 0])
for a in assets:
    m[a["mime"]][0] += 1; m[a["mime"]][1] += a["size_bytes"]
day = defaultdict(lambda: {"blob_bytes": 0, "asset_bytes": 0, "captures": 0, "markets": set()})
for b in blobs:
    day[b["created_at"][:10]]["blob_bytes"] += b["size_bytes"]
for a in assets:
    day[a["created_at"][:10]]["asset_bytes"] += a["size_bytes"]
for s in snaps:
    d = day[s["captured_at"][:10]]
    d["captures"] += 1; d["markets"].add(s["market"])
out["blob_kinds"] = dict(k)
out["asset_mimes"] = dict(m)
out["days"] = {d: {**v, "markets": len(v["markets"])} for d, v in sorted(day.items())}
out["snapshots"] = len(snaps)
out["sizes"] = dict(defaultdict(int, {s: sum(1 for x in snaps if x["change_size"] == s) for s in ("big", "medium", "small", "none")}))
tree = fetch_all("tree_entries", select="entry_id,fingerprints,fields_null:fields->>sys")
out["tree"] = {"rows": len(tree), "with_fingerprints": sum(1 for t in tree if t["fingerprints"])}
print(json.dumps(out, indent=1))
