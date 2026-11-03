import { describe, expect, it } from "vitest";
import { type Route, parseHash, toHash } from "./route";

const CASES: [string, Route][] = [
  ["#/runs/R12", { kind: "run", code: "R12" }],
  ["#/runs?state=failed", { kind: "runs", state: "failed" }],
  ["#/runs", { kind: "runs" }],
  ["#/tasks/t_4f1a9c", { kind: "task", id: "t_4f1a9c" }],
  ["#/tasks?state=waiting", { kind: "tasks", state: "waiting" }],
  ["#/effects/push", { kind: "effects", effect: "push" }],
  ["#/approvals/ap_k7q2", { kind: "approval", id: "ap_k7q2" }],
  ["#/questions/7", { kind: "question", id: 7 }],
  ["#/extensions/github", { kind: "extension", name: "github" }],
  ["#/jobs/core/pulse", { kind: "job", ext: "core", id: "pulse" }],
  ["#/pulse/core/run%2FR12", { kind: "pulse", source: "core", key: "run/R12" }],
  ["#/health/ext%3Agithub", { kind: "health", name: "ext:github" }],
  ["#/brain/2026-10-16T08%3A59%3A50%2B00%3A00", { kind: "brain-tool", at: "2026-10-16T08:59:50+00:00" }],
  ["#/diagram/tool%3Aweather%2Fweather-mcp%2Fforecast", { kind: "node", id: "tool:weather/weather-mcp/forecast" }],
];

describe("route", () => {
  it.each(CASES)("parses and rebuilds %s", (hash, route) => {
    expect(parseHash(hash)).toEqual(route);
    expect(toHash(route)).toBe(hash);
  });

  it("ignores hashes that aren't routes (login fragments, junk)", () => {
    for (const h of ["", "#", "#access_token=abc&type=recovery", "#/", "#/nope/1", "#/questions/x", "#/jobs/core", "#/pulse/core"])
      expect(parseHash(h)).toBeNull();
  });

  it("rejects malformed escapes instead of throwing", () => {
    expect(parseHash("#/runs/%E0%A4%A")).toBeNull();
  });

  it("encodes ids so a slash or space can't change the route", () => {
    const r: Route = { kind: "pulse", source: "a b", key: "x/y?z" };
    expect(parseHash(toHash(r))).toEqual(r);
  });
});
