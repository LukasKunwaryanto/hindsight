"""Human-readable descriptions of snapshots, for the app's timeline and promotion list.

Works on stored module HTML only, so the same code describes new captures and
back-fills old ones:
  module_info(html)      -> title (headline people see) and the Contentful IDs inside
  describe_changes(...)  -> per-block changes plus the snapshot's size, title and description
"""
import re
from urllib.parse import urlparse

import lxml.html

ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9]{20,24}$")
ASSET_RE = re.compile(r"hs-asset:([0-9a-f]{16})|src=\"(https?://[^\"]+)\"")
TOP_BLOCKS = 3      # the first blocks on the page count as the "hero area"
MANY = 3            # this many added/removed/replaced blocks is a big change anyway


def _text(el) -> str:
    """Visible text with a space between separate lines/spans."""
    return " ".join(t.strip() for t in el.itertext() if t.strip())


def image_key(url: str) -> str:
    """The same picture loaded at another size or resolution is the same image."""
    return urlparse(url).path.lower() if url else ""


def _clean(s: str, n: int = 60) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def module_info(html: str) -> dict:
    """Headline and contained Contentful IDs of one stored block."""
    try:
        el = lxml.html.fromstring(html)
    except Exception:
        return {"title": "", "ids": []}
    ids = [e.get("id") for e in el.iter() if isinstance(e.tag, str) and e.get("id") and ENTRY_ID_RE.match(e.get("id"))]
    title = ""
    for xp in ("//h1", "//h2", "//h3", "//*[contains(@class,'title')]", "//p"):
        for h in el.xpath(xp):
            t = _clean(_text(h))
            if len(t) >= 2:
                title = t
                break
        if title:
            break
    if not title:
        for e in el.xpath("//a[@title]|//img[@alt]"):
            t = _clean(e.get("title") or e.get("alt") or "")
            if t:
                title = t
                break
    return {"title": title, "ids": list(dict.fromkeys(ids))}


CLASS_RE = re.compile(r' class="[^"]*"')
SVG_RE = re.compile(r"hs-asset:([0-9a-f]{16}):image/svg\+xml")
BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
# Not changes: countdowns ticking, header states, carousels reshuffling the same products, icons not loaded yet
NOISE = ("timer", "state", "reorder", "flake")
CARD_RE = re.compile(r'<hs-card [ik]="[^"]+"></hs-card>')
SITE_PARTS = ("header", "footer", "chrome")


def _kind_of_edit(before: str, after: str, asset_urls: dict, label: str = "") -> str:
    """image | text | style, for a block whose HTML changed but kept its identity.
    asset_urls: stored image hash -> original URL, so re-encodes of one picture compare equal."""
    cards = lambda h: re.findall(r'<hs-card [ik]="([^"]+)"', h)  # in page order
    if CARD_RE.sub("", before) == CARD_RE.sub("", after) and cards(before) != cards(after):
        # only product cards moved around inside a carousel (v2 keys by SKU): same products → no change
        same = sorted(k.split("~")[0] for k in cards(before)) == sorted(k.split("~")[0] for k in cards(after))
        return "reorder" if same else "products"
    if label in SITE_PARTS and CLASS_RE.sub("", before) == CLASS_RE.sub("", after):
        return "state"  # header/bar only switched a styling state (e.g. scrolled), nothing visible changed
    img = lambda h: {image_key(asset_urls.get(a, a)) if a else image_key(b) for a, b in ASSET_RE.findall(h)}
    if img(before) != img(after):
        # an icon that hadn't loaded yet (stored as a blank) is not a new image
        svg = lambda h: {image_key(asset_urls.get(a, a)) for a in SVG_RE.findall(h)}
        if (img(before) ^ img(after)) <= (svg(before) | svg(after)) and (BLANK in before or BLANK in after):
            return "flake"
        return "image"
    txt = lambda h: " ".join(_text(lxml.html.fromstring(h)).split()) if h.strip() else ""
    try:
        a, b = txt(before), txt(after)
        if a != b:
            # only numbers ticked over, e.g. a countdown "6:15" -> "6:14": not an edit
            return "timer" if re.sub(r"\d", "", a) == re.sub(r"\d", "", b) else "text"
    except Exception:
        return "text"
    return "style"


