import { ageSeconds } from "@/lib/time";
import type {
  ApprovalBrief,
  BrainActivity,
  BrainToolBrief,
  HealthState,
  PulseBrief,
  QuestionBrief,
  RunBrief,
  Snapshot,
} from "@/types";

/** Snapshot is considered stale after this many seconds without a newer `taken_at`. */
export const STALE_AFTER_S = { local: 60, remote: 180 } as const;

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const isRows = (v: unknown) => Array.isArray(v) && v.every(isObj);
const isCounts = (v: unknown) => isObj(v) && Object.values(v).every((n) => typeof n === "number");

/**
 * Checks every field the pages read without a guard, so a malformed or older snapshot
 * (or a renamed field) becomes a clear error instead of a crash mid-render.
 * Returns the first problem found, or null.
 */
export function snapshotProblem(raw: unknown): string | null {
  if (!isObj(raw)) return "not an object";
  const s = raw;
  if (s.schema !== 1) return `unsupported schema ${JSON.stringify(s.schema)}; rebuild the dashboard`;
  if (s.tier !== "local" && s.tier !== "remote") return "tier";
  for (const k of ["taken_at", "version", "started_at"]) if (typeof s[k] !== "string") return k;
  if (s.last_tick !== null && typeof s.last_tick !== "string") return "last_tick";
  if (s.autonomy !== "on" && s.autonomy !== "off") return "autonomy";
  if (typeof s.paused !== "boolean") return "paused";
  if (typeof s.dry_run !== "boolean") return "dry_run";
  if (!Array.isArray(s.health) || !s.health.every((h) => isObj(h) && typeof h.name === "string" && typeof h.state === "string"))
    return "health";
  if (!isObj(s.tasks) || !isCounts(s.tasks.by_state)) return "tasks.by_state";
  if (!isObj(s.slots) || typeof s.slots.used !== "number" || typeof s.slots.max !== "number") return "slots";
  if (!isObj(s.runs) || !isCounts(s.runs.by_state)) return "runs.by_state";
  if (s.runs.items !== undefined && !Array.isArray(s.runs.items)) return "runs.items";
  if (s.runs.brief !== undefined && !isRows(s.runs.brief)) return "runs.brief";
  const detected = s.runs.detected;
  if (detected !== undefined && !(isObj(detected) && isCounts(detected.by_kind) && typeof detected.high === "number"))
    return "runs.detected";
  for (const k of ["approvals", "questions", "pulse"]) {
    const v = s[k];
    if (!isObj(v) || typeof v.open !== "number") return `${k}.open`;
    if (v.items !== undefined && !Array.isArray(v.items)) return `${k}.items`;
    if (v.brief !== undefined && !isRows(v.brief)) return `${k}.brief`;
  }
  if (isObj(s.pulse) && s.pulse.untold !== undefined && typeof s.pulse.untold !== "number") return "pulse.untold";
  if (!Array.isArray(s.extensions) || !s.extensions.every((e) => isObj(e) && typeof e.name === "string" && Array.isArray(e.jobs)))
    return "extensions";
  if (!isObj(s.runner) || typeof s.runner.set_up !== "boolean") return "runner";
  if (s.runner.missing !== undefined && !Array.isArray(s.runner.missing)) return "runner.missing";
  for (const k of ["trust_rules", "repos"]) if (s[k] !== undefined && !Array.isArray(s[k])) return k;
  if (s.brain_activity !== undefined) {
    const b = s.brain_activity;
    if (!isObj(b) || typeof b.calls_today !== "number" || typeof b.writes_today !== "number") return "brain_activity";
    if (b.recent !== undefined && !isRows(b.recent)) return "brain_activity.recent";
  }
  return null;
}

export function isSnapshot(raw: unknown): raw is Snapshot {
  return snapshotProblem(raw) === null;
}

export function assertSnapshot(raw: unknown): Snapshot {
  const problem = snapshotProblem(raw);
  if (problem === null) return raw as Snapshot;
  throw new Error(
    problem.startsWith("unsupported") ? `Snapshot has an ${problem}` : `Not a valid dashboard snapshot (${problem})`,
  );
}

