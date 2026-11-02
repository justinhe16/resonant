import type {
  V1ApprovalBrief,
  V1ApprovalItem,
  V1PulseBrief,
  V1PulseItem,
  V1QuestionBrief,
  V1QuestionItem,
  V1RunBrief,
  V1RunItem,
  V1Snapshot,
} from "@/lib/legacy";
import { ageSeconds } from "@/lib/time";
import type {
  ApprovalItem,
  ClaudeBrain,
  Extension,
  HealthState,
  Limits,
  Normalized,
  PulseItem,
  QuestionItem,
  RunItem,
  Snapshot,
} from "@/types";

/** Snapshot is considered stale after this many seconds without a newer `taken_at`. */
export const STALE_AFTER_S = { local: 60, remote: 180 } as const;

/** The row limits of a v2 snapshot, used when a v1 one doesn't say. */
export const DEFAULT_LIMITS: Limits = {
  runs: 50,
  run_detail: 20,
  tasks: 50,
  task_detail: 20,
  approvals_recent: 20,
  approvals_pending: 20,
  questions: 20,
  pulse: 60,
  brain_tools: 20,
};

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const isRows = (v: unknown) => Array.isArray(v) && v.every(isObj);
const isCounts = (v: unknown) => isObj(v) && Object.values(v).every((n) => typeof n === "number");

/** Checks shared by schema 1 and 2. */
function commonProblem(s: Obj): string | null {
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
  const detected = s.runs.detected;
  if (detected !== undefined && !(isObj(detected) && isCounts(detected.by_kind) && typeof detected.high === "number"))
    return "runs.detected";
  for (const k of ["approvals", "questions", "pulse"]) {
    const v = s[k];
    if (!isObj(v) || typeof v.open !== "number") return `${k}.open`;
  }
  if (isObj(s.pulse) && s.pulse.untold !== undefined && typeof s.pulse.untold !== "number") return "pulse.untold";
  if (!Array.isArray(s.extensions) || !s.extensions.every((e) => isObj(e) && typeof e.name === "string" && Array.isArray(e.jobs)))
    return "extensions";
  if (!isObj(s.runner) || typeof s.runner.set_up !== "boolean") return "runner";
  if (s.runner.missing !== undefined && !Array.isArray(s.runner.missing)) return "runner.missing";
  if (s.brain_activity !== undefined) {
    const b = s.brain_activity;
    if (!isObj(b) || typeof b.calls_today !== "number" || typeof b.writes_today !== "number") return "brain_activity";
  }
  return null;
}

/**
 * Checks every field the pages read without a guard, so a malformed or older snapshot
 * (or a renamed field) becomes a clear error instead of a crash mid-render. Schema 1 and 2
 * are both accepted (`normalize` maps 1 onto 2). Returns the first problem found, or null.
 */
export function snapshotProblem(raw: unknown): string | null {
  if (!isObj(raw)) return "not an object";
  const s = raw;
  if (s.schema !== 1 && s.schema !== 2) return `unsupported schema ${JSON.stringify(s.schema)}; rebuild the dashboard`;
  const common = commonProblem(s);
  if (common) return common;
  const runs = s.runs as Obj;
  if (s.schema === 1) {
    if (runs.items !== undefined && !Array.isArray(runs.items)) return "runs.items";
    if (runs.brief !== undefined && !isRows(runs.brief)) return "runs.brief";
    for (const k of ["approvals", "questions", "pulse"]) {
      const v = s[k] as Obj;
      if (v.items !== undefined && !Array.isArray(v.items)) return `${k}.items`;
      if (v.brief !== undefined && !isRows(v.brief)) return `${k}.brief`;
    }
    for (const k of ["trust_rules", "repos"]) if (s[k] !== undefined && !Array.isArray(s[k])) return k;
    const b = s.brain_activity;
    if (isObj(b) && b.recent !== undefined && !isRows(b.recent)) return "brain_activity.recent";
    return null;
  }
  if (!isObj(s.limits)) return "limits";
  if (!isRows((s.tasks as Obj).items)) return "tasks.items";
  if (!isRows(runs.items)) return "runs.items";
  for (const k of ["approvals", "questions", "pulse"]) {
    if (!isRows((s[k] as Obj).items)) return `${k}.items`;
  }
  if (!isRows((s.approvals as Obj).recent)) return "approvals.recent";
  if (typeof (s.pulse as Obj).untold !== "number") return "pulse.untold";
  if (!isRows(s.core_jobs)) return "core_jobs";
  for (const k of ["trust_rules", "repos"]) if (!isRows(s[k])) return k;
  if (!(s.extensions as Obj[]).every((e) => Array.isArray(e.tools))) return "extensions";
  if (!Array.isArray((s.runner as Obj).missing)) return "runner.missing";
  if (!isObj(s.brain_activity)) return "brain_activity";
  if (s.claude_brain != null && !(isObj(s.claude_brain) && Array.isArray(s.claude_brain.tools))) return "claude_brain";
  return null;
}

export function isSnapshot(raw: unknown): boolean {
  return snapshotProblem(raw) === null;
}

