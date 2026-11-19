// Copied from hindsight-app (server/rawData.ts at c2db105). Keep in sync; change it there first.
/**
 * Raw metric rows for the client. The client sums them per period (a snapshot's live
 * period, the previous period, 12 months) and applies filters, so the server only reads.
 * Read-only: every call is a select through lib/hindsight/supabase.ts.
 */
import process from 'node:process';
import { select } from './supabase.ts';

const DAY = /^\d{4}-\d{2}-\d{2}$/;
const ENTRY = /^[A-Za-z0-9_-]{1,64}$/;

function ensureCredentials() {
  if (!process.env.SUPABASE_PUBLISHABLE_KEY) {
    throw new Error('SUPABASE_PUBLISHABLE_KEY is not set. Add it in AI Studio → Settings → Secrets.');
  }
}
function checkDays(from: string, to: string) {
  if (!DAY.test(from) || !DAY.test(to)) throw new Error('Invalid date range');
}

/** PostgREST returns at most 1000 rows per call by default: page through. */
async function selectAll<T>(table: string, query: string): Promise<T[]> {
  const out: T[] = [];
  for (let off = 0; ; off += 1000) {
    const rows = await select<T>(table, `${query}&limit=1000&offset=${off}`);
    out.push(...rows);
    if (rows.length < 1000) return out;
  }
}

/** Every metric row for one market between two Stockholm dates (inclusive). */
export async function getMarketMetrics(market: string, from: string, to: string) {
  ensureCredentials();
  checkDays(from, to);
  const range = `market=eq.${market}&day=gte.${from}&day=lte.${to}&order=day.asc`;
  const [promos, pages, segments, ab] = await Promise.all([
    selectAll('promotion_metrics_daily',
      `select=entry_id,promotion_name,day,views,clicks,quickbacks,click_sessions,product_view_sessions,cart_sessions,purchase_sessions,revenue_sek,source&${range}`),
    selectAll('page_metrics_daily',
      `select=day,page_views,users,sessions,promo_view_page_views,scroll_samples,avg_pixels_total,bucket_px,reached,source&${range}`),
    selectAll('segment_metrics_daily', `select=day,dimension,value,sessions,ctr_index,conv_index&${range}`).catch(() => []),
    selectAll('ab_test_metrics_daily',
      `select=day,entry_id,test_name,version,share,views,clicks,click_sessions,purchase_sessions&${range}`).catch(() => []),
  ]);
  return { promos, pages, segments, ab };
}

/** Page and promotion totals per market for a date range (All markets view). */
export async function getMarketsSummary(from: string, to: string) {
  ensureCredentials();
  checkDays(from, to);
  const range = `day=gte.${from}&day=lte.${to}`;
  const [promos, pages] = await Promise.all([
    selectAll<any>('promotion_metrics_daily',
      `select=market,views,clicks,quickbacks,click_sessions,purchase_sessions,revenue_sek&${range}`),
    selectAll<any>('page_metrics_daily', `select=market,page_views,users,promo_view_page_views&${range}`),
  ]);
  const by: Record<string, any> = {};
  const get = (m: string) => (by[m] ??= { market: m, views: 0, clicks: 0, quickbacks: 0, click_sessions: 0, purchase_sessions: 0, revenue_sek: 0, page_views: 0, users: 0, promo_view_page_views: 0 });
  for (const r of promos) {
    const o = get(r.market);
    for (const k of ['views', 'clicks', 'quickbacks', 'click_sessions', 'purchase_sessions', 'revenue_sek']) o[k] += Number(r[k] || 0);
  }
  for (const r of pages) {
    const o = get(r.market);
    for (const k of ['page_views', 'users', 'promo_view_page_views']) o[k] += Number(r[k] || 0);
  }
  return Object.values(by);
}

/** One promotion's totals per market for a date range ("Same promotion in other markets"). */
export async function getPromotionMarkets(entryId: string, from: string, to: string) {
  ensureCredentials();
  checkDays(from, to);
  if (!ENTRY.test(entryId)) throw new Error('Invalid entry id');
  const rows = await selectAll<any>('promotion_metrics_daily',
    `select=market,day,views,clicks,click_sessions,purchase_sessions,quickbacks&entry_id=eq.${entryId}&day=gte.${from}&day=lte.${to}`);
  const by: Record<string, any> = {};
  for (const r of rows) {
    const o = (by[r.market] ??= { market: r.market, first_day: r.day, views: 0, clicks: 0, click_sessions: 0, purchase_sessions: 0, quickbacks: 0 });
    if (r.day < o.first_day) o.first_day = r.day;
    for (const k of ['views', 'clicks', 'click_sessions', 'purchase_sessions', 'quickbacks']) o[k] += Number(r[k] || 0);
  }
  return Object.values(by);
}

/** For each market, the desktop snapshot that was live at a moment (All markets view). */
export async function getSnapshotsAt(at: string) {
  ensureCredentials();
  if (Number.isNaN(Date.parse(at))) throw new Error('Invalid time');
  const rows = await selectAll<any>('snapshots',
    `select=id,market,captured_at,page_height&device=eq.desktop&page=eq./&captured_at=lte.${encodeURIComponent(new Date(at).toISOString())}&order=captured_at.asc`);
  const firsts = await selectAll<any>('snapshots', 'select=market,captured_at&device=eq.desktop&page=eq./&order=captured_at.asc');
  const latest: Record<string, any> = {};
  for (const r of rows) latest[r.market] = r;
  const first: Record<string, string> = {};
  for (const r of firsts) first[r.market] ??= r.captured_at;
  return Object.keys(first).map((m) => ({ market: m, snapshot: latest[m] || null, first_capture_at: first[m] }));
}

/** The top promotion's GA name per snapshot of a market, for campaign labels on the timeline. */
export async function getSnapshotHeroes(market: string) {
  ensureCredentials();
  const snaps = await selectAll<any>('snapshots', `select=id&market=eq.${market}&device=eq.desktop&page=eq./&order=captured_at.asc`);
  const out: Record<string, string | null> = {};
  for (let i = 0; i < snaps.length; i += 80) {
    const ids = snaps.slice(i, i + 80).map((s) => s.id);
    const rows = await selectAll<any>('snapshot_modules',
      `select=snapshot_id,position,promotion_names&snapshot_id=in.(${ids.join(',')})&entry_id=not.is.null&order=snapshot_id.asc,position.asc`);
    for (const r of rows) if (!(r.snapshot_id in out)) out[r.snapshot_id] = (r.promotion_names || [])[0] || null;
  }
  return out;
}
