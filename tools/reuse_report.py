"""How much of a market's pages is shared? For the newest snapshot of every page (startpage and inner pages)
per market and device: distinct versions of each site part, the CSS and the page skeleton, and what the
unshared copies cost. Env: SUPABASE_URL, SUPABASE_SECRET_KEY."""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
rows, off = [], 0
while True:
    page = db._req("GET", "/rest/v1/snapshots", params={"select": "market,device,page,captured_at,manifest",
                   "order": "captured_at.desc", "limit": "200", "offset": str(off)}) or []
    rows += page
    if len(page) < 200:
        break
    off += 200
latest = {}
for r in rows:
    latest.setdefault((r["market"], r["device"], r["page"]), r)
sizes = {}
def size(hs):
    need = [h for h in hs if h not in sizes]
    for i in range(0, len(need), 150):
        for b in db._req("GET", "/rest/v1/blobs", params={"select": "hash,size_bytes", "hash": f"in.({','.join(need[i:i+150])})"}) or []:
            sizes[b["hash"]] = b["size_bytes"]
    return sum(sizes.get(h, 0) for h in hs)

groups = defaultdict(lambda: defaultdict(set))
pages_per = defaultdict(int)
for (m, d, p), r in latest.items():
    man = r["manifest"]
    pages_per[(m, d)] += 1
    g = groups[(m, d)]
    parts = [x for x in man["modules"] if x["label"] in ("header", "footer", "chrome")]
    for i, x in enumerate(parts):
        g[f"part{i}:{x['label']}"].add(x["ref"])
    g["shell"].add(man["shell"])
    g["ruleset"].add(man["ruleset"])
    g["static_css"].add(tuple(man["static_css"]))
    g["page_type"].add("start" if p == "/" else "inner")
out = {"markets": {}, "totals": defaultdict(lambda: [0, 0])}
for (m, d), g in sorted(groups.items()):
    n = pages_per[(m, d)]
    res = {"pages": n}
    for k, v in g.items():
        if k == "page_type":
            continue
        hs = [h for t in v for h in (t if isinstance(t, tuple) else (t,))]
        res[k] = [len(v), size(hs)]
        out["totals"][k][0] += len(v)
        out["totals"][k][1] += size(hs)
    out["markets"][f"{m}|{d}"] = res
# ruleset chunk sharing: across all latest pages, how many distinct chunks vs references
chunk_refs, chunks = 0, set()
rs = {r["manifest"]["ruleset"] for r in latest.values()}
texts = db.get_blobs(sorted(rs))
for h, t in texts.items():
    j = json.loads(t)
    if isinstance(j, dict):
        chunk_refs += len(j["chunks"]); chunks |= set(j["chunks"])
out["chunks"] = {"references": chunk_refs, "distinct": len(chunks), "distinct_bytes": size(sorted(chunks))}
out["pages_total"] = len(latest)
print("REUSE_JSON_START"); print(json.dumps(out)); print("REUSE_JSON_END")
