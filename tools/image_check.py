"""Daily: are the product images that stored pages link to still on the site's CDN? Checks a batch of the
least recently checked product_images (HEAD requests) and records when one disappears. Missing images can
then be fetched from another source or shown as a placeholder.
Env: SUPABASE_URL, SUPABASE_SECRET_KEY, HINDSIGHT_IMAGE_BATCH (default 1000)."""
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
from store import SupabaseStore  # noqa: E402

db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
N = int(os.environ.get("HINDSIGHT_IMAGE_BATCH") or 1000)
rows = db._req("GET", "/rest/v1/product_images", params={
    "select": "sku,src,missing_since", "src": "neq.", "asset": "is.null",
    "order": "checked_at.asc.nullsfirst", "limit": str(N)}) or []
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def status(r):
    req = urllib.request.Request(r["src"], method="HEAD", headers={"User-Agent": "hindsight-image-check"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return r, resp.status
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return r, e.code
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return r, None  # unknown: try again another day


with ThreadPoolExecutor(8) as ex:
    results = list(ex.map(status, rows))
gone = [r for r, c in results if c in (404, 410)]
ok = [r for r, c in results if c and c < 400]
for batch, fields in ((ok, {"checked_at": now, "missing_since": None}), (gone, {"checked_at": now})):
    for i in range(0, len(batch), 100):
        skus = ",".join(f'"{r["sku"]}"' for r in batch[i:i + 100])
        db._req("PATCH", "/rest/v1/product_images", fields, {"Prefer": "return=minimal"}, params={"sku": f"in.({skus})"})
for r in gone:
    if not r.get("missing_since"):
        db._req("PATCH", "/rest/v1/product_images", {"missing_since": now}, {"Prefer": "return=minimal"},
                params={"sku": f"eq.{r['sku']}"})
md = f"## Product images\n\nChecked {len(results)}: {len(ok)} on the CDN, {len(gone)} gone, {len(results) - len(ok) - len(gone)} unknown."
print(md)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
