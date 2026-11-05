"""Rebuild a stored snapshot into one HTML page. Mirrors what the viewer app will do."""
import base64
from html import unescape as html_unescape
import json
import re

PH = re.compile(r'<hs-module ref="([0-9a-f]{16})"></hs-module>')
CARD = re.compile(r'<hs-card (i|k)="([^"]+)"></hs-card>')
ASSET = re.compile(r"hs-asset:([0-9a-f]{16}):([a-z/+0-9.-]+)")


def rebuild(manifest: dict, get_blobs, asset_src) -> str:
    """get_blobs(list[hash]) -> {hash: text}; asset_src(hash, mime) -> URL or data URI."""
    want = {manifest["shell"], manifest["ruleset"], *manifest["static_css"],
            *[m["ref"] for m in manifest["modules"]]}
    for c in manifest.get("cards", []):
        want |= {c["card"], *(b["ref"] for b in c["badges"])} | ({c["price"]} if c.get("price") else set())
    texts = get_blobs(sorted(want))
    rule_keys = json.loads(texts[manifest["ruleset"]])
    if isinstance(rule_keys, dict):  # v2: list of chunks, each a list of rules
        chunks = rule_keys["chunks"]
        texts.update(get_blobs(sorted(set(chunks) - set(texts))))
        rule_keys = [r for c in chunks for r in json.loads(texts[c])]
    texts.update(get_blobs(sorted(set(rule_keys) - set(texts))))

    html = texts[manifest["shell"]]
    cards = manifest.get("cards", [])
    by_key = {c["key"]: c for c in cards if c.get("key")}

    def card_html(m):
        c = cards[int(m.group(2))] if m.group(1) == "i" else by_key.get(html_unescape(m.group(2)))
        if c is None:
            return ""
        h = texts[c["card"]]
        h = h.replace('<hs-slot n="price"></hs-slot>', texts.get(c.get("price") or "", ""))
        return h.replace('<hs-slot n="badges"></hs-slot>', "".join(texts[b["ref"]] for b in c["badges"]))

    for _ in range(6):  # blocks -> cards -> parts
        nxt = PH.sub(lambda m: texts.get(m.group(1)) or _fetch(m.group(1), texts, get_blobs), html)
        nxt = CARD.sub(card_html, nxt)
        if nxt == html:
            break
        html = nxt

    css = "".join(f"<style>{texts[k]}</style>" for k in manifest["static_css"])
    rules = [json.loads(texts[k]) for k in rule_keys]
    css += "<style>" + "".join(f"{m}{{{r}}}" if m else r for m, r in rules) + "</style>"
    html = re.sub(r"<head\b[^>]*>", lambda m: m.group(0) + '<meta charset="utf-8">' + css, html, count=1)
    return ASSET.sub(lambda m: asset_src(m.group(1), m.group(2)) or "", html)


def _fetch(h, texts, get_blobs):
    texts.update(get_blobs([h]))
    return texts.get(h, "")


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()
