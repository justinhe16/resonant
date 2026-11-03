import { describe, expect, it } from "vitest";
import { ageSeconds, duration, localStamp, relative, short } from "./time";

const now = Date.parse("2026-10-16T09:00:00Z");

describe("time", () => {
  it("formats past and future relative times", () => {
    expect(relative("2026-10-16T09:00:02Z", now)).toBe("just now");
    expect(relative("2026-10-16T08:59:15Z", now)).toBe("45s ago");
    expect(relative("2026-10-16T08:48:00Z", now)).toBe("12 min ago");
    expect(relative("2026-10-16T06:00:00Z", now)).toBe("3 h ago");
    expect(relative("2026-10-13T09:00:00Z", now)).toBe("3 d ago");
    expect(relative("2026-10-16T09:05:00Z", now)).toBe("in 5 min");
  });

  it("handles missing and bad timestamps", () => {
    expect(relative(null, now)).toBe("never");
    expect(relative("not a date", now)).toBe("never");
    expect(ageSeconds(undefined, now)).toBeNull();
    expect(duration(null, now)).toBe("unknown");
  });

  it("reads offsets", () => {
    expect(ageSeconds("2026-10-16T11:00:00+02:00", now)).toBe(0);
    expect(duration("2026-10-15T21:00:00Z", now)).toBe("12 h");
  });
});

describe("short and localStamp (PER-279 rows)", () => {
  const now = Date.parse("2026-10-16T09:00:00Z");
  it("says a row's time compactly", () => {
    expect(short("2026-10-16T08:59:58Z", now)).toBe("now");
    expect(short("2026-10-16T08:59:15Z", now)).toBe("45s");
    expect(short("2026-10-16T08:48:00Z", now)).toBe("12m");
    expect(short("2026-10-16T06:00:00Z", now)).toBe("3h");
    expect(short("2026-10-13T09:00:00Z", now)).toBe("3d");
    expect(short("2026-10-16T09:05:00Z", now)).toBe("in 5m");
    expect(short(null, now)).toBe("never");
  });
  it("gives a task's local creation time with its zone", () => {
    expect(localStamp("2026-10-26T16:30:00Z", "America/Los_Angeles")).toBe("9:30am PT, Oct 26");
    expect(localStamp("2026-10-27T01:05:00Z", "America/Los_Angeles")).toBe("6:05pm PT, Oct 26");
    expect(localStamp(null)).toBe("");
    expect(localStamp("not a time")).toBe("");
  });
});
