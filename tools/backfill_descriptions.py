"""Fill change_size/title/description, block titles, promotion names and change lists
for snapshots captured before those existed. Safe to re-run.

Env: SUPABASE_URL, SUPABASE_SECRET_KEY.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from describe import ASSET_RE, describe_changes, module_info  # noqa: E402
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


snaps = fetch_all("snapshots", select="id,market,device,captured_at", order="captured_at")
mods = defaultdict(list)
for i in range(0, len(snaps), 100):
    ids = ",".join(s["id"] for s in snaps[i:i + 100])
    for m in fetch_all("snapshot_modules", select="snapshot_id,position,label,entry_id,blob_hash,promotion_names",
                       snapshot_id=f"in.({ids})", order="snapshot_id,position"):
        mods[m["snapshot_id"]].append(m)

texts = db.get_blobs(sorted({m["blob_hash"] for ms in mods.values() for m in ms}))
infos = {h: module_info(t) for h, t in texts.items()}
tree = db.tree_names(sorted({i for inf in infos.values() for i in inf["ids"]}))
urls = db.asset_urls(sorted({a for t in texts.values() for a, _ in ASSET_RE.findall(t) if a}))
print(f"{len(snaps)} snapshots, {len(texts)} distinct blocks")

prev_of = {}
sizes = defaultdict(int)
mod_rows, snap_updates, change_rows, done_ids = [], [], [], []
for s in snaps:
    key = (s["market"], s["device"])
    cur = mods.get(s["id"], [])
    for m in cur:
        inf = infos.get(m["blob_hash"], {"title": "", "ids": []})
        m["title"] = inf["title"]
        if not m.get("promotion_names") and m.get("entry_id") in tree:
            m["promotion_names"] = [tree[m["entry_id"]]]
        mod_rows.append({"snapshot_id": s["id"], "position": m["position"], "label": m["label"], "entry_id": m["entry_id"],
                         "blob_hash": m["blob_hash"], "title": m["title"], "promotion_names": m.get("promotion_names") or []})
    prev = prev_of.get(key)
    changes, summary = describe_changes(prev[1] if prev else None, cur, texts, urls)
    sizes[summary["change_size"]] += 1
    snap_updates.append((s["id"], summary))
    change_rows += [{**c, "snapshot_id": s["id"], "previous_snapshot_id": prev[0] if prev else None} for c in changes]
    done_ids.append(s["id"])
    prev_of[key] = (s["id"], cur)

# blocks: one upsert per 500 rows
for i in range(0, len(mod_rows), 500):
    db._req("POST", "/rest/v1/snapshot_modules", mod_rows[i:i + 500],
            {"Prefer": "return=minimal,resolution=merge-duplicates"}, params={"on_conflict": "snapshot_id,position"})
print(f"{len(mod_rows)} blocks updated")
# snapshots: grouped by identical summary to keep requests few
by_summary = defaultdict(list)
for sid, summ in snap_updates:
    by_summary[tuple(sorted(summ.items()))].append(sid)
for summ, ids in by_summary.items():
    for i in range(0, len(ids), 100):
        db._req("PATCH", "/rest/v1/snapshots", dict(summ), {"Prefer": "return=minimal"},
                params={"id": f"in.({','.join(ids[i:i + 100])})"})
# change lists: replace the old, cruder ones (keeps any with an AI summary)
for i in range(0, len(done_ids), 100):
    db._req("DELETE", "/rest/v1/snapshot_changes", headers={"Prefer": "return=minimal"},
            params={"snapshot_id": f"in.({','.join(done_ids[i:i + 100])})", "summary_status": "eq.none"})
db._insert("snapshot_changes", change_rows)
print(f"{len(change_rows)} changes written")

md = f"## Backfill\n\n{len(snaps)} snapshots described: " + ", ".join(f"{v} {k}" for k, v in sorted(sizes.items()))
print(md)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
