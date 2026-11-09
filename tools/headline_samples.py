"""Show how block headlines are found: for the longest stored titles, the elements the text comes from."""
import os
import sys

import lxml.html

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from describe import module_info  # noqa: E402
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
rows = db._req("GET", "/rest/v1/snapshot_modules", params={"select": "blob_hash,title", "order": "snapshot_id.desc", "limit": "3000"})
seen = {}
for r in rows:
    if r.get("title") and r["blob_hash"] not in seen:
        seen[r["blob_hash"]] = r["title"]
top = sorted(seen.items(), key=lambda kv: -len(kv[1]))[:25]
texts = db.get_blobs([h for h, _ in top])
for h, t in top:
    html = texts.get(h, "")
    print("=" * 100)
    print("TITLE:", t)
    el = lxml.html.fromstring(html)
    for e in el.iter():
        if not isinstance(e.tag, str):
            continue
        own = (e.text or "").strip()
        if own:
            anc = []
            p = e
            while p is not None and len(anc) < 4:
                anc.append(f"{p.tag}.{(p.get('class') or '').split(' ')[0][:30]}")
                p = p.getparent()
            print(f"  {own[:70]!r:75} <- {' < '.join(anc)}")
