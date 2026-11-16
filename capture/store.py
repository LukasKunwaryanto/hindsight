"""Where pieces go. SupabaseStore talks to the real project; MemoryStore is for local tests.

Both expose the same methods, so the capture code doesn't care which one it uses.
"""
import gzip
import json
import os
import pickle
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ASSET_BUCKET = "assets"
EXT = {"image/avif": "avif", "image/webp": "webp", "image/png": "png", "image/svg+xml": "svg",
       "image/gif": "gif", "image/jpeg": "jpg", "font/woff2": "woff2", "font/woff": "woff"}


def modules_from_manifest(manifest: dict) -> list[dict]:
    """Every block of a stored page, in order, as module rows (without titles)."""
    out = []
    for i, m in enumerate(manifest.get("modules", [])):
        eid = m["label"][6:] if m["label"].startswith("block:") and m["label"] != "block:anon" else None
        out.append({"position": i, "label": m["label"], "entry_id": eid, "blob_hash": m["ref"], "look": m.get("look")})
    return out


class SupabaseStore:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key}
        self.calls, self.wait = 0, 0.0
        if key.startswith("eyJ"):  # legacy JWT keys also go in Authorization
            self.headers["Authorization"] = f"Bearer {key}"

    # --- low level ---------------------------------------------------------------
    def _req(self, method, path, body=None, headers=None, raw=False, params=None):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe="(),.*:")
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        h = dict(self.headers)
        if not raw and body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    txt = r.read()
                    self.calls += 1
                    self.wait += time.monotonic() - t0
                    if time.monotonic() - t0 > 3:
                        print(f"  slow: {method} {path} {time.monotonic() - t0:.1f}s", flush=True)
                    return json.loads(txt) if txt and r.headers.get("content-type", "").startswith("application/json") else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")[:500]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"Supabase {method} {path} -> HTTP {e.code}: {msg}") from None
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                # a dropped connection (e.g. SSL EOF): repeat requests that are safe to send twice —
                # reads, updates, and inserts that ignore or merge duplicates
                safe = method in ("GET", "HEAD", "PATCH", "DELETE") or "resolution=" in h.get("Prefer", "")
                if safe and attempt < 3:
                    print(f"  retry after network error: {method} {path}: {e}", flush=True)
                    time.sleep(3 * (attempt + 1))
                    continue
                raise

    def _existing(self, table: str, hashes: list[str]) -> set[str]:
        found = set()
        for i in range(0, len(hashes), 150):
            chunk = hashes[i:i + 150]
            rows = self._req("GET", f"/rest/v1/{table}", params={"select": "hash", "hash": f"in.({','.join(chunk)})"})
            found |= {r["hash"] for r in rows or []}
        return found

    def _insert(self, table: str, rows: list[dict], on_conflict: str | None = None, ret=False, merge=False):
        out = []
        resolution = ",resolution=merge-duplicates" if merge else ",resolution=ignore-duplicates"
        prefer = ("return=representation" if ret else "return=minimal") + (resolution if on_conflict else "")
        params = {"on_conflict": on_conflict} if on_conflict else None
        for i in range(0, len(rows), 200):
            res = self._req("POST", f"/rest/v1/{table}", rows[i:i + 200], {"Prefer": prefer}, params=params)
            out += res or []
        return out

    # --- pieces ----------------------------------------------------------------
    def save_blobs(self, blobs: dict[str, tuple[str, str]]) -> dict:
        existing = self._existing("blobs", list(blobs))
        new = [h for h in blobs if h not in existing]
        rows, stats = [], {}
        for h in new:
            kind, text = blobs[h]
            z = gzip.compress(text.encode("utf-8"), 9)
            rows.append({"hash": h, "kind": kind, "content": "\\x" + z.hex(), "size_bytes": len(z)})
            stats[kind] = stats.get(kind, 0) + len(z)
        self._insert("blobs", rows, on_conflict="hash")
        return stats

    def save_assets(self, assets: dict[str, dict]) -> int:
        existing = self._existing("assets", list(assets))
        new = [h for h in assets if h not in existing]
        path = lambda h: f"{h[:2]}/{h}.{EXT.get(assets[h]['mime'], 'bin')}"

        def upload(h):
            self._req("POST", f"/storage/v1/object/{ASSET_BUCKET}/{path(h)}", assets[h]["bytes"],
                      {"Content-Type": assets[h]["mime"], "x-upsert": "true", "cache-control": "max-age=31536000"}, raw=True)

        with ThreadPoolExecutor(8) as pool:  # files first, then the rows that point at them
            list(pool.map(upload, new))
        self._insert("assets", [{"hash": h, "mime": assets[h]["mime"], "storage_path": path(h), "width": assets[h]["w"],
                                 "height": assets[h]["h"], "size_bytes": len(assets[h]["bytes"]),
                                 "source_url": assets[h]["url"][:2000],
                                 "source_key": assets[h].get("source_key"),
                                 "source_path": (assets[h].get("source_key") or "").split("|")[0] or None}
                                for h in new], on_conflict="hash")
        # Files stored before source keys existed: fill them in, so the next capture finds them
        for h in existing:
            k = assets.get(h, {}).get("source_key")
            if k:
                self._req("PATCH", "/rest/v1/assets", {"source_key": k, "source_path": k.split("|")[0]},
                          {"Prefer": "return=minimal"}, params={"hash": f"eq.{h}", "source_key": "is.null"})
        return sum(len(assets[h]["bytes"]) for h in new)

    def card_prices(self, market: str, skus: list[str]) -> dict[str, str]:
        out = {}
        for i in range(0, len(skus), 100):
            chunk = [s.replace(",", "") for s in skus[i:i + 100]]
            rows = self._req("GET", "/rest/v1/card_prices", params={
                "select": "sku,blob_hash", "market": f"eq.{market}", "sku": f"in.({','.join(chunk)})"})
            out.update({r["sku"]: r["blob_hash"] for r in rows or []})
        return out

    def save_card_prices(self, market: str, prices: dict[str, str]):
        self._insert("card_prices", [{"market": market, "sku": s, "blob_hash": h} for s, h in prices.items()],
                     on_conflict="market,sku")

    # --- snapshots -------------------------------------------------------------
    def latest_snapshot(self, market: str, device: str, page: str = "/"):
        """(id, all blocks incl. header/chrome from its manifest, manifest) of the newest snapshot."""
        rows = self._req("GET", "/rest/v1/snapshots", params={
            "select": "id,manifest", "market": f"eq.{market}", "device": f"eq.{device}", "page": f"eq.{page}",
            "order": "captured_at.desc", "limit": "1"})
        if not rows:
            return None, None, None
        return rows[0]["id"], modules_from_manifest(rows[0]["manifest"]), rows[0]["manifest"]

    def known_assets(self, paths: list[str]) -> dict[str, str]:
        """source_key -> hs-asset ref for files stored before from these source paths."""
        out = {}
        paths = sorted({p for p in paths if p and '"' not in p})
        for i in range(0, len(paths), 50):
            chunk = ",".join(f'"{p}"' for p in paths[i:i + 50])
            rows = self._req("GET", "/rest/v1/assets", params={
                "select": "hash,mime,source_key", "source_path": f"in.({chunk})", "order": "created_at.asc,hash.asc"})
            for r in rows or []:  # several files can share a key: always reuse the oldest, so captures agree
                if r.get("source_key"):
                    out.setdefault(r["source_key"], f"hs-asset:{r['hash']}:{r['mime']}")
        return out

    def asset_urls(self, hashes: list[str]) -> dict[str, str]:
        out = {}
        for i in range(0, len(hashes), 100):
            rows = self._req("GET", "/rest/v1/assets", params={
                "select": "hash,source_url", "hash": f"in.({','.join(hashes[i:i + 100])})"})
            out.update({r["hash"]: r["source_url"] or "" for r in rows or []})
        return out

    def tree_names(self, ids: list[str]) -> dict[str, str]:
        """Contentful internal names (the promotion naming convention) for entry IDs."""
        out = {}
        for i in range(0, len(ids), 100):
            rows = self._req("GET", "/rest/v1/tree_entries", params={
                "select": "entry_id,name", "entry_id": f"in.({','.join(ids[i:i + 100])})"})
            out.update({r["entry_id"]: r["name"] for r in rows or [] if r.get("name")})
        return out

    def save_snapshot(self, row: dict, modules: list[dict], changes: list[dict]) -> str:
        if row.get("page", "/") != "/":
            # inner pages: changes go in the snapshot row, blocks are in the manifest (fewer rows per snapshot)
            return self._insert("snapshots", [{**row, "changes": changes}], ret=True)[0]["id"]
        sid = self._insert("snapshots", [row], ret=True)[0]["id"]
        # rows only for content blocks (promotions); the full list is in the manifest
        self._insert("snapshot_modules", [{**{k: v for k, v in m.items() if k != "look"}, "snapshot_id": sid}
                                          for m in modules if m.get("entry_id")])
        if changes:
            self._insert("snapshot_changes", [{**c, "snapshot_id": sid, "previous_snapshot_id": row.get("previous_snapshot_id")} for c in changes])
        return sid

    # --- site bars, product images, pages ----------------------------------------
    def latest_site_parts(self, market: str, device: str):
        rows = self._req("GET", "/rest/v1/site_parts", params={
            "select": "parts", "market": f"eq.{market}", "device": f"eq.{device}", "order": "captured_at.desc", "limit": "1"})
        return rows[0]["parts"] if rows else None

    def save_site_parts(self, market: str, device: str, parts: list[dict]):
        self._insert("site_parts", [{"market": market, "device": device, "parts": parts}])

    def save_product_images(self, images: dict[str, str]):
        """SKU -> image, once per product. A stored copy (hs-asset) replaces a plain link when it comes along."""
        rows = [{"sku": k, "src": v} for k, v in images.items() if k != "unknown" and not v.startswith("hs-asset:")]
        kept = [{"sku": k, "asset": v} for k, v in images.items() if k != "unknown" and v.startswith("hs-asset:")]
        if rows:
            self._insert("product_images", rows, on_conflict="sku")
        for r in kept:  # rarely many: only key pages store product images
            if not self._req("PATCH", "/rest/v1/product_images", {"asset": r["asset"]},
                             {"Prefer": "return=representation"}, params={"sku": f"eq.{r['sku']}", "asset": "is.null"}):
                self._insert("product_images", [{"sku": r["sku"], "src": "", "asset": r["asset"]}], on_conflict="sku")

    def product_images(self, skus: list[str]) -> dict[str, str]:
        out = {}
        skus = [s for s in skus if s and "," not in s]
        for i in range(0, len(skus), 100):
            rows = self._req("GET", "/rest/v1/product_images", params={
                "select": "sku,src,asset", "sku": f"in.({','.join(skus[i:i + 100])})"})
            out.update({r["sku"]: r["asset"] or r["src"] for r in rows or []})
        return out

    def pages(self) -> list[dict]:
        return self._req("GET", "/rest/v1/pages", params={"select": "market,path,kind", "enabled": "eq.true",
                                                           "order": "market,path"}) or []

    # --- capture requests ------------------------------------------------------
    def open_request(self, market: str, device: str, reason: str, page: str = "/"):
        """Use the request the poller started for this run, else claim a waiting one, else make one.
        Scheduled passes over many pages log in the run's summary instead (no row per page)."""
        if reason == "pass":
            return None
        for status in ("started", "waiting"):
            rows = self._req("GET", "/rest/v1/capture_requests", params={
                "select": "id", "market": f"eq.{market}", "device": f"eq.{device}", "page": f"eq.{page}",
                "status": f"eq.{status}", "order": "id.desc", "limit": "1"})
            if rows:
                rid = rows[0]["id"]
                break
        else:
            rid = self._insert("capture_requests", [{"market": market, "device": device, "reason": reason, "page": page}],
                               ret=True)[0]["id"]
        self.update_request(rid, status="started", started_at="now()")
        return rid

    def update_request(self, rid: int, **fields):
        if rid is None:
            return
        if fields.get("started_at") == "now()" or fields.get("finished_at") == "now()":
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            fields = {k: (now if v == "now()" else v) for k, v in fields.items()}
        self._req("PATCH", "/rest/v1/capture_requests", fields, {"Prefer": "return=minimal"}, params={"id": f"eq.{rid}"})

    def markets(self) -> list[dict]:
        try:
            return self._req("GET", "/rest/v1/markets", params={"select": "locale,url,lang,site", "enabled": "eq.true", "order": "sort_order"})
        except Exception:  # database without the site columns yet
            return self._req("GET", "/rest/v1/markets", params={"select": "locale,url", "enabled": "eq.true", "order": "sort_order"})

    # --- reading back (for rebuild checks) -------------------------------------
    def get_blobs(self, hashes: list[str]) -> dict[str, str]:
        out = {}
        for i in range(0, len(hashes), 100):
            rows = self._req("GET", "/rest/v1/blobs", params={"select": "hash,content", "hash": f"in.({','.join(hashes[i:i+100])})"})
            for r in rows or []:
                out[r["hash"]] = gzip.decompress(bytes.fromhex(r["content"][2:])).decode("utf-8")
        return out

    def public_asset_url(self, h: str, mime: str) -> str:
        """Stored files live at <bucket>/<first 2 of hash>/<hash>.<ext>: no lookup needed."""
        return f"{self.url}/storage/v1/object/public/{ASSET_BUCKET}/{h[:2]}/{h}.{EXT.get(mime, 'bin')}"

    def update_snapshot(self, sid: str, **fields):
        self._req("PATCH", "/rest/v1/snapshots", fields, {"Prefer": "return=minimal"}, params={"id": f"eq.{sid}"})

    def asset_url(self, h: str) -> str | None:
        rows = self._req("GET", "/rest/v1/assets", params={"select": "storage_path", "hash": f"eq.{h}"})
        return f"{self.url}/storage/v1/object/public/{ASSET_BUCKET}/{rows[0]['storage_path']}" if rows else None


