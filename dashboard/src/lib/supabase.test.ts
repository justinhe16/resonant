import type { SupabaseClient } from "@supabase/supabase-js";
import { describe, expect, it, vi } from "vitest";
import { remote, remoteV1Raw } from "@/test/fixtures";
import { wrapSupabase } from "./supabase";

type Row = { data: unknown; taken_at: string };

interface FakeOpts {
  session?: boolean;
  aal?: "aal1" | "aal2";
  totp?: { id: string; status: string; factor_type?: string }[];
  /** the history table's newest row */
  row?: Row;
  /** the one-row snapshot_latest table */
  latest?: Row;
  rowError?: string;
}

function fake(o: FakeOpts) {
  const totp = (o.totp ?? []).map((f) => ({ factor_type: "totp", ...f }));
  const queries: Record<string, { cols: string[]; query: Record<string, ReturnType<typeof vi.fn>> }> = {};
  const from = vi.fn((table: string) => {
    const cols: string[] = [];
    const query: Record<string, ReturnType<typeof vi.fn>> = {};
    query.select = vi.fn((c: string) => (cols.push(c), query));
    query.order = vi.fn(() => query);
    query.limit = vi.fn(() => query);
    query.maybeSingle = vi.fn(async () => {
      const row = table === "snapshot_latest" ? o.latest : o.row;
      // A column list of just taken_at returns just that.
      const data = row && cols.at(-1) === "taken_at" ? { taken_at: row.taken_at } : (row ?? null);
      return { data, error: o.rowError ? { message: o.rowError } : null };
    });
    queries[table] = { cols, query };
    return query;
  });
  const auth = {
    getSession: vi.fn(async () => ({ data: { session: o.session ? {} : null }, error: null })),
    signInWithPassword: vi.fn(async () => ({ data: {}, error: null })),
    signOut: vi.fn(async () => ({ error: null })),
    onAuthStateChange: vi.fn(() => ({ data: { subscription: { unsubscribe: vi.fn() } } })),
    mfa: {
      getAuthenticatorAssuranceLevel: vi.fn(async () => ({
        data: { currentLevel: o.aal ?? "aal1", nextLevel: "aal2" },
        error: null,
      })),
      listFactors: vi.fn(async () => ({
        data: { all: totp, totp: totp.filter((f) => f.status === "verified") },
        error: null,
      })),
      challengeAndVerify: vi.fn(async () => ({ data: {}, error: null })),
    },
  };
  const sb = { auth, from } as unknown as SupabaseClient;
  return { sb, auth, from, queries };
}

