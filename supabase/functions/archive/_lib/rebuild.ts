// Copied from hindsight-app (lib/hindsight/rebuild.ts at c2db105). Keep in sync; change it there first.
/**
 * Rebuild a stored snapshot into one self-contained HTML page.
 *
 * A snapshot is stored as pieces named by hash (see HANDOVER.md): a page skeleton with
 * placeholders, one piece per block, product cards, CSS rules and images. This puts them
 * back together. It is a direct port of `capture/rebuild.py` in the hindsight repo, which
 * the capture pipeline is tested against. Do not change the logic; wrap it instead.
 *
 * Pure function: data comes in through `getBlobs` and `assetUrl`, so it runs on the
 * server (see supabase.ts) or in a test with fixtures.
 */

export interface Manifest {
  version: number;
  base: string;
  shell: string;
  static_css: string[];
  ruleset: string;
  modules: { label: string; ref: string }[];
  cards: { pos: number; sku: string; key?: string; card: string; price: string | null; badges: { text: string; ref: string }[] }[];
  /** inner pages with a listing grid: the products in grid order (their slots are <hs-gcard>) */
  grid?: string[];
  grid_ratio?: number | null;
  /** the real product card's height in the listing, kept by the placeholder cards */
  grid_card_h?: number | null;
}

/** SKUs -> image URL or stored "hs-asset:…" ref (table product_images). */
export type ProductImages = (skus: string[]) => Promise<Record<string, string>>;
/** The site bars to show instead of the captured ones (table site_parts), in page order. */
export type SiteParts = { label: string; ref: string }[];

const SITE_PARTS = ["header", "footer", "chrome"];
const GCARD = /<hs-gcard><\/hs-gcard>/g;

/** Placeholder for one product in a listing grid: its image, at the real card's height. */
export function gridCard(src: string, ratio?: number | null, height?: number | null): string {
  const ar = ratio ? `aspect-ratio:1/${ratio};` : "aspect-ratio:1/1;";
  const img = src
    ? `<img src="${src}" alt="" style="display:block;width:100%;${ar}object-fit:contain;opacity:1">`
    : `<div style="width:100%;${ar}background:#f2f2f2"></div>`;
  const mh = height ? `min-height:${height}px;` : "";
  return `<div class="hs-gcard" style="display:block;width:100%;${mh}">${img}</div>`;
}

/** hashes -> text of each stored piece (missing hashes may be left out). */
export type GetBlobs = (hashes: string[]) => Promise<Record<string, string>>;
/** stored image/font hash + mime -> URL the browser can load. */
export type AssetUrl = (hash: string, mime: string) => string;

const MODULE = /<hs-module ref="([0-9a-f]{16})"><\/hs-module>/g;
const CARD = /<hs-card (i|k)="([^"]+)"><\/hs-card>/g;  // v1: index, v2: stable key (SKU~n)
const ASSET = /hs-asset:([0-9a-f]{16}):([a-z/+0-9.-]+)/g;

export async function rebuild(manifest: Manifest, getBlobs: GetBlobs, assetUrl: AssetUrl,
                              productImages?: ProductImages, siteParts?: SiteParts): Promise<string> {
  // site bars from their own timeline (ignored if the page has a different number of bars)
  const swap = new Map<string, string>();
  const own = manifest.modules.filter((m) => SITE_PARTS.includes(m.label));
  if (siteParts && siteParts.length === own.length) own.forEach((m, i) => { if (m.ref !== siteParts[i].ref) swap.set(m.ref, siteParts[i].ref); });
  const sw = (h: string) => swap.get(h) ?? h;
  const want = new Set<string>([manifest.shell, manifest.ruleset, ...manifest.static_css, ...manifest.modules.map((m) => sw(m.ref))]);
  for (const c of manifest.cards ?? []) {
    want.add(c.card);
    c.badges.forEach((b) => want.add(b.ref));
    if (c.price) want.add(c.price);
  }
  const texts: Record<string, string> = await getBlobs([...want].sort());
  // v1: a list of rule refs; v2: {chunks: [...]}, each chunk a list of rule refs (order kept)
  const parsed = JSON.parse(texts[manifest.ruleset]);
  let ruleKeys: string[] = parsed;
  if (!Array.isArray(parsed)) {
    const chunks: string[] = parsed.chunks;
    Object.assign(texts, await getBlobs([...new Set(chunks.filter((c) => !(c in texts)))].sort()));
    ruleKeys = chunks.flatMap((c) => JSON.parse(texts[c]) as string[]);
  }
  const missingRules = ruleKeys.filter((k) => !(k in texts));
  Object.assign(texts, await getBlobs([...new Set(missingRules)].sort()));

  const cards = manifest.cards ?? [];
  const byKey = new Map(cards.filter((c) => c.key).map((c) => [c.key as string, c]));
  const unescape = (s: string) => s.replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  const cardHtml = (kind: string, ref: string) => {
    const c = kind === "i" ? cards[Number(ref)] : byKey.get(unescape(ref));
    if (!c) return "";
    let h = texts[c.card] ?? "";
    // function replacers, so "$" in prices is never read as a replacement pattern
    h = h.replace('<hs-slot n="price"></hs-slot>', () => (c.price && texts[c.price]) || "");
    return h.replace('<hs-slot n="badges"></hs-slot>', () => c.badges.map((b) => texts[b.ref] ?? "").join(""));
  };

  let html = texts[manifest.shell] ?? "";
  for (let round = 0; round < 6; round++) {  // blocks -> cards -> card parts
    const missing = [...html.matchAll(MODULE)].map((m) => sw(m[1])).filter((h) => !(h in texts));
    if (missing.length) Object.assign(texts, await getBlobs([...new Set(missing)]));
    const next = html.replace(MODULE, (_, h) => texts[sw(h)] ?? "").replace(CARD, (_, kind, ref) => cardHtml(kind, ref));
    if (next === html) break;
    html = next;
  }

  const grid = manifest.grid ?? [];
  if (grid.length) {
    const imgs = productImages ? await productImages([...new Set(grid)].sort()) : {};
    let i = 0;
    html = html.replace(GCARD, () => gridCard(imgs[grid[i++] ?? ""] ?? "", manifest.grid_ratio, manifest.grid_card_h));
  }

  let css = manifest.static_css.map((k) => `<style>${texts[k] ?? ""}</style>`).join("");
  const rules = ruleKeys.map((k) => JSON.parse(texts[k]) as [string, string]);
  css += "<style>" + rules.map(([media, rule]) => (media ? `${media}{${rule}}` : rule)).join("") + "</style>";
  html = html.replace(/<head\b[^>]*>/, (m) => m + '<meta charset="utf-8">' + css);  // function: CSS may contain "$"
  return html.replace(ASSET, (_, h, mime) => assetUrl(h, mime) || "");
}
