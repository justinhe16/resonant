import type { HealthState, Snapshot } from "@/types";

/** Snapshot is considered stale after this many seconds without a newer `taken_at`. */
export const STALE_AFTER_S = { local: 60, remote: 180 } as const;

export function isSnapshot(raw: unknown): raw is Snapshot {
  if (!raw || typeof raw !== "object") return false;
  const s = raw as Record<string, unknown>;
  return (
    s.schema === 1 &&
    (s.tier === "local" || s.tier === "remote") &&
    typeof s.taken_at === "string" &&
    Array.isArray(s.health)
  );
}

export function assertSnapshot(raw: unknown): Snapshot {
  if (!isSnapshot(raw)) {
    const schema = raw && typeof raw === "object" ? (raw as { schema?: unknown }).schema : undefined;
    throw new Error(
      schema !== undefined && schema !== 1
        ? `Unsupported snapshot schema ${String(schema)}; rebuild the dashboard`
        : "Response is not a dashboard snapshot",
    );
  }
  return raw;
}

const RANK: Record<HealthState, number> = { ok: 0, unknown: 1, degraded: 2, failing: 3 };

export function worstHealth(states: string[]): HealthState {
  let worst: HealthState = "ok";
  for (const s of states) {
    const st = (s in RANK ? s : "unknown") as HealthState;
    if (RANK[st] > RANK[worst]) worst = st;
  }
  return worst;
}

export function sum(by: Record<string, number>, keys?: string[]): number {
  return Object.entries(by)
    .filter(([k]) => !keys || keys.includes(k))
    .reduce((n, [, v]) => n + v, 0);
}

/** Run states in the order a glance wants them: live work first, finished last. */
export const RUN_STATE_ORDER = [
  "running",
  "waiting",
  "queued",
  "ready",
  "failed",
  "merged",
  "done",
  "no_changes",
  "cancelled",
];
export const ACTIVE_RUN_STATES = ["running", "waiting", "queued"];
export const ACTIVE_TASK_STATES = ["queued", "running", "waiting"];

export function ordered(by: Record<string, number>, order: string[]): [string, number][] {
  const rank = (k: string) => {
    const i = order.indexOf(k);
    return i === -1 ? order.length : i;
  };
  return Object.entries(by).sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
}
