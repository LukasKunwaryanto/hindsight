"""Give snapshots named the old way the new timeline names ("Archive starts", "New hero: X (was Y)",
"· 3 more changes"). Only touches change_title and change_description; safe to re-run.

Env: SUPABASE_URL, SUPABASE_SECRET_KEY. DRY_RUN=1 lists the renames without writing.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from describe import rename  # noqa: E402
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
DRY = os.environ.get("DRY_RUN") == "1"


def fetch_all(table, **params):
    rows, off = [], 0
    while True:
        page = db._req("GET", f"/rest/v1/{table}", params={**params, "limit": "1000", "offset": str(off)}) or []
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


snaps = fetch_all("snapshots", select="id,market,page,change_title,change_description,changes,page_title:stats->>title",
                  order="captured_at")
# startpages keep their change list in snapshot_changes; only replacements are needed (for "was")
replaced = defaultdict(list)
for r in fetch_all("snapshot_changes", select="snapshot_id,kind,detail", kind="eq.replaced"):
    replaced[r["snapshot_id"]].append(r)

updates = []
for s in snaps:
    changes = s.get("changes") or replaced.get(s["id"], [])
    new = rename(s.get("change_title") or "", s.get("change_description") or "", changes, s.get("page_title") or "")
    if new and new != (s.get("change_title"), s.get("change_description")):
        updates.append((s, new))

lines = [f"## Retitle snapshots{' (dry run)' if DRY else ''}", "",
         f"{len(snaps)} snapshots, {len(updates)} renamed", "", "| Before | After | Market | Page |", "|---|---|---|---|"]
for s, (t, d) in updates[:40]:
    lines.append(f'| {s["change_title"]}: {s["change_description"] or ""} | {t}: {d} | {s["market"]} | {s["page"]} |')

if not DRY:
    for s, (t, d) in updates:
        db._req("PATCH", "/rest/v1/snapshots", {"change_title": t, "change_description": d},
                {"Prefer": "return=minimal"}, params={"id": f"eq.{s['id']}"})
    lines += ["", f"{len(updates)} snapshots updated"]

md = "\n".join(lines)
print(md)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
