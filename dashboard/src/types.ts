// Mirrors the snapshot contract in docs/dashboard-plan.md §3 (built by
// resonant/dashboard/snapshot.py, schema in docs/snapshot.schema.json).
//
// One type covers both tiers. Fields marked "local only" are absent from a `remote`
// snapshot (what leaves the Mini): no message bodies, asks, task contents, repo names or
// URLs. The UI hides whatever the tier doesn't carry.

/** ISO 8601 timestamp, e.g. "2026-10-16T09:30:00+00:00". */
export type Timestamp = string;

export type Tier = "local" | "remote";

export type HealthState = "ok" | "degraded" | "failing" | "unknown";

export interface HealthRow {
  name: string;
  state: HealthState;
  since: Timestamp | null;
  /** local only */
  reason?: string | null;
}

/** Counts keyed by state, e.g. {running: 2, waiting: 1}. */
export type ByState = Record<string, number>;

export interface Slots {
  used: number;
  max: number;
}

export interface RunItem {
  code: string;
  state: string;
  repo: string | null;
  ask: string;
  branch: string | null;
  pr_url: string | null;
  pr_number: number | null;
  started_at: Timestamp | null;
  ended_at: Timestamp | null;
  waiting_reason: string | null;
  last_note: string | null;
}

export interface Runs {
  by_state: ByState;
  /** local only */
  items?: RunItem[];
}

export interface ApprovalItem {
  /** null for an approval sent without a code */
  code: string | null;
  summary: string;
  level: string;
  asked_at: Timestamp;
  expires_at: Timestamp | null;
}

export interface QuestionItem {
  run_code: string;
  text: string;
  options: string[];
  asked_at: Timestamp;
}

export type Urgency = "urgent" | "normal" | "low";

export interface PulseItem {
  /** "core" or the extension that reported it; (source, key) is unique */
  source: string;
  key: string;
  title: string;
  urgency: Urgency | string;
  /** new: the owner hasn't heard this version yet */
  state: "new" | "told";
  updated_at: Timestamp;
}

export interface OpenList<T> {
  open: number;
  /** local only */
  items?: T[];
}

export interface Job {
  id: string;
  next_due: Timestamp | null;
  last_run_at: Timestamp | null;
  last_outcome: string | null;
  fail_streak: number;
}

export interface Extension {
  name: string;
  status: "enabled" | "disabled" | "disabled_tampered";
  health: HealthState | string;
  jobs: Job[];
}

export interface Runner {
  set_up: boolean;
  /** local only */
  missing?: string[];
}

export interface TrustRule {
  n: number;
  text: string;
}

export interface Repo {
  name: string;
  mode: string;
  base: string;
  /** CI seen on the repo; null when not checked yet */
  ci: boolean | null;
}

export interface Snapshot {
  schema: 1;
  tier: Tier;
  taken_at: Timestamp;
  /** git sha, 8 chars */
  version: string;
  started_at: Timestamp;
  last_tick: Timestamp | null;
  autonomy: "on" | "off";
  paused: boolean;
  dry_run: boolean;
  health: HealthRow[];
  tasks: { by_state: ByState };
  slots: Slots;
  runs: Runs;
  approvals: OpenList<ApprovalItem>;
  questions: OpenList<QuestionItem>;
  pulse: OpenList<PulseItem>;
  extensions: Extension[];
  runner: Runner;
  /** local only */
  trust_rules?: TrustRule[];
  /** local only */
  repos?: Repo[];
}

/** Runtime config read from config.json next to index.html. */
export type DashboardConfig =
  | { source: "local" }
  | { source: "supabase"; url: string; anon_key: string };
