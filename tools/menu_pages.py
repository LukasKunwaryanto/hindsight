"""Keep the menu pages in table `pages`: the top-level links in each market's menu (Nyheter, campaign pages,
main categories, brands, inspiration), read from the newest stored startpage header. Each page gets its
Contentful entry where the site's delivery key is set, so publishes under it start a capture. Menu pages
that left the menu are switched off (their history stays).

Env: SUPABASE_URL, SUPABASE_SECRET_KEY, CONTENTFUL_DELIVERY_TOKEN[_KITCHENTIME|_SVENSSONS]."""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import lxml.html

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))
from store import SupabaseStore  # noqa: E402
from sync_test import PAGE_TYPES, SITES  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
SKIP = re.compile(r"favori|wishlist|ønskeliste|toivelista|wunschliste|mina-sidor|my-pages|kundservice|customer", re.I)
TYPES = [t for t in ("productListingPage", "collection", "brand", "inspiration", "inspirationCategory", "editorial",
                     "rootCategory", "room")]


def cda(space, token, path, **q):
    url = f"https://cdn.contentful.com/spaces/{space}/environments/master{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


ERRORS = {}
_index = {}


def page_index(site):
    """(language, path) -> (entry id, internal name) for every page entry of a site. `urls` is a JSON field
    (the page's parent entry ids + its path, per language), which Contentful can't filter on, so all page
    entries are read once with only their ids and urls."""
    if site in _index:
        return _index[site]
    space, _, token_env, _ = SITES[site]
    token = os.environ.get(token_env, "").strip()
    idx = {}
    if token:
        for t in TYPES:
            skip = 0
            while True:
                try:
                    res = cda(space, token, "/entries", content_type=t, locale="*", limit=1000, skip=skip,
                              select="sys.id,fields.urls,fields.internalName")
                except Exception as e:
                    ERRORS.setdefault((site, t), str(e)[:200])
                    break
                for e in res.get("items", []):
                    f = e.get("fields", {})
                    name = next(iter((f.get("internalName") or {}).values()), None)
                    for lang, urls in (f.get("urls") or {}).items():
                        # a list of URLs, each a list of parent entry ids ending with the path
                        flat = [x for u in urls for x in (u if isinstance(u, list) else [u])] if isinstance(urls, list) else []
                        for u in flat:
                            if isinstance(u, str) and u.startswith("/"):
                                idx.setdefault((lang, u if u.endswith("/") else u + "/"), (e["sys"]["id"], name))
                skip += 1000
                if skip >= res.get("total", 0):
                    break
    _index[site] = idx
    print(f"{site}: {len(idx)} page URLs in Contentful", flush=True)
    return idx


def entries_for(site, lang, paths):
    idx = page_index(site)
    default = next((l for (l, _) in idx if l == "yo"), None)  # KitchenTime: most languages use the default
    return {p: idx.get((lang, p)) or idx.get((default, p)) for p in paths if idx.get((lang, p)) or idx.get((default, p))}


markets = db._req("GET", "/rest/v1/markets", params={"select": "locale,url,lang,site", "enabled": "eq.true"})
existing = {(p["market"], p["path"]): p for p in db._req("GET", "/rest/v1/pages", params={"select": "market,path,kind,enabled,entry_id,title"}) or []}
rows, seen, report = [], set(), []
for m in markets:
    _, mods, man = db.latest_snapshot(m["locale"], "desktop", "/")
    if not man:
        continue
    header = next((x["ref"] for x in man["modules"] if x["label"] == "header"), None)
    html = db.get_blobs([header]).get(header, "") if header else ""
    host = urllib.parse.urlparse(m["url"]).netloc
    doc = lxml.html.fromstring(html or "<div/>")
    paths = []
    for a in doc.xpath("//nav//a[@href]"):
        u = urllib.parse.urlparse(a.get("href"))
        if u.netloc and u.netloc != host:
            continue
        path = u.path if u.path.endswith("/") else u.path + "/"
        if path == "/" or SKIP.search(path) or path.count("/") > 3:
            continue
        paths.append(path)
    paths = list(dict.fromkeys(paths))
    found = entries_for(m["site"] or "nordicnest", m["lang"] or m["locale"],
                        [p for p in paths if not (existing.get((m["locale"], p)) or {}).get("entry_id")])
    for path in paths:
        seen.add((m["locale"], path))
        old = existing.get((m["locale"], path))
        eid, title = (old["entry_id"], None) if old and old.get("entry_id") else found.get(path, (None, None))
        rows.append({"market": m["locale"], "path": path, "kind": "menu", "enabled": True, "entry_id": eid,
                     "title": title or (old or {}).get("title")})
    report.append(f"| {m['locale']} | {len(paths)} | {sum(1 for r in rows if r['market'] == m['locale'] and r['entry_id'])} | {' '.join(paths)} |")

for i in range(0, len(rows), 200):
    db._req("POST", "/rest/v1/pages", rows[i:i + 200], {"Prefer": "return=minimal,resolution=merge-duplicates"},
            params={"on_conflict": "market,path"})
gone = [k for k, p in existing.items() if p["kind"] == "menu" and p["enabled"] and k not in seen
        and any(k[0] == m["locale"] for m in markets)]
for mk, path in gone:
    db._req("PATCH", "/rest/v1/pages", {"enabled": False}, {"Prefer": "return=minimal"},
            params={"market": f"eq.{mk}", "path": f"eq.{path}", "kind": "eq.menu"})
print("Contentful errors (first per site and type):", json.dumps({f"{k[0]}/{k[1]}": v for k, v in ERRORS.items()}, indent=1)[:3000])
md = "\n".join(["## Menu pages", "", f"{len(rows)} menu pages, {sum(1 for r in rows if r['entry_id'])} with a Contentful entry; "
                f"{len(gone)} switched off (left the menu).", "", "| Market | Pages | With entry | Paths |", "| --- | --- | --- | --- |"] + report)
print(md)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
