import type { V1Snapshot } from "@/lib/legacy";
import { assertSnapshot } from "@/lib/snapshot";
import type { Snapshot } from "@/types";
import emptyJson from "./fixtures/empty.json";
import localJson from "./fixtures/local.json";
import remoteJson from "./fixtures/remote.json";
import remoteV1Json from "./fixtures/remote-v1.json";

// JSON imports widen literal types; the cast is checked at runtime by the schema tests.
export const local = localJson as unknown as Snapshot;
export const remote = remoteJson as unknown as Snapshot;
export const empty = emptyJson as unknown as Snapshot;
/** A schema 1 remote snapshot as an un-updated daemon pushed it (briefs, no texts). */
export const remoteV1Raw = remoteV1Json as unknown as V1Snapshot;
/** The same, normalized the way the page reads it. */
export const remoteV1 = assertSnapshot(remoteV1Json);

/** Fixtures are taken at 09:00:00Z; this is 20 seconds later. */
export const FIXTURE_NOW = new Date("2026-10-16T09:00:20Z");

export function clone(s: Snapshot, patch: Partial<Snapshot> = {}): Snapshot {
  return { ...structuredClone(s), ...patch };
}
