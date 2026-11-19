// Stands in for Node's process.env in the modules copied from hindsight-app. The edge runtime
// doesn't let code set environment variables, so the values are handed over here instead.
// Supabase provides these to every edge function, so no key is stored anywhere new.
// Uses the publishable (anon) key on purpose: the same read-only access hindsight-app has. Never the secret key.

function publishableKey(): string {
  const keys = Deno.env.get("SUPABASE_PUBLISHABLE_KEYS");
  if (keys) {
    try {
      const v = Object.values(JSON.parse(keys))[0];
      if (typeof v === "string") return v;
    } catch { /* fall through */ }
  }
  return Deno.env.get("SUPABASE_ANON_KEY") ?? "";
}

export const process = {
  env: {
    SUPABASE_URL: Deno.env.get("SUPABASE_URL") ?? "",
    SUPABASE_PUBLISHABLE_KEY: publishableKey(),
  } as Record<string, string | undefined>,
};
