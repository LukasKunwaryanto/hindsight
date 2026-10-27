"""CSS helpers: split stylesheets into rules and give generated class names stable names.

The site's CSS-in-JS assigns class names (m0, h6, ...) in render order, so the same block
gets different names on different pages. We rename each class after a hash of every rule
it takes part in, so identical styling gets identical names everywhere.
"""
import hashlib
import json
import re

CLASS_RE = re.compile(r"\.([A-Za-z][\w-]*)")


def parse_rules(css: str, media: str = ""):
    """Yield (media, selector, body). Top-level @media blocks are opened one level;
    any other at-rule (keyframes, supports, nested media) is kept whole."""
    i, n = 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        head = css[i:j].strip()
        if head.startswith("@"):
            depth, k = 1, j + 1
            while k < n and depth:
                depth += {"{": 1, "}": -1}.get(css[k], 0)
                k += 1
            if head.startswith("@media") and not media:
                yield from parse_rules(css[j + 1:k - 1], head)
            else:
                yield (media, head, css[j + 1:k - 1])
            i = k
        else:
            k = css.find("}", j)
            if k < 0:
                break
            yield (media, head, css[j + 1:k].strip())
            i = k + 1


def class_map(injected_sheets: list[dict]) -> dict[str, str]:
    """Map each generated class to a stable name derived from all rules it appears in."""
    defs: dict[str, list] = {}
    for sh in injected_sheets:
        css = sh["css"] if not sh.get("media") else f"@media {sh['media']}{{{sh['css']}}}"
        for media, sel, body in parse_rules(css):
            for s in sel.split(","):
                s = s.strip()
                for c in set(CLASS_RE.findall(s)):
                    shape = CLASS_RE.sub(lambda m: "&" if m.group(1) == c else ".*", s)
                    defs.setdefault(c, []).append([media, shape, re.sub(r"\s+", " ", body)])
    return {c: "n" + hashlib.sha1(json.dumps(sorted(v)).encode()).hexdigest()[:7] for c, v in defs.items()}


def rename_selector(sel: str, cmap: dict[str, str]) -> str:
    return CLASS_RE.sub(lambda m: "." + cmap.get(m.group(1), m.group(1)), sel)
