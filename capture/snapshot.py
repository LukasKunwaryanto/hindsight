"""Turn a frozen page into stored pieces: skeleton, blocks, product cards, badges,
CSS rules, fonts and banner images. Identical pieces are stored once (named by hash).
"""
import gzip
import io
import json
import re
from urllib.parse import urljoin, urlparse

import lxml.html
from PIL import Image

from browser import sha16
from css import class_map, parse_rules, rename_selector

ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9]{20,24}$")
CARD_PARTS = ("product-card-add-to-wishlist", "product-card-add-to-cart", "stock-status")
TRANSPARENT_GIF = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="


def is_product_image(url: str) -> bool:
    return "/assets/blobs/" in url


class Pieces:
    """Collects everything a snapshot needs, before it is sent to the store."""

    def __init__(self):
        self.blobs: dict[str, tuple[str, str]] = {}           # hash -> (kind, text)
        self.assets: dict[str, dict] = {}                    # hash -> {bytes, mime, w, h, url}

    def blob(self, kind: str, text: str) -> str:
        h = sha16(text)
        self.blobs.setdefault(h, (kind, text))
        return h

    def asset(self, data: bytes, mime: str, url: str, w=None, h=None) -> str:
        key = sha16(data)
        self.assets.setdefault(key, dict(bytes=data, mime=mime, url=url, w=w, h=h))
        return f"hs-asset:{key}:{mime}"


def _encode_image(raw: bytes, ct: str, width: float | None, mask: bool):
    """Return (bytes, mime, w, h) at the size it is displayed, or None if not an image."""
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
    if mask:  # CSS mask: an opaque image only lets everything through, keep a 1px stand-in
        rgba = im.convert("RGBA")
        if rgba.getchannel("A").getextrema()[0] == 255:
            out = Image.new("RGBA", (1, 1), (0, 0, 0, 255))
        else:
            s = min(1, 400 / rgba.width)
            out = rgba.resize((max(1, round(rgba.width * s)), max(1, round(rgba.height * s))))
        buf = io.BytesIO()
        out.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png", out.width, out.height
    W, H = im.size
    if not width or width < 1:
        width = min(W, 300)
    s = min(1, width / W)
    size = (max(1, round(W * s)), max(1, round(H * s)))
    frames = getattr(im, "n_frames", 1)
    buf = io.BytesIO()
    if frames > 1:  # animated GIF: keep the animation
        seq, durations = [], []
        for f in range(frames):
            im.seek(f)
            seq.append(im.convert("RGBA").resize(size, Image.LANCZOS))
            durations.append(im.info.get("duration", 100))
        seq[0].save(buf, "WEBP", save_all=True, append_images=seq[1:], duration=durations, loop=0, quality=40)
        return buf.getvalue(), "image/webp", *size
    mode = "RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB"
    im.convert(mode).resize(size, Image.LANCZOS).save(buf, "AVIF", quality=35, speed=6)
    return buf.getvalue(), "image/avif", *size