describe("wrapSupabase", () => {
  it("maps sessions to stages", async () => {
    expect(await wrapSupabase(fake({}).sb).stage()).toEqual({ kind: "signed_out" });
    expect(await wrapSupabase(fake({ session: true, aal: "aal2" }).sb).stage()).toEqual({ kind: "ready" });
    expect(
      await wrapSupabase(fake({ session: true, totp: [{ id: "f1", status: "verified" }] }).sb).stage(),
    ).toEqual({ kind: "mfa_challenge", factorId: "f1" });
    expect(
      await wrapSupabase(fake({ session: true, totp: [{ id: "f0", status: "unverified" }] }).sb).stage(),
    ).toEqual({ kind: "mfa_missing" });
  });

  it("surfaces auth errors", async () => {
    const f = fake({});
    f.auth.signInWithPassword.mockResolvedValueOnce({ data: {}, error: { message: "Invalid login" } } as never);
    await expect(wrapSupabase(f.sb).signIn("a@b.c", "x")).rejects.toThrow("Invalid login");
  });

  it("reads the one-row snapshot_latest table", async () => {
    const f = fake({ latest: { data: remote, taken_at: remote.taken_at } });
    const snap = await wrapSupabase(f.sb).latest();
    expect(snap?.tier).toBe("remote");
    expect(f.from).toHaveBeenCalledWith("snapshot_latest");
    expect(f.from).not.toHaveBeenCalledWith("snapshots");
  });

  it("fetches the body only when taken_at changed", async () => {
    const f = fake({ latest: { data: remote, taken_at: remote.taken_at } });
    const client = wrapSupabase(f.sb);
    const first = await client.latest();
    const second = await client.latest();
    expect(second).toBe(first); // the cached one
    const selects = f.from.mock.results.map((r) => (r.value as { select: { mock: { calls: string[][] } } }).select.mock.calls[0]![0]);
    expect(selects).toEqual(["taken_at", "data, taken_at", "taken_at"]); // body once, time twice
  });

  it("fetches again when a newer snapshot arrives", async () => {
    const o: FakeOpts = { latest: { data: remote, taken_at: remote.taken_at } };
    const f = fake(o);
    const client = wrapSupabase(f.sb);
    await client.latest();
    const newer = { ...remote, taken_at: "2026-10-16T09:01:00+00:00" };
    o.latest = { data: newer, taken_at: newer.taken_at };
    expect((await client.latest())?.taken_at).toBe(newer.taken_at);
  });

  it("falls back to the newest history row when snapshot_latest is empty", async () => {
    const f = fake({ row: { data: remoteV1Raw, taken_at: remoteV1Raw.taken_at } });
    const snap = await wrapSupabase(f.sb).latest();
    expect(snap?.legacy).toBe(true);
    expect(f.from).toHaveBeenCalledWith("snapshots");
    expect(f.queries.snapshots!.query.order).toHaveBeenCalledWith("taken_at", { ascending: false });
    expect(f.queries.snapshots!.query.limit).toHaveBeenCalledWith(1);
  });

  it("treats a slim history row as no snapshot", async () => {
    const slim = { schema: 2, tier: "remote", taken_at: remote.taken_at, slim: true, health: [], counts: {} };
    expect(await wrapSupabase(fake({ row: { data: slim, taken_at: slim.taken_at } }).sb).latest()).toBeNull();
  });

  it("returns null with no rows and throws on errors", async () => {
    expect(await wrapSupabase(fake({}).sb).latest()).toBeNull();
    await expect(wrapSupabase(fake({ rowError: "JWT expired" }).sb).latest()).rejects.toThrow("JWT expired");
  });
});

describe("panelDay", () => {
  function daysFake(row: { data: unknown } | null, error?: string) {
    const eqs: [string, string][] = [];
    const query: Record<string, ReturnType<typeof vi.fn>> = {};
    query.select = vi.fn(() => query);
    query.eq = vi.fn((k: string, v: string) => (eqs.push([k, v]), query));
    query.maybeSingle = vi.fn(async () => ({ data: row, error: error ? { message: error } : null }));
    const from = vi.fn(() => query);
    return { sb: { auth: {}, from } as unknown as SupabaseClient, from, eqs };
  }

  it("reads the panel_days row of that ext, panel and day", async () => {
    const f = daysFake({ data: { views: [], health: "ok", truncated: false } });
    const got = await wrapSupabase(f.sb).panelDay("github", "prs", "2026-10-15");
    expect(f.from).toHaveBeenCalledWith("panel_days");
    expect(f.eqs).toEqual([
      ["ext", "github"],
      ["panel_id", "prs"],
      ["day", "2026-10-15"],
    ]);
    expect(got).toEqual({ views: [], health: "ok", truncated: false });
  });

  it("is null without a row or for a non-day, and throws on an error", async () => {
    expect(await wrapSupabase(daysFake(null).sb).panelDay("github", "prs", "2026-10-15")).toBeNull();
    const f = daysFake(null);
    expect(await wrapSupabase(f.sb).panelDay("github", "prs", "latest")).toBeNull();
    expect(f.from).not.toHaveBeenCalled();
    await expect(wrapSupabase(daysFake(null, "denied").sb).panelDay("github", "prs", "2026-10-15")).rejects.toThrow("denied");
  });
});

