// Copied from hindsight-app (server/dataService.ts at c2db105). Keep in sync; change it there first.
/**
 * Read-only Supabase data service for Hindsight.
 * Strictly queries Supabase using lib/hindsight/supabase.ts.
 * NO fallback / mock data is permitted. If secrets are missing or requests fail,
 * an error is thrown.
 */
import { process } from './env.ts';
import { select, snapshotHtml } from './supabase.ts';

/** Calendar day in Stockholm (YYYY-MM-DD): metric days are Stockholm dates, timestamps are UTC. */
const stockholmDay = (ts: string | Date) =>
  new Date(ts).toLocaleDateString('sv-SE', { timeZone: 'Europe/Stockholm' });

// Map locale metadata for display (flags, clean market codes)
const MARKET_METADATA: Record<string, { code: string; name: string; flag: string; tld: string }> = {
  'sv-SE': { code: 'SE', name: 'Sweden', flag: '🇸🇪', tld: 'se' },
  'en-US': { code: 'COM', name: 'International', flag: '🌐', tld: 'com' },
  'de-DE': { code: 'DE', name: 'Germany', flag: '🇩🇪', tld: 'de' },
  'nb-NO': { code: 'NO', name: 'Norway', flag: '🇳🇴', tld: 'no' },
  'da-DK': { code: 'DK', name: 'Denmark', flag: '🇩🇰', tld: 'dk' },
  'fi-FI': { code: 'FI', name: 'Finland', flag: '🇫🇮', tld: 'fi' },
  'nl-NL': { code: 'NL', name: 'Netherlands', flag: '🇳🇱', tld: 'nl' },
  'fr-FR': { code: 'FR', name: 'France', flag: '🇫🇷', tld: 'fr' },
  'es-ES': { code: 'ES', name: 'Spain', flag: '🇪🇸', tld: 'es' },
  'it-IT': { code: 'IT', name: 'Italy', flag: '🇮🇹', tld: 'it' },
  'pl-PL': { code: 'PL', name: 'Poland', flag: '🇵🇱', tld: 'pl' },
  'pt-PT': { code: 'PT', name: 'Portugal', flag: '🇵🇹', tld: 'pt' },
  'el-GR': { code: 'GR', name: 'Greece', flag: '🇬🇷', tld: 'gr' },
  'ja-JP': { code: 'JP', name: 'Japan', flag: '🇯🇵', tld: 'jp' },
  'ko-KR': { code: 'KR', name: 'South Korea', flag: '🇰🇷', tld: 'kr' },
  'zh-CN': { code: 'CN', name: 'China', flag: '🇨🇳', tld: 'cn' },
};

function ensureCredentials() {
  if (!process.env.SUPABASE_PUBLISHABLE_KEY) {
    throw new Error('SUPABASE_PUBLISHABLE_KEY is not set. Add it in AI Studio → Settings → Secrets.');
  }
}

export async function getMarkets() {
  ensureCredentials();
  interface MarketRow {
    locale: string;
    url: string;
    enabled: boolean;
    sort_order: number;
  }
  const rows = await select<MarketRow>('markets', 'select=locale,url,enabled,sort_order&enabled=eq.true&order=sort_order.asc');
  if (!rows || rows.length === 0) {
    throw new Error('No enabled markets found in the database.');
  }

  return rows.map((r) => {
    const meta = MARKET_METADATA[r.locale] || {
      code: r.locale.split('-')[1] || r.locale.slice(0, 2).toUpperCase(),
      name: r.locale,
      flag: '🌐',
      tld: 'com',
    };
    return {
      locale: r.locale,
      url: r.url,
      code: meta.code,
      name: meta.name,
      flag: meta.flag,
      tld: meta.tld,
      sort_order: r.sort_order,
    };
  });
}

export async function getSnapshots(marketLocale: string, includeSmall: boolean = false) {
  ensureCredentials();
  interface SnapshotRow {
    id: string;
    market: string;
    device: string;
    captured_at: string;
    page_height: number;
    change_size: string;
    change_title: string;
    change_description: string;
    previous_snapshot_id: string | null;
  }

  const rows = await select<SnapshotRow>(
    'snapshots',
    `select=id,market,device,captured_at,page_height,change_size,change_title,change_description,previous_snapshot_id&market=eq.${marketLocale}&device=eq.desktop&page=eq./&order=captured_at.asc`
  );

  // Filter per HANDOVER.md:
  // "Timeline: show big and medium; small only with 'Show small tweaks'; never show none"
  return rows.filter((r) => {
    if (r.change_size === 'none') return false;
    if (r.change_size === 'small' && !includeSmall) return false;
    return true;
  });
}