def _keyed(mods):
    seen, out = {}, {}
    for m in mods:
        n = seen[m["label"]] = seen.get(m["label"], 0) + 1
        out[f'{m["label"]}#{n}'] = m
    return out


def describe_changes(prev: list[dict] | None, cur: list[dict], texts: dict[str, str], asset_urls: dict | None = None,
                     products_are_noise: bool = False, ignore_site_parts: bool = False, page_title: str = ""):
    """Compare two snapshots' blocks.

    prev/cur: module rows (position, label, entry_id, blob_hash, title).
    texts: blob_hash -> HTML for every block in both lists.
    products_are_noise: a carousel showing other products is not a change (captures not caused by a
      Contentful publish: there it is usually a recommendation or bestseller list rotating).
    ignore_site_parts: bars, header and footer are kept in their own timeline (inner pages).
    page_title: the page's <title>, named in a first capture when no block has a headline.
    Returns (changes, summary) where summary = {change_size, change_title, change_description}.
    """
    if prev is None:
        hero = next((m for m in cur if m["label"].startswith("block:") and m.get("entry_id") and m.get("title")), None)
        return ([{"kind": "added", "entry_id": None, "position": None, "detail": {"note": "first snapshot"}}],
                {"change_size": "big", "change_title": FIRST_TITLE,
                 "change_description": _clean((hero or {}).get("title") or page_name(page_title), 80)})

    # rank of each content block on the page (spacers and header/footer don't count)
    def ranks(mods):
        r, out = 0, {}
        for m in mods:
            if m["label"].startswith("block:") and m.get("entry_id"):
                out[id(m)] = r
                r += 1
        return out

    pr, cr = ranks(prev), ranks(cur)
    pk, ck = _keyed(prev), _keyed(cur)
    added = [ck[k] for k in ck if k not in pk]
    removed = [pk[k] for k in pk if k not in ck]

    changes = []
    # a block that disappeared where a new one appeared at the same rank was replaced
    removed_by_rank = {pr.get(id(m)): m for m in removed if id(m) in pr}
    for m in added:
        rank = cr.get(id(m))
        old = removed_by_rank.pop(rank, None) if rank is not None else None
        if old is not None:
            removed.remove(old)
            changes.append({"kind": "replaced", "entry_id": m["entry_id"], "position": m["position"], "rank": rank,
                            "detail": {"label": m["label"], "title": m.get("title", ""), "was": old.get("title", ""),
                                       "was_entry_id": old.get("entry_id")}})
        else:
            changes.append({"kind": "added", "entry_id": m["entry_id"], "position": m["position"], "rank": rank,
                            "detail": {"label": m["label"], "title": m.get("title", "")}})
    for m in removed:
        changes.append({"kind": "removed", "entry_id": m["entry_id"], "position": m["position"], "rank": pr.get(id(m)),
                        "detail": {"label": m["label"], "title": m.get("title", "")}})
    for k, m in ck.items():
        p = pk.get(k)
        if p and p["blob_hash"] != m["blob_hash"]:
            kind = _kind_of_edit(texts.get(p["blob_hash"], ""), texts.get(m["blob_hash"], ""), asset_urls or {}, m["label"])
            changes.append({"kind": kind, "entry_id": m["entry_id"], "position": m["position"], "rank": cr.get(id(m)),
                            "detail": {"label": m["label"], "title": m.get("title", ""), "was": p.get("title", ""),
                                       "before": p["blob_hash"], "after": m["blob_hash"]}})
    order_prev = [k for k in pk if k in ck]
    order_cur = [k for k in ck if k in pk]
    if order_prev != order_cur:
        changes.append({"kind": "moved", "entry_id": None, "position": None, "rank": None,
                        "detail": {"before": order_prev, "after": order_cur}})

    # countdowns ticking and header states are not changes; header, footer and spacers never make a change "big"
    changes = [c for c in changes if c["kind"] not in NOISE and not (products_are_noise and c["kind"] == "products")
               and not (ignore_site_parts and c.get("detail", {}).get("label") in SITE_PARTS)]
    content = [c for c in changes if c.get("rank") is not None]
    structural = [c for c in content if c["kind"] in ("added", "removed", "replaced")]
    top = [c for c in content if (c["rank"] < TOP_BLOCKS and c["kind"] in ("added", "removed", "replaced"))
           or (c["rank"] == 0 and c["kind"] == "image")]
    if not changes:
        size = "none"
    elif top or len(structural) >= MANY:
        size = "big"
    elif structural or any(c["kind"] == "image" for c in content):
        size = "medium"
    else:
        size = "small"

    # the headline change: hero first, then structural, then images, then the rest
    prio = {"replaced": 0, "added": 1, "removed": 2, "image": 3, "text": 4, "products": 5, "style": 6, "moved": 7}
    main = sorted(content or changes, key=lambda c: (prio.get(c["kind"], 9), c.get("rank") if c.get("rank") is not None else 99))
    title, desc = "No visible change", ""
    if main:
        c = main[0]
        d = c.get("detail", {})
        others = len([x for x in (content or changes) if x is not c and x["kind"] not in ("moved", "style")])
        title, desc = headline(c["kind"], c.get("rank") == 0, d.get("title", ""), d.get("was", ""), others)
    for c in changes:
        c.pop("rank", None)
    return changes, {"change_size": size, "change_title": title, "change_description": desc}


