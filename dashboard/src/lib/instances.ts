import { loadLocal } from "@/lib/local";
import { assertSnapshot } from "@/lib/snapshot";
import { absolute } from "@/lib/time";
import type { Normalized } from "@/types";

/**
 * PER-306: the Resonant instances this page can show (personal Resonant, Cori). Locally the
 * daemon lists them (`GET /api/instances`: itself first, then its `instances:` config) and
 * proxies each other one's snapshot over loopback (`/api/instances/<name>/dashboard`), so the
 * page stays same-origin. Remotely they are the rows of `snapshot_latest`. One instance (or
 * none known) means no switcher: the page is what it was before.
 */
export interface InstanceRef {
  name: string;
  display_name: string;
  /** locally: the daemon serving this page (read from `/api/dashboard` itself) */
  self?: boolean;
}

/** The choice is remembered for this browser session only. */
export const INSTANCE_KEY = "resonant.instance";

export function readChoice(): string | null {
  try {
    return window.sessionStorage.getItem(INSTANCE_KEY);
  } catch {
    return null;
  }
}

export function writeChoice(name: string) {
  try {
    window.sessionStorage.setItem(INSTANCE_KEY, name);
  } catch {
    /* not remembered */
  }
}

/** The remembered instance when it is still listed, else personal Resonant (or the first). */
export function pickInstance(list: InstanceRef[], remembered: string | null): InstanceRef | null {
  return (
    list.find((i) => i.name === remembered) ??
    list.find((i) => i.self) ??
    list.find((i) => i.name === "resonant") ??
    list[0] ??
    null
  );
}

const NAME = /^[a-z][a-z0-9-]{0,31}$/;

/** Checks a list from the daemon or Supabase: well-formed names, each once. */
export function parseInstances(raw: unknown): InstanceRef[] {
  if (!Array.isArray(raw)) return [];
  const out: InstanceRef[] = [];
  for (const row of raw as unknown[]) {
    if (typeof row !== "object" || row === null) continue;
    const r = row as Record<string, unknown>;
    if (typeof r.name !== "string" || !NAME.test(r.name) || out.some((i) => i.name === r.name)) continue;
    const display =
      typeof r.display_name === "string" && r.display_name.trim()
        ? r.display_name.trim().slice(0, 40)
        : r.name.charAt(0).toUpperCase() + r.name.slice(1);
    out.push({ name: r.name, display_name: display, ...(r.self === true && { self: true }) });
  }
  return out;
}

/** The daemon's instance list; an older daemon (404) or any failure means just this one. */
export async function loadLocalInstances(fetchImpl: typeof fetch = fetch): Promise<InstanceRef[]> {
  try {
    const res = await fetchImpl("/api/instances", { cache: "no-store", headers: { accept: "application/json" } });
    if (!res.ok) return [];
    const body: unknown = await res.json();
    return parseInstances(typeof body === "object" && body !== null ? (body as { instances?: unknown }).instances : null);
  } catch {
    return [];
  }
}

/** Another instance's snapshot through this daemon's loopback proxy. A 502 says since when it
 * hasn't answered: "Cori isn't answering (since 11/20/2026, 9:30:00 AM)". */
export async function loadProxied(
  inst: InstanceRef,
  signal?: AbortSignal,
  fetchImpl: typeof fetch = fetch,
): Promise<Normalized> {
  const res = await fetchImpl(`/api/instances/${encodeURIComponent(inst.name)}/dashboard`, {
    cache: "no-store",
    headers: { accept: "application/json" },
    signal,
  });
  if (res.status === 502) {
    let since = "";
    let detail = "";
    try {
      const body = (await res.json()) as { since?: unknown; detail?: unknown };
      since = typeof body.since === "string" ? absolute(body.since) : "";
      detail = typeof body.detail === "string" ? body.detail : "";
    } catch {
      /* no body: say what we know */
    }
    const what = !detail || detail.endsWith("isn't answering") ? "isn't answering" : `isn't answering: ${detail}`;
    throw new Error(`${inst.display_name} ${what}${since ? ` (since ${since})` : ""}`);
  }
  if (!res.ok) throw new Error(`${inst.display_name}: Resonant answered HTTP ${res.status}`);
  return assertSnapshot(await res.json());
}

/** The loader for one instance on the local page. */
export function localLoaderFor(inst: InstanceRef | null): (signal: AbortSignal) => Promise<Normalized> {
  if (!inst || inst.self) return (signal) => loadLocal(signal);
  return (signal) => loadProxied(inst, signal);
}