/** The loop ticks every 30s by default; four missed ticks means it has stalled. */
export const LOOP_STALL_S = 120;

export type Staleness =
  | { kind: "snapshot"; seconds: number }
  | { kind: "loop"; seconds: number };

/**
 * Old data looks fine at a glance, so say so. A remote snapshot is stale when the Mini stops
 * sending; a local one is built per request, so there the sign is a loop that stopped ticking.
 */
export function staleness(snap: Snapshot, now: number): Staleness | null {
  const age = ageSeconds(snap.taken_at, now);
  if (age !== null && age > STALE_AFTER_S[snap.tier]) return { kind: "snapshot", seconds: age };
  const takenAt = Date.parse(snap.taken_at);
  const since = snap.last_tick ?? snap.started_at;
  const tickAge = ageSeconds(since, Number.isNaN(takenAt) ? now : takenAt);
  if (tickAge !== null && tickAge > LOOP_STALL_S) return { kind: "loop", seconds: tickAge };
  return null;
}

/** Extensions muted right now (`/ext mute`), with when the mute ends: a muted reporter's
 * items stay untold, so the Pulse card says whose they are. */
export function mutedExtensions(snap: Snapshot, now: number): { name: string; until: string }[] {
  return snap.extensions.flatMap((e) =>
    e.muted_until && Date.parse(e.muted_until) > now ? [{ name: e.name, until: e.muted_until }] : [],
  );
}

/** Only plain https links leave the page (PR links come from the local tier). */
export function safeHttpsUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    return u.protocol === "https:" && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
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

const runBrief = (r: RunBrief): RunBrief => ({
  code: r.code,
  state: r.state,
  scratch: r.scratch,
  has_pr: r.has_pr,
  started_at: r.started_at,
  ended_at: r.ended_at,
  waiting_reason: r.waiting_reason,
});
const approvalBrief = (a: ApprovalBrief): ApprovalBrief => ({
  level: a.level,
  effect: a.effect,
  needs_code: a.needs_code,
  asked_at: a.asked_at,
  expires_at: a.expires_at,
});
const questionBrief = (q: QuestionBrief): QuestionBrief => ({
  run_code: q.run_code,
  options: q.options,
  asked_at: q.asked_at,
});
const pulseBrief = (p: PulseBrief): PulseBrief => ({
  source: p.source,
  urgency: p.urgency,
  state: p.state,
  updated_at: p.updated_at,
});
const toolBrief = (t: BrainToolBrief): BrainToolBrief => ({
  at: t.at,
  tool: t.tool,
  decision: t.decision,
  status: t.status,
  write: t.write,
});
const activity = (b: BrainActivity): BrainActivity => ({
  calls_today: b.calls_today,
  writes_today: b.writes_today,
  state: b.state,
  turns: b.turns,
  recent: b.recent?.map(toolBrief),
});

/**
 * Drops local-only fields from a remote snapshot, and keeps only the known fields of each
 * brief. The pusher never sends more (D3); this is defence in depth so a mistake there
 * can't put texts on the remote page.
 */
export function forTier(snap: Snapshot): Snapshot {
  if (snap.tier === "local") return snap;
  return {
    ...snap,
    health: snap.health.map((h) => ({ name: h.name, state: h.state, since: h.since })),
    runs: {
      by_state: snap.runs.by_state,
      ...(snap.runs.detected && { detected: snap.runs.detected }),
      brief: snap.runs.brief?.map(runBrief),
    },
    approvals: { open: snap.approvals.open, brief: snap.approvals.brief?.map(approvalBrief) },
    questions: { open: snap.questions.open, brief: snap.questions.brief?.map(questionBrief) },
    pulse: {
      open: snap.pulse.open,
      ...(snap.pulse.untold !== undefined && { untold: snap.pulse.untold }),
      brief: snap.pulse.brief?.map(pulseBrief),
    },
    runner: { set_up: snap.runner.set_up, gaps: snap.runner.gaps },
    trust_rules: undefined,
    repos: undefined,
    claude_brain: undefined,
    brain_activity: snap.brain_activity && activity(snap.brain_activity),
  };
}