/** `at` (optional ISO time): show the page with the site bars live at that moment (default: its capture time). */
export async function getSnapshotHtmlContent(snapshotId: string, at?: string): Promise<string> {
  ensureCredentials();
  return snapshotHtml(snapshotId, at);
}

export async function getSnapshotModules(snapshotId: string) {
  ensureCredentials();
  interface ModuleRow {
    snapshot_id: string;
    position: number;
    label: string;
    entry_id: string | null;
    title: string | null;
    promotion_names: string[];
    blob_hash: string;
  }

  const rows = await select<ModuleRow>(
    'snapshot_modules',
    `select=snapshot_id,position,label,entry_id,title,promotion_names,blob_hash&snapshot_id=eq.${snapshotId}&order=position.asc`
  );

  // Rank among blocks with entry_id (1 = top) per HANDOVER.md
  let rank = 1;
  return rows.map((m) => {
    const isPromo = !!m.entry_id;
    const currentRank = isPromo ? rank++ : undefined;
    return {
      ...m,
      rank: currentRank,
      position_label: currentRank ? `Rank ${currentRank}` : m.label,
    };
  });
}

export async function getSnapshotChanges(snapshotId: string) {
  ensureCredentials();
  interface ChangeRow {
    id: string;
    snapshot_id: string;
    kind: string;
    detail: {
      label: string;
      title: string;
      was?: string;
      was_entry_id?: string;
    };
  }

  return select<ChangeRow>(
    'snapshot_changes',
    `select=id,snapshot_id,entry_id,position,kind,detail&snapshot_id=eq.${snapshotId}&order=position.asc`
  );
}

