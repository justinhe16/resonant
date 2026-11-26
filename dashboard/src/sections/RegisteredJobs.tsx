import { CollapsibleSection, HeaderBadge } from "@/components/CollapsibleSection";
import { ListRow, type RowPill } from "@/components/ListRow";
import { short } from "@/lib/time";
import { runTone } from "@/lib/tone";
import type { RegisteredJob, Snapshot } from "@/types";

/** PER-306: the jobs people registered in chat ("every Monday at 9, check flaky CI"). Each
 * firing is one worker run; a row opens its last run when the snapshot still has it. */
export function RegisteredJobs({ snap, now }: { snap: Snapshot; now: number }) {
  const jobs = snap.jobs ?? [];
  const runs = new Set(snap.runs.items.map((r) => r.code));
  return (
    <CollapsibleSection
      id="registered-jobs"
      title="Registered jobs"
      total={jobs.length}
      rows={jobs}
      render={(j) => <RegisteredJobRow key={j.id} job={j} now={now} linked={j.last_run_code !== null && runs.has(j.last_run_code)} />}
      empty="No registered jobs."
      explainer="registered_jobs"
      action={<HeaderBadge n={jobs.filter(failing).length} label="failing" tone="bad" testId="registered-jobs-failing" />}
    />
  );
}

/** Active, and its last firing didn't start or failed (the red pills below). */
const failing = (j: RegisteredJob) => j.state !== "paused" && (!!j.last_error || j.last_run_status === "failed");

function pillOf(j: RegisteredJob): RowPill {
  if (j.state === "paused") return { tone: "neutral", label: "paused", plain: true, title: j.last_error ?? undefined };
  if (j.last_error) return { tone: "bad", label: "didn't start", title: j.last_error };
  if (j.last_run_status) return { tone: runTone(j.last_run_status), label: j.last_run_status, title: j.last_run_code ?? undefined };
  return { tone: "neutral", label: "not run", plain: true };
}

export function RegisteredJobRow({ job: j, now, linked }: { job: RegisteredJob; now: number; linked: boolean }) {
  return (
    <ListRow
      to={linked && j.last_run_code ? { kind: "run", code: j.last_run_code } : undefined}
      refText={j.id}
      title={j.name}
      outside={j.outside.includes("name")}
      pill={pillOf(j)}
      hint={j.next_due ? `next ${short(j.next_due, now)} · ${j.created_by}` : `${j.schedule} · ${j.created_by}`}
      time={j.last_run_at}
      now={now}
      testId="registered-job-row"
    />
  );
}
