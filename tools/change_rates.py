"""Visible changes stored per market and device per day (calibration for storage estimates)."""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
rows, off = [], 0
while True:
    page = db._req("GET", "/rest/v1/snapshots", params={
        "select": "market,device,captured_at,change_size,stats,capture_requests(reason)",
        "order": "captured_at", "limit": "1000", "offset": str(off)}) or []
    rows += page
    if len(page) < 1000:
        break
    off += 1000
days = defaultdict(Counter)
reasons = Counter()
sizes = Counter()
newb = defaultdict(list)
for r in rows:
    if r["market"].count(":"):
        continue
    reason = (r.get("capture_requests") or {}).get("reason", "?")
    reasons[reason] += 1
    d = r["captured_at"][:10]
    if r["change_size"] not in ("none",) and reason in ("contentful", "scheduled", "manual"):
        days[d][r["change_size"]] += 1
    sizes[(reason, r["change_size"])] += 1
    nb = (r.get("stats") or {}).get("new_bytes") or {}
    if r["change_size"] != "none" and reason == "contentful":
        newb[r["captured_at"][:10] >= "2026-11-05"].append(sum(v for v in nb.values() if isinstance(v, (int, float))))
out = {"per_day_visible_changes_all_nn_markets_and_devices": {d: dict(c) for d, c in sorted(days.items())},
       "reasons": dict(reasons), "sizes_by_reason": {f"{a}|{b}": n for (a, b), n in sizes.items()},
       "new_bytes_per_contentful_change": {str(k): {"n": len(v), "mean": sum(v) / len(v) if v else 0,
                                                    "median": sorted(v)[len(v) // 2] if v else 0} for k, v in newb.items()}}
print("CHANGES_JSON_START"); print(json.dumps(out)); print("CHANGES_JSON_END")
