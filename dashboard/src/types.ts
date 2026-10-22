// Mirrors the snapshot contract in docs/dashboard-plan.md §3 (built by
// resonant/dashboard/snapshot.py, schema in docs/snapshot.schema.json).
//
// One type covers both tiers. Fields marked "local only" are absent from a `remote`
// snapshot (what leaves the Mini): no message bodies, asks, task contents, repo names or
// URLs. Each local-only `items` list has a `brief` beside it on both tiers (codes, kinds,
// states and times, no text), so the remote page can still list them. The UI hides
// whatever the tier doesn't carry.

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
  /** side effects detected in its commands (TR-1); absent before PER-263 */
  detected?: number;
  remote_url?: string; // RC-1, local tier only: the running session's Remote Control link
}

/** PER-263: side effects detected in the listed runs' commands (TR-1), counts in both tiers. */
export interface DetectedEffects {
  /** by kind: push, merge, release, repo, deploy, publish, http, self, message, mcp, github */
  by_kind: Record<string, number>;
  /** how many were high-impact (a base push, a publish, a delete) */
  high: number;
}

/** A run without its texts: no ask, repo, branch, note or link. Both tiers. */
export interface RunBrief {
  code: string;
  state: string;
  /** no repo */
  scratch: boolean;
  has_pr: boolean;
  started_at: Timestamp | null;
  ended_at: Timestamp | null;
  waiting_reason: string | null;
}

export interface Runs {
  by_state: ByState;
  /** absent in snapshots from before PER-263 */
  detected?: DetectedEffects;
  /** local only */
  items?: RunItem[];
  /** both tiers; absent in snapshots from before it */
  brief?: RunBrief[];
}

/** One change of an approval batch: its line in the batch's prompt and its own level. */
export interface ApprovalChange {
  summary: string;
  level: string;
}

/** What one answer decides: a lone approval, or a batch (PER-254) as one row with the batch's
 * code, its strictest level and its one expiry. */
export interface ApprovalItem {
  /** null for an approval sent without a code */
  code: string | null;
  summary: string;
  level: string;
  asked_at: Timestamp;
  expires_at: Timestamp | null;
  /** a batch's changes, in order; absent for a lone approval (and before PER-254) */
  changes?: ApprovalChange[];
}

/** A pending approval without its summary or code. Both tiers. */
export interface ApprovalBrief {
  level: string;
  effect: "read" | "write" | "pay" | string;
  /** the owner has to type its code to say yes */
  needs_code: boolean;
  asked_at: Timestamp;
  expires_at: Timestamp | null;
}

export interface QuestionItem {
  run_code: string;
  text: string;
  options: string[];
  asked_at: Timestamp;
}

/** A worker question without its text or options. Both tiers. */
export interface QuestionBrief {
  run_code: string;
  /** how many options it offers */
  options: number;
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

/** An open pulse item without its key or title. Both tiers. */
export interface PulseBrief {
  /** "core", the extension, or "other" */
  source: string;
  urgency: Urgency | string;
  state: "new" | "told";
  updated_at: Timestamp;
}

export interface OpenList<T, B> {
  open: number;
  /** local only */
  items?: T[];
  /** both tiers; absent in snapshots from before it */
  brief?: B[];
}

export interface PulseList extends OpenList<PulseItem, PulseBrief> {
  /** PER-263: items whose latest version the owner hasn't heard (muted sources included), in
   * both tiers; absent in snapshots from before PER-263 */
  untold?: number;
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
  /** PER-263: when `/ext mute` ends, while muted; absent in snapshots from before it */
  muted_until?: Timestamp | null;
  jobs: Job[];
}

export interface Runner {
  set_up: boolean;
  /** local only */
  missing?: string[];
  /** how many things are missing (both tiers) */
  gaps?: number;
}

/** One of the Claude brain's tool calls (PER-258). */
export interface BrainTool {
  at: Timestamp;
  tool: string;
  /** allowed (the read list), read, gate, granted, asked, refused, denied, resonant */
  decision: string;
  /** redacted and clipped */
  command: string;
  cwd: string;
  status: "ok" | "error";
  write: boolean;
}

/** BR-11: the Claude brain's long-lived session (local tier only). */
export interface ClaudeBrain {
  state: "up" | "down";
  turns: number;
  /** while Remote Control is on */
  remote_url?: string;
  /** its last tool calls, newest first */
  tools?: BrainTool[];
}

/** A Claude brain tool call without its command or directory. Both tiers. */
export interface BrainToolBrief {
  at: Timestamp;
  /** the tool's kind: "bash", "read", "mcp", ... or "other" */
  tool: string;
  decision: string;
  status: "ok" | "error";
  write: boolean;
}

/** The Claude brain's tool calls today, its session's state and its last calls, briefly. Both tiers. */
export interface BrainActivity {
  calls_today: number;
  writes_today: number;
  /** null before its first start */
  state?: "up" | "down" | null;
  turns?: number;
  recent?: BrainToolBrief[];
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
  approvals: OpenList<ApprovalItem, ApprovalBrief>;
  questions: OpenList<QuestionItem, QuestionBrief>;
  pulse: PulseList;
  extensions: Extension[];
  runner: Runner;
  /** local only */
  trust_rules?: TrustRule[];
  /** local only */
  repos?: Repo[];
  /** local only; absent before the Claude brain first starts */
  claude_brain?: ClaudeBrain;
  /** absent in snapshots from before PER-258 */
  brain_activity?: BrainActivity;
  /** how many trust rules and repos (both tiers; the rules and repos are local only) */
  trust_rule_count?: number;
  repo_count?: number;
}

/** Runtime config read from config.json next to index.html. */
export type DashboardConfig =
  | { source: "local" }
  | { source: "supabase"; url: string; anon_key: string };
