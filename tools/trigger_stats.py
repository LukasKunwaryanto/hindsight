"""Inputs for the storage comparison: Contentful-triggered captures vs stored ones (since unchanged captures
stopped being stored, 5 Nov 18:00 UTC), and the size of stored product images."""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
SINCE = "2026-11-05T18:00:00Z"


def all_rows(table, **p):
    out, off = [], 0
    while True:
        page = db._req("GET", f"/rest/v1/{table}", params={**p, "limit": "1000", "offset": str(off)}) or []
        out += page
        if len(page) < 1000:
            return out
        off += 1000


req = all_rows("capture_requests", select="market,page,device,status,reason,finished_at", reason="eq.contentful",
               status="eq.done", finished_at=f"gte.{SINCE}")
snap = all_rows("snapshots", select="market,page,device,captured_at,capture_requests(reason)", captured_at=f"gte.{SINCE}")
site = lambda m: m.split(":")[0] if ":" in m else "nordicnest"
trig = Counter((site(r["market"]), r["page"] == "/") for r in req)
stored = Counter((site(s["market"]), s["page"] == "/") for s in snap if (s.get("capture_requests") or {}).get("reason") == "contentful")
imgs = all_rows("assets", select="size_bytes,width,mime", source_path="like.file:*")
sizes = sorted(a["size_bytes"] for a in imgs)
out = {"since": SINCE, "triggered": {f"{k[0]}|{'start' if k[1] else 'inner'}": v for k, v in trig.items()},
       "stored": {f"{k[0]}|{'start' if k[1] else 'inner'}": v for k, v in stored.items()},
       "product_images": {"n": len(sizes), "mean": sum(sizes) / len(sizes) if sizes else 0,
                          "median": sizes[len(sizes) // 2] if sizes else 0,
                          "widths": Counter(round((a["width"] or 0) / 50) * 50 for a in imgs).most_common(6)}}
print("TRIG_JSON_START"); print(json.dumps(out)); print("TRIG_JSON_END")
