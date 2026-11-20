import type { SupabaseClient } from "@supabase/supabase-js";
import { type InstanceRef, parseInstances } from "@/lib/instances";
import { assertDay, isDay } from "@/lib/panelDays";
import { assertSnapshot } from "@/lib/snapshot";
import type { Normalized, PanelDay } from "@/types";

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
  /** PER-306: the instances with a row in `snapshot_latest` (personal Resonant first). Empty
   *  when none is listed or the table has no `instance` column yet (before PER-304). */
  instances(): Promise<InstanceRef[]>;
  /** The newest snapshot of `instance` (PER-306); without one, the newest of any row. */
  latest(instance?: string): Promise<Normalized | null>;
  /** PER-286: one archived day of a panel (the `panel_days` row), or null. */
  panelDay(ext: string, panel: string, day: string, instance?: string): Promise<PanelDay | null>;
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
  // The last body per instance ("" for the unfiltered read), refetched only when its time moves.
  const cache = new Map<string, { at: string; snap: Normalized }>();
  const remember = (key: string, row: { data: unknown; taken_at: string }): Normalized => {
    const snap = assertSnapshot(row.data);
    const out = { ...snap, taken_at: snap.taken_at || row.taken_at };
    cache.set(key, { at: row.taken_at, snap: out });
    return out;
  };
  // PER-306: one row per instance in `snapshot_latest`, so every read names its instance;
  // `undefined` (no instance known) reads the newest row, which never errors on two rows.
  const latestOf = (cols: string, instance: string | undefined) => {
    const q = sb.from("snapshot_latest").select(cols);
    return instance === undefined ? q.order("taken_at", { ascending: false }).limit(1) : q.eq("instance", instance);
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
    async instances() {
      // A few bytes per row: the name and the display name inside the stored snapshot.
      const res = await sb.from("snapshot_latest").select("instance, display_name:data->instance->>display_name");
      if (res.error || !Array.isArray(res.data)) return [];
      const rows = (res.data as { instance?: unknown; display_name?: unknown }[]).map((r) => ({
        name: r.instance,
        display_name: r.display_name,
      }));
      const list = parseInstances(rows);
      return list.sort((a, b) => (a.name === "resonant" ? -1 : b.name === "resonant" ? 1 : a.name.localeCompare(b.name)));
    },
    async latest(instance) {
      // `snapshot_latest` holds the full newest snapshot of each instance. Ask for its time
      // first (a few bytes) and fetch the body only when it changed since the last poll.
      const key = instance ?? "";
      const head = check(await latestOf("taken_at", instance).maybeSingle<{ taken_at: string }>());
      if (head) {
        const hit = cache.get(key);
        if (hit && hit.at === head.taken_at) return hit.snap;
        const row = check(
          await latestOf("data, taken_at", instance).maybeSingle<{ data: unknown; taken_at: string }>(),
        );
        if (row) return remember(key, row);
      }
      // Nothing in `snapshot_latest` yet: the newest history row, when it is a whole snapshot
      // (schema 2 history rows are slim: health and counts only, not enough for this page).
      const history = sb.from("snapshots").select("data, taken_at");
      const row = check(
        await (instance === undefined ? history : history.eq("instance", instance))
          .order("taken_at", { ascending: false })
          .limit(1)
          .maybeSingle<{ data: unknown; taken_at: string }>(),
      );
      if (!row) return null;
      if (typeof row.data === "object" && row.data !== null && (row.data as { slim?: unknown }).slim === true)
        return null;
      return remember(key, row);
    },
    async panelDay(ext, panel, day, instance) {
      if (!isDay(day)) return null;
      const q = sb.from("panel_days").select("data").eq("ext", ext).eq("panel_id", panel).eq("day", day);
      const row = check(await (instance === undefined ? q : q.eq("instance", instance)).maybeSingle<{ data: unknown }>());
      return row ? assertDay(row.data) : null;
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
