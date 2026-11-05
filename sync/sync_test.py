"""Hindsight – Phase 0 Contentful Sync API test.

1. Walks the Nordic Nest startpage entry and everything it links to (blocks, banners,
   buttons, links, images) and reports the tree per market.
2. Lists which of those entries were published recently, and in which markets they show.
3. Runs a full initial sync of the space (what the poller needs once) and measures it,
   then makes one delta call with the returned token.

Needs the CONTENTFUL_DELIVERY_TOKEN environment variable (a read-only delivery key).
Usage:  python sync/sync_test.py --days 7
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# One Contentful space per site: (space, startpage entry, secret holding its delivery key, market id prefix).
# Keep in sync with SITES in supabase/functions/sync-poll/index.ts.
SITES = {
    "nordicnest":  ("p7vzp7ftmsr1", "6q7zaSZcLXtokLbBepLSgo", "CONTENTFUL_DELIVERY_TOKEN", ""),
    "kitchentime": ("ft75u7lxp5jx", "30hwuZt1hYaRZfxowVl3Nq", "CONTENTFUL_DELIVERY_TOKEN_KITCHENTIME", "kitchentime:"),
    "svenssons":   ("5dqc39pi2ix6", "7xIUJL0SDo8S0otVLWePsx", "CONTENTFUL_DELIVERY_TOKEN_SVENSSONS", "svenssons:"),
}
SITE = os.environ.get("HINDSIGHT_SITE", "nordicnest")
SPACE, START_ID, TOKEN_ENV, MARKET_PREFIX = SITES[SITE]
ENV = "master"
BASE = f"https://cdn.contentful.com/spaces/{SPACE}/environments/{ENV}"
TOKEN = os.environ.get(TOKEN_ENV, "").strip()


def get(path_or_url: str, params: dict | None = None, too_big_ok: bool = False) -> dict:
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited: wait as told
                time.sleep(float(e.headers.get("X-Contentful-RateLimit-Reset", 1)) + 0.5)
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            if too_big_ok and "Response size too big" in body:
                raise TooBig()
            raise SystemExit(f"Contentful returned HTTP {e.code} for {url.split('?')[0]}: {body}")
    raise SystemExit("Rate limited too many times")


def links_in(value):
    """Yield (type, id) for every Link found anywhere inside a field value."""
    if isinstance(value, dict):
        sys = value.get("sys")
        if isinstance(sys, dict) and sys.get("type") == "Link":
            yield sys.get("linkType"), sys.get("id")
            return
        for v in value.values():
            yield from links_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from links_in(v)


class TooBig(Exception):
    pass


def fetch_entries(ids: list[str], chunk: int = 25) -> list[dict]:
    """Fetch entries with all locales, no linked entries included. Halves the batch
    when Contentful says the response is too big (some entries are very large)."""
    out = []
    i = 0
    while i < len(ids):
        part = ids[i:i + chunk]
        try:
            res = get("/entries", {"sys.id[in]": ",".join(part), "locale": "*",
                                   "include": 0, "limit": len(part)}, too_big_ok=True)
        except TooBig:
            if chunk == 1:
                print(f"Skipping entry {part[0]}: too large to fetch with all locales")
                i += 1
                continue
            chunk = max(1, chunk // 2)
            continue
        out += res.get("items", [])
        i += len(part)
    return out


# Entries that are pages of their own. The startpage can link to them (a button, a tile),
# but their content is not shown on the startpage, so the walk stops there.
PAGE_TYPES = {
    "start", "productListingPage", "collection", "brand", "designer", "editorial", "room",
    "rootCategory", "inspiration", "inspirationArticle", "inspirationCategory", "recipe",
    "storeListing", "store", "search", "checkout", "expresscheckout", "orderConfirmation",
    "AuthorizationPage", "myPagesProfilePage", "myPagesStartPage", "productConfiguration",
    "seoInfo", "ContentTag",
}


def start_content(start: dict, langs: list[str] | None = None, default: str | None = None) -> dict:
    """The startpage's block list per language. A language without its own list shows the
    default language's list (Contentful fallback), e.g. KitchenTime's da-DK, de-DE, fi-FI."""
    content = start["fields"].get("content", {})
    if not langs:
        return content
    return {L: content.get(L, content.get(default)) for L in langs if content.get(L, content.get(default))}


def walk_tree(max_depth: int = 8, langs: list[str] | None = None, default: str | None = None):
    """Breadth-first walk from the startpage. Returns entries, asset ids and, per entry,
    the markets (locales) whose startpage reaches it."""
    entries, assets = {}, set()
    markets_of = defaultdict(set)
    start = fetch_entries([START_ID])
    if not start:
        raise SystemExit("Startpage entry not found with this key.")
    entries[START_ID] = start[0]
    # markets: the startpage's own per-locale content lists
    frontier = []
    for locale, blocks in start_content(start[0], langs, default).items():
        for typ, eid in links_in(blocks):
            if typ == "Entry":
                markets_of[eid].add(locale)
                frontier.append(eid)
    depth = 1
    while frontier and depth <= max_depth:
        new = [e for e in dict.fromkeys(frontier) if e not in entries]
        fetched = fetch_entries(new)
        next_frontier = []
        for e in fetched:
            eid = e["sys"]["id"]
            entries[eid] = e
        for eid in dict.fromkeys(frontier):
            e = entries.get(eid)
            if not e:
                continue
            if e["sys"]["contentType"]["sys"]["id"] in PAGE_TYPES:
                continue  # linked page: shown as a link only, don't descend into it
            for field in e.get("fields", {}).values():
                for typ, child in links_in(field):
                    if typ == "Asset":
                        assets.add(child)
                    elif typ == "Entry":
                        markets_of[child] |= markets_of[eid]
                        next_frontier.append(child)
        frontier = next_frontier
        depth += 1
    return entries, assets, markets_of, depth - 1


def entry_name(e: dict) -> str:
    f = e.get("fields", {})
    for key in ("name", "internalName", "title"):
        v = f.get(key)
        if isinstance(v, dict) and v:
            return str(next(iter(v.values())))[:80]
    return ""


def full_initial_sync(time_budget_s: int = 900):
    started = time.monotonic()
    counts, pages, bytes_total = Counter(), 0, 0
    url, params = "/sync", {"initial": "true", "limit": 200}
    next_sync = None
    while url:
        try:
            res = get(url, params, too_big_ok=True)
        except TooBig:
            if params and params["limit"] > 10:   # first page: retry with smaller pages
                params["limit"] //= 2
                continue
            return dict(complete=False, pages=pages, counts=counts, mb=bytes_total / 1e6,
                        seconds=round(time.monotonic() - started), next_sync=None,
                        note="a page was too large to return")
        if params:
            print(f"Initial sync page size: {params['limit']}")
        params = None
        pages += 1
        bytes_total += len(json.dumps(res))
        for item in res.get("items", []):
            counts[item["sys"]["type"]] += 1
        if res.get("nextSyncUrl"):
            next_sync = res["nextSyncUrl"]
            url = None
        else:
            url = res.get("nextPageUrl")
        if time.monotonic() - started > time_budget_s:
            return dict(complete=False, pages=pages, counts=counts, mb=bytes_total / 1e6,
                        seconds=round(time.monotonic() - started), next_sync=None)
    return dict(complete=True, pages=pages, counts=counts, mb=bytes_total / 1e6,
                seconds=round(time.monotonic() - started), next_sync=next_sync)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--skip-full-sync", action="store_true")
    args = ap.parse_args()
    if not TOKEN:
        raise SystemExit("CONTENTFUL_DELIVERY_TOKEN is not set.")

    md = ["## Hindsight Sync API test", ""]

    # 1. Startpage tree
    t0 = time.monotonic()
    entries, assets, markets_of, depth = walk_tree()
    by_type = Counter(e["sys"]["contentType"]["sys"]["id"] for e in entries.values())
    start = entries[START_ID]
    per_locale = {loc: sum(1 for t, _ in links_in(v) if t == "Entry")
                  for loc, v in start["fields"].get("content", {}).items()}
    md += [
        f"**Startpage tree:** {len(entries)} entries and {len(assets)} images/files, "
        f"{depth} levels deep, read in {time.monotonic() - t0:.0f} s.",
        "",
        "| Market | Blocks on startpage |",
        "| --- | --- |",
        *[f"| {loc} | {n} |" for loc, n in sorted(per_locale.items())],
        "",
        "| Content type | Entries in tree |",
        "| --- | --- |",
        *[f"| {t} | {n} |" for t, n in by_type.most_common()],
        "",
    ]

    # 2. Recently published entries in the tree
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    recent = []
    for eid, e in entries.items():
        ts = datetime.fromisoformat(e["sys"]["updatedAt"].replace("Z", "+00:00"))
        if ts >= since:
            mk = sorted(markets_of.get(eid, set())) if eid != START_ID else sorted(per_locale)
            recent.append((ts, e["sys"]["contentType"]["sys"]["id"], entry_name(e), eid, mk, e["sys"].get("revision")))
    recent.sort(reverse=True)
    md += [f"**Published in the last {args.days} days:** {len(recent)} of the startpage's entries.", "",
           "| Published (UTC) | Type | Name | Markets | Revision |", "| --- | --- | --- | --- | --- |"]
    for ts, typ, name, eid, mk, rev in recent[:40]:
        mtxt = "all" if len(mk) >= len(per_locale) and per_locale else (", ".join(mk) if mk else "?")
        md.append(f"| {ts:%Y-%m-%d %H:%M} | {typ} | {name or eid} | {mtxt} | {rev} |")
    md.append("")

    # 3. Full initial sync + one delta call
    if not args.skip_full_sync:
        s = full_initial_sync()
        c = s["counts"]
        md += [
            f"**Full initial sync:** {'complete' if s['complete'] else 'stopped at the time limit'} in "
            f"{s['seconds']} s, {s['pages']} pages, {s['mb']:.1f} MB.",
            "",
            "| Item type | Count |", "| --- | --- |",
            *[f"| {k} | {v} |" for k, v in c.most_common()],
            "",
        ]
        if s["next_sync"]:
            d = get(s["next_sync"])
            items = d.get("items", [])
            md.append(f"**Delta call right after:** {len(items)} changed items; a new sync token was returned "
                      f"({'yes' if d.get('nextSyncUrl') else 'no'}).")
            md.append("")

    text = "\n".join(md)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    with open("sync-report.md", "w", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
