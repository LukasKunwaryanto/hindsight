/**
 * archive: read-only Hindsight API for pages that can't hold a key (claude.ai artifacts).
 *
 * Same routes and answers as hindsight-app's /api (server.ts), served at
 *   https://<project>.supabase.co/functions/v1/archive/api/...
 * The function reads with the project's publishable key from its own environment, so callers need no key.
 * GET only. Ids, markets, dates and entry ids are checked before they reach a query.
 *
 * ALLOWED_ORIGINS (optional function secret): comma-separated origins, or "*". Default: claude.ai artifact
 * origins and localhost. CORS only limits browsers; the data is the same the publishable key already reads.
 */
import { process } from "./_lib/env.ts";
import {
  getMarkets, getSnapshots, getSnapshotHtmlContent, getSnapshotModules, getSnapshotChanges,
  getSnapshotMetrics, getCaptureHealth,
} from "./_lib/dataService.ts";
import {
  getMarketMetrics, getMarketsSummary, getPromotionMarkets, getSnapshotsAt, getSnapshotHeroes,
} from "./_lib/rawData.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const LOCALE = /^[a-z]{2}-[A-Z]{2}$/;
const DEFAULT_ORIGINS = [
  /^https:\/\/([a-z0-9-]+\.)*claudeusercontent\.com$/,
  /^https:\/\/([a-z0-9-]+\.)*claude\.ai$/,
  /^https:\/\/([a-z0-9-]+\.)*claude\.site$/,
  /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/,
];

function allowOrigin(origin: string | null): string | null {
  const conf = (Deno.env.get("ALLOWED_ORIGINS") ?? "").trim();
  if (conf === "*") return "*";
  if (!origin) return null;
  if (conf) return conf.split(",").map((s) => s.trim()).includes(origin) ? origin : null;
  return DEFAULT_ORIGINS.some((re) => re.test(origin)) ? origin : null;
}

function headers(req: Request, type: string, cache?: string): Headers {
  const h = new Headers({ "Content-Type": type, "Vary": "Origin" });
  const o = allowOrigin(req.headers.get("Origin"));
  if (o) {
    h.set("Access-Control-Allow-Origin", o);
    h.set("Access-Control-Allow-Methods", "GET, OPTIONS");
    h.set("Access-Control-Allow-Headers", "Content-Type");
    h.set("Access-Control-Max-Age", "86400");
  }
  if (cache) h.set("Cache-Control", cache);
  return h;
}

const json = (req: Request, body: unknown, status = 200, cache?: string) =>
  new Response(JSON.stringify(body), { status, headers: headers(req, "application/json; charset=utf-8", cache) });
const fail = (req: Request, err: unknown, status = 502) =>
  json(req, { error: (err as Error)?.message || "Can't reach the archive" }, status);
const escapeHtml = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: headers(req, "text/plain") });
  if (req.method !== "GET") return json(req, { error: "Read-only" }, 405);

  const url = new URL(req.url);
  // Path arrives as /archive/api/... (or /functions/v1/archive/api/... locally)
  const path = url.pathname.replace(/^.*?\/archive(?=\/|$)/, "") || "/";
  const q = url.searchParams;
  const market = q.get("market");
  if (market !== null && !LOCALE.test(market)) return json(req, { error: "Invalid market" }, 400);
  const mk = market || "sv-SE";
  const from = q.get("from") ?? "", to = q.get("to") ?? "";

  try {
    if (path === "/" || path === "/api/status") {
      return json(req, process.env.SUPABASE_PUBLISHABLE_KEY ? { ok: true } : { ok: false, error: "The function has no publishable key in its environment." });
    }
    if (path === "/api/markets") return json(req, await getMarkets(), 200, "max-age=300");
    if (path === "/api/snapshots") return json(req, await getSnapshots(mk, q.get("includeSmall") === "true"), 200, "max-age=60");
    if (path === "/api/health") return json(req, await getCaptureHealth(), 200, "max-age=60");
    if (path === "/api/metrics") return json(req, await getMarketMetrics(mk, from, to), 200, "max-age=300");
    if (path === "/api/markets/summary") return json(req, await getMarketsSummary(from, to), 200, "max-age=300");
    if (path === "/api/snapshots/heroes") return json(req, await getSnapshotHeroes(mk), 200, "max-age=300");
    if (path === "/api/snapshots/at") return json(req, await getSnapshotsAt(q.get("at") ?? ""), 200, "max-age=300");

    let m = path.match(/^\/api\/promotion\/([^/]+)\/markets$/);
    if (m) return json(req, await getPromotionMarkets(decodeURIComponent(m[1]), from, to), 200, "max-age=300");

    m = path.match(/^\/api\/snapshot\/([^/]+)\/(html|modules|changes|metrics)$/);
    if (m) {
      const [, id, what] = m;
      if (!UUID.test(id)) return json(req, { error: "Invalid snapshot id" }, 400);
      if (what === "html") {
        try {
          const html = await getSnapshotHtmlContent(id, q.get("at") ?? undefined);
          // Snapshots never change: let browsers keep them.
          return new Response(html, { headers: headers(req, "text/html; charset=utf-8", "max-age=86400") });
        } catch (err) {
          return new Response(`<!doctype html><html><body><h1>Can’t reach the archive</h1><p>${escapeHtml(String((err as Error).message))}</p></body></html>`,
            { status: 502, headers: headers(req, "text/html; charset=utf-8") });
        }
      }
      if (what === "modules") return json(req, await getSnapshotModules(id), 200, "max-age=86400");
      if (what === "changes") return json(req, await getSnapshotChanges(id), 200, "max-age=86400");
      return json(req, await getSnapshotMetrics(id, mk), 200, "max-age=300");
    }
    return json(req, { error: "Not found" }, 404);
  } catch (err) {
    return fail(req, err);
  }
});
