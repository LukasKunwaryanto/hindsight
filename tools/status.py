"""Print the poller's state, recent Contentful changes and capture requests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
if len(sys.argv) > 2 and sys.argv[2]:  # print one market's desktop timeline
    rows = db._req("GET", "/rest/v1/snapshots", params={"select": "id,captured_at,change_size,change_title,change_description",
                   "market": f"eq.{sys.argv[2]}", "device": "eq.desktop", "order": "captured_at"}) or []
    for r in rows:
        ch = db._req("GET", "/rest/v1/snapshot_changes", params={"select": "kind,position,detail->>label,detail->>title,detail->>was",
                     "snapshot_id": f"eq.{r['id']}", "order": "position"}) or []
        print(f"{r['captured_at'][:16]} {r['change_size']:6} {r['change_title']} / {r['change_description']}")
        for c in ch:
            print(f"      {c['kind']:8} pos {c['position']} {c['label']} | {c['title']} | was {c['was']}")
    sys.exit(0)
if len(sys.argv) > 1 and sys.argv[1]:  # test: queue a request that is already due
    old = "2026-01-01T00:00:00Z"
    db._insert("capture_requests", [{"market": sys.argv[1], "device": d, "reason": "test", "first_change_at": old,
                                     "last_change_at": old} for d in ("desktop", "mobile")])
    print(f"Queued a test capture request for {sys.argv[1]}; the next 5-minute check should start it.")
q = lambda table, **p: db._req("GET", f"/rest/v1/{table}", params=p) or []
lines = ["## Hindsight status", ""]
for s in q("sync_state", select="last_polled_at,last_error"):
    lines += [f"**Last Contentful check:** {s['last_polled_at']}  ", f"**Last error:** {s['last_error'] or 'none'}", ""]
lines += ["### Recent startpage changes", "", "| Detected | Type | Name | Change | Markets |", "| --- | --- | --- | --- | --- |"]
for c in q("content_changes", select="detected_at,content_type,name,change_type,markets", order="id.desc", limit="15"):
    lines.append(f"| {c['detected_at'][:16]} | {c['content_type']} | {c['name']} | {c['change_type']} | {', '.join(c['markets'])} |")
lines += ["", "### Capture requests", "", "| Market | Device | Status | Reason | Last change | Started | Finished | Error |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
for r in q("capture_requests", select="market,device,status,reason,last_change_at,started_at,finished_at,error", order="id.desc", limit="20"):
    lines.append(" | ".join(["", r["market"], r["device"], r["status"], r["reason"], (r["last_change_at"] or "")[:16],
                             (r["started_at"] or "")[:16], (r["finished_at"] or "")[:16], (r["error"] or "")[:80], ""]).strip())
md = "\n".join(lines)
print(md)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
