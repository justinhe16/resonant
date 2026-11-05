import { describe, expect, it } from "vitest";
import type { Snapshot } from "@/types";
import { clone, empty, local, remote, remoteV1, remoteV1Raw } from "@/test/fixtures";
import {
  assertSnapshot,
  githubLinks,
  isSnapshot,
  normalize,
  ordered,
  RUN_STATE_ORDER,
  safeHttpsUrl,
  snapshotProblem,
  staleness,
  sum,
  worstHealth,
} from "./snapshot";

describe("snapshot fixtures", () => {
  it("are valid snapshots", () => {
    for (const s of [local, remote, empty, remoteV1Raw]) expect(isSnapshot(s)).toBe(true);
  });

  it("have the same shape on both tiers apart from the local-only fields", () => {
    expect(remote.tier).toBe("remote");
    const expected = structuredClone(local) as Snapshot;
    expected.tier = "remote";
    for (const r of expected.runs.items) delete r.remote_url;
    for (const a of [...expected.approvals.items, ...expected.approvals.recent]) delete a.code;
    delete expected.claude_brain?.remote_url;
    for (const e of expected.extensions)
      for (const p of e.panels) {
        delete p.local_url;
        if (p.history) delete p.history.pending; // PER-286: days the relay doesn't have yet
      }
    expect(remote).toEqual(expected);
    expect(JSON.stringify(remote)).not.toContain("remote_url");
    expect(JSON.stringify(remote)).not.toContain("local_url");
    for (const a of [...remote.approvals.items, ...remote.approvals.recent]) expect(a).not.toHaveProperty("code");
    expect(local.approvals.items[0]).toHaveProperty("code");
  });
});

describe("helpers", () => {
  it("accepts schema 1 and 2 and rejects others", () => {
    expect(snapshotProblem({ ...local, schema: 3 })).toMatch(/unsupported schema 3/);
    expect(() => assertSnapshot({ ...local, schema: 3 })).toThrow(/schema 3/);
    expect(() => assertSnapshot("<html>")).toThrow(/Not a valid dashboard snapshot/);
    expect(snapshotProblem(remoteV1Raw)).toBeNull();
  });

  it("names the first missing or malformed field", () => {
    const noSlots: Record<string, unknown> = { ...local };
    delete noSlots.slots;
    expect(() => assertSnapshot(noSlots)).toThrow("(slots)");
    expect(() => assertSnapshot({ ...local, runs: { state_counts: {} } })).toThrow("(runs.by_state)");
    expect(() => assertSnapshot({ ...local, pulse: { open: "3" } })).toThrow("(pulse.open)");
    expect(() => assertSnapshot({ ...local, extensions: [{ name: "x" }] })).toThrow("(extensions)");
    expect(() => assertSnapshot({ ...local, repos: {} })).toThrow("(repos)");
    expect(() => assertSnapshot({ ...local, pulse: { open: 3, untold: "2" } })).toThrow("(pulse.untold)");
    expect(() => assertSnapshot({ ...local, runs: { by_state: {}, detected: { high: 0 } } })).toThrow("(runs.detected)");
  });

  it("checks the v2 lists", () => {
    const without = (path: [string, string?]) => {
      const c = structuredClone(local) as unknown as Record<string, Record<string, unknown>>;
      if (path[1]) delete c[path[0]]![path[1]];
      else delete (c as Record<string, unknown>)[path[0]];
      return c;
    };
    expect(snapshotProblem(without(["limits"]))).toBe("limits");
    expect(snapshotProblem(without(["tasks", "items"]))).toBe("tasks.items");
    expect(snapshotProblem(without(["runs", "items"]))).toBe("runs.items");
    expect(snapshotProblem(without(["approvals", "recent"]))).toBe("approvals.recent");
    expect(snapshotProblem(without(["questions", "items"]))).toBe("questions.items");
    expect(snapshotProblem(without(["pulse", "items"]))).toBe("pulse.items");
    expect(snapshotProblem(without(["core_jobs"]))).toBe("core_jobs");
    expect(snapshotProblem(without(["trust_rules"]))).toBe("trust_rules");
  });

  it("ranks health", () => {
    expect(worstHealth([])).toBe("ok");
    expect(worstHealth(["ok", "unknown"])).toBe("unknown");
    expect(worstHealth(["ok", "degraded", "unknown"])).toBe("degraded");
    expect(worstHealth(["failing", "degraded"])).toBe("failing");
    expect(worstHealth(["weird"])).toBe("unknown");
  });

  it("sums and orders counts", () => {
    expect(sum({ running: 1, waiting: 2, done: 9 }, ["running", "waiting"])).toBe(3);
    expect(ordered({ done: 1, zeta: 1, running: 2 }, RUN_STATE_ORDER).map(([k]) => k)).toEqual([
      "running",
      "done",
      "zeta",
    ]);
  });
});

