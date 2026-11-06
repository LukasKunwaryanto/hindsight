"""Count every page per site and market from the sites' sitemaps (robots.txt -> sitemap index -> files).
Prints JSON: {domain: {sitemap_file: url_count}} plus sample URLs per file."""
import gzip
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36 hindsight-inventory"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def locs(xml):
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def domain(d):
    out = {"sitemaps": {}, "samples": {}, "errors": []}
    try:
        robots = fetch(f"https://{d}/robots.txt")
        maps = re.findall(r"(?im)^sitemap:\s*(\S+)", robots) or [f"https://{d}/sitemap.xml"]
    except Exception as e:
        out["errors"].append(f"robots: {e}")
        maps = [f"https://{d}/sitemap.xml"]
    seen = set()
    while maps:
        m = maps.pop()
        if m in seen:
            continue
        seen.add(m)
        try:
            xml = fetch(m)
        except Exception as e:
            out["errors"].append(f"{m}: {e}")
            continue
        if "<sitemapindex" in xml:
            maps += locs(xml)
        else:
            u = locs(xml)
            out["sitemaps"][m] = len(u)
            out["samples"][m] = u[:3]
    return d, out


domains = sys.argv[1:]
with ThreadPoolExecutor(8) as ex:
    res = dict(ex.map(domain, domains))
print("INVENTORY_JSON_START")
print(json.dumps(res))
print("INVENTORY_JSON_END")
