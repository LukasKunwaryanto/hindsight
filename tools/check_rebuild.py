"""Rebuild the newest snapshot of some markets from the database and check nothing is missing."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from rebuild import rebuild  # noqa: E402
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
markets = (sys.argv[1] if len(sys.argv) > 1 else "sv-SE").split(",")
bad = 0
for m in markets:
    for dev in ("desktop", "mobile"):
        row = (db._req("GET", "/rest/v1/snapshots", params={"select": "id,captured_at,manifest", "market": f"eq.{m}",
                                                              "device": f"eq.{dev}", "order": "captured_at.desc", "limit": "1"}) or [None])[0]
        if not row:
            continue
        man = row["manifest"]
        html = rebuild(man, db.get_blobs, lambda h, mime: f"https://x/{h}")
        left = len(re.findall(r"<hs-(module|card|slot)", html))
        cards = html.count('data-test-id="product-card"')
        ok = left == 0 and cards == len(man.get("cards", []))
        bad += not ok
        print(f"{m} {dev} v{man.get('version')} {row['captured_at'][:16]} html={len(html)} cards={cards}/{len(man.get('cards', []))} "
              f"unresolved={left} {'OK' if ok else 'PROBLEM'}")
sys.exit(1 if bad else 0)
