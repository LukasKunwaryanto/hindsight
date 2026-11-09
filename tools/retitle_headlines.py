"""Re-read block headlines with the current rule (no buttons, call-to-action links or footnotes) and update
what was named with the old ones: block titles, change lists and the timeline descriptions. Safe to re-run.

Env: SUPABASE_URL, SUPABASE_SECRET_KEY. DRY_RUN=1 lists the changes without writing.
"""
import os
import re
import sys

import lxml.html

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from describe import FIRST_TITLE, KIND_OF, _clean, _text, headline, module_info  # noqa: E402
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
DRY = os.environ.get("DRY_RUN") == "1"
SITE_PARTS = ("header", "footer", "chrome")


def fetch_all(table, **params):
    rows, off = [], 0
    while True:
        page = db._req("GET", f"/rest/v1/{table}", params={**params, "limit": "1000", "offset": str(off)}) or []
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def old_title(html: str) -> str:
    """The headline rule used before 9 Nov (all text of the element, buttons included)."""
    try:
        el = lxml.html.fromstring(html)
    except Exception:
        return ""
    for xp in ("//h1", "//h2", "//h3", "//*[contains(@class,'title')]", "//p"):
        for h in el.xpath(xp):
            t = _clean(_text(h))
            if len(t) >= 2:
                return t
    for e in el.xpath("//a[@title]|//img[@alt]"):
        t = _clean(e.get("title") or e.get("alt") or "")
        if t:
            return t
    return ""


# 1. every stored block, startpages (snapshot_modules) and inner pages (manifest)
mod_rows = fetch_all("snapshot_modules", select="blob_hash,title")
inner = fetch_all("snapshots", select="id,changes,modules:manifest->modules", page="neq./")
hashes = {r["blob_hash"] for r in mod_rows}
hashes |= {m["ref"] for s in inner for m in (s.get("modules") or []) if m.get("label") not in SITE_PARTS}
texts = {}
hl = sorted(hashes)
for i in range(0, len(hl), 200):
    texts.update(db.get_blobs(hl[i:i + 200]))
stored = {r["blob_hash"]: r.get("title") or "" for r in mod_rows}
new_of_hash, mapping = {}, {}
for h, html in texts.items():
    new = module_info(html)["title"]
    new_of_hash[h] = new
    for old in {stored.get(h) or "", old_title(html)}:
        if old and old != new:
            mapping.setdefault(old, new)
print(f"{len(texts)} blocks, {len(mapping)} headlines changed")


def m(text: str) -> str:
    """Old headline (possibly shortened with …) -> new headline."""
    if not text:
        return text
    if text in mapping:
        return mapping[text]
    if text.endswith("…"):
        cands = {v for k, v in mapping.items() if k.startswith(text[:-1])}
        if len(cands) == 1:
            return cands.pop()
    return text


def fix_detail(d: dict) -> dict:
    d = dict(d or {})
    for k in ("title", "was"):
        if d.get(k):
            d[k] = m(d[k])
    return d


DESC_RE = re.compile(r"^(.*?)(?: \(was (.*?)\))?(?: · (\d+) more changes?)?$")


def fix_desc(title: str, desc: str):
    if not desc:
        return desc
    if title == FIRST_TITLE:
        return _clean(m(desc), 80)
    if title not in KIND_OF:
        return desc
    head, was, more = DESC_RE.match(desc).groups()
    kind, hero = KIND_OF[title]
    if head.startswith("was ") and not was:
        if kind == "replaced":
            head, was = "", head[4:]
        else:  # the block had no headline of its own: described by the one it had before
            return headline(kind, hero, "was " + m(head[4:]), "", int(more or 0))[1]
    return headline(kind, hero, m(head or ""), m(was or ""), int(more or 0))[1]


# 2. what to write
block_updates = [(h, t) for h, t in new_of_hash.items() if h in stored and stored[h] != t]
change_rows = fetch_all("snapshot_changes", select="id,detail")
change_updates = [(r["id"], fix_detail(r["detail"])) for r in change_rows
                  if r.get("detail") and fix_detail(r["detail"]) != r["detail"]]
inner_updates = []
for s in inner:
    ch = [{**c, "detail": fix_detail(c.get("detail"))} for c in (s.get("changes") or [])]
    if ch != (s.get("changes") or []):
        inner_updates.append((s["id"], ch))
snaps = fetch_all("snapshots", select="id,market,page,change_title,change_description")
desc_updates = []
for s in snaps:
    new = fix_desc(s.get("change_title") or "", s.get("change_description") or "")
    if new != (s.get("change_description") or ""):
        desc_updates.append((s, new))

lines = [f"## Retitle headlines{' (dry run)' if DRY else ''}", "",
         f"{len(block_updates)} block titles, {len(change_updates)} change rows, {len(inner_updates)} inner-page "
         f"change lists, {len(desc_updates)} timeline descriptions", "",
         "| Timeline title | Before | After | Market | Page |", "|---|---|---|---|---|"]
for s, new in desc_updates[:40]:
    lines.append(f'| {s["change_title"]} | {s["change_description"]} | {new} | {s["market"]} | {s["page"]} |')

if not DRY:
    for h, t in block_updates:
        db._req("PATCH", "/rest/v1/snapshot_modules", {"title": t}, {"Prefer": "return=minimal"},
                params={"blob_hash": f"eq.{h}"})
    for cid, d in change_updates:
        db._req("PATCH", "/rest/v1/snapshot_changes", {"detail": d}, {"Prefer": "return=minimal"}, params={"id": f"eq.{cid}"})
    for sid, ch in inner_updates:
        db._req("PATCH", "/rest/v1/snapshots", {"changes": ch}, {"Prefer": "return=minimal"}, params={"id": f"eq.{sid}"})
    for s, d in desc_updates:
        db._req("PATCH", "/rest/v1/snapshots", {"change_description": d}, {"Prefer": "return=minimal"},
                params={"id": f"eq.{s['id']}"})
    lines += ["", "Written."]

md = "\n".join(lines)
print(md)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
