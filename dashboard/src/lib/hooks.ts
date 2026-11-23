import { useCallback, useEffect, useRef, useState } from "react";
import type { Normalized } from "@/types";

export const POLL_MS = 10_000;

export type Loader = (signal: AbortSignal) => Promise<Normalized | null>;

export interface PollState {
  snapshot: Normalized | null;
  /** Set when the last fetch failed; the previous snapshot is kept. */
  error: string | null;
  /** True until the first fetch settles. */
  loading: boolean;
  refresh: () => void;
}

/** Fetches now, then every `intervalMs`, and again as soon as the tab becomes visible. */
export function usePolling(load: Loader, intervalMs: number = POLL_MS): PollState {
  const [snapshot, setSnapshot] = useState<Normalized | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [kick, setKick] = useState(0);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  }, [load]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let ctrl: AbortController | undefined;
    let alive = true;

    const tick = async () => {
      ctrl?.abort();
      ctrl = new AbortController();
      const mine = ctrl;
      try {
        const snap = await loadRef.current(mine.signal);
        if (!alive || mine.signal.aborted) return;
        setSnapshot(snap);
        setError(null);
      } catch (e) {
        if (!alive || mine.signal.aborted) return;
        setError(e instanceof Error ? e.message : String(e));
      }
      setLoading(false);
      clearTimeout(timer);
      // Hidden tabs stop polling; the visibility handler fetches again on return.
      if (document.visibilityState !== "hidden") timer = setTimeout(tick, intervalMs);
    };

    const onVisible = () => {
      if (document.visibilityState === "visible") {
        clearTimeout(timer);
        void tick();
      }
    };

    void tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      clearTimeout(timer);
      ctrl?.abort();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs, kick]);

  const refresh = useCallback(() => setKick((k) => k + 1), []);
  return { snapshot, error, loading, refresh };
}

/** PER-315: the other instances' snapshots are read this often (only the credentials card
 * uses them; the remote client fetches a body only when its `taken_at` moved). */
export const PEER_POLL_MS = 60_000;

/** The newest snapshot of each of `peers` (by name), read now and every `intervalMs`. A failed
 * read keeps that instance's last good one (none at first). */
export function usePeerSnapshots(
  peers: { name: string; load: Loader }[],
  intervalMs: number = PEER_POLL_MS,
): Record<string, Normalized | null> {
  const [snaps, setSnaps] = useState<Record<string, Normalized | null>>({});
  useEffect(() => {
    if (!peers.length) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ctrl = new AbortController();
    let alive = true;
    const tick = async () => {
      const got = await Promise.all(
        peers.map(async (p) => {
          try {
            return [p.name, await p.load(ctrl.signal)] as const;
          } catch {
            return null;
          }
        }),
      );
      if (!alive) return;
      setSnaps((prev) => {
        const next = { ...prev };
        for (const hit of got) if (hit) next[hit[0]] = hit[1];
        return next;
      });
      timer = setTimeout(tick, intervalMs);
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [peers, intervalMs]);
  return snaps;
}

/** The current time, re-read every `intervalMs` so relative times keep moving. */
export function useNow(intervalMs: number = POLL_MS): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
