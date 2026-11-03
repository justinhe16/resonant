import type { SupabaseClient } from "@supabase/supabase-js";
import { assertSnapshot } from "@/lib/snapshot";
import type { Normalized } from "@/types";

export type AuthStage =
  | { kind: "signed_out" }
  | { kind: "mfa_challenge"; factorId: string }
  /** Signed in with a password but no verified TOTP factor. Enrolment is an owner step
   *  done outside this page (go-live), so a stolen password can't add its own factor. */
  | { kind: "mfa_missing" }
  | { kind: "ready" };

/** The slice of Supabase the dashboard uses; tests swap in a fake. */
export interface RemoteClient {
  stage(): Promise<AuthStage>;
  signIn(email: string, password: string): Promise<void>;
  verify(factorId: string, code: string): Promise<void>;
  signOut(): Promise<void>;
  latest(): Promise<Normalized | null>;
  onAuthChange(cb: () => void): () => void;
}

type Result = { data: unknown; error: { message: string } | null };

/** Unwraps a Supabase `{data, error}` result, narrowing to its success branch. */
function check<R extends Result>(r: R): Exclude<R, { error: object }>["data"] {
  if (r.error) throw new Error(r.error.message);
  return r.data as Exclude<R, { error: object }>["data"];
}

export function wrapSupabase(sb: SupabaseClient): RemoteClient {
  const auth = sb.auth;
  let cache: { at: string; snap: Normalized } | null = null;
  const remember = (row: { data: unknown; taken_at: string }): Normalized => {
    const snap = assertSnapshot(row.data);
    const out = { ...snap, taken_at: snap.taken_at || row.taken_at };
    cache = { at: row.taken_at, snap: out };
    return out;
  };
  return {
    async stage() {
      const { session } = check(await auth.getSession());
      if (!session) return { kind: "signed_out" };
      const aal = check(await auth.mfa.getAuthenticatorAssuranceLevel());
      if (aal.currentLevel === "aal2") return { kind: "ready" };
      const factors = check(await auth.mfa.listFactors());
      const totp = factors.totp.find((f) => f.status === "verified");
      return totp ? { kind: "mfa_challenge", factorId: totp.id } : { kind: "mfa_missing" };
    },
    async signIn(email, password) {
      check(await auth.signInWithPassword({ email, password }));
    },
    async verify(factorId, code) {
      check(await auth.mfa.challengeAndVerify({ factorId, code }));
    },
    async signOut() {
      await auth.signOut();
    },
    async latest() {
      // The one-row `snapshot_latest` table holds the full newest snapshot. Ask for its time
      // first (a few bytes) and fetch the body only when it changed since the last poll.
      const head = check(
        await sb.from("snapshot_latest").select("taken_at").maybeSingle<{ taken_at: string }>(),
      );
      if (head) {
        if (cache && cache.at === head.taken_at) return cache.snap;
        const row = check(
          await sb
            .from("snapshot_latest")
            .select("data, taken_at")
            .maybeSingle<{ data: unknown; taken_at: string }>(),
        );
        if (row) return remember(row);
      }
      // Nothing in `snapshot_latest` yet: the newest history row, when it is a whole snapshot
      // (schema 2 history rows are slim: health and counts only, not enough for this page).
      const row = check(
        await sb
          .from("snapshots")
          .select("data, taken_at")
          .order("taken_at", { ascending: false })
          .limit(1)
          .maybeSingle<{ data: unknown; taken_at: string }>(),
      );
      if (!row) return null;
      if (typeof row.data === "object" && row.data !== null && (row.data as { slim?: unknown }).slim === true)
        return null;
      return remember(row);
    },
    onAuthChange(cb) {
      const { data } = auth.onAuthStateChange(() => cb());
      return () => data.subscription.unsubscribe();
    },
  };
}

/** Loaded lazily so the local build never pulls supabase-js into its first chunk. */
export async function createRemoteClient(url: string, anonKey: string): Promise<RemoteClient> {
  const { createClient } = await import("@supabase/supabase-js");
  const sb = createClient(url, anonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: false,
      storageKey: "resonant.auth",
    },
  });
  return wrapSupabase(sb);
}