export async function getSnapshotMetrics(snapshotId: string, marketLocale: string) {
  ensureCredentials();

  // 1. Get snapshot period
  interface PeriodRow {
    snapshot_id: string;
    market: string;
    device: string;
    live_from: string;
    live_to: string | null;
  }

  const periods = await select<PeriodRow>(
    'snapshot_periods',
    `select=snapshot_id,market,device,live_from,live_to&snapshot_id=eq.${snapshotId}`
  );

  if (!periods || periods.length === 0) {
    throw new Error(`No snapshot period found for snapshot ${snapshotId}`);
  }

  const period = periods[0];
  const liveFromDate = period.live_from ? stockholmDay(period.live_from) : '';
  const liveToDate = stockholmDay(period.live_to ?? new Date());

  // 2. Query page metrics
  interface PageMetricRow {
    market: string;
    day: string;
    page_views: number;
    users: number;
    sessions: number;
    promo_view_page_views: number;
    reached: number[];
    source?: string;
  }

  const pageMetrics = await select<PageMetricRow>(
    'page_metrics_daily',
    `select=market,day,page_views,users,sessions,promo_view_page_views,reached,source&market=eq.${marketLocale}&day=gte.${liveFromDate}&day=lte.${liveToDate}`
  );

  // 3. Query promotion metrics
  interface PromoMetricRow {
    entry_id: string;
    market: string;
    day: string;
    views: number;
    clicks: number;
    quickbacks: number;
    click_sessions: number;
    product_view_sessions: number;
    cart_sessions: number;
    purchase_sessions: number;
    revenue_sek: number;
    source?: string;
  }

  const promoMetrics = await select<PromoMetricRow>(
    'promotion_metrics_daily',
    `select=entry_id,market,day,views,clicks,quickbacks,click_sessions,product_view_sessions,cart_sessions,purchase_sessions,revenue_sek,source&market=eq.${marketLocale}&day=gte.${liveFromDate}&day=lte.${liveToDate}`
  );

  // 4. Query AB test metrics
  interface AbMetricRow {
    entry_id: string;
    market: string;
    day: string;
    version: string;
    source?: string;
    views?: number;
    clicks?: number;
    purchase_sessions?: number;
    click_sessions?: number;
  }

  let abMetrics: AbMetricRow[] = [];
  try {
    abMetrics = await select<AbMetricRow>(
      'ab_test_metrics_daily',
      `select=entry_id,market,day,version,source&market=eq.${marketLocale}&day=gte.${liveFromDate}&day=lte.${liveToDate}`
    );
  } catch {
    // optional table
  }

  // Aggregate page metrics
  let totalPageViews = 0;
  let totalUsers = 0;
  let totalPromoViewPageViews = 0;
  const reachedAcc: number[] = [];
  let isSampleData = false;

  for (const pm of pageMetrics) {
    totalPageViews += Number(pm.page_views || 0);
    totalUsers += Number(pm.users || 0);
    totalPromoViewPageViews += Number(pm.promo_view_page_views || 0);
    if (pm.source === 'mock') isSampleData = true;

    if (Array.isArray(pm.reached)) {
      pm.reached.forEach((val, i) => {
        reachedAcc[i] = (reachedAcc[i] || 0) + Number(val || 0);
      });
    }
  }

  // Group promotions by entry_id
  const promoMap = new Map<string, {
    entry_id: string;
    views: number;
    clicks: number;
    quickbacks: number;
    click_sessions: number;
    product_view_sessions: number;
    cart_sessions: number;
    purchase_sessions: number;
    revenue_sek: number;
    dailyCtr: number[];
  }>();

  let totalPromoClicks = 0;
  let totalPromoViews = 0;
  let totalPromoPurchases = 0;
  let totalPromoClickSessions = 0;
  let totalPromoQuickbacks = 0;

  for (const row of promoMetrics) {
    if (row.source === 'mock') isSampleData = true;
    let p = promoMap.get(row.entry_id);
    if (!p) {
      p = {
        entry_id: row.entry_id,
        views: 0,
        clicks: 0,
        quickbacks: 0,
        click_sessions: 0,
        product_view_sessions: 0,
        cart_sessions: 0,
        purchase_sessions: 0,
        revenue_sek: 0,
        dailyCtr: [],
      };
      promoMap.set(row.entry_id, p);
    }
    p.views += Number(row.views || 0);
    p.clicks += Number(row.clicks || 0);
    p.quickbacks += Number(row.quickbacks || 0);
    p.click_sessions += Number(row.click_sessions || 0);
    p.product_view_sessions += Number(row.product_view_sessions || 0);
    p.cart_sessions += Number(row.cart_sessions || 0);
    p.purchase_sessions += Number(row.purchase_sessions || 0);
    p.revenue_sek += Number(row.revenue_sek || 0);
    const dayCtr = row.views ? (Number(row.clicks || 0) / Number(row.views)) * 100 : 0;
    p.dailyCtr.push(dayCtr);

    totalPromoClicks += Number(row.clicks || 0);
    totalPromoViews += Number(row.views || 0);
    totalPromoPurchases += Number(row.purchase_sessions || 0);
    totalPromoClickSessions += Number(row.click_sessions || 0);
    totalPromoQuickbacks += Number(row.quickbacks || 0);
  }

  // Calculate overall rates
  const overallCtr = totalPromoViews > 0 ? totalPromoClicks / totalPromoViews : 0;
  const overallViewRate = totalPageViews > 0 ? totalPromoViewPageViews / totalPageViews : 0;
  const overallConvRate = totalPromoClickSessions > 0 ? totalPromoPurchases / totalPromoClickSessions : 0;
  const overallQbRate = totalPromoClicks > 0 ? totalPromoQuickbacks / totalPromoClicks : 0;

  // Build promotions array
  const promotions = Array.from(promoMap.values()).map((p) => {
    const ctr = p.views > 0 ? p.clicks / p.views : 0;
    const view_rate = totalPageViews > 0 ? p.views / totalPageViews : 0;
    const conv_rate = p.click_sessions > 0 ? p.purchase_sessions / p.click_sessions : 0;
    const quickback_rate = p.clicks > 0 ? p.quickbacks / p.clicks : 0;
    const vs_similar = overallCtr > 0 ? ctr / overallCtr : 1.0;

    let status: 'good' | 'bad' | 'neutral' = 'neutral';
    if (vs_similar >= 1.15) status = 'good';
    else if (vs_similar <= 0.85) status = 'bad';

    const hasAb = abMetrics.some((a) => a.entry_id === p.entry_id);

    return {
      entry_id: p.entry_id,
      title: p.entry_id,
      position_label: 'Promotion block',
      views: p.views,
      clicks: p.clicks,
      quickbacks: p.quickbacks,
      purchases: p.purchase_sessions,
      view_rate,
      ctr,
      conv_rate,
      quickback_rate,
      vs_similar,
      trend: p.dailyCtr.length > 0 ? p.dailyCtr : [ctr * 100],
      status,
      is_ab_test: hasAb,
    };
  });

  // Calculate scroll steps from reached array (bucket_px = 250)
  const bucket_px = 250;
  const totalDepth = Math.max(2000, reachedAcc.length * bucket_px);
  const scroll_data: any[] = [];
  const baseReached = reachedAcc[0] || 1;

  let maxDrop = 0;
  let maxDropIndex = -1;

  for (let i = 0; i <= 20; i++) {
    const depth_px = Math.round((i / 20) * totalDepth);
    const bucketIdx = Math.floor(depth_px / bucket_px);
    const reachedVal = reachedAcc[bucketIdx] || 0;
    const reached_pct = Math.round((reachedVal / baseReached) * 100);
    const prevIdx = i > 0 ? Math.floor(Math.round(((i - 1) / 20) * totalDepth) / bucket_px) : 0;
    const prevVal = reachedAcc[prevIdx] || baseReached;
    const drop_pct = Math.max(0, Math.round(((prevVal - reachedVal) / baseReached) * 100));

    if (drop_pct > maxDrop && i > 0) {
      maxDrop = drop_pct;
      maxDropIndex = i;
    }

    scroll_data.push({
      index: i,
      depth_px,
      reached_pct,
      drop_pct,
      is_fold: depth_px >= 750 && depth_px <= 900,
      is_biggest_drop: false,
    });
  }

  if (maxDropIndex >= 0 && scroll_data[maxDropIndex]) {
    scroll_data[maxDropIndex].is_biggest_drop = true;
  }

  // Check provisional (within 3 days of today)
  const now = new Date();
  const liveToOrNow = period.live_to ? new Date(period.live_to) : now;
  const daysDiff = (now.getTime() - liveToOrNow.getTime()) / (1000 * 3600 * 24);
  const is_provisional = daysDiff < 3;

  // Generate data-driven findings from real data
  const findings: any[] = [];
  for (const promo of promotions) {
    if (promo.status === 'good' && promo.vs_similar >= 1.25) {
      findings.push({
        id: `f-${promo.entry_id}-ctr`,
        metric: 'ctr',
        tone: 'good',
        title: `High click-through rate`,
        promotion_title: promo.title,
        entry_id: promo.entry_id,
        stat: `${(promo.ctr * 100).toFixed(1)}% CTR (+${Math.round((promo.vs_similar - 1) * 100)}% vs average)`,
        comparison: 'Performing well above page average',
      });
    } else if (promo.quickback_rate > 0.12) {
      findings.push({
        id: `f-${promo.entry_id}-qb`,
        metric: 'qb',
        tone: 'bad',
        title: `Elevated quickbacks`,
        promotion_title: promo.title,
        entry_id: promo.entry_id,
        stat: `${(promo.quickback_rate * 100).toFixed(1)}% quickback rate`,
        comparison: 'Visitors returning to startpage quickly',
      });
    }
  }

  return {
    users: totalUsers,
    page_views: totalPageViews,
    ctr: overallCtr,
    ctr_delta: 0,
    view_rate: overallViewRate,
    view_rate_delta: 0,
    conversion_rate: overallConvRate,
    conv_delta: 0,
    quickbacks_rate: overallQbRate,
    qb_delta: 0,
    is_provisional,
    is_sample_data: isSampleData,
    findings,
    scroll_data,
    average_fold_px: 820,
    biggest_drop_px: maxDropIndex >= 0 ? scroll_data[maxDropIndex].depth_px : 1000,
    promotions,
  };
}

export async function getCaptureHealth() {
  ensureCredentials();
  interface HealthRow {
    market: string;
    device: string;
    last_capture_at: string;
    failures_7d: number;
    latest_request_status: string;
  }
  return select<HealthRow>('capture_health', 'select=market,device,last_capture_at,failures_7d,latest_request_status&device=eq.desktop');
}
