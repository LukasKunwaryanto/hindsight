"""Show what actually differs in 'style' and 'text' changes (technical noise hunting)."""
import difflib
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
rows = db._req("GET", "/rest/v1/snapshot_changes", params={
    "select": "kind,detail", "kind": "in.(style,text)", "order": "id.desc", "limit": "400"}) or []
pairs = [(r["detail"].get("label"), r["detail"]["before"], r["detail"]["after"]) for r in rows if r["detail"].get("before")]
texts = db.get_blobs(sorted({h for _, a, b in pairs for h in (a, b)}))
patterns = Counter()
examples = {}
for label, a, b in pairs:
    x, y = texts.get(a, ""), texts.get(b, "")
    sm = difflib.SequenceMatcher(None, x, y, autojunk=False)
    for t, i1, i2, j1, j2 in sm.get_opcodes():
        if t == "equal":
            continue
        ctx = x[max(0, i1 - 40):i1]
        attr = re.findall(r'([\w:-]+)="[^"]*$', ctx)
        key = f"{label.split(':')[0]} | attr={attr[-1] if attr else '?'} | {x[i1:i2][:30]!r} -> {y[j1:j2][:30]!r}"
        k2 = re.sub(r"\d", "9", key)
        patterns[k2] += 1
        examples.setdefault(k2, (label, x[max(0, i1 - 120):i2 + 40], y[max(0, j1 - 120):j2 + 40]))
for k, n in patterns.most_common(25):
    print(f"{n:4}  {k}")
print("\n--- examples")
for k, _ in patterns.most_common(8):
    lab, p, q = examples[k]
    print(lab, "\n  A:", repr(p), "\n  B:", repr(q))
