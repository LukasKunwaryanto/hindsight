"""What a capture really costs, per page type: new bytes (pieces by kind, images), rows and seconds,
split into first captures and later ones. Env: SUPABASE_URL, SUPABASE_SECRET_KEY."""
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


kind = {(p["market"], p["path"]): p["kind"] for p in fetch_all("pages", select="market,path,kind")}
snaps = fetch_all("snapshots", select="id,market,device,page,captured_at,change_size,previous_snapshot_id,stats,"
                  "manifest_len:manifest", order="captured_at")
reqs = {r["id"]: r for r in fetch_all("capture_requests", select="id,reason")}
agg = defaultdict(lambda: defaultdict(float))
streams = defaultdict(set)
for s in snaps:
    t = "start" if s["page"] == "/" else kind.get((s["market"], s["page"]), "other")
    first = "first" if not s.get("previous_snapshot_id") else "later"
    st = s.get("stats") or {}
    nb = st.get("new_bytes") or {}
    a = agg[f"{t}|{first}"]
    a["n"] += 1
    a["assets"] += nb.get("assets", 0) or 0
    for k, v in nb.items():
        if k != "assets":
            a["pieces"] += v or 0
            a[f"p_{k}"] += v or 0
    a["manifest"] += len(json.dumps(s.get("manifest_len") or {}))
    a["seconds"] += st.get("seconds", 0) or 0
    a["with_seconds"] += 1 if st.get("seconds") else 0
    a["cards"] += st.get("cards", 0) or 0
    a["grid"] += st.get("grid", 0) or 0
    if s["captured_at"] >= "2026-11-08":
        d = agg[f"{t}|since_nov8"]
        d["n"] += 1
        d["bytes"] += (nb.get("assets", 0) or 0) + sum(v or 0 for k, v in nb.items() if k != "assets")
    streams[t].add((s["market"], s["device"], s["page"]))
out = {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in sorted(agg.items())}
out["streams"] = {k: len(v) for k, v in streams.items()}
out["first_at"] = snaps[0]["captured_at"] if snaps else None
out["last_at"] = snaps[-1]["captured_at"] if snaps else None
print("COSTS_JSON_START" + json.dumps(out) + "COSTS_JSON_END")
