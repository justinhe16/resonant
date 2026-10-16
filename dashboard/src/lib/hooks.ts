import { useCallback, useEffect, useRef, useState } from "react";
import type { Snapshot } from "@/types";

export const POLL_MS = 10_000;

export type Loader = (signal: AbortSignal) => Promise<Snapshot | null>;

export interface PollState {
  snapshot: Snapshot | null;
  /** Set when the last fetch failed; the previous snapshot is kept. */
  error: string | null;
  /** True until the first fetch settles. */
  loading: boolean;
  refresh: () => void;
}

/** Fetches now, then every `intervalMs`, and again as soon as the tab becomes visible. */
export function usePolling(load: Loader, intervalMs: number = POLL_MS): PollState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
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

/** The current time, re-read every `intervalMs` so relative times keep moving. */
export function useNow(intervalMs: number = POLL_MS): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** Current hash route, e.g. "#/workers" → "workers". */
export function useHashRoute(fallback: string): [string, (r: string) => void] {
  const read = () => window.location.hash.replace(/^#\/?/, "") || fallback;
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(window.location.hash.replace(/^#\/?/, "") || fallback);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, [fallback]);
  const go = useCallback((r: string) => {
    window.location.hash = `/${r}`;
  }, []);
  return [route, go];
}
