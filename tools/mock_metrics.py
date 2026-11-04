"""Fill the metric tables with sample numbers for the POC demo.

Stands in for the daily BigQuery sync until we have access. It reads the real captured
snapshots and the promotions (Contentful entry IDs) on them, and writes believable,
repeatable numbers into the same tables the BigQuery sync will fill. Rows are marked
source = 'mock', so the real sync can replace them later without touching the app.

Usage:
  python tools/mock_metrics.py                 # all markets, from each market's first capture to yesterday
  python tools/mock_metrics.py --days-back 14  # also make up history before the first capture (not used for the demo)
  python tools/mock_metrics.py --dry-run       # print a summary, write nothing

Only numbers our tracking can really provide are generated: promotion views and clicks,
quickbacks, sessions with a click and what they did next, revenue, scroll depth in pixels,
device, visitor type, traffic channel and A/B test version.
"""
import argparse
import hashlib
import math
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))

STHLM = ZoneInfo("Europe/Stockholm")
BUCKET_PX = 250
FOLD_PX = 820

# Startpage views per day on an ordinary weekday, before noise (all devices, consenting visitors).
BASE_VIEWS = {
    "sv-SE": 42000, "en-US": 16000, "de-DE": 12000, "nb-NO": 9000, "da-DK": 7500, "fi-FI": 5500,
    "nl-NL": 4500, "fr-FR": 3800, "es-ES": 2600, "it-IT": 2600, "pl-PL": 2200, "pt-PT": 1100,
    "el-GR": 500, "ja-JP": 3200, "ko-KR": 2100, "zh-CN": 700,
}
WEEKDAY = [1.0, 0.97, 0.95, 0.97, 0.92, 0.9, 1.12]  # Mon..Sun

DEVICES = [("desktop", 0.36, 1.25, 1.10), ("mobile", 0.64, 0.86, 0.85)]
VISITORS = [("new", 0.56, 0.88, 0.95), ("returning", 0.30, 1.16, 1.05), ("member", 0.14, 1.30, 1.10)]
CHANNELS = [("organic_search", 0.29, 1.00, 1.00), ("paid_search", 0.22, 0.82, 0.95), ("paid_social", 0.09, 0.70, 0.85),
            ("organic_social", 0.03, 0.90, 0.95), ("email", 0.10, 1.55, 1.05), ("affiliate", 0.04, 0.95, 1.00),
            ("referral", 0.03, 1.05, 1.00), ("direct", 0.20, 1.08, 1.00)]


def rng(*parts) -> random.Random:
    """A random generator that gives the same numbers for the same inputs, run after run."""
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def season(d: date) -> float:
    """Traffic boost around Black Week and Christmas."""
    if date(d.year, 11, 23) <= d <= date(d.year, 12, 1):
        return 1.6
    if date(d.year, 12, 2) <= d <= date(d.year, 12, 20):
        return 1.25
    return 1.0


def promo_profile(entry_id: str) -> dict:
    """Fixed traits of a promotion: how clickable it is, how well it converts."""
    r = rng("profile", entry_id)
    return {
        "ctr": 0.006 + r.random() * 0.024,                       # clicks per view when fully seen
        "repeat": 1.05 + r.random() * 0.5,                       # clicks per clicking session
        "pv": 0.6 + r.random() * 0.25,                           # sessions that then view a product
        "conv": 0.05 + r.random() * 0.13,                        # sessions that then buy
        "qb": 0.13 + r.random() * 0.07 if r.random() < 0.15 else 0.03 + r.random() * 0.06,
        "aov": 650 + r.random() * 900,
        "ab": r.random() < 0.12,                                 # runs an A/B test
        "ab_ctr": 0.8 + r.random() * 0.7, "ab_conv": 0.8 + r.random() * 0.6,
    }


def exposure(y_px: float) -> float:
    """Share of page views where a block at this depth is on screen."""
    return max(0.12, min(0.995, math.exp(-max(0.0, y_px - FOLD_PX) / 3200)))


