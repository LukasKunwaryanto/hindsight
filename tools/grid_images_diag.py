"""For captures that failed their check: how many grid products have no image to show."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
rows = db._req("GET", "/rest/v1/snapshots", params={
    "select": "id,market,page,device,captured_at,grid:manifest->grid,verify:stats->verify",
    "captured_at": "gte.2026-11-08", "page": "neq./", "order": "captured_at.desc", "limit": "1000"}) or []
out = []
for r in rows:
    v = r.get("verify") or {}
    grid = r.get("grid") or []
    if not grid:
        continue
    imgs = db._req("GET", "/rest/v1/product_images", params={"select": "sku,src,asset", "sku": f"in.({','.join(sorted(set(grid))[:150])})"}) or []
    have = {i["sku"]: (i.get("asset") or i.get("src") or "") for i in imgs}
    out.append({"id": r["id"], "market": r["market"], "page": r["page"], "device": r["device"], "at": r["captured_at"][:16],
                "ok": v.get("ok"), "problems": v.get("problems"), "live": v.get("images_live"), "stored": v.get("images_stored"),
                "grid": len(grid), "no_row": sum(1 for s in set(grid) if s not in have),
                "empty_src": sum(1 for s in set(grid) if s in have and not have[s]),
                "sample_srcs": [have.get(s) for s in grid[:3]], "sample_skus": grid[:5]})
bad = [o for o in out if o["ok"] is False]
good = [o for o in out if o["ok"]]
print("DIAG_START" + json.dumps({"bad": bad[:25], "good_sample": good[:8],
                                  "bad_no_image": sum(o["no_row"] + o["empty_src"] for o in bad),
                                  "good_no_image": sum(o["no_row"] + o["empty_src"] for o in good),
                                  "n_bad": len(bad), "n_good": len(good)}) + "DIAG_END")