class MemoryStore:
    """Same interface, kept in a local file. For testing without Supabase."""

    def __init__(self, path: str):
        self.path = path
        self.d = pickle.load(open(path, "rb")) if os.path.exists(path) else {
            "blobs": {}, "assets": {}, "prices": {}, "snapshots": [], "requests": 0}

    def flush(self):
        pickle.dump(self.d, open(self.path, "wb"))

    def save_blobs(self, blobs):
        stats = {}
        for h, (kind, text) in blobs.items():
            if h not in self.d["blobs"]:
                z = gzip.compress(text.encode(), 9)
                self.d["blobs"][h] = (kind, z)
                stats[kind] = stats.get(kind, 0) + len(z)
        return stats

    def save_assets(self, assets):
        total = 0
        for h, a in assets.items():
            if h not in self.d["assets"]:
                self.d["assets"][h] = a
                total += len(a["bytes"])
        return total

    def card_prices(self, market, skus):
        return {s: self.d["prices"][(market, s)] for s in skus if (market, s) in self.d["prices"]}

    def save_card_prices(self, market, prices):
        for s, h in prices.items():
            self.d["prices"].setdefault((market, s), h)

    def tree_names(self, ids):
        return {}

    def asset_urls(self, hashes):
        return {h: self.d["assets"][h]["url"] for h in hashes if h in self.d["assets"]}

    def latest_snapshot(self, market, device, page="/"):
        for s in reversed(self.d["snapshots"]):
            if s["row"]["market"] == market and s["row"]["device"] == device and s["row"].get("page", "/") == page:
                return s["id"], modules_from_manifest(s["row"]["manifest"]), s["row"]["manifest"]
        return None, None, None

    def known_assets(self, paths):
        want = set(paths)
        return {a["source_key"]: f"hs-asset:{h}:{a['mime']}" for h, a in self.d["assets"].items()
                if a.get("source_key") and a["source_key"].split("|")[0] in want}

    def save_snapshot(self, row, modules, changes):
        sid = f"mem-{len(self.d['snapshots']) + 1}"
        self.d["snapshots"].append({"id": sid, "row": row, "modules": modules, "changes": changes})
        self.flush()
        return sid

    def open_request(self, market, device, reason, page="/"):
        self.d["requests"] += 1
        return self.d["requests"]

    def latest_site_parts(self, market, device):
        v = [p for p in self.d.setdefault("site_parts", []) if p["market"] == market and p["device"] == device]
        return v[-1]["parts"] if v else None

    def save_site_parts(self, market, device, parts):
        self.d.setdefault("site_parts", []).append({"market": market, "device": device, "parts": parts})

    def save_product_images(self, images):
        pi = self.d.setdefault("product_images", {})
        for k, v in images.items():
            if v.startswith("hs-asset:") or k not in pi:
                pi[k] = v

    def product_images(self, skus):
        pi = self.d.get("product_images", {})
        return {k: pi[k] for k in skus if k in pi}

    def pages(self):
        return []

    def update_request(self, rid, **fields):
        pass

    def get_blobs(self, hashes):
        return {h: gzip.decompress(self.d["blobs"][h][1]).decode() for h in hashes if h in self.d["blobs"]}

    def public_asset_url(self, h, mime):
        a = self.d["assets"].get(h)
        import base64
        return f"data:{a['mime']};base64," + base64.b64encode(a["bytes"]).decode() if a else ""

    def update_snapshot(self, sid, **fields):
        for s in self.d["snapshots"]:
            if s["id"] == sid:
                s["row"].update(fields)

    def asset_bytes(self, h):
        a = self.d["assets"].get(h)
        return (a["bytes"], a["mime"]) if a else (None, None)
