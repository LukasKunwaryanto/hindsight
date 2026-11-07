"""Debug: are these Contentful entries in the tracked content trees, and on which markets/pages?"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
ids = sys.argv[1].split(",")
rows = db._req("GET", "/rest/v1/tree_entries", params={"select": "entry_id,content_type,name,markets,pages,published_at,updated_at",
                                                       "entry_id": f"in.({','.join(ids)})"}) or []
found = {r["entry_id"] for r in rows}
print(json.dumps(rows, indent=1)[:6000])
print("not in any tree:", [i for i in ids if i not in found])
print(json.dumps(db._req("GET", "/rest/v1/sync_state", params={"select": "space_id,last_polled_at,last_error,updated_at"}), indent=1))
print("tree rows with pages:", len(db._req("GET", "/rest/v1/tree_entries", params={"select": "entry_id", "pages": "neq.{}", "limit": "5000"}) or []))
