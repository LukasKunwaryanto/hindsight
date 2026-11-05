"""Delete snapshots captured at or after a time (e.g. a bad capture version). Their blocks and change
lists go with them; stored pieces stay (they are shared and harmless). Env: SUPABASE_URL, SUPABASE_SECRET_KEY."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

since = sys.argv[1]
db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
rows = db._req("GET", "/rest/v1/snapshots", params={"select": "id,market,device,captured_at", "captured_at": f"gte.{since}"}) or []
print(f"{len(rows)} snapshots captured since {since}")
if rows:
    db._req("DELETE", "/rest/v1/snapshots", headers={"Prefer": "return=minimal"}, params={"captured_at": f"gte.{since}"})
    print("deleted")
