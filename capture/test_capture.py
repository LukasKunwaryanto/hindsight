"""Hindsight – Phase 0 capture test.

Opens a page in Chrome on desktop and mobile, the way Hindsight will capture it,
and reports whether it loaded normally or was blocked by bot protection.

Usage:  python capture/test_capture.py --url https://www.nordicnest.se/ --out results
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

DEVICES = {
    "desktop": dict(viewport={"width": 1440, "height": 900}),
    "mobile": dict(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    ),
}

# Signs that Cloudflare (or similar) showed a challenge instead of the real page
CHALLENGE_MARKERS = [
    "Just a moment",
    "challenges.cloudflare.com",
    "cf-chl-",
    "Attention Required",
    "Verify you are human",
]

PAGE_STATS_JS = """() => {
  const sheets = [...document.styleSheets];
  let rules = 0;
  for (const s of sheets) { try { rules += s.cssRules.length; } catch (e) {} }
  const blocks = document.querySelectorAll('main > *').length;
  const contentfulIds = new Set([...document.querySelectorAll('[id]')]
    .map(e => e.id).filter(id => /^[A-Za-z0-9]{20,24}$/.test(id))).size;
  const imgs = [...document.querySelectorAll('img')];
  return {
    title: document.title,
    lang: document.documentElement.lang,
    height: document.documentElement.scrollHeight,
    main_blocks: blocks,
    contentful_entry_ids: contentfulIds,
    images: imgs.length,
    images_loaded: imgs.filter(i => i.complete && i.naturalWidth > 0).length,
    stylesheets: sheets.length,
    css_rules: rules,
    product_cards: document.querySelectorAll('[data-test-id="product-card"]').length,
  };
}"""


async def capture(browser, url: str, device: str, out: Path) -> dict:
    started = time.monotonic()
    ctx = await browser.new_context(locale="sv-SE", device_scale_factor=1, **DEVICES[device])
    extra = os.environ.get("HINDSIGHT_BYPASS_HEADER")  # e.g. "X-Hindsight-Token: abc", set later
    if extra and ":" in extra:
        name, value = extra.split(":", 1)
        await ctx.set_extra_http_headers({name.strip(): value.strip()})
    page = await ctx.new_page()
    result = {"device": device, "url": url}
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        result["http_status"] = resp.status if resp else None
        result["cf_mitigated"] = (resp.headers.get("cf-mitigated") if resp else None)
        await page.wait_for_timeout(3000)

        html = await page.content()
        blocked = [m for m in CHALLENGE_MARKERS if m in html]
        result["challenge_markers"] = blocked

        # Same steps as the real capture: remove cookie dialog, render everything, scroll
        await page.evaluate("document.querySelectorAll('[id^=ccc]').forEach(e => e.remove())")
        await page.add_style_tag(content="*{content-visibility:visible!important}")
        height, y = await page.evaluate("document.body.scrollHeight"), 0
        while y < height:
            y += 600
            await page.evaluate(f"window.scrollTo(0,{y})")
            await page.wait_for_timeout(350)
            height = await page.evaluate("document.body.scrollHeight")
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0,0)")
        await page.wait_for_timeout(1000)

        result.update(await page.evaluate(PAGE_STATS_JS))
        await page.screenshot(path=str(out / f"{device}.png"), full_page=True)
        (out / f"{device}.html").write_text(await page.content(), encoding="utf-8")

        ok = (result["http_status"] or 0) < 400 and not blocked and result["main_blocks"] > 0
        result["verdict"] = "OK" if ok else "BLOCKED_OR_EMPTY"
    except Exception as e:  # keep going so the other device still runs
        result["verdict"] = "ERROR"
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        result["seconds"] = round(time.monotonic() - started, 1)
        await ctx.close()
    return result


def summary_markdown(results: list[dict]) -> str:
    lines = [
        "## Hindsight capture test",
        "",
        "| Device | Verdict | HTTP | Seconds | Page height | Blocks | Contentful IDs | Images loaded | CSS rules |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r['device']} | **{r['verdict']}** | {r.get('http_status', '')} | {r['seconds']} | "
            f"{r.get('height', '')} | {r.get('main_blocks', '')} | {r.get('contentful_entry_ids', '')} | "
            f"{r.get('images_loaded', '')}/{r.get('images', '')} | {r.get('css_rules', '')} |"
        )
    for r in results:
        if r.get("challenge_markers") or r.get("cf_mitigated") or r.get("error"):
            lines.append("")
            lines.append(f"- {r['device']}: challenge markers {r.get('challenge_markers')}, "
                         f"cf-mitigated={r.get('cf_mitigated')}, error={r.get('error')}")
    lines.append("")
    lines.append("Screenshots and captured HTML are attached to this run as the `capture-test` artifact.")
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.nordicnest.se/")
    ap.add_argument("--devices", default="desktop,mobile")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        results = [await capture(browser, args.url, d.strip(), out) for d in args.devices.split(",")]
        await browser.close()

    (out / "report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    md = summary_markdown(results)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")
    if any(r["verdict"] != "OK" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
