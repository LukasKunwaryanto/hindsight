"""Print the poller's state, recent Contentful changes and capture requests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
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
