"""Health of the capture pipeline, and what "Style updated" changes actually changed.

Env: SUPABASE_URL, SUPABASE_SECRET_KEY. DAYS (default 7), STYLE_SAMPLES (default 10).
"""
import difflib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
DAYS = int(os.environ.get("DAYS", "7"))
SAMPLES = int(os.environ.get("STYLE_SAMPLES", "10"))
now = datetime.now(timezone.utc)
since = (now - timedelta(days=DAYS)).isoformat()
out = {"generated": now.isoformat(), "days": DAYS}


def fetch_all(table, **params):
    rows, off = [], 0
    while True:
        page = db._req("GET", f"/rest/v1/{table}", params={**params, "limit": "1000", "offset": str(off)}) or []
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


kind = {(p["market"], p["path"]): p["kind"] for p in fetch_all("pages", select="market,path,kind,enabled")}
ptype = lambda m, p: "start" if p == "/" else kind.get((m, p), "other")

# ---------- captures ----------
snaps = fetch_all("snapshots", select="id,market,device,page,captured_at,change_size,change_title,previous_snapshot_id,"
                  "verify:stats->verify,new_bytes:stats->new_bytes", captured_at=f"gte.{since}", order="captured_at")
per_day = defaultdict(Counter)
titles = Counter()
titles_by_type = defaultdict(Counter)
verify = Counter()
verify_problems = Counter()
verify_failed = []
new_bytes = defaultdict(float)
for s in snaps:
    day, t = s["captured_at"][:10], ptype(s["market"], s["page"])
    per_day[day][t] += 1
    title = s.get("change_title") or ""
    titles[title] += 1
    titles_by_type[t][title] += 1
    v = s.get("verify")
    if isinstance(v, dict):
        verify["ok" if v.get("ok") else "problem"] += 1
        for p in v.get("problems") or []:
            verify_problems[re.sub(r"\d+(\.\d+)?", "#", p)[:80]] += 1
        if not v.get("ok"):
            verify_failed.append({"market": s["market"], "page": s["page"], "device": s["device"], "at": s["captured_at"][:16],
                                  "id": s["id"], "problems": v.get("problems"), "live": v.get("live"), "got": v.get("got")})
    else:
        verify["not checked"] += 1
    nb = s.get("new_bytes") or {}
    new_bytes[day] += sum(x or 0 for x in nb.values())
out["snapshots"] = {"total": len(snaps), "per_day": {d: dict(c) for d, c in sorted(per_day.items())},
                    "titles": titles.most_common(), "titles_by_type": {t: c.most_common(8) for t, c in titles_by_type.items()},
                    "verify": dict(verify), "verify_problems": verify_problems.most_common(10), "verify_failed": verify_failed,
                    "new_mb_per_day": {d: round(b / 1e6, 2) for d, b in sorted(new_bytes.items())}}

# ---------- capture requests ----------
reqs = fetch_all("capture_requests", select="id,market,page,device,status,reason,created_at,started_at,finished_at,error",
                 created_at=f"gte.{since}")
out["requests"] = {
    "by_status_reason": Counter(f'{r["status"]}/{r["reason"]}' for r in reqs).most_common(),
    "errors": Counter(re.sub(r"\d+", "#", (r.get("error") or ""))[:110] for r in reqs if r["status"] == "failed").most_common(10),
    "waiting_now": [{k: r[k] for k in ("market", "page", "device", "created_at")} for r in reqs if r["status"] == "waiting"][:20],
    "started_now": [{k: r[k] for k in ("market", "page", "device", "started_at")} for r in reqs if r["status"] == "started"][:20],
}
# a failed page that never succeeded afterwards
latest = {}
for r in sorted(reqs, key=lambda r: r["created_at"]):
    latest[(r["market"], r["page"], r["device"])] = r
out["requests"]["still_failed"] = [{"market": k[0], "page": k[1], "device": k[2], "error": (r.get("error") or "")[:120],
                                    "at": r["created_at"]} for k, r in latest.items() if r["status"] == "failed"]

