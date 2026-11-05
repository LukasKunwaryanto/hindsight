// sync-poll: runs every 5 minutes (pg_cron). Asks Contentful what changed since last time,
// works out which markets' startpages are affected, and starts captures on GitHub once
// a market has been quiet for 15 minutes (or has waited 60 minutes in total).
//
// Secrets: CONTENTFUL_DELIVERY_TOKEN, HINDSIGHT_GITHUB_TOKEN (set in the dashboard).
// Called with header x-hindsight-cron, checked against a value only the database knows.

const SPACE = "p7vzp7ftmsr1";
const CF_BASE = `https://cdn.contentful.com/spaces/${SPACE}/environments/master`;
const START_ID = "6q7zaSZcLXtokLbBepLSgo";
const REPO = "LukasKunwaryanto/hindsight";
const WORKFLOW = "capture.yml";
const DEVICES = ["desktop", "mobile"];
const QUIET_MIN = 15;     // wait this long after the last change...
const MAX_WAIT_MIN = 60;  // ...but never longer than this after the first one
const STALE_MIN = 90;     // a started capture with no result after this is marked failed
const PAGE_TYPES = new Set([
  "start", "productListingPage", "collection", "brand", "designer", "editorial", "room",
  "rootCategory", "inspiration", "inspirationArticle", "inspirationCategory", "recipe",
  "storeListing", "store", "search", "checkout", "expresscheckout", "orderConfirmation",
  "AuthorizationPage", "myPagesProfilePage", "myPagesStartPage", "productConfiguration",
  "seoInfo", "ContentTag",
]);

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SB_KEY = (() => {
  const keys = Deno.env.get("SUPABASE_SECRET_KEYS");
  if (keys) {
    try { const v = Object.values(JSON.parse(keys))[0]; if (typeof v === "string") return v; } catch { /* fall back */ }
  }
  return Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
})();
const CF_TOKEN = (Deno.env.get("CONTENTFUL_DELIVERY_TOKEN") ?? "").trim();
const GH_TOKEN = (Deno.env.get("HINDSIGHT_GITHUB_TOKEN") ?? "").trim();

type Row = Record<string, any>;

