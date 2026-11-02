// Schema 1 snapshots (before PER-276): the remote tier carried briefs (codes, states and
// times, no text) beside local-only item lists. A relay that still holds such a row renders
// through `normalize` in lib/snapshot.ts, which maps these onto the schema 2 types.

import type { BrainTool, DetectedEffects, Extension, HealthRow, Runner, Slots, Tier, TrustRule } from "@/types";

export interface V1RunItem {
  code: string;
  state: string;
  repo: string | null;
  ask: string;
  branch: string | null;
  pr_url: string | null;
  pr_number: number | null;
  started_at: string | null;
  ended_at: string | null;
  waiting_reason: string | null;
  last_note: string | null;
  detected?: number;
  remote_url?: string;
}

export interface V1RunBrief {
  code: string;
  state: string;
  scratch: boolean;
  has_pr: boolean;
  started_at: string | null;
  ended_at: string | null;
  waiting_reason: string | null;
}

export interface V1ApprovalItem {
  code: string | null;
  summary: string;
  level: string;
  asked_at: string;
  expires_at: string | null;
  changes?: { summary: string; level: string }[];
}

export interface V1ApprovalBrief {
  level: string;
  effect: string;
  needs_code: boolean;
  asked_at: string;
  expires_at: string | null;
}

export interface V1QuestionItem {
  run_code: string;
  text: string;
  options: string[];
  asked_at: string;
}

export interface V1QuestionBrief {
  run_code: string;
  options: number;
  asked_at: string;
}

export interface V1PulseItem {
  source: string;
  key: string;
  title: string;
  urgency: string;
  state: "new" | "told";
  updated_at: string;
}

export interface V1PulseBrief {
  source: string;
  urgency: string;
  state: "new" | "told";
  updated_at: string;
}

export interface V1BrainToolBrief {
  at: string;
  tool: string;
  decision: string;
  status: "ok" | "error";
  write: boolean;
}

export interface V1Snapshot {
  schema: 1;
  tier: Tier;
  taken_at: string;
  version: string;
  started_at: string;
  last_tick: string | null;
  autonomy: "on" | "off";
  paused: boolean;
  dry_run: boolean;
  health: (Pick<HealthRow, "name" | "state" | "since"> & { reason?: string | null })[];
  tasks: { by_state: Record<string, number> };
  slots: Slots;
  runs: { by_state: Record<string, number>; detected?: DetectedEffects; items?: V1RunItem[]; brief?: V1RunBrief[] };
  approvals: { open: number; items?: V1ApprovalItem[]; brief?: V1ApprovalBrief[] };
  questions: { open: number; items?: V1QuestionItem[]; brief?: V1QuestionBrief[] };
  pulse: { open: number; untold?: number; items?: V1PulseItem[]; brief?: V1PulseBrief[] };
  extensions: (Pick<Extension, "name" | "status" | "health"> & {
    muted_until?: string | null;
    jobs: { id: string; next_due: string | null; last_run_at: string | null; last_outcome: string | null; fail_streak: number }[];
  })[];
  runner: Pick<Runner, "set_up"> & { missing?: string[]; gaps?: number };
  trust_rules?: TrustRule[];
  repos?: { name: string; mode: string; base: string; ci: boolean | null }[];
  claude_brain?: { state: "up" | "down"; turns: number; remote_url?: string; tools?: Omit<BrainTool, "outside">[] };
  brain_activity?: {
    calls_today: number;
    writes_today: number;
    state?: "up" | "down" | null;
    turns?: number;
    recent?: V1BrainToolBrief[];
  };
  trust_rule_count?: number;
  repo_count?: number;
}
