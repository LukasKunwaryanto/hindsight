"""How often Contentful pages are published, per space and page type (for storage estimates).
Uses the management API (CONTENTFUL_MANAGEMENT_TOKEN): sys.publishedCounter and firstPublishedAt give a
lifetime publish rate; sys.publishedAt shows how many pages were touched recently."""
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

TOKEN = os.environ["CONTENTFUL_MANAGEMENT_TOKEN"].strip()
SPACES = {"nordicnest": "p7vzp7ftmsr1", "svenssons": "5dqc39pi2ix6", "kitchentime": "ft75u7lxp5jx"}
TYPES = ["start", "productListingPage", "collection", "brand", "designer", "editorial", "room", "rootCategory",
         "inspiration", "inspirationArticle", "inspirationCategory", "campaign", "campaignPage", "landingPage", "page"]
NOW = datetime.now(timezone.utc)


def get(space, path, **q):
    url = f"https://api.contentful.com/spaces/{space}/environments/master{path}?{urllib.parse.urlencode(q)}"
    for a in range(5):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 + a * 2)
                continue
            raise
    raise RuntimeError(url)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


out = {}
for site, space in SPACES.items():
    try:
        types = {c["sys"]["id"] for c in get(space, "/content_types", limit=1000)["items"]}
    except Exception as e:
        out[site] = {"error": str(e)}
        continue
    res = {}
    for t in [t for t in TYPES if t in types]:
        st = defaultdict(float)
        skip = 0
        while True:
            page = get(space, "/entries", content_type=t, limit=500, skip=skip,
                       select="sys.id,sys.publishedCounter,sys.firstPublishedAt,sys.publishedAt",
                       **{"sys.publishedAt[exists]": "true"})
            for e in page["items"]:
                s = e["sys"]
                age = max((NOW - ts(s["firstPublishedAt"])).days, 1) if s.get("firstPublishedAt") else 365
                last = (NOW - ts(s["publishedAt"])).days
                st["pages"] += 1
                st["publishes"] += s.get("publishedCounter", 1)
                st["page_days"] += age
                st["last30"] += last <= 30
                st["last90"] += last <= 90
                st["last365"] += last <= 365
                # publishes per page per year, for pages at least 90 days old (young pages skew high)
                if age >= 90:
                    st["mature_pages"] += 1
                    st["mature_rate_sum"] += s.get("publishedCounter", 1) / age * 365
            skip += 500
            if skip >= page["total"]:
                break
        st = dict(st)
        st["publishes_per_page_year"] = round(st["publishes"] / st["page_days"] * 365, 2) if st.get("page_days") else None
        st["mature_rate_mean"] = round(st["mature_rate_sum"] / st["mature_pages"], 2) if st.get("mature_pages") else None
        res[t] = st
    out[site] = res
print("RATES_JSON_START")
print(json.dumps(out))
print("RATES_JSON_END")
