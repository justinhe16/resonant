import type { Snapshot } from "@/types";
import emptyJson from "./fixtures/empty.json";
import localJson from "./fixtures/local.json";
import remoteJson from "./fixtures/remote.json";

// JSON imports widen literal types; the cast is checked at runtime by the schema tests.
export const local = localJson as unknown as Snapshot;
export const remote = remoteJson as unknown as Snapshot;
export const empty = emptyJson as unknown as Snapshot;

/** Fixtures are taken at 09:00:00Z; this is 20 seconds later. */
export const FIXTURE_NOW = new Date("2026-10-16T09:00:20Z");

export function clone(s: Snapshot, patch: Partial<Snapshot> = {}): Snapshot {
  return { ...structuredClone(s), ...patch };
}
