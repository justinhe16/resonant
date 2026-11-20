import { describe, expect, it, vi } from "vitest";
import { loadLocalInstances, loadProxied, parseInstances, pickInstance } from "./instances";
import { cori } from "@/test/fixtures";

const res = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

describe("instances", () => {
  it("parses a list: good names once, display names defaulted", () => {
    expect(
      parseInstances([
        { name: "resonant", display_name: "Resonant", self: true },
        { name: "cori" },
        { name: "cori", display_name: "dupe" },
        { name: "Bad Name" },
        "x",
      ]),
    ).toEqual([
      { name: "resonant", display_name: "Resonant", self: true },
      { name: "cori", display_name: "Cori" },
    ]);
    expect(parseInstances(null)).toEqual([]);
  });

  it("picks the remembered one, else personal Resonant", () => {
    const list = parseInstances([{ name: "cori" }, { name: "resonant" }]);
    expect(pickInstance(list, "cori")?.name).toBe("cori");
    expect(pickInstance(list, "gone")?.name).toBe("resonant");
    expect(pickInstance([], null)).toBeNull();
  });

  it("an older daemon or a failure means no list", async () => {
    expect(await loadLocalInstances(vi.fn(async () => new Response("", { status: 404 })))).toEqual([]);
    expect(await loadLocalInstances(vi.fn(async () => Promise.reject(new Error("down"))))).toEqual([]);
  });

  it("loads a proxied snapshot, and says since when it isn't answering", async () => {
    const inst = { name: "cori", display_name: "Cori" };
    const ok = vi.fn(async () => res(cori));
    expect((await loadProxied(inst, undefined, ok)).instance?.name).toBe("cori");
    expect(ok).toHaveBeenCalledWith("/api/instances/cori/dashboard", expect.anything());
    const since = "2026-10-16T08:40:00Z";
    await expect(
      loadProxied(inst, undefined, vi.fn(async () => res({ detail: "cori isn't answering", since }, 502))),
    ).rejects.toThrow(`Cori isn't answering (since ${new Date(since).toLocaleString()})`);
    await expect(
      loadProxied(inst, undefined, vi.fn(async () => res({ detail: "cori answered HTTP 500", since }, 502))),
    ).rejects.toThrow("Cori isn't answering: cori answered HTTP 500");
  });
});