describe("normalize", () => {
  it("returns a v2 snapshot as it is", () => {
    expect(normalize(local)).toBe(local);
    expect(assertSnapshot(local)).toBe(local);
    expect(local).not.toHaveProperty("legacy");
  });

  it("maps a v1 remote snapshot's briefs onto v2 rows with no detail", () => {
    const n = remoteV1;
    expect(n.schema).toBe(2);
    expect(n.legacy).toBe(true);
    expect(n.trimmed).toBe("none");
    expect(n.limits.runs).toBe(50);
    expect(n.runs.items.map((r) => r.code)).toEqual(remoteV1Raw.runs.brief!.map((r) => r.code));
    expect(n.runs.items[0]).toMatchObject({ ask: "", repo: "", detail: false, did: [], history: [], github_repo: null });
    expect(n.approvals.items).toHaveLength(remoteV1Raw.approvals.brief!.length);
    expect(n.approvals.items[0]).toMatchObject({ summary: "", effect: "write", needs_code: true, status: "pending" });
    expect(n.approvals.recent).toEqual([]);
    expect(n.questions.items[0]).toMatchObject({ run_code: "R11", text: "", options: [] });
    expect(n.pulse.items).toHaveLength(remoteV1Raw.pulse.brief!.length);
    expect(n.pulse.untold).toBe(2);
    expect(n.tasks).toEqual({ by_state: remoteV1Raw.tasks.by_state, items: [] });
    expect(n.core_jobs).toEqual([]);
    expect(n.runs.detected.high).toBe(1);
    expect(n.extensions[0]).toMatchObject({ tools: [], mcp_servers: [], secrets: [], description: null });
    expect(n.brain_activity).toMatchObject({ calls_today: 4, state: "up", turns: 3 });
    // The brain's last calls become tool rows with no command.
    expect(n.claude_brain?.tools).toHaveLength(2);
    expect(n.claude_brain?.tools[0]).toMatchObject({ tool: "bash", command: "", outside: [] });
  });

  it("fills missing optional v1 parts with empty values", () => {
    const old = structuredClone(remoteV1Raw) as unknown as Record<string, Record<string, unknown>>;
    delete old.runs!.brief;
    delete old.approvals!.brief;
    delete old.pulse!.untold;
    delete old.runs!.detected;
    delete old.brain_activity;
    const n = normalize(old as never);
    expect(n.runs.items).toEqual([]);
    expect(n.approvals.items).toEqual([]);
    expect(n.pulse.untold).toBe(0);
    expect(n.runs.detected).toEqual({ by_kind: {}, high: 0 });
    expect(n.brain_activity).toEqual({ calls_today: 0, writes_today: 0, state: null, turns: 0 });
    expect(n.claude_brain).toBeNull();
  });

  it("maps v1 local items (texts, codes) too", () => {
    const v1 = {
      ...structuredClone(remoteV1Raw),
      tier: "local" as const,
      runs: {
        by_state: { running: 1 },
        items: [
          {
            code: "R1", state: "running", repo: "resonant", ask: "Do it", branch: "b", pr_url: null, pr_number: null,
            started_at: "2026-10-16T08:00:00+00:00", ended_at: null, waiting_reason: null, last_note: "ok",
          },
        ],
      },
      approvals: {
        open: 1,
        items: [{ code: "K7", summary: "merge it", level: "L1", asked_at: "2026-10-16T08:00:00+00:00", expires_at: null }],
      },
    };
    const n = normalize(v1);
    expect(n.runs.items[0]).toMatchObject({ ask: "Do it", repo: "resonant", branch: "b", last_note: "ok" });
    expect(n.approvals.items[0]).toMatchObject({ code: "K7", summary: "merge it", needs_code: true });
  });

  it("drops the local-only fields from a remote snapshot that carries them", () => {
    const leaky = clone(local, { tier: "remote" });
    const n = normalize(leaky);
    expect(n.runs.items.some((r) => "remote_url" in r)).toBe(false);
    expect(n.approvals.items.some((a) => "code" in a)).toBe(false);
    expect(n.claude_brain).not.toHaveProperty("remote_url");
    expect(JSON.stringify(n)).not.toContain("claude.ai/code");
    expect(JSON.stringify(n)).not.toContain("K7Q2");
    // A local snapshot keeps them.
    expect(normalize(local).runs.items[0]).toHaveProperty("remote_url");
    // PER-280: a panel's local_url is stripped from a remote snapshot even if one slipped through
    const leaked = clone(remote);
    leaked.extensions[0]!.panels[0]!.local_url = "http://127.0.0.1:8765/x";
    expect(normalize(leaked).extensions[0]!.panels[0]).not.toHaveProperty("local_url");
    expect(normalize(local).extensions[0]!.panels[0]).toHaveProperty("local_url");
  });
});

