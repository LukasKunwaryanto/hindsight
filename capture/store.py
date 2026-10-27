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

ASSET_BUCKET = "assets"
EXT = {"image/avif": "avif", "image/webp": "webp", "image/png": "png", "image/svg+xml": "svg",
       "image/gif": "gif", "image/jpeg": "jpg", "font/woff2": "woff2", "font/woff": "woff"}


class SupabaseStore:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key}
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
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    txt = r.read()
                    return json.loads(txt) if txt and r.headers.get("content-type", "").startswith("application/json") else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")[:500]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"Supabase {method} {path} -> HTTP {e.code}: {msg}") from None

    def _existing(self, table: str, hashes: list[str]) -> set[str]:
        found = set()
        for i in range(0, len(hashes), 150):
            chunk = hashes[i:i + 150]
            rows = self._req("GET", f"/rest/v1/{table}", params={"select": "hash", "hash": f"in.({','.join(chunk)})"})
            found |= {r["hash"] for r in rows or []}
        return found

    def _insert(self, table: str, rows: list[dict], on_conflict: str | None = None, ret=False):
        out = []
        prefer = ("return=representation" if ret else "return=minimal") + (",resolution=ignore-duplicates" if on_conflict else "")
        params = {"on_conflict": on_conflict} if on_conflict else None
        for i in range(0, len(rows), 200):
            res = self._req("POST", f"/rest/v1/{table}", rows[i:i + 200], {"Prefer": prefer}, params=params)
            out += res or []
        return out

    # --- pieces ----------------------------------------------------------------
    def save_blobs(self, blobs: dict[str, tuple[str, str]]) -> dict:
        new = [h for h in blobs if h not in self._existing("blobs", list(blobs))]
        rows, stats = [], {}
        for h in new:
            kind, text = blobs[h]
            z = gzip.compress(text.encode("utf-8"), 9)
            rows.append({"hash": h, "kind": kind, "content": "\\x" + z.hex(), "size_bytes": len(z)})
            stats[kind] = stats.get(kind, 0) + len(z)
        self._insert("blobs", rows, on_conflict="hash")
        return stats

    def save_assets(self, assets: dict[str, dict]) -> int:
        new = [h for h in assets if h not in self._existing("assets", list(assets))]
        total = 0
        for h in new:
            a = assets[h]
            path = f"{h[:2]}/{h}.{EXT.get(a['mime'], 'bin')}"
            self._req("POST", f"/storage/v1/object/{ASSET_BUCKET}/{path}", a["bytes"],
                      {"Content-Type": a["mime"], "x-upsert": "true", "cache-control": "max-age=31536000"}, raw=True)
            self._insert("assets", [{"hash": h, "mime": a["mime"], "storage_path": path, "width": a["w"],
                                     "height": a["h"], "size_bytes": len(a["bytes"]), "source_url": a["url"][:2000]}],
                         on_conflict="hash")
            total += len(a["bytes"])
        return total

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
    def latest_snapshot(self, market: str, device: str):
        rows = self._req("GET", "/rest/v1/snapshots", params={
            "select": "id", "market": f"eq.{market}", "device": f"eq.{device}",
            "order": "captured_at.desc", "limit": "1"})
        if not rows:
            return None, None
        sid = rows[0]["id"]
        mods = self._req("GET", "/rest/v1/snapshot_modules", params={
            "select": "position,label,entry_id,blob_hash", "snapshot_id": f"eq.{sid}", "order": "position"})
        return sid, mods

    def save_snapshot(self, row: dict, modules: list[dict], changes: list[dict]) -> str:
        sid = self._insert("snapshots", [row], ret=True)[0]["id"]
        self._insert("snapshot_modules", [{**m, "snapshot_id": sid} for m in modules])
        if changes:
            self._insert("snapshot_changes", [{**c, "snapshot_id": sid, "previous_snapshot_id": row.get("previous_snapshot_id")} for c in changes])
        return sid

    # --- capture requests ------------------------------------------------------
    def open_request(self, market: str, device: str, reason: str) -> int:
        rows = self._req("GET", "/rest/v1/capture_requests", params={
            "select": "id", "market": f"eq.{market}", "device": f"eq.{device}", "status": "in.(waiting,started)"})
        if rows:
            rid = rows[0]["id"]
        else:
            rid = self._insert("capture_requests", [{"market": market, "device": device, "reason": reason}], ret=True)[0]["id"]
        self.update_request(rid, status="started", started_at="now()")
        return rid

    def update_request(self, rid: int, **fields):
        if fields.get("started_at") == "now()" or fields.get("finished_at") == "now()":
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            fields = {k: (now if v == "now()" else v) for k, v in fields.items()}
        self._req("PATCH", "/rest/v1/capture_requests", fields, {"Prefer": "return=minimal"}, params={"id": f"eq.{rid}"})

    def markets(self) -> list[dict]:
        return self._req("GET", "/rest/v1/markets", params={"select": "locale,url", "enabled": "eq.true", "order": "sort_order"})

    # --- reading back (for rebuild checks) -------------------------------------
    def get_blobs(self, hashes: list[str]) -> dict[str, str]:
        out = {}
        for i in range(0, len(hashes), 100):
            rows = self._req("GET", "/rest/v1/blobs", params={"select": "hash,content", "hash": f"in.({','.join(hashes[i:i+100])})"})
            for r in rows or []:
                out[r["hash"]] = gzip.decompress(bytes.fromhex(r["content"][2:])).decode("utf-8")
        return out

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

    def latest_snapshot(self, market, device):
        for s in reversed(self.d["snapshots"]):
            if s["row"]["market"] == market and s["row"]["device"] == device:
                return s["id"], s["modules"]
        return None, None

    def save_snapshot(self, row, modules, changes):
        sid = f"mem-{len(self.d['snapshots']) + 1}"
        self.d["snapshots"].append({"id": sid, "row": row, "modules": modules, "changes": changes})
        self.flush()
        return sid

    def open_request(self, market, device, reason):
        self.d["requests"] += 1
        return self.d["requests"]

    def update_request(self, rid, **fields):
        pass

    def get_blobs(self, hashes):
        return {h: gzip.decompress(self.d["blobs"][h][1]).decode() for h in hashes if h in self.d["blobs"]}

    def asset_bytes(self, h):
        a = self.d["assets"].get(h)
        return (a["bytes"], a["mime"]) if a else (None, None)