describe("instances (PER-306)", () => {
  /** `snapshot_latest` with one row per instance: select, eq, order/limit, maybeSingle, and a
   * plain await for the instance list. */
  function multi(rows: Record<string, Row>, opts: { listError?: string } = {}) {
    const reads: { table: string; cols: string; eqs: [string, string][] }[] = [];
    const from = vi.fn((table: string) => {
      const read = { table, cols: "", eqs: [] as [string, string][] };
      reads.push(read);
      const pick = () => {
        const inst = read.eqs.find(([k]) => k === "instance")?.[1];
        const all = Object.entries(rows);
        const hit = inst === undefined ? all.sort((a, b) => b[1].taken_at.localeCompare(a[1].taken_at))[0] : all.find(([n]) => n === inst);
        return hit ? { name: hit[0], row: hit[1] } : null;
      };
      const query: Record<string, unknown> = {};
      query.select = vi.fn((c: string) => ((read.cols = c), query));
      query.eq = vi.fn((k: string, v: string) => (read.eqs.push([k, v]), query));
      query.order = vi.fn(() => query);
      query.limit = vi.fn(() => query);
      query.maybeSingle = vi.fn(async () => {
        const hit = table === "snapshot_latest" ? pick() : null;
        const data = hit && read.cols === "taken_at" ? { taken_at: hit.row.taken_at } : (hit?.row ?? null);
        return { data, error: null };
      });
      query.then = (resolve: (v: unknown) => unknown) =>
        resolve(
          opts.listError
            ? { data: null, error: { message: opts.listError } }
            : {
                data: Object.entries(rows).map(([name, row]) => ({
                  instance: name,
                  display_name: (row.data as { instance?: { display_name?: string } }).instance?.display_name ?? null,
                })),
                error: null,
              },
        );
      return query;
    });
    return { sb: { auth: {}, from } as unknown as SupabaseClient, reads };
  }

  const coriRow = { data: { ...remote, instance: { name: "cori", display_name: "Cori" } }, taken_at: "2026-10-16T09:00:30Z" };

  it("lists the instances from snapshot_latest, personal Resonant first", async () => {
    const f = multi({ cori: coriRow, resonant: { data: remote, taken_at: remote.taken_at } });
    expect(await wrapSupabase(f.sb).instances()).toEqual([
      { name: "resonant", display_name: "Resonant" },
      { name: "cori", display_name: "Cori" },
    ]);
    expect(f.reads[0]!.cols).toContain("data->instance->>display_name");
  });

  it("is empty when the table has no instance column yet", async () => {
    expect(await wrapSupabase(multi({}, { listError: "column snapshot_latest.instance does not exist" }).sb).instances()).toEqual([]);
  });

  it("reads each instance's own row, with two rows present, and caches per instance", async () => {
    const f = multi({ resonant: { data: remote, taken_at: remote.taken_at }, cori: coriRow });
    const client = wrapSupabase(f.sb);
    expect((await client.latest("resonant"))?.instance?.name ?? "resonant").toBe("resonant");
    expect((await client.latest("cori"))?.instance?.name).toBe("cori");
    expect(f.reads.filter((r) => r.table === "snapshot_latest").every((r) => r.eqs.some(([k]) => k === "instance"))).toBe(true);
    const before = f.reads.length;
    await client.latest("cori");
    expect(f.reads.length - before).toBe(1); // the time only: the body is cached
  });

  it("filters an archived panel day by instance", async () => {
    const eqs: [string, string][] = [];
    const query: Record<string, ReturnType<typeof vi.fn>> = {};
    query.select = vi.fn(() => query);
    query.eq = vi.fn((k: string, v: string) => (eqs.push([k, v]), query));
    query.maybeSingle = vi.fn(async () => ({ data: null, error: null }));
    const sb = { auth: {}, from: vi.fn(() => query) } as unknown as SupabaseClient;
    await wrapSupabase(sb).panelDay("github", "prs", "2026-10-15", "cori");
    expect(eqs).toContainEqual(["instance", "cori"]);
  });
});
