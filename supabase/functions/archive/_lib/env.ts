// Runs before the copied modules read process.env: gives them the project's read-only key.
// Supabase provides these to every edge function, so no key is stored anywhere new.
// Uses the publishable (anon) key on purpose: the same read-only access hindsight-app has. Never the secret key.
import process from "node:process";

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

process.env.SUPABASE_URL ??= Deno.env.get("SUPABASE_URL") ?? "";
process.env.SUPABASE_PUBLISHABLE_KEY ??= publishableKey();