# --- names in the timeline: what happened (title) + what it is about (description) -------------
FIRST_TITLE = "Archive starts"


def page_name(title: str) -> str:
    """'Nyheter | Nordic Nest' -> 'Nyheter'."""
    return re.split(r"\s+[|–—-]\s+", title or "")[0].strip()


def headline(kind: str, hero: bool, head: str, was: str = "", others: int = 0) -> tuple[str, str]:
    """Title and description for the main change of a snapshot.
    head = visible headline of the changed block, was = the one it replaced."""
    names = {"replaced": "New hero" if hero else "New banner", "added": "Banner added", "removed": "Banner removed",
             "image": "New hero image" if hero else "New image", "text": "Text updated", "style": "Style updated",
             "products": "Products changed", "moved": "Reordered"}
    title = names.get(kind, "Change")
    was = _clean(was, 28)
    head = _clean(head, 48 if was and kind == "replaced" else 80)
    if kind == "replaced" and was and was != head:
        desc = f"{head} (was {was})" if head else f"was {was}"
    else:
        desc = head
    more = f"{others} more change{'s' if others > 1 else ''}" if others else ""
    if more:
        desc = f"{_clean(desc, 80 - len(more) - 3)} · {more}" if desc else more
    return title, _clean(desc, 80)


OLD_NAMES = {"First capture": FIRST_TITLE, "Text edit": "Text updated", "Style tweak": "Style updated"}
KIND_OF = {"New hero": ("replaced", True), "New banner": ("replaced", False), "Banner added": ("added", False),
           "Banner removed": ("removed", False), "New hero image": ("image", True), "New image": ("image", False),
           "Text updated": ("text", False), "Style updated": ("style", False), "Products changed": ("products", False),
           "Reordered": ("moved", False)}


def rename(title: str, desc: str, changes: list[dict], page_title: str = ""):
    """New-style name for a snapshot named the old way ("First capture", "New banner +3", desc "was X").
    Returns (title, description), or None when it already has a new-style name."""
    m = re.match(r"^(.*?)(?: \+(\d+))?$", title or "")
    base, others = m.group(1), int(m.group(2) or 0)
    old = bool(m.group(2)) or base in OLD_NAMES
    base = OLD_NAMES.get(base, base)
    if base == FIRST_TITLE:
        return (FIRST_TITLE, _clean(desc or page_name(page_title), 80)) if old else None
    if base not in KIND_OF:
        return None
    kind, hero = KIND_OF[base]
    if kind == "removed" and (desc or "").startswith("was "):
        old, desc = True, desc[4:]
    was = ""
    if kind == "replaced" and desc and "(was " not in desc:
        was = next((c.get("detail", {}).get("was", "") for c in changes or []
                    if c.get("kind") == "replaced" and _clean(c.get("detail", {}).get("title", ""), 60) == desc), "")
        old = old or bool(was)
    if kind == "replaced" and (desc or "").startswith("was "):
        desc, was = "", desc[4:]
    return headline(kind, hero, desc or "", was, others) if old else None
