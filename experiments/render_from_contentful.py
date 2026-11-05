"""Experiment: draw the startpage from Contentful data alone (no browser, no captured HTML).

Input: out/startpage_sv.json + out/images/ from contentful_dump.py.
Everything shown comes from Contentful except a few general layout rules (marked GUESS),
which stand in for the website's own code.
"""
import base64
import html
import json
import re
import sys
from pathlib import Path

SRC = Path(sys.argv[1])
FONTS = Path(sys.argv[2]) if len(sys.argv) > 2 else None
d = json.loads((SRC / "startpage_sv.json").read_text())
E, A = d["entries"], d["assets"]
START = "6q7zaSZcLXtokLbBepLSgo"
ct = lambda e: e["sys"]["contentType"]["sys"]["id"]
link_id = lambda v: v["sys"]["id"] if isinstance(v, dict) and v.get("sys", {}).get("type") == "Link" else None

# --- GUESS: general layout rules the website's code would apply ------------------------
WIDTH = {"Full": "100%", "Normal": "1376px", "Narrow": "860px"}
GAP = {"Default": 16, "None": 0, "Small": 8, "Large": 32}
MARGIN_BOTTOM = 48
FRACTIONS = {"Half": 1 / 2, "Third": 1 / 3, "Quarter": 1 / 4, "TwoThirds": 2 / 3, "ThreeQuarters": 3 / 4}
stats = {"blocks": 0, "images": 0, "placeholders": [], "unknown": set(), "borrowed": []}
BORROW = SRC / "borrowed"  # optional: header.png, footer.png, <entryId>.png cut from a real capture


def borrowed(name):
    p = BORROW / f"{name}.png"
    if p.exists():
        stats["borrowed"].append(name)
        return f'<img class="borrowed" src="data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}">'
    return None


RENDERED = set()


def img_src(aid):
    for ext, mime in (("webp", "image/webp"), ("svg", "image/svg+xml")):
        p = SRC / "images" / f"{aid}.{ext}"
        if p.exists():
            return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    return None


def asset_ratio(aid):
    img = (((A.get(aid) or {}).get("fields", {}).get("file") or {}).get("details") or {}).get("image") or {}
    return img.get("width"), img.get("height")


def columns(layout, n):
    parts = re.findall(r"TwoThirds|ThreeQuarters|Half|Third|Quarter", layout or "")
    if not parts:  # "Auto" and unknown layouts: equal columns
        return [1 / max(1, n)] * n
    return [FRACTIONS[p] for p in parts]


def rich_doc(node):
    """Contentful rich text document -> HTML."""
    if isinstance(node, dict) and node.get("nodeType") == "text":
        t = html.escape(node.get("value", "")).replace("\n", "<br>")
        for m in node.get("marks", []):
            t = {"bold": f"<b>{t}</b>", "italic": f"<i>{t}</i>", "underline": f"<u>{t}</u>"}.get(m["type"], t)
        return t
    inner = "".join(rich_doc(c) for c in (node or {}).get("content", []))
    tag = {"paragraph": "p", "heading-1": "h1", "heading-2": "h2", "heading-3": "h3", "heading-4": "h4",
           "unordered-list": "ul", "ordered-list": "ol", "list-item": "li"}.get(node.get("nodeType"))
    if node.get("nodeType") == "hyperlink":
        return f'<a href="{html.escape(node["data"].get("uri", ""))}">{inner}</a>'
    return f"<{tag}>{inner}</{tag}>" if tag else inner


def button(eid):
    e = E.get(eid)
    if not e:
        return ""
    f = e["fields"]
    style = (f"background:{f.get('backgroundColor', '#000')};color:{f.get('textColor', '#fff')};"
             f"padding:{'8px 20px' if f.get('thin') else '12px 28px'};display:inline-block;")  # GUESS: padding
    text = re.sub(r"^<p>|</p>$", "", (f.get("text") or f.get("name") or "").strip())
    return f'<span class="btn" style="{style}">{text}</span>'


def image_block(e, default_color):
    f = e["fields"]
    aid = link_id(f.get("largeViewportImage"))
    src = img_src(aid) if aid else None
    w, h = asset_ratio(aid) if aid else (None, None)
    stats["images"] += bool(src)
    content = f.get("content") or ""
    if "{buttons}" in content:
        content = content.replace("{buttons}", "".join(button(link_id(b)) for b in f.get("buttons") or []))
    elif f.get("buttons"):
        content += "<p>" + "".join(button(link_id(b)) for b in f["buttons"]) + "</p>"
    ha = {"Left": "flex-start", "Center": "center", "Right": "flex-end"}.get(f.get("contentHorizontalAlignment"), "center")
    va = {"Top": "flex-start", "Center": "center", "Bottom": "flex-end"}.get(f.get("contentVerticalAlignment"), "center")
    grad = f.get("gradientOpacity") or 0
    img = (f'<img src="{src}" style="width:100%;display:block;aspect-ratio:{w}/{h}">' if src
           else '<div class="missing">image not in Contentful data</div>')
    overlay = (f'<div class="overlay" style="justify-content:{va};align-items:{ha};text-align:{ha.replace("flex-", "")};'
               f'color:{default_color};background:rgba(0,0,0,{grad})">{content}</div>' if content.strip() else "")
    bottom = f'<div class="bottom">{f["bottomContentTinyMce"]}</div>' if f.get("bottomContentTinyMce") else ""
    return f'<div class="imgblock"><div class="imgwrap">{img}{overlay}</div>{bottom}</div>'


