import { describe, expect, it } from "vitest";
import { empty, local, remote } from "@/test/fixtures";
import { clone } from "@/test/fixtures";
import { assertSnapshot, forTier, isSnapshot, ordered, RUN_STATE_ORDER, safeHttpsUrl, staleness, sum, worstHealth } from "./snapshot";

describe("snapshot fixtures", () => {
  it("are valid snapshots", () => {
    for (const s of [local, remote, empty]) expect(isSnapshot(s)).toBe(true);
  });

  it("keep remote a strict subset with no text fields (plan §1 D3)", () => {
    expect(remote.tier).toBe("remote");
    for (const h of remote.health) expect(h).not.toHaveProperty("reason");
    expect(remote.runs).not.toHaveProperty("items");
    expect(remote.approvals).not.toHaveProperty("items");
    expect(remote.questions).not.toHaveProperty("items");
    expect(remote.pulse).not.toHaveProperty("items");
    expect(remote.runner).not.toHaveProperty("missing");
    expect(remote).not.toHaveProperty("trust_rules");
    expect(remote).not.toHaveProperty("repos");
    // Every remote key exists in the local tier.
    for (const k of Object.keys(remote)) expect(local).toHaveProperty(k);
  });
});

describe("helpers", () => {
  it("rejects other schemas", () => {
    expect(() => assertSnapshot({ ...local, schema: 2 })).toThrow(/schema 2/);
    expect(() => assertSnapshot("<html>")).toThrow(/Not a valid dashboard snapshot/);
  });

  it("names the first missing or malformed field", () => {
    const noSlots: Record<string, unknown> = { ...local };
    delete noSlots.slots;
    expect(() => assertSnapshot(noSlots)).toThrow("(slots)");
    expect(() => assertSnapshot({ ...local, runs: { state_counts: {} } })).toThrow("(runs.by_state)");
    expect(() => assertSnapshot({ ...local, pulse: { open: "3" } })).toThrow("(pulse.open)");
    expect(() => assertSnapshot({ ...local, extensions: [{ name: "x" }] })).toThrow("(extensions)");
    expect(() => assertSnapshot({ ...local, repos: {} })).toThrow("(repos)");
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

describe("forTier", () => {
  it("strips local-only fields from a remote snapshot that carries them", () => {
    const leaky = clone(local, {
      tier: "remote",
      claude_brain: { state: "up", turns: 3, remote_url: "https://claude.ai/code/x" },
    });
    const out = forTier(leaky);
    expect(out.runs.items).toBeUndefined();
    expect(out.approvals.items).toBeUndefined();
    expect(out.questions.items).toBeUndefined();
    expect(out.pulse.items).toBeUndefined();
    expect(out.runner.missing).toBeUndefined();
    expect(out.trust_rules).toBeUndefined();
    expect(out.repos).toBeUndefined();
    expect(out.claude_brain).toBeUndefined();
    for (const h of out.health) expect(h.reason).toBeUndefined();
    expect(out.approvals.open).toBe(1);
  });

  it("leaves a local snapshot alone", () => {
    expect(forTier(local)).toBe(local);
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