def build(frozen: dict, page_url: str) -> tuple[dict, list[dict], Pieces]:
    """Split a frozen page. Returns (manifest, module rows, pieces)."""
    P = Pieces()
    res = frozen["resources"]
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}/"
    cache: dict = {}

    def asset_for(url: str, width=None, mask=False) -> str | None:
        key = (url, round(width or 0, 2), mask)
        if key in cache:
            return cache[key]
        raw, ct = res.get(url, (None, ""))
        out = None
        if raw is None:
            out = None
        elif "svg" in ct or url.split("?")[0].endswith(".svg"):
            out = P.asset(raw, "image/svg+xml", url)
        elif "font" in ct or url.split("?")[0].endswith((".woff2", ".woff", ".ttf")):
            out = P.asset(raw, ct.split(";")[0] or "font/woff2", url)
        else:
            enc = _encode_image(raw, ct, width, mask)
            if enc:
                out = P.asset(enc[0], enc[1], url, enc[2], enc[3])
        cache[key] = out
        return out

    def fix_css_urls(css: str, sheet_base: str) -> str:
        """Point url(...) at stored assets. Masks are shrunk; product images stay external."""
        out = []
        for decl in re.split(r"(;)", css):
            if "url(" not in decl:
                out.append(decl)
                continue
            is_mask = "mask" in decl.split(":", 1)[0]

            def rep(m):
                ref = m.group(1).strip("'\" ")
                if ref.startswith("data:"):
                    return m.group(0)
                absu = urljoin(sheet_base, ref)
                if is_product_image(absu):
                    return f'url("{absu}")'
                a = asset_for(absu, 1440 if not is_mask else None, mask=is_mask)
                return f'url("{a}")' if a else 'url("")'

            out.append(re.sub(r"url\(([^)]+)\)", rep, decl))
        return "".join(out)

    # --- CSS: static files whole, injected rules one by one --------------------
    injected = [s for s in frozen["sheets"] if not s.get("href")]
    cmap = class_map(injected)
    static, rules, seen = [], [], set()
    for sh in frozen["sheets"]:
        if sh.get("href"):
            if sh["href"] in seen:
                continue
            seen.add(sh["href"])
            static.append(P.blob("static_css", fix_css_urls(sh["css"], sh["href"])))
        else:
            css = sh["css"] if not sh.get("media") else f"@media {sh['media']}{{{sh['css']}}}"
            for media, sel, body in parse_rules(css):
                rules.append(P.blob("rule", json.dumps([media, f"{rename_selector(sel, cmap)}{{{fix_css_urls(body, base)}}}"])))
    ruleset = P.blob("ruleset", json.dumps(rules))

    # --- DOM ---------------------------------------------------------------------
    doc = lxml.html.fromstring(frozen["html"])
    for el in doc.xpath("//script|//style|//link|//noscript|//iframe"):
        el.getparent().remove(el)
    imgs = {str(i["id"]): i for i in frozen["imgs"]}
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if el.get("class"):
            el.set("class", " ".join(sorted(cmap.get(c, c) for c in el.get("class").split())))
        if el.tag == "img" and el.get("data-hs-id") in imgs:
            info = imgs[el.get("data-hs-id")]
            src = info["src"]
            if src.startswith("data:"):
                pass
            elif is_product_image(src):
                el.set("src", src)
            else:
                el.set("src", asset_for(src, info["w"]) or TRANSPARENT_GIF)
        for a in ("data-hs-id",):
            el.attrib.pop(a, None)
        href = el.get("href")
        if href and href.startswith("/") and not href.startswith("//"):
            el.set("href", urljoin(base, href))

    frag = lambda e: lxml.html.tostring(e, encoding="unicode", with_tail=False)
    elems = lambda e: [k for k in e if isinstance(k.tag, str)]

    def placeholder(el, markup):
        ph = lxml.html.fromstring(markup)
        ph.tail = el.tail
        el.getparent().replace(el, ph)

    # Product cards: stable card + price per SKU + badges per snapshot
    cards = []
    for i, card in enumerate(doc.xpath('//*[@data-test-id="product-card"]')):
        href = (card.xpath('.//a[@data-test-id="product-link"]/@href') or [""])[0]
        m = re.search(r"variantId=([^&\"]+)", href)
        sku = m.group(1) if m else "unknown"
        for part in card.xpath(".//*[@data-test-id]"):
            if part.get("data-test-id") in CARD_PARTS:
                placeholder(part, f'<hs-module ref="{P.blob("part", frag(part))}"></hs-module>')
        price = None
        pe = card.xpath('.//*[@data-test-id="price-current" or @data-test-id="price-red-color"]')
        if pe:
            block = pe[0].getparent()
            price = P.blob("price", frag(block))
            placeholder(block, '<hs-slot n="price"></hs-slot>')
        badges = []
        kids = elems(card)
        if kids and elems(kids[-1]):
            row = elems(kids[-1])[0]
            for b in elems(row):
                text = " ".join(b.text_content().split())
                if text:
                    badges.append({"text": text, "ref": P.blob("badge", frag(b))})
                row.remove(b)
            row.append(lxml.html.fromstring('<hs-slot n="badges"></hs-slot>'))
        card_ref = P.blob("card", frag(card))
        placeholder(card, f'<hs-card i="{i}"></hs-card>')
        cards.append({"pos": i + 1, "sku": sku, "card": card_ref, "price": price, "badges": badges})

    # Blocks in <main>, plus header, footer and other chrome
    modules = []
    cont = doc.get_element_by_id("container", None)
    if cont is None:
        raise RuntimeError("Page has no #container element; layout changed?")

    def cut(el, label, entry_id=None):
        h = P.blob("module", frag(el))
        placeholder(el, f'<hs-module ref="{h}"></hs-module>')
        modules.append({"position": len(modules), "label": label, "entry_id": entry_id, "blob_hash": h})

    for c in list(cont):
        if not isinstance(c.tag, str):
            continue
        if c.tag == "main":
            for b in list(c):
                if not isinstance(b.tag, str):
                    continue
                ids = [e.get("id") for e in b.iter() if isinstance(e.tag, str) and e.get("id") and ENTRY_ID_RE.match(e.get("id"))]
                cut(b, "block:" + (ids[0] if ids else "anon"), ids[0] if ids else None)
        else:
            cut(c, {"header": "header", "footer": "footer"}.get(c.tag, "chrome"))

    shell = P.blob("shell", "<!DOCTYPE html>\n" + lxml.html.tostring(doc, encoding="unicode"))
    manifest = {"version": 1, "base": base, "shell": shell, "static_css": static, "ruleset": ruleset,
                "modules": [{"label": m["label"], "ref": m["blob_hash"]} for m in modules], "cards": cards}
    return manifest, modules, P


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), 9)


def diff_modules(prev: list[dict] | None, cur: list[dict]) -> list[dict]:
    """Rule-based change list between two snapshots' block lists."""
    if prev is None:
        return [{"kind": "added", "entry_id": None, "position": None, "detail": {"note": "first snapshot"}}]
    def keyed(mods):  # labels can repeat ("chrome", "block:anon"): pair them by order
        seen, out = {}, {}
        for m in mods:
            n = seen[m["label"]] = seen.get(m["label"], 0) + 1
            out[f'{m["label"]}#{n}'] = m
        return out

    changes = []
    pk, ck = keyed(prev), keyed(cur)
    for k, m in ck.items():
        p = pk.get(k)
        if p is None:
            changes.append({"kind": "added", "entry_id": m["entry_id"], "position": m["position"], "detail": {"label": m["label"]}})
        elif p["blob_hash"] != m["blob_hash"]:
            changes.append({"kind": "other", "entry_id": m["entry_id"], "position": m["position"],
                            "detail": {"label": m["label"], "before": p["blob_hash"], "after": m["blob_hash"]}})
    for k, m in pk.items():
        if k not in ck:
            changes.append({"kind": "removed", "entry_id": m["entry_id"], "position": m["position"], "detail": {"label": m["label"]}})
    order_prev = [k for k in pk if k in ck]
    order_cur = [k for k in ck if k in pk]
    if order_prev != order_cur:
        changes.append({"kind": "moved", "entry_id": None, "position": None, "detail": {"before": order_prev, "after": order_cur}})
    return changes