// ---------- database (PostgREST) ----------
async function db(method: string, path: string, body?: unknown, prefer = "return=minimal"): Promise<any> {
  const headers: Record<string, string> = { apikey: SB_KEY, "Content-Type": "application/json", Prefer: prefer };
  if (SB_KEY.startsWith("eyJ")) headers.Authorization = `Bearer ${SB_KEY}`;
  const r = await fetch(`${SB_URL}/rest/v1/${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  if (!r.ok) throw new Error(`DB ${method} ${path.split("?")[0]} -> ${r.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}
const inList = (ids: string[]) => `in.(${ids.map((i) => `"${i}"`).join(",")})`;
async function dbGetIn(table: string, select: string, col: string, ids: string[]): Promise<Row[]> {
  const out: Row[] = [];
  for (let i = 0; i < ids.length; i += 80) {
    out.push(...await db("GET", `${table}?select=${select}&${col}=${encodeURIComponent(inList(ids.slice(i, i + 80)))}`));
  }
  return out;
}

// ---------- Contentful ----------
async function cf(urlOrPath: string): Promise<any> {
  const url = urlOrPath.startsWith("http") ? urlOrPath : CF_BASE + urlOrPath;
  for (let attempt = 0; attempt < 4; attempt++) {
    const r = await fetch(url, { headers: { Authorization: `Bearer ${CF_TOKEN}` } });
    if (r.status === 429) { await new Promise((s) => setTimeout(s, 1500)); continue; }
    const text = await r.text();
    if (!r.ok) throw new Error(`Contentful ${r.status}: ${text.slice(0, 200)}`);
    return JSON.parse(text);
  }
  throw new Error("Contentful rate limit");
}

async function fetchEntries(ids: string[]): Promise<Row[]> {
  const out: Row[] = [];
  let chunk = 20;
  for (let i = 0; i < ids.length;) {
    const part = ids.slice(i, i + chunk);
    try {
      const res = await cf(`/entries?sys.id[in]=${part.join(",")}&locale=*&include=0&limit=${part.length}`);
      out.push(...(res.items ?? []));
      i += part.length;
    } catch (e) {
      if (String(e).includes("too big") && chunk > 1) { chunk = Math.max(1, chunk >> 1); continue; }
      if (chunk === 1) { i += 1; continue; }  // a single entry too large: skip it
      throw e;
    }
  }
  return out;
}

function linksIn(v: unknown, entries: Set<string>, assets: Set<string>) {
  if (Array.isArray(v)) { for (const x of v) linksIn(x, entries, assets); return; }
  if (v && typeof v === "object") {
    const sys = (v as Row).sys;
    if (sys && sys.type === "Link") {
      if (sys.linkType === "Entry") entries.add(sys.id);
      else if (sys.linkType === "Asset") assets.add(sys.id);
      return;
    }
    for (const x of Object.values(v as Row)) linksIn(x, entries, assets);
  }
}
function linksOf(fields: Row) {
  const e = new Set<string>(), a = new Set<string>();
  linksIn(fields ?? {}, e, a);
  return { links: [...e].sort(), asset_ids: [...a].sort() };
}
// JSON with sorted keys, so values read back from Postgres compare equal to Contentful's
function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v as Row).sort().map((k) => `${JSON.stringify(k)}:${stable((v as Row)[k])}`).join(",")}}`;
  }
  return JSON.stringify(v ?? null);
}
/** One short hash per language of an entry's field values: enough to tell which markets changed. */
async function fingerprints(fields: Row): Promise<Record<string, string>> {
  const per: Record<string, Row> = {};
  for (const [k, v] of Object.entries(fields ?? {})) {
    for (const [loc, val] of Object.entries((v ?? {}) as Row)) (per[loc] ??= {})[k] = val;
  }
  const out: Record<string, string> = {};
  for (const [loc, vals] of Object.entries(per)) {
    const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(stable(vals)));
    out[loc] = [...new Uint8Array(d).slice(0, 8)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  return out;
}
function changedFingerprints(oldF: Record<string, string>, newF: Record<string, string>): Set<string> {
  const out = new Set<string>();
  for (const loc of new Set([...Object.keys(oldF ?? {}), ...Object.keys(newF ?? {})])) if (oldF?.[loc] !== newF?.[loc]) out.add(loc);
  return out;
}
/** Rows written before fingerprints existed: compute them from the stored fields and drop the text. */
async function slimTree(limit = 50): Promise<number> {
  const rows: Row[] = await db("GET", `tree_entries?select=entry_id,fields&fingerprints=eq.{}&fields=not.is.null&limit=${limit}`);
  for (const r of rows) {
    await db("PATCH", `tree_entries?entry_id=eq.${r.entry_id}`,
             { fingerprints: await fingerprints(r.fields), ...(r.entry_id === START_ID ? {} : { fields: null }) });
  }
  return rows.length;
}
function changedLocales(oldF: Row, newF: Row): Set<string> {
  const out = new Set<string>();
  for (const k of new Set([...Object.keys(oldF ?? {}), ...Object.keys(newF ?? {})])) {
    const o = oldF?.[k] ?? {}, n = newF?.[k] ?? {};
    for (const loc of new Set([...Object.keys(o), ...Object.keys(n)])) {
      if (stable(o[loc]) !== stable(n[loc])) out.add(loc);
    }
  }
  return out;
}
function nameOf(fields: Row): string {
  for (const k of ["name", "internalName", "title"]) {
    const v = fields?.[k];
    if (v && typeof v === "object") { const first = Object.values(v)[0]; if (first != null) return String(first).slice(0, 120); }
  }
  return "";
}

let defaultLocale: string | null = null;

// ---------- keep the startpage tree current ----------
async function rebuildTree(): Promise<number> {
  const rows: Row[] = await db("GET", "tree_entries?select=entry_id,content_type,links,markets");
  const byId = new Map(rows.map((r) => [r.entry_id, r]));
  const [start] = await db("GET", `tree_entries?select=fields&entry_id=eq.${START_ID}`);
  const marketsOf = new Map<string, Set<string>>();
  const add = (id: string, locs: Iterable<string>) => {
    const s = marketsOf.get(id) ?? new Set<string>(); for (const l of locs) s.add(l); marketsOf.set(id, s);
  };
  let frontier: string[] = [];
  for (const [loc, blocks] of Object.entries(start?.fields?.content ?? {})) {
    const e = new Set<string>(); linksIn(blocks, e, new Set());
    for (const id of e) { add(id, [loc]); frontier.push(id); }
  }
  const visited = new Set<string>();
  let inserted = 0;
  for (let depth = 0; depth < 8 && frontier.length; depth++) {
    const todo = [...new Set(frontier)].filter((id) => !visited.has(id));
    const missing = todo.filter((id) => !byId.has(id));
    if (missing.length) {  // newly linked entries: fetch and add them to the tree
      const fetched = await fetchEntries(missing);
      const newRows = await Promise.all(fetched.map(async (e) => {
        const ct = e.sys.contentType.sys.id;
        return { entry_id: e.sys.id, content_type: ct, name: nameOf(e.fields), is_page: PAGE_TYPES.has(ct),
                 markets: [], fields: e.sys.id === START_ID ? e.fields : null, fingerprints: await fingerprints(e.fields),
                 ...linksOf(e.fields), revision: e.sys.revision, published_at: e.sys.updatedAt };
      }));
      if (newRows.length) {
        await db("POST", "tree_entries?on_conflict=entry_id", newRows, "return=minimal,resolution=merge-duplicates");
        inserted += newRows.length;
        for (const r of newRows) byId.set(r.entry_id, r);
      }
    }
    const next: string[] = [];
    for (const id of todo) {
      visited.add(id);
      const r = byId.get(id);
      if (!r || PAGE_TYPES.has(r.content_type)) continue;
      for (const child of r.links ?? []) { add(child, marketsOf.get(id) ?? []); next.push(child); }
    }
    frontier = next;
  }
  marketsOf.set(START_ID, new Set(Object.keys(start?.fields?.content ?? {})));
  // save markets that changed (entries no longer reachable get none)
  for (const [id, r] of byId) {
    const want = [...(marketsOf.get(id) ?? [])].sort();
    if (stable(want) !== stable([...(r.markets ?? [])].sort())) {
      await db("PATCH", `tree_entries?entry_id=eq.${id}`, { markets: want, updated_at: new Date().toISOString() });
    }
  }
  return inserted;
}

// ---------- 1. read changes from Contentful ----------
async function poll(out: Row) {
  const [state] = await db("GET", `sync_state?select=next_sync_url&space_id=eq.${SPACE}`);
  if (!state?.next_sync_url) { out.note = "Not set up yet: run the 'Contentful setup' workflow on GitHub once."; return; }

  const items: Row[] = [];
  let url: string = state.next_sync_url, nextSync = "";
  for (let page = 0; page < 50; page++) {
    const res = await cf(url);
    items.push(...(res.items ?? []));
    if (res.nextPageUrl) url = res.nextPageUrl; else { nextSync = res.nextSyncUrl; break; }
  }
  if (!nextSync) throw new Error("Contentful returned more than 50 pages of changes");
  out.items = items.length;

  if (items.length) {
    if (!defaultLocale) defaultLocale = (await cf("/locales")).items.find((l: Row) => l.default)?.code ?? null;
    const entryItems = items.filter((i) => i.sys.type === "Entry" || i.sys.type === "DeletedEntry");
    const assetItems = items.filter((i) => i.sys.type === "Asset" || i.sys.type === "DeletedAsset");
    const rows = entryItems.length
      ? await dbGetIn("tree_entries", "entry_id,content_type,name,is_page,markets,fields,fingerprints,links",
                      "entry_id", [...new Set(entryItems.map((i) => i.sys.id))])
      : [];
    const tree = new Map(rows.map((r) => [r.entry_id, r]));
    const hit = new Set<string>();
    const changes: Row[] = [];
    let treeChanged = false;

    for (const it of entryItems) {
      const r = tree.get(it.sys.id);
      if (!r) continue;  // not on the startpage
      const now = new Date().toISOString();
      if (it.sys.type === "DeletedEntry") {
        changes.push({ entry_id: r.entry_id, content_type: r.content_type, name: r.name, change_type: "unpublish",
                       markets: r.is_page ? [] : r.markets, published_at: it.sys.deletedAt ?? now });
        if (!r.is_page) r.markets.forEach((m: string) => hit.add(m));
        await db("DELETE", `tree_entries?entry_id=eq.${r.entry_id}`);
        treeChanged = true;
        continue;
      }
      const fp = await fingerprints(it.fields);
      const locs = Object.keys(r.fingerprints ?? {}).length
        ? changedFingerprints(r.fingerprints, fp)
        : changedLocales(r.fields, it.fields);  // row not slimmed yet
      if (!locs.size) continue;  // republished without changes
      const ls = linksOf(it.fields);
      if (stable(ls.links) !== stable([...(r.links ?? [])].sort())) treeChanged = true;
      await db("PATCH", `tree_entries?entry_id=eq.${r.entry_id}`, {
        fields: r.entry_id === START_ID ? it.fields : null, fingerprints: fp, ...ls, name: nameOf(it.fields),
        revision: it.sys.revision, published_at: it.sys.updatedAt, updated_at: now,
      });
      let mk: string[] = [];
      if (r.entry_id === START_ID) mk = [...locs];
      else if (!r.is_page) mk = defaultLocale && locs.has(defaultLocale) ? r.markets : r.markets.filter((m: string) => locs.has(m));
      mk.forEach((m) => hit.add(m));
      changes.push({ entry_id: r.entry_id, content_type: r.content_type, name: nameOf(it.fields),
                     change_type: "publish", markets: mk, published_at: it.sys.updatedAt });
    }

    if (assetItems.length) {  // an image replaced inside an asset the startpage uses
      const ids = [...new Set(assetItems.map((i) => i.sys.id))];
      const users: Row[] = await db("GET",
        `tree_entries?select=entry_id,markets,is_page,asset_ids&asset_ids=ov.${encodeURIComponent(`{${ids.join(",")}}`)}`);
      for (const a of assetItems) {
        const mk = new Set<string>();
        for (const u of users) {
          if (!u.is_page && u.asset_ids.includes(a.sys.id)) u.markets.forEach((m: string) => mk.add(m));
        }
        if (!mk.size) continue;
        mk.forEach((m) => hit.add(m));
        changes.push({ entry_id: a.sys.id, content_type: "asset", name: nameOf(a.fields ?? {}),
                       change_type: a.sys.type === "Asset" ? "publish" : "unpublish", markets: [...mk],
                       published_at: a.sys.updatedAt ?? a.sys.deletedAt });
      }
    }

    if (treeChanged) out.new_tree_entries = await rebuildTree();
    if (changes.length) await db("POST", "content_changes", changes);
    out.changes = changes.length;

    // 2. one waiting capture request per affected market and device
    const enabled = new Set((await db("GET", "markets?select=locale&enabled=eq.true")).map((m: Row) => m.locale));
    const markets = [...hit].filter((m) => enabled.has(m)).sort();
    out.markets = markets;
    if (markets.length) {
      const now = new Date().toISOString();
      const waiting: Row[] = await db("GET",
        `capture_requests?select=id,market,device&status=eq.waiting&market=${encodeURIComponent(inList(markets))}`);
      const have = new Set(waiting.map((w) => `${w.market}|${w.device}`));
      if (waiting.length) {
        await db("PATCH", `capture_requests?id=${encodeURIComponent(`in.(${waiting.map((w) => w.id).join(",")})`)}`,
                 { last_change_at: now });
      }
      const fresh = markets.flatMap((m) => DEVICES.filter((d) => !have.has(`${m}|${d}`))
        .map((d) => ({ market: m, device: d, reason: "contentful", first_change_at: now, last_change_at: now })));
      if (fresh.length) await db("POST", "capture_requests", fresh);
    }
  }

  // save the new position only after everything above succeeded
  await db("PATCH", `sync_state?space_id=eq.${SPACE}`,
           { next_sync_url: nextSync, last_polled_at: new Date().toISOString(), last_error: null, updated_at: new Date().toISOString() });
}

// ---------- 3. start captures that are due ----------
async function dispatchDue(out: Row) {
  const now = Date.now();
  const staleBefore = new Date(now - STALE_MIN * 60_000).toISOString();
  await db("PATCH", `capture_requests?status=eq.started&started_at=lt.${staleBefore}`,
           { status: "failed", finished_at: new Date().toISOString(), error: `No result within ${STALE_MIN} minutes` });

  const waiting: Row[] = await db("GET", "capture_requests?select=id,market,first_change_at,last_change_at&status=eq.waiting");
  const due = waiting.filter((w) =>
    now - Date.parse(w.last_change_at) >= QUIET_MIN * 60_000 || now - Date.parse(w.first_change_at) >= MAX_WAIT_MIN * 60_000);
  if (!due.length) return;
  const markets = [...new Set(due.map((d) => d.market))].sort();
  // a market's desktop and mobile requests always go together
  const ids = waiting.filter((w) => markets.includes(w.market)).map((w) => w.id);

  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hindsight-sync-poll" },
    body: JSON.stringify({ ref: "main", inputs: { markets: markets.join(","), devices: DEVICES.join(","), reason: "contentful" } }),
  });
  if (!r.ok) throw new Error(`GitHub dispatch ${r.status}: ${(await r.text()).slice(0, 200)}`);
  await db("PATCH", `capture_requests?id=${encodeURIComponent(`in.(${ids.join(",")})`)}`,
           { status: "started", started_at: new Date().toISOString() });
  out.dispatched = markets;
}

Deno.serve(async (req) => {
  const ok = await db("POST", "rpc/hindsight_cron_ok", { secret: req.headers.get("x-hindsight-cron") ?? "" }, "return=representation");
  if (ok !== true) return new Response("Forbidden", { status: 403 });

  const out: Row = {};
  const errors: string[] = [];
  try { await poll(out); } catch (e) { errors.push(String(e)); }
  try { await dispatchDue(out); } catch (e) { errors.push(String(e)); }
  try { const n = await slimTree(); if (n) out.slimmed = n; } catch (e) { errors.push(String(e)); }
  if (errors.length) {
    out.errors = errors;
    await db("PATCH", `sync_state?space_id=eq.${SPACE}`,
             { last_error: errors.join(" | ").slice(0, 1000), last_polled_at: new Date().toISOString() }).catch(() => {});
  }
  return new Response(JSON.stringify(out), { status: errors.length ? 500 : 200, headers: { "Content-Type": "application/json" } });
});
