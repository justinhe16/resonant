// Mirrors the snapshot contract (built by resonant/dashboard/snapshot.py, schema in
// docs/snapshot.schema.json), schema 2.
//
// Both tiers carry the same fields, with free text redacted and clipped. Two fields exist only
// in the `local` tier: `remote_url` (the Remote Control link) and an approval's `code` (the
// second factor the owner types). The UI just doesn't show them when they are absent.
//
// Text written by a worker, an extension or a web page is listed by field name in the
// `outside` array of the row that holds it; the UI renders it as plain text with a badge.
// Schema 1 (briefs, no texts on the remote tier) is in `lib/legacy.ts` and is mapped onto
// these types by `normalize`.

/** ISO 8601 timestamp, e.g. "2026-10-16T09:30:00+00:00". */
export type Timestamp = string;

export type Tier = "local" | "remote";

export type HealthState = "ok" | "degraded" | "failing" | "unknown";

export type Urgency = "urgent" | "normal" | "low";

/** Counts keyed by state, e.g. {running: 2, waiting: 1}. */
export type ByState = Record<string, number>;

/** What the pusher shrank to fit the relay: nothing, history, lists, detail, or every item. */
export type Trimmed = "none" | "history" | "lists" | "detail" | "items";

/** How many rows of each list a snapshot keeps (the UI quotes these). */
export interface Limits {
  runs: number;
  run_detail: number;
  tasks: number;
  task_detail: number;
  approvals_recent: number;
  approvals_pending: number;
  questions: number;
  pulse: number;
  brain_tools: number;
}

export interface HealthRow {
  name: string;
  state: HealthState;
  since: Timestamp | null;
  reason: string | null;
  outside: string[];
}

export interface Slots {
  used: number;
  max: number;
}

/** One audit event; run events have no `run.` prefix (start, side_effect, ready, merged, failed). */
export interface Event {
  at: Timestamp;
  event: string;
}

export type TaskStatus = "queued" | "running" | "waiting" | "done" | "failed" | "cancelled";

export interface TaskItem {
  id: string;
  kind: string;
  runner: string;
  extension: string | null;
  status: TaskStatus;
  wait_reason: string | null;
  wake_at: Timestamp | null;
  attempts: number;
  step_no: number;
  created_at: Timestamp;
  updated_at: Timestamp;
  label: string | null;
  error: string | null;
  run_code: string | null;
  /** only the first `limits.task_detail` tasks carry history */
  history: Event[];
  approval_ids: string[];
  outside: string[];
}

export interface Tasks {
  by_state: ByState;
  items: TaskItem[];
}

/** side effects a run's commands showed (push, merge, release, repo, deploy, publish, http, self, message, mcp, github) */
export type EffectKind = string;

export interface RunEffect {
  kind: EffectKind;
  what: string;
  /** a base push, a publish or a delete */
  high: boolean;
}

export interface RunItem {
  code: string;
  state: string;
  waiting_reason: string | null;
  started_at: Timestamp | null;
  ended_at: Timestamp | null;
  /** how many side effects were detected in its commands */
  detected: number;
  /** "" for a run with no repo (it works in a scratch folder) */
  repo: string;
  ask: string;
  branch: string | null;
  base_branch: string | null;
  head_sha: string | null;
  pr_url: string | null;
  pr_number: number | null;
  /** owner/name of the repo on GitHub; null for scratch runs */
  github_repo: string | null;
  created_at: Timestamp;
  /** local tier only: the running session's Remote Control link */
  remote_url?: string;
  task_id: string | null;
  /** where the run is: prepare, session, harvest, publish or reply */
  step: string;
  attempts: number;
  sessions: number;
  exit_code: number | null;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  session_seconds: number;
  wait_seconds: number;
  files_changed: number;
  flagged_paths: string[];
  summary: string | null;
  failure: string | null;
  last_note: string | null;
  /** what the worker said it did (newest last) */
  did: string[];
  effects: RunEffect[];
  merged: string[];
  merged_by: "resonant" | "run" | null;
  /** oldest first */
  history: Event[];
  approval_ids: string[];
  question_ids: number[];
  /** Linear ids found in the ask and branch, as plain text (no workspace is known) */
  linear_ids: string[];
  /** false for runs past `limits.run_detail`: their texts and history are left out */
  detail: boolean;
  outside: string[];
}

export interface DetectedEffects {
  by_kind: Record<string, number>;
  high: number;
}

export interface Runs {
  by_state: ByState;
  detected: DetectedEffects;
  items: RunItem[];
}

export interface ApprovalChange {
  summary: string;
  level: string;
}

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

