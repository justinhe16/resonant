import type { SupabaseClient } from "@supabase/supabase-js";
import { describe, expect, it, vi } from "vitest";
import { remote } from "@/test/fixtures";
import { wrapSupabase } from "./supabase";

interface FakeOpts {
  session?: boolean;
  aal?: "aal1" | "aal2";
  totp?: { id: string; status: string; factor_type?: string }[];
  row?: unknown;
  rowError?: string;
}

function fake(o: FakeOpts) {
  const totp = (o.totp ?? []).map((f) => ({ factor_type: "totp", ...f }));
  const query = {
    select: vi.fn(() => query),
    order: vi.fn(() => query),
    limit: vi.fn(() => query),
    maybeSingle: vi.fn(async () => ({
      data: o.row ?? null,
      error: o.rowError ? { message: o.rowError } : null,
    })),
  };
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
  const sb = { auth, from: vi.fn(() => query) } as unknown as SupabaseClient;
  return { sb, auth, query };
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

  it("reads the newest snapshot row", async () => {
    const f = fake({ row: { data: remote, taken_at: remote.taken_at } });
    const snap = await wrapSupabase(f.sb).latest();
    expect(snap?.tier).toBe("remote");
    expect(f.sb.from).toHaveBeenCalledWith("snapshots");
    expect(f.query.order).toHaveBeenCalledWith("taken_at", { ascending: false });
    expect(f.query.limit).toHaveBeenCalledWith(1);
  });

  it("returns null with no rows and throws on errors", async () => {
    expect(await wrapSupabase(fake({}).sb).latest()).toBeNull();
    await expect(wrapSupabase(fake({ rowError: "JWT expired" }).sb).latest()).rejects.toThrow("JWT expired");
  });
});
