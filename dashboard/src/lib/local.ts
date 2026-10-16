import { assertSnapshot } from "@/lib/snapshot";
import type { Snapshot } from "@/types";

/** Reads the local tier from the daemon on loopback. */
export async function loadLocal(signal?: AbortSignal, fetchImpl: typeof fetch = fetch): Promise<Snapshot> {
  const res = await fetchImpl("/api/dashboard", {
    cache: "no-store",
    headers: { accept: "application/json" },
    signal,
  });
  if (!res.ok) throw new Error(`Resonant answered HTTP ${res.status}`);
  return assertSnapshot(await res.json());
}