describe("staleness", () => {
  const at = (iso: string) => Date.parse(iso);

  it("is fresh for a fresh snapshot with a recent tick", () => {
    expect(staleness(local, at("2026-10-16T09:00:20Z"))).toBeNull();
    expect(staleness(remote, at("2026-10-16T09:02:00Z"))).toBeNull();
  });

  it("flags an old snapshot per tier", () => {
    expect(staleness(local, at("2026-10-16T09:01:30Z"))?.kind).toBe("snapshot");
    expect(staleness(remote, at("2026-10-16T09:01:30Z"))).toBeNull();
    expect(staleness(remote, at("2026-10-16T09:04:00Z"))?.kind).toBe("snapshot");
  });

  it("flags a stalled loop even when the snapshot is new", () => {
    const stuck = clone(local, { last_tick: "2026-10-16T08:55:00+00:00" });
    expect(staleness(stuck, at("2026-10-16T09:00:05Z"))).toEqual({ kind: "loop", seconds: 300 });
  });

  it("measures a loop that never ticked from start", () => {
    const never = clone(local, { last_tick: null, started_at: "2026-10-16T08:50:00+00:00" });
    expect(staleness(never, at("2026-10-16T09:00:05Z"))?.kind).toBe("loop");
  });
});

describe("safeHttpsUrl", () => {
  it("allows only plain https", () => {
    expect(safeHttpsUrl("https://github.com/a/b/pull/1")).toBe("https://github.com/a/b/pull/1");
    expect(safeHttpsUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpsUrl("http://github.com/a")).toBeNull();
    expect(safeHttpsUrl("https://user:pw@github.com/a")).toBeNull();
    expect(safeHttpsUrl("not a url")).toBeNull();
    expect(safeHttpsUrl(null)).toBeNull();
  });
});

describe("githubLinks", () => {
  const base = {
    pr_url: "https://github.com/example/resonant/pull/81",
    github_repo: "example/resonant",
    branch: "resonant/r10-quiet hours",
    head_sha: "9f8e7d6c5b",
  };

  it("builds the PR, branch and commit links on github.com", () => {
    expect(githubLinks(base)).toEqual({
      pr: "https://github.com/example/resonant/pull/81",
      branch: "https://github.com/example/resonant/tree/resonant/r10-quiet%20hours",
      commit: "https://github.com/example/resonant/commit/9f8e7d6c5b",
    });
  });

  it("has no branch or commit link without a GitHub repo", () => {
    expect(githubLinks({ ...base, github_repo: null })).toEqual({ pr: base.pr_url, branch: null, commit: null });
  });

  it("refuses other hosts, schemes and malformed slugs or shas", () => {
    expect(githubLinks({ ...base, pr_url: "https://evil.example/pull/1" }).pr).toBeNull();
    expect(githubLinks({ ...base, pr_url: "javascript:alert(1)" }).pr).toBeNull();
    expect(githubLinks({ ...base, pr_url: "http://github.com/a/b/pull/1" }).pr).toBeNull();
    expect(githubLinks({ ...base, github_repo: "a/b/../../evil" }).branch).toBeNull();
    expect(githubLinks({ ...base, head_sha: "zzzz" }).commit).toBeNull();
  });
});

describe("panels (PER-280)", () => {
  it("defaults panels to [] for a daemon from before it", () => {
    const old = structuredClone(local) as unknown as { extensions: Record<string, unknown>[] };
    for (const e of old.extensions) delete e.panels;
    expect(assertSnapshot(old).extensions.every((e) => Array.isArray(e.panels) && e.panels.length === 0)).toBe(true);
  });

  it("maps a schema 1 push onto extensions with no panels", () => {
    expect(remoteV1.extensions.every((e) => e.panels.length === 0)).toBe(true);
  });
});