def block(eid):
    e = E.get(eid)
    if not e:
        return ""  # unpublished: the site skips it too
    stats["blocks"] += 1
    RENDERED.add(eid)
    t, f = ct(e), e["fields"]
    mb = f'margin-bottom:{MARGIN_BOTTOM}px;' if f.get("marginBottom") else ""
    width = WIDTH.get(f.get("contentWidth"), "100%")
    if t == "spacerBlock":
        return f'<div style="height:{f.get("desktopHeight", 0)}px"></div>'
    if t == "layoutBlock":
        kids = [k for k in (link_id(b) for b in f.get("blocks") or []) if k in E]
        # A layout with fixed columns shows only that many blocks: the first published ones win
        # (how the team uses slots: line up banners, publish the ones to show).
        slots = len(re.findall(r"TwoThirds|ThreeQuarters|Half|Third|Quarter", f.get("layout") or ""))
        if slots and len(kids) > slots:
            kids = kids[:slots]
        cols = columns(f.get("layout"), len(kids))
        gap = GAP.get(f.get("gap"), 16)
        if len(kids) > len(cols):  # more blocks than the layout's columns: they wrap
            cols = (cols * (len(kids) // len(cols) + 1))[:len(kids)]
        cells = "".join(f'<div style="flex:0 0 calc({c * 100:.4f}% - {gap * (1 - c):.2f}px);min-width:0">{block(k)}</div>'
                        for k, c in zip(kids, cols))
        return (f'<div class="layout" style="max-width:{width};{mb}"><div style="display:flex;flex-wrap:wrap;gap:{gap}px">'
                f'{cells}</div></div>')
    if t == "nnImageBlock":
        return f'<div style="{mb}">{image_block(e, "#fff")}</div>'  # GUESS: text on images is white
    if t == "nnHeroBlock":
        return f'<div class="layout" style="max-width:{width};{mb}">{image_block(e, "#000")}</div>'
    if t == "editorialBlock":
        body = rich_doc(f["content"]) if isinstance(f.get("content"), dict) else ""
        return f'<div class="layout editorial" style="max-width:{width};{mb}">{body}</div>' if body else ""
    if t in ("productListBlock", "cevoidBlock") and borrowed(e["sys"]["id"]):
        return f'<div style="{mb}">{borrowed(e["sys"]["id"])}</div>'
    if t == "productListBlock":
        skus = re.findall(r'"([\w-]+)"', f.get("voyadoElevateProductRules") or "")[: f.get("numberOfProducts") or 8]
        stats["placeholders"].append(f"Product carousel “{f.get('heading')}”: {len(skus)} products named by code only")
        cards = "".join(f'<div class="card"><div class="cardimg"></div><span>{s}</span></div>' for s in skus[:5])
        return (f'<div class="layout" style="max-width:1376px;{mb}"><h3 class="plh">{html.escape(f.get("heading") or "")}</h3>'
                f'<div class="cards">{cards}</div><p class="note">Products, photos and prices come from the shop system, '
                f'not Contentful. Contentful only lists {len(skus)} product codes.</p></div>')
    if t == "cevoidBlock":
        stats["placeholders"].append("Instagram gallery (Cevoid, external service)")
        return (f'<div class="layout" style="max-width:1376px;{mb}"><h3 class="plh">{html.escape(f.get("title") or "")}</h3>'
                f'<div class="note box">Instagram gallery from Cevoid — not in Contentful</div></div>')
    stats["unknown"].add(t)
    return f'<div class="note box">Unknown block type {t}</div>'


body = "".join(block(link_id(l)) for l in E[START]["fields"]["content"])
font_css = ""
if FONTS:
    for name, file in (("haptik", "haptik.woff2"), ("InterVariable", "inter.woff2")):
        p = FONTS / file
        if p.exists():
            font_css += f"@font-face{{font-family:{name};src:url(data:font/woff2;base64,{base64.b64encode(p.read_bytes()).decode()})}}"
page = f"""<!doctype html><html><head><meta charset="utf-8"><style>{font_css}
body{{margin:0;font-family:haptik,InterVariable,sans-serif;color:#000;background:#fff;width:1440px}}
.layout{{margin-left:auto;margin-right:auto}} .imgwrap{{position:relative}}
.overlay{{position:absolute;inset:0;display:flex;flex-direction:column;padding:48px;box-sizing:border-box}}
.overlay p{{margin:0 0 .3em}} .bottom{{padding:8px 0}} .bottom p{{margin:0}} .btn{{margin-top:12px}}
.missing,.box{{background:#f3f3f3;color:#888;padding:40px;text-align:center;font:14px sans-serif}}
.plh{{text-align:center;font-weight:400;font-size:18px}} .cards{{display:flex;gap:16px}}
.card{{flex:1;font:12px monospace;color:#999}} .cardimg{{aspect-ratio:3/4;background:#f3f3f3;margin-bottom:6px}}
.note{{font:13px sans-serif;color:#999;text-align:center}} .editorial{{columns:1;font-size:14px;padding:0 32px}}
img.borrowed{{display:block;width:1440px}} .sitebar{{background:#f3f3f3;color:#888;font:13px sans-serif;text-align:center;padding:14px}}
</style></head><body>
{borrowed("header") or '<div class="sitebar">Header, menu and top bar — separate site-wide content, not part of the startpage entry</div>'}
{body}
{borrowed("footer") or '<div class="sitebar">Footer — separate site-wide content</div>'}</body></html>"""
(SRC / "from_contentful.html").write_text(page, encoding="utf-8")
stats["rendered_ids"] = sorted(RENDERED)
print(json.dumps({**stats, "unknown": sorted(stats["unknown"]), "rendered_ids": len(RENDERED)}, ensure_ascii=False))
(SRC / "render_stats.json").write_text(json.dumps({**stats, "unknown": sorted(stats["unknown"])}, ensure_ascii=False))