/** What one answer decides: a lone approval, or a batch as one row with its members. */
export interface ApprovalItem {
  /** the first member's approval id */
  id: string;
  member_ids: string[];
  /** local tier only: the code the owner types; never in the relay */
  code?: string;
  summary: string;
  level: string;
  effect: "read" | "write" | "pay";
  needs_code: boolean;
  tool: string;
  extension: string | null;
  status: ApprovalStatus;
  asked_at: Timestamp;
  expires_at: Timestamp | null;
  decided_at: Timestamp | null;
  decided_by: string | null;
  task_id: string | null;
  run_code: string | null;
  changes: ApprovalChange[] | null;
  outside: string[];
}

export interface Approvals {
  open: number;
  /** pending */
  items: ApprovalItem[];
  /** decided in the last 7 days, newest first */
  recent: ApprovalItem[];
}

export interface QuestionItem {
  id: number;
  run_code: string;
  kind: string;
  text: string;
  options: string[];
  urgent: boolean;
  asked_at: Timestamp;
  outside: string[];
}

export interface Questions {
  open: number;
  items: QuestionItem[];
}

export interface PulseItem {
  /** "core" or the extension that reported it; (source, key) is unique */
  source: string;
  key: string;
  title: string;
  urgency: Urgency;
  /** new: the owner hasn't heard this version yet */
  state: "new" | "told";
  updated_at: Timestamp;
  expires_at: Timestamp;
  told_at: Timestamp | null;
  run_code: string | null;
  outside: string[];
}

export interface Pulse {
  open: number;
  /** items whose latest version the owner hasn't heard (muted sources included) */
  untold: number;
  items: PulseItem[];
}

export interface Job {
  id: string;
  schedule: string | null;
  tz: string | null;
  effect: string | null;
  level: string | null;
  next_due: Timestamp | null;
  last_run_at: Timestamp | null;
  last_outcome: string | null;
  fail_streak: number;
}

export interface ExtTool {
  name: string;
  via: "cli" | "mcp";
  server: string | null;
  effect: string;
  level: string;
  description: string | null;
}

export interface McpServer {
  id: string;
  transport: "stdio" | "http";
  /** the URL's host only */
  host: string | null;
}

export interface Extension {
  name: string;
  status: "enabled" | "disabled" | "disabled_tampered";
  health: HealthState | string;
  muted_until: Timestamp | null;
  jobs: Job[];
  description: string | null;
  source_kind: "repo" | "path" | "other";
  commit: string | null;
  installed_at: Timestamp;
  tools: ExtTool[];
  mcp_servers: McpServer[];
  /** names only */
  secrets: string[];
  report_max: Urgency | null;
  repos: string[];
}

export interface Runner {
  set_up: boolean;
  missing: string[];
  gaps: number;
}

/** One of the Claude brain's tool calls. */
export interface BrainTool {
  at: Timestamp;
  tool: string;
  /** allowed (the read list), read, gate, granted, asked, refused, denied, resonant, other */
  decision: string;
  command: string;
  cwd: string;
  status: "ok" | "error";
  write: boolean;
  outside: string[];
}

export interface ClaudeBrain {
  state: "up" | "down";
  turns: number;
  /** local tier only; while Remote Control is on */
  remote_url?: string;
  /** newest first */
  tools: BrainTool[];
}

export interface BrainActivity {
  calls_today: number;
  writes_today: number;
  /** null before its first start */
  state: "up" | "down" | null;
  turns: number;
}

export interface TrustRule {
  n: number;
  text: string;
}

export interface Repo {
  name: string;
  slug: string;
  mode: string;
  base: string;
  /** CI seen on the repo; null when not checked yet */
  ci: boolean | null;
}

export interface Snapshot {
  schema: 2;
  tier: Tier;
  taken_at: Timestamp;
  /** git sha, 8 chars */
  version: string;
  started_at: Timestamp;
  last_tick: Timestamp | null;
  autonomy: "on" | "off";
  paused: boolean;
  dry_run: boolean;
  trimmed: Trimmed;
  limits: Limits;
  health: HealthRow[];
  tasks: Tasks;
  slots: Slots;
  runs: Runs;
  approvals: Approvals;
  questions: Questions;
  pulse: Pulse;
  extensions: Extension[];
  core_jobs: Job[];
  runner: Runner;
  trust_rules: TrustRule[];
  repos: Repo[];
  /** absent before the Claude brain first starts */
  claude_brain?: ClaudeBrain | null;
  brain_activity: BrainActivity;
  trust_rule_count: number;
  repo_count: number;
}

/** A normalized snapshot; `legacy` is set when it came from a schema 1 push (no detail). */
export type Normalized = Snapshot & { legacy?: true };

/** Runtime config read from config.json next to index.html. */
export type DashboardConfig =
  | { source: "local" }
  | { source: "supabase"; url: string; anon_key: string };