# ---------- startpages and poller ----------
health = fetch_all("capture_health", select="market,device,last_capture_at,failures_7d,latest_request_status")
out["startpages"] = {
    "markets": len({h["market"] for h in health}),
    "oldest_capture": sorted(((h["last_capture_at"] or "never"), h["market"], h["device"]) for h in health)[:6],
    "failures_7d": sum(h.get("failures_7d") or 0 for h in health),
}
out["sync_state"] = fetch_all("sync_state", select="space_id,last_polled_at,last_error")
out["pages"] = Counter(f'{p["kind"]}' for p in fetch_all("pages", select="kind", enabled="eq.true"))
pi = fetch_all("product_images", select="sku,missing_since")
out["product_images"] = {"total": len(pi), "missing": sum(1 for p in pi if p.get("missing_since"))}

# ---------- what "Style updated" changed ----------
style = [s for s in snaps if s.get("change_title") == "Style updated" and s.get("previous_snapshot_id")]
out["style"] = {"count": len(style), "by_type": Counter(ptype(s["market"], s["page"]) for s in style).most_common(),
                "by_day": Counter(s["captured_at"][:10] for s in style).most_common()}
# spread the sample over page types and days
style.sort(key=lambda s: s["captured_at"])
sample = style[:: max(1, len(style) // SAMPLES)][:SAMPLES]
TAG = re.compile(r"(<[^>]+>)")
ATTR = re.compile(r'([\w:-]+)="([^"]*)"')


def manifest(sid):
    rows = db._req("GET", "/rest/v1/snapshots", params={"select": "manifest", "id": f"eq.{sid}"}) or [{}]
    return rows[0].get("manifest") or {}


def keyed(mods):
    seen, res = {}, {}
    for m in mods:
        n = seen[m["label"]] = seen.get(m["label"], 0) + 1
        res[f'{m["label"]}#{n}'] = m["ref"]
    return res


diff_kinds = Counter()
examples = []
for s in sample:
    cur, prev = manifest(s["id"]), manifest(s["previous_snapshot_id"])
    a, b = keyed(prev.get("modules", [])), keyed(cur.get("modules", []))
    changed = [k for k in b if k in a and a[k] != b[k] and not k.startswith(("header", "footer", "chrome"))]
    texts = db.get_blobs(sorted({a[k] for k in changed} | {b[k] for k in changed})) if changed else {}
    shell_changed = prev.get("shell") != cur.get("shell")
    css_changed = prev.get("ruleset") != cur.get("ruleset")
    ex = {"market": s["market"], "page": s["page"], "device": s["device"], "at": s["captured_at"],
          "blocks_changed": changed, "shell_changed": shell_changed, "css_changed": css_changed, "diffs": []}
    for k in changed[:3]:
        x, y = TAG.split(texts.get(a[k], "")), TAG.split(texts.get(b[k], ""))
        kinds = set()
        lines = []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            old, new = "".join(x[i1:i2]), "".join(y[j1:j2])
            ao, an = dict(ATTR.findall(old)), dict(ATTR.findall(new))
            if TAG.sub("", old).strip() != TAG.sub("", new).strip():
                kinds.add("text")
            for attr in set(ao) | set(an):
                if ao.get(attr) != an.get(attr):
                    kinds.add("attr:" + ("data-*" if attr.startswith("data-") else attr))
            if op != "replace":
                kinds.add("element " + op)
            if len(lines) < 4:
                lines.append({"old": old[:220], "new": new[:220]})
        diff_kinds.update(kinds)
        ex["diffs"].append({"block": k, "kinds": sorted(kinds), "examples": lines})
    examples.append(ex)
out["style"]["diff_kinds"] = diff_kinds.most_common()
out["style"]["examples"] = examples
print("HEALTH_JSON_START" + json.dumps(out, default=str) + "HEALTH_JSON_END")
