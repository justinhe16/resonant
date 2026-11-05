import type { Extension, Panel, PanelDay } from "@/types";

/** Fetches one archived day of a panel (PER-286); null when there is none for that day. */
export type DayLoader = (ext: string, panel: string, day: string) => Promise<PanelDay | null>;

/** "Today (live)" in the day picker. */
export const LIVE = "live";

const DAY = /^\d{4}-\d{2}-\d{2}$/;

export const isDay = (s: string) => DAY.test(s);

/** A day's copy, checked just enough to draw: an object with a `views` array. */
export function assertDay(raw: unknown): PanelDay {
  if (!raw || typeof raw !== "object" || !Array.isArray((raw as { views?: unknown }).views)) {
    throw new Error("not a panel day");
  }
  const d = raw as PanelDay;
  return { views: d.views, health: d.health ?? null, truncated: d.truncated === true };
}

/** Reads a day from the daemon's local API on loopback; 404 is "no data that day". */
export async function loadLocalDay(
  ext: string,
  panel: string,
  day: string,
  fetchImpl: typeof fetch = fetch,
): Promise<PanelDay | null> {
  if (!isDay(day)) return null;
  const e = encodeURIComponent;
  const res = await fetchImpl(`/api/ext/${e(ext)}/panels/${e(panel)}/days/${day}`, {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Resonant answered HTTP ${res.status}`);
  return assertDay(await res.json());
}

/** A panel's archived days: on the relay, plus (local tier) those not uploaded yet. */
export const dayList = (p: Panel): string[] => [...(p.history?.days ?? []), ...(p.history?.pending ?? [])];

/** Every archived day of the extension's panels, newest first, without repeats. */
export function archivedDays(ext: Extension): string[] {
  const all = new Set<string>();
  for (const p of ext.panels) for (const d of dayList(p)) if (isDay(d)) all.add(d);
  return [...all].sort().reverse();
}

/** Whether the extension page shows the day picker: some panel has history. */
export const hasHistory = (ext: Extension) => ext.panels.some((p) => p.history != null);

/** "Tue 11/3" for 2026-11-03: the calendar day itself, whatever the viewer's zone. */
export function dayLabel(day: string): string {
  const [y, m, d] = day.split("-").map(Number) as [number, number, number];
  const date = new Date(Date.UTC(y, m - 1, d));
  const weekday = date.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
  return `${weekday} ${m}/${d}`;
}

const pickKey = (ext: string) => `resonant.day.${ext}`;

/** The day last picked on this extension's page in this browser session (else live). */
export function readPick(ext: string): string {
  try {
    const v = window.sessionStorage.getItem(pickKey(ext));
    return v && (v === LIVE || isDay(v)) ? v : LIVE;
  } catch {
    return LIVE;
  }
}

export function savePick(ext: string, day: string): void {
  try {
    window.sessionStorage.setItem(pickKey(ext), day);
  } catch {
    /* not remembered */
  }
}