def reach(y_px: float, page_height: int) -> float:
    """Share of visitors whose deepest scroll reaches y_px."""
    if y_px <= FOLD_PX:
        return 1.0
    if y_px > page_height:
        return 0.0
    return math.exp(-(y_px - FOLD_PX) / 2900)


def stockholm_day(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(STHLM).date()


def plan(snapshots: list[dict], modules: list[dict], days_back: int, until: date) -> dict:
    """Work out every metric row. Pure function, so it can be tested without a database."""
    mods = defaultdict(list)
    for m in modules:
        mods[m["snapshot_id"]].append(m)

    by_market = defaultdict(list)
    for s in snapshots:
        by_market[s["market"]].append(s)

    out = {"promotion": [], "page": [], "segment": [], "ab": []}
    for market, snaps in by_market.items():
        # Desktop decides what was on the page; fall back to mobile if a market has no desktop capture.
        desk = [s for s in snaps if s["device"] == "desktop"] or snaps
        desk.sort(key=lambda s: s["captured_at"])
        first = stockholm_day(desk[0]["captured_at"])
        start = first - timedelta(days=days_back)
        first_live = {}
        d = start
        while d <= until:
            live = [s for s in desk if stockholm_day(s["captured_at"]) <= d]
            snap = live[-1] if live else desk[0]
            page_h = snap.get("page_height") or 6800
            r = rng("day", market, d)
            pv = int(BASE_VIEWS.get(market, 1500) * WEEKDAY[d.weekday()] * season(d) * (0.92 + r.random() * 0.16))
            sessions = int(pv * 0.86)
            users = int(sessions * 0.9)

            # Scroll depth in pixels (scroll fires when the visitor leaves the page).
            samples = int(pv * 0.8)
            n_buckets = page_h // BUCKET_PX + 1
            reached = [int(samples * reach(k * BUCKET_PX, page_h)) for k in range(n_buckets)]
            out["page"].append({"market": market, "day": d.isoformat(), "page_views": pv, "users": users,
                                "sessions": sessions, "scroll_samples": samples, "avg_pixels_total": page_h,
                                "bucket_px": BUCKET_PX, "reached": reached})

            # Promotions on the page that day, in page order, one row per entry.
            rows = sorted(mods.get(snap["id"], []), key=lambda m: m["position"])
            last_pos = max([m["position"] for m in rows], default=1) or 1
            seen_any = 0.0
            done = set()
            for m in rows:
                eid = m.get("entry_id")
                if not eid or eid in done:
                    continue
                done.add(eid)
                first_live.setdefault(eid, d)
                p = promo_profile(eid)
                rr = rng("promo", market, d, eid)
                y = (m["position"] + 0.5) / (last_pos + 1) * page_h
                exp_ = exposure(y)
                seen_any = max(seen_any, exp_)
                fatigue = 1 - min(0.25, (d - first_live[eid]).days * 0.008)
                views = int(pv * exp_ * (0.9 + rr.random() * 0.15))
                clicks = int(views * p["ctr"] * fatigue * (0.85 + rr.random() * 0.3))
                cs = int(clicks / p["repeat"])
                buy = int(cs * p["conv"] * (0.85 + rr.random() * 0.3))
                cart = min(int(cs * p["pv"] * 0.8), max(buy, int(buy * (2.2 + rr.random()))))
                names = m.get("promotion_names") or []
                out["promotion"].append({
                    "market": market, "day": d.isoformat(), "entry_id": eid,
                    "promotion_name": names[0] if names else m.get("label"),
                    "views": views, "clicks": clicks, "quickbacks": int(clicks * p["qb"]),
                    "click_sessions": cs, "product_view_sessions": int(cs * p["pv"]),
                    "cart_sessions": cart, "purchase_sessions": buy,
                    "revenue_sek": round(buy * p["aov"] * (0.9 + rr.random() * 0.2), 2)})
                if p["ab"]:
                    name = "Test: " + (names[0] if names else eid)
                    for ver, ci, vi in (("A", 1.0, 1.0), ("B", p["ab_ctr"], p["ab_conv"])):
                        v = views // 2
                        c = int(v * p["ctr"] * fatigue * ci)
                        vcs = int(c / p["repeat"])
                        out["ab"].append({"market": market, "day": d.isoformat(), "entry_id": eid, "test_name": name,
                                          "version": ver, "share": 0.5, "views": v, "clicks": c,
                                          "click_sessions": vcs, "purchase_sessions": int(vcs * p["conv"] * vi)})
            out["page"][-1]["promo_view_page_views"] = int(pv * seen_any)

            # Filter splits.
            for dim, segs in (("device", DEVICES), ("visitor", VISITORS), ("channel", CHANNELS)):
                for value, share, ctr_i, conv_i in segs:
                    j = 0.95 + r.random() * 0.1
                    out["segment"].append({"market": market, "day": d.isoformat(), "dimension": dim, "value": value,
                                           "sessions": int(sessions * share * j), "ctr_index": round(ctr_i * j, 3),
                                           "conv_index": round(conv_i * (0.97 + r.random() * 0.06), 3)})
            d += timedelta(days=1)
    return out


def fetch_all(db, table: str, select: str, **params) -> list[dict]:
    rows, off = [], 0
    while True:
        page = db._req("GET", f"/rest/v1/{table}", params={"select": select, "limit": "1000", "offset": str(off), **params}) or []
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def upsert(db, table: str, rows: list[dict], keys: str):
    for i in range(0, len(rows), 500):
        db._req("POST", f"/rest/v1/{table}", rows[i:i + 500],
                {"Prefer": "return=minimal,resolution=merge-duplicates"}, params={"on_conflict": keys})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=0,
                    help="made-up history before each market's first capture (0 = only days with a capture)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    from store import SupabaseStore
    db = SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    snaps = fetch_all(db, "snapshots", "id,market,device,captured_at,page_height", order="captured_at")
    if not snaps:
        print("No snapshots yet: nothing to attach numbers to.")
        return
    mods = []
    ids = [s["id"] for s in snaps]
    for i in range(0, len(ids), 100):
        mods += fetch_all(db, "snapshot_modules", "snapshot_id,position,entry_id,label,promotion_names",
                          snapshot_id=f"in.({','.join(ids[i:i + 100])})", entry_id="not.is.null")
    until = datetime.now(STHLM).date() - timedelta(days=1)  # today's numbers are never synced yet
    rows = plan(snaps, mods, a.days_back, until)

    summary = (f"{len(snaps)} snapshots, {len(mods)} promotion blocks -> {len(rows['promotion'])} promotion rows, "
               f"{len(rows['page'])} page rows, {len(rows['segment'])} segment rows, {len(rows['ab'])} A/B rows")
    print(summary)
    if a.dry_run:
        return
    # Numbers only for days we have captures for: drop sample rows from before each market's start
    starts = {}
    for r in rows["page"]:
        starts[r["market"]] = min(starts.get(r["market"], r["day"]), r["day"])
    for market, first_day in starts.items():
        for table in ("promotion_metrics_daily", "page_metrics_daily", "segment_metrics_daily", "ab_test_metrics_daily"):
            db._req("DELETE", f"/rest/v1/{table}", headers={"Prefer": "return=minimal"},
                    params={"market": f"eq.{market}", "day": f"lt.{first_day}", "source": "eq.mock"})
    upsert(db, "promotion_metrics_daily", rows["promotion"], "market,day,entry_id")
    upsert(db, "page_metrics_daily", rows["page"], "market,day")
    upsert(db, "segment_metrics_daily", rows["segment"], "market,day,dimension,value")
    upsert(db, "ab_test_metrics_daily", rows["ab"], "market,day,entry_id,version")
    print("Written.")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(f"## Sample metrics\n\n{summary}\n")


if __name__ == "__main__":
    main()
