import { describe, expect, it } from "vitest";
import { empty, local, remote } from "@/test/fixtures";
import { assertSnapshot, isSnapshot, ordered, RUN_STATE_ORDER, sum, worstHealth } from "./snapshot";

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
    expect(() => assertSnapshot("<html>")).toThrow(/not a dashboard snapshot/);
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
