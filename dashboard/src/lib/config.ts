import type { DashboardConfig } from "@/types";

/** Reads config.json next to index.html. A missing file means the local daemon. */
export async function loadConfig(fetchImpl: typeof fetch = fetch): Promise<DashboardConfig> {
  // Only a missing file means local: a network error must not quietly switch a remote
  // deployment over to polling /api/dashboard.
  const res = await fetchImpl("./config.json", { cache: "no-store" });
  if (res.status === 404) return { source: "local" };
  if (!res.ok) throw new Error(`config.json: HTTP ${res.status}`);
  let raw: unknown;
  try {
    raw = await res.json();
  } catch {
    throw new Error("config.json is not valid JSON");
  }
  return parseConfig(raw);
}

export function parseConfig(raw: unknown): DashboardConfig {
  if (!raw || typeof raw !== "object") throw new Error("config.json: not an object");
  const c = raw as Record<string, unknown>;
  if (c.source === "local") return { source: "local" };
  if (c.source === "supabase") {
    if (typeof c.url !== "string" || !/^https:\/\//.test(c.url)) {
      throw new Error("config.json: supabase needs an https url");
    }
    if (typeof c.anon_key !== "string" || !c.anon_key) {
      throw new Error("config.json: supabase needs anon_key");
    }
    return { source: "supabase", url: c.url, anon_key: c.anon_key };
  }
  throw new Error(`config.json: unknown source ${JSON.stringify(c.source)}`);
}
