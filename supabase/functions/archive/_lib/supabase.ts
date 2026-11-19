// Copied from hindsight-app (lib/hindsight/supabase.ts at c2db105). Keep in sync; change it there first.
/**
 * Read-only access to Hindsight's Supabase data. SERVER-SIDE ONLY (Node).
 *
 * Needs two secrets in AI Studio (Settings / Secrets):
 *   SUPABASE_URL              https://arztscplszpzldtpiawc.supabase.co
 *   SUPABASE_PUBLISHABLE_KEY  the "publishable" key (sb_publishable_...), read-only by design
 * Never use the secret / service_role key in this app.
 */
import { gunzipSync } from "node:zlib";
import { Buffer } from "node:buffer";
import { process } from "./env.ts";
import { rebuild, type Manifest, type SiteParts } from "./rebuild.ts";

const URL_ = (process.env.SUPABASE_URL ?? "").replace(/\/$/, "");
const KEY = process.env.SUPABASE_PUBLISHABLE_KEY ?? "";

/** GET from the Supabase REST API. `query` uses PostgREST syntax, e.g. "select=id&market=eq.sv-SE". */
export async function select<T = any>(table: string, query: string): Promise<T[]> {
  const r = await fetch(`${URL_}/rest/v1/${table}?${query}`, { headers: { apikey: KEY } });
  if (!r.ok) throw new Error(`Supabase ${table}: ${r.status} ${await r.text()}`);
  return r.json();
}

// Stored pieces never change (they are named by their content), so cache them forever.
const blobCache = new Map<string, string>();

/** Stored pieces by hash. Content is gzip in a bytea column, returned by PostgREST as "\x<hex>". */
export async function getBlobs(hashes: string[]): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  const todo = hashes.filter((h) => {
    const hit = blobCache.get(h);
    if (hit !== undefined) out[h] = hit;
    return hit === undefined;
  });
  for (let i = 0; i < todo.length; i += 100) {
    const rows = await select<{ hash: string; content: string }>("blobs",
      `select=hash,content&hash=in.(${todo.slice(i, i + 100).join(",")})`);
    for (const row of rows) {
      const text = gunzipSync(Buffer.from(row.content.slice(2), "hex")).toString("utf-8");
      blobCache.set(row.hash, text);
      out[row.hash] = text;
    }
  }
  return out;
}

const EXT: Record<string, string> = {
  "image/avif": "avif", "image/webp": "webp", "image/png": "png", "image/svg+xml": "svg",
  "image/gif": "gif", "image/jpeg": "jpg", "font/woff2": "woff2", "font/woff": "woff",
};

/** Public URL of a stored image or font. The path follows from the hash, no lookup needed. */
export function assetUrl(hash: string, mime: string): string {
  return `${URL_}/storage/v1/object/public/assets/${hash.slice(0, 2)}/${hash}.${EXT[mime] ?? "bin"}`;
}

/** SKU -> product image (a stored copy when there is one) for listing grids. */
export async function productImages(skus: string[]): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  const safe = skus.filter((k) => k && !/[,()"]/.test(k));
  for (let i = 0; i < safe.length; i += 100) {
    const rows = await select<{ sku: string; src: string; asset: string | null }>("product_images",
      `select=sku,src,asset&sku=in.(${safe.slice(i, i + 100).map(encodeURIComponent).join(",")})`);
    for (const r of rows) out[r.sku] = r.asset || r.src;
  }
  return out;
}

/** The site bars (header, bars, footer) live at a time: they have their own timeline. */
async function sitePartsAt(market: string, device: string, at: string) {
  const [row] = await select<{ parts: SiteParts }>("site_parts",
    `select=parts&market=eq.${encodeURIComponent(market)}&device=eq.${device}&captured_at=lte.${encodeURIComponent(at)}&order=captured_at.desc&limit=1`);
  return row?.parts;
}

/**
 * The full HTML of one snapshot, ready for <iframe srcdoc>. The page is shown with the site bars that were
 * live at `at` (default: when the snapshot was captured).
 */
export async function snapshotHtml(snapshotId: string, at?: string): Promise<string> {
  const [s] = await select<{ manifest: Manifest; market: string; device: string; captured_at: string }>("snapshots",
    `select=manifest,market,device,captured_at&id=eq.${snapshotId}`);
  if (!s) throw new Error(`No snapshot ${snapshotId}`);
  const when = at && !Number.isNaN(Date.parse(at)) ? new Date(at).toISOString() : s.captured_at;
  const parts = await sitePartsAt(s.market, s.device, when);
  return rebuild(s.manifest, getBlobs, assetUrl, productImages, parts);
}
