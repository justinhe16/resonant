import { afterEach, describe, expect, it, vi } from "vitest";
import { local } from "@/test/fixtures";
import { archivedDays, assertDay, dayLabel, hasHistory, LIVE, loadLocalDay, readPick, savePick } from "./panelDays";

const day = { views: [{ id: "v", title: "V", widgets: [{ type: "text", text: "hi" }] }], health: "ok", truncated: false };

describe("panel days", () => {
  afterEach(() => window.sessionStorage.clear());

  it("labels a day by its calendar date, whatever the viewer's zone", () => {
    expect(dayLabel("2026-11-03")).toBe("Tue 11/3");
    expect(dayLabel("2026-01-01")).toBe("Thu 1/1");
  });

  it("lists every panel's days newest first, once", () => {
    const github = local.extensions.find((e) => e.name === "github")!;
    expect(hasHistory(github)).toBe(true);
    expect(archivedDays(github)).toEqual(["2026-10-15", "2026-10-14", "2026-10-12"]);
    const twice = { ...github, panels: github.panels.map((p) => ({ ...p, history: { days: ["2026-10-14", "bad", "2026-10-16"] } })) };
    expect(archivedDays(twice)).toEqual(["2026-10-16", "2026-10-14"]);
    expect(hasHistory({ ...github, panels: github.panels.map((p) => ({ ...p, history: null })) })).toBe(false);
  });

  it("fetches a day from the local API; 404 is no data", async () => {
    const f = vi.fn(async (url: string) =>
      url.endsWith("/2026-10-15") ? new Response(JSON.stringify(day), { status: 200 }) : new Response("", { status: 404 }),
    );
    const got = await loadLocalDay("git hub", "prs", "2026-10-15", f as unknown as typeof fetch);
    expect(f).toHaveBeenCalledWith("/api/ext/git%20hub/panels/prs/days/2026-10-15", expect.anything());
    expect(got?.views[0]?.id).toBe("v");
    expect(await loadLocalDay("github", "prs", "2026-10-14", f as unknown as typeof fetch)).toBeNull();
    expect(await loadLocalDay("github", "prs", "../x", f as unknown as typeof fetch)).toBeNull();
    expect(f).toHaveBeenCalledTimes(2); // a non-day is never fetched
    const err = vi.fn(async () => new Response("", { status: 500 }));
    await expect(loadLocalDay("github", "prs", "2026-10-15", err as unknown as typeof fetch)).rejects.toThrow("HTTP 500");
  });

  it("checks a day's shape", () => {
    expect(assertDay(day).truncated).toBe(false);
    expect(() => assertDay({ views: "x" })).toThrow();
    expect(() => assertDay(null)).toThrow();
  });

  it("remembers the pick for the session only, per extension", () => {
    expect(readPick("github")).toBe(LIVE);
    savePick("github", "2026-10-14");
    expect(readPick("github")).toBe("2026-10-14");
    expect(readPick("linear")).toBe(LIVE);
    window.sessionStorage.setItem("resonant.day.github", "garbage");
    expect(readPick("github")).toBe(LIVE);
    expect(window.localStorage.getItem("resonant.day.github")).toBeNull();
  });
});
