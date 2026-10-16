import { describe, expect, it, vi } from "vitest";
import { loadConfig, parseConfig } from "./config";

const respond = (status: number, body?: unknown) =>
  vi.fn(async () => new Response(body === undefined ? null : JSON.stringify(body), { status }));

describe("config", () => {
  it("reads the local source", async () => {
    const f = respond(200, { source: "local" });
    expect(await loadConfig(f)).toEqual({ source: "local" });
    expect(f).toHaveBeenCalledWith("./config.json", { cache: "no-store" });
  });

  it("reads the supabase source", async () => {
    const cfg = { source: "supabase", url: "https://x.supabase.co", anon_key: "public-anon" };
    expect(await loadConfig(respond(200, cfg))).toEqual(cfg);
  });

  it("falls back to local when config.json is missing", async () => {
    expect(await loadConfig(respond(404))).toEqual({ source: "local" });
  });

  it("rejects bad configs", () => {
    expect(() => parseConfig(null)).toThrow(/not an object/);
    expect(() => parseConfig({ source: "s3" })).toThrow(/unknown source/);
    expect(() => parseConfig({ source: "supabase", url: "http://x", anon_key: "k" })).toThrow(/https/);
    expect(() => parseConfig({ source: "supabase", url: "https://x" })).toThrow(/anon_key/);
  });
});
