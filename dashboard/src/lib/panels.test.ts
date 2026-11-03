import { describe, expect, it } from "vitest";
import {
  areaPath,
  barRects,
  deltaTone,
  extent,
  formatDelta,
  formatValue,
  fromUnix,
  linePath,
  PLOT,
  points,
  stackLevels,
  statusTone,
  tickIndexes,
} from "./panels";

describe("formatValue", () => {
  it("formats each kind", () => {
    expect(formatValue(1234.5, "number")).toBe("1,235");
    expect(formatValue(3.14159, "number")).toBe("3.14");
    expect(formatValue(2.6, "integer")).toBe("3");
    expect(formatValue(1234.5, "money")).toBe("$1,234.50");
    expect(formatValue(5, "money", { currency: "EUR" })).toBe("€5.00");
    expect(formatValue(12.34, "percent")).toBe("12.3%");
    expect(formatValue(45, "duration")).toBe("45s");
    expect(formatValue(5400, "duration")).toBe("1h 30m");
    expect(formatValue(93784, "duration")).toBe("1d 2h");
    expect(formatValue(512, "bytes")).toBe("512 B");
    expect(formatValue(1536, "bytes")).toBe("1.5 KB");
  });

  it("adds a unit, shows strings as they are and nulls as a dash", () => {
    expect(formatValue(7, "integer", { unit: "PRs" })).toBe("7 PRs");
    expect(formatValue("fine", "number", { unit: "ish" })).toBe("fine ish");
    expect(formatValue(null)).toBe("—");
    expect(formatValue(true)).toBe("yes");
  });

  it("does not throw on a currency it doesn't know", () => {
    expect(formatValue(1, "money", { currency: "ZZZ" })).toContain("1");
  });
});

describe("formatDelta", () => {
  it("signs the change and formats it in its own format", () => {
    expect(formatDelta(1.2, "percent")).toBe("+1.2%");
    expect(formatDelta(-3, "money")).toBe("-$3.00");
    expect(formatDelta(0, "number")).toBe("0");
    expect(deltaTone(2)).toBe("ok");
    expect(deltaTone(-2)).toBe("bad");
    expect(deltaTone(0)).toBe("neutral");
  });
});

describe("tones and time", () => {
  it("maps status states", () => {
    expect(["up", "down", "warn", "unknown"].map(statusTone)).toEqual(["ok", "bad", "warn", "neutral"]);
  });
  it("turns unix seconds into an ISO time", () => {
    expect(fromUnix(1760605200)).toBe("2025-10-16T09:00:00.000Z");
  });
});

describe("series geometry", () => {
  it("breaks a line at a null", () => {
    const pts = points([1, null, 3, 4], [0, 10, 20, 30], [0, 4]);
    const d = linePath(pts);
    expect(d.match(/M/g)).toHaveLength(2);
    expect(d).toContain("L");
  });

  it("draws a lone point as a dash, not nothing", () => {
    expect(linePath(points([null, 2, null], [0, 10, 20], [0, 4]))).toMatch(/^M[\d.]+ [\d.]+ h0\.01$/);
  });

  it("closes one area piece per run of points", () => {
    const pts = points([1, 2, null, 3, 4], [0, 1, 2, 3, 4], [0, 4]);
    expect(areaPath(pts, PLOT.y1).match(/Z/g)).toHaveLength(2);
  });

  it("includes zero for bars and sums a stack", () => {
    expect(extent([{ name: "a", values: [5, 8] }], { zero: true })[0]).toBe(0);
    expect(extent([{ name: "a", values: [1, 2] }, { name: "b", values: [3, null] }], { stacked: true })).toEqual([0, 4]);
    expect(extent([{ name: "a", values: [null] }])).toEqual([0, 1]);
  });

  it("places grouped and stacked bars", () => {
    const s = [
      { name: "a", values: [1, 2] },
      { name: "b", values: [3, null] },
    ];
    const grouped = barRects(s, 2, [0, 4], false);
    expect(grouped).toHaveLength(3); // a null draws nothing
    expect(grouped[0]!.x).toBeLessThan(grouped[1]!.x);
    const stacked = barRects(s, 2, [0, 4], true);
    expect(stacked[0]!.x).toBe(stacked[1]!.x);
    expect(stacked[1]!.y).toBeLessThan(stacked[0]!.y); // b sits on top of a
    expect(stackLevels(s, 2)[1]!.top).toEqual([4, null]);
  });

  it("picks up to six ticks, first and last included", () => {
    expect(tickIndexes(3)).toEqual([0, 1, 2]);
    const t = tickIndexes(240, 6);
    expect(t).toHaveLength(6);
    expect([t[0], t.at(-1)]).toEqual([0, 239]);
    expect(tickIndexes(0)).toEqual([]);
  });
});
