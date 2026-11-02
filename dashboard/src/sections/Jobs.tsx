import { CollapsibleSection } from "@/components/CollapsibleSection";
import { JobRow } from "@/sections/Extensions";
import type { Snapshot } from "@/types";

/** Resonant's own scheduled jobs (briefing, pulse, backup, …). */
export function Jobs({ snap, now }: { snap: Snapshot; now: number }) {
  return (
    <CollapsibleSection
      id="jobs"
      title="Jobs"
      total={snap.core_jobs.length}
      rows={snap.core_jobs}
      render={(j) => <JobRow key={j.id} job={j} ext="core" now={now} />}
      empty="No core jobs reported."
      explainer="jobs"
    />
  );
}