/** Validates, then maps schema 1 onto schema 2 (see `normalize`). */
export function assertSnapshot(raw: unknown): Normalized {
  const problem = snapshotProblem(raw);
  if (problem === null) return normalize(raw as Snapshot | V1Snapshot);
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

/** Only plain https links leave the page. */
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


const SLUG = /^[\w.-]+\/[\w.-]+$/;

/** A GitHub link (https://github.com only); anything else is dropped. */
export function githubUrl(raw: string | null | undefined): string | null {
  const safe = safeHttpsUrl(raw);
  if (!safe) return null;
  return new URL(safe).hostname === "github.com" ? safe : null;
}

export interface GithubLinks {
  pr: string | null;
  branch: string | null;
  commit: string | null;
}

/**
 * The run's links on GitHub. The PR comes from `pr_url`; the branch and commit are built from
 * the repo's slug, so a run with no GitHub repo (scratch) has none. Only https://github.com.
 */
export function githubLinks(run: Pick<RunItem, "pr_url" | "github_repo" | "branch" | "head_sha">): GithubLinks {
  const slug = run.github_repo && SLUG.test(run.github_repo) ? run.github_repo : null;
  const base = slug ? `https://github.com/${slug}` : null;
  const branch = base && run.branch ? run.branch.split("/").map(encodeURIComponent).join("/") : null;
  const sha = run.head_sha && /^[0-9a-f]{7,40}$/.test(run.head_sha) ? run.head_sha : null;
  return {
    pr: githubUrl(run.pr_url),
    branch: base && branch ? githubUrl(`${base}/tree/${branch}`) : null,
    commit: base && sha ? githubUrl(`${base}/commit/${sha}`) : null,
  };
}

/** Whether `row.outside` lists `field`: the text was written outside Resonant. */
export const isOutside = (row: { outside?: string[] }, field: string) => !!row.outside?.includes(field);

const legacyRun = (r: V1RunItem | V1RunBrief): RunItem => {
  const item = r as Partial<V1RunItem> & V1RunBrief;
  return {
    code: r.code,
    state: r.state,
    waiting_reason: r.waiting_reason,
    started_at: r.started_at,
    ended_at: r.ended_at,
    detected: item.detected ?? 0,
    repo: item.repo ?? "",
    ask: item.ask ?? "",
    branch: item.branch ?? null,
    base_branch: null,
    head_sha: null,
    pr_url: item.pr_url ?? null,
    pr_number: item.pr_number ?? null,
    github_repo: null,
    created_at: r.started_at ?? r.ended_at ?? "",
    ...(item.remote_url && { remote_url: item.remote_url }),
    task_id: null,
    step: "",
    attempts: 0,
    sessions: 0,
    exit_code: null,
    cost_usd: 0,
    tokens_in: 0,
    tokens_out: 0,
    session_seconds: 0,
    wait_seconds: 0,
    files_changed: 0,
    flagged_paths: [],
    summary: null,
    failure: null,
    last_note: item.last_note ?? null,
    did: [],
    effects: [],
    merged: [],
    merged_by: null,
    history: [],
    approval_ids: [],
    question_ids: [],
    linear_ids: [],
    detail: false,
    outside: item.last_note ? ["last_note"] : [],
  };
};

const legacyApproval = (a: V1ApprovalItem | V1ApprovalBrief, i: number): ApprovalItem => {
  const item = a as Partial<V1ApprovalItem> & V1ApprovalBrief;
  const full = item.summary !== undefined;
  return {
    id: `legacy-${i + 1}`,
    member_ids: [],
    ...(item.code ? { code: item.code } : {}),
    summary: item.summary ?? "",
    level: a.level,
    effect: full ? "write" : (item.effect as ApprovalItem["effect"]),
    needs_code: full ? item.code != null : item.needs_code,
    tool: "",
    extension: null,
    status: "pending",
    asked_at: a.asked_at,
    expires_at: a.expires_at,
    decided_at: null,
    decided_by: null,
    task_id: null,
    run_code: null,
    changes: item.changes?.length ? item.changes : null,
    outside: item.summary ? ["summary"] : [],
  };
};

const legacyQuestion = (q: V1QuestionItem | V1QuestionBrief, i: number): QuestionItem => {
  const item = q as Partial<V1QuestionItem> & V1QuestionBrief;
  return {
    id: i + 1,
    run_code: q.run_code,
    kind: "",
    text: item.text ?? "",
    options: Array.isArray(item.options) ? item.options : [],
    urgent: false,
    asked_at: q.asked_at,
    outside: item.text ? ["text", "options"] : [],
  };
};

const legacyPulse = (p: V1PulseItem | V1PulseBrief): PulseItem => {
  const item = p as Partial<V1PulseItem> & V1PulseBrief;
  return {
    source: p.source,
    key: item.key ?? "",
    title: item.title ?? "",
    urgency: p.urgency as PulseItem["urgency"],
    state: p.state,
    updated_at: p.updated_at,
    expires_at: "",
    told_at: null,
    run_code: null,
    outside: item.title ? ["title"] : [],
  };
};

/** The page reads one shape: this maps a schema 1 push onto it (briefs become rows with no
 * detail) and returns a schema 2 snapshot as it is. */
export function normalize(raw: Snapshot | V1Snapshot): Normalized {
  const out = raw.schema === 2 ? raw : fromV1(raw);
  return out.tier === "remote" ? withoutLocalOnly(out) : out;
}

/** The two fields the relay never carries (the Remote Control link and an approval's code):
 * dropped from a remote snapshot even if one slipped through, so they can't be shown. */
function withoutLocalOnly(s: Normalized): Normalized {
  const bare = <T extends object>(row: T, key: string): T =>
    Object.fromEntries(Object.entries(row).filter(([k]) => k !== key)) as T;
  return {
    ...s,
    runs: { ...s.runs, items: s.runs.items.map((r) => bare(r, "remote_url")) },
    approvals: {
      ...s.approvals,
      items: s.approvals.items.map((a) => bare(a, "code")),
      recent: s.approvals.recent.map((a) => bare(a, "code")),
    },
    claude_brain: s.claude_brain ? bare(s.claude_brain, "remote_url") : s.claude_brain,
  };
}

function fromV1(s: V1Snapshot): Normalized {
  const runItems = s.runs.items ?? s.runs.brief ?? [];
  const approvals = s.approvals.items ?? s.approvals.brief ?? [];
  const questions = s.questions.items ?? s.questions.brief ?? [];
  const pulse = s.pulse.items ?? s.pulse.brief ?? [];
  const recent = s.brain_activity?.recent ?? [];
  const brain: ClaudeBrain | null = s.claude_brain
    ? {
        state: s.claude_brain.state,
        turns: s.claude_brain.turns,
        ...(s.claude_brain.remote_url && { remote_url: s.claude_brain.remote_url }),
        tools: (s.claude_brain.tools ?? []).map((t) => ({ ...t, outside: ["command"] })),
      }
    : s.brain_activity?.state
      ? {
          state: s.brain_activity.state,
          turns: s.brain_activity.turns ?? 0,
          tools: recent.map((t) => ({ ...t, command: "", cwd: "", outside: [] })),
        }
      : null;
  const ext = (e: V1Snapshot["extensions"][number]): Extension => ({
    name: e.name,
    status: e.status,
    health: e.health,
    muted_until: e.muted_until ?? null,
    jobs: e.jobs.map((j) => ({ schedule: null, tz: null, effect: null, level: null, ...j })),
    description: null,
    source_kind: "other",
    commit: null,
    installed_at: "",
    tools: [],
    mcp_servers: [],
    secrets: [],
    report_max: null,
    repos: [],
  });
  return {
    schema: 2,
    tier: s.tier,
    taken_at: s.taken_at,
    version: s.version,
    started_at: s.started_at,
    last_tick: s.last_tick,
    autonomy: s.autonomy,
    paused: s.paused,
    dry_run: s.dry_run,
    trimmed: "none",
    limits: DEFAULT_LIMITS,
    health: s.health.map((h) => ({ name: h.name, state: h.state, since: h.since, reason: h.reason ?? null, outside: [] })),
    tasks: { by_state: s.tasks.by_state, items: [] },
    slots: s.slots,
    runs: {
      by_state: s.runs.by_state,
      detected: s.runs.detected ?? { by_kind: {}, high: 0 },
      items: runItems.map(legacyRun),
    },
    approvals: { open: s.approvals.open, items: approvals.map(legacyApproval), recent: [] },
    questions: { open: s.questions.open, items: questions.map(legacyQuestion) },
    pulse: { open: s.pulse.open, untold: s.pulse.untold ?? 0, items: pulse.map(legacyPulse) },
    extensions: s.extensions.map(ext),
    core_jobs: [],
    runner: { set_up: s.runner.set_up, missing: s.runner.missing ?? [], gaps: s.runner.gaps ?? s.runner.missing?.length ?? 0 },
    trust_rules: s.trust_rules ?? [],
    repos: (s.repos ?? []).map((r) => ({ slug: "", ...r })),
    claude_brain: brain,
    brain_activity: {
      calls_today: s.brain_activity?.calls_today ?? 0,
      writes_today: s.brain_activity?.writes_today ?? 0,
      state: s.brain_activity?.state ?? null,
      turns: s.brain_activity?.turns ?? 0,
    },
    trust_rule_count: s.trust_rule_count ?? s.trust_rules?.length ?? 0,
    repo_count: s.repo_count ?? s.repos?.length ?? 0,
    legacy: true,
  };
}

/** What an approval is when its summary isn't known (a schema 1 brief). */
export function approvalTitle(a: ApprovalItem): string {
  if (a.summary) return a.summary;
  const what = a.effect === "pay" ? "A payment" : a.effect === "read" ? "A read" : "A change";
  return a.needs_code ? `${what}, needs its code` : what;
}

/** The approval (pending or decided) with this id or that has it as a member. */
export const findApproval = (snap: Snapshot, id: string): ApprovalItem | undefined =>
  [...snap.approvals.items, ...snap.approvals.recent].find((a) => a.id === id || a.member_ids.includes(id));
