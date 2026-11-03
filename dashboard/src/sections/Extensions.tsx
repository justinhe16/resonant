import { CollapsibleSection } from "@/components/CollapsibleSection";
import { ListRow } from "@/components/ListRow";
import { PanelGlance } from "@/components/panel/PanelGlance";
import { absolute } from "@/lib/time";
import { healthTone, outcomeTone } from "@/lib/tone";
import type { Extension, Job, Snapshot } from "@/types";

export function Extensions({ snap, now }: { snap: Snapshot; now: number }) {
  return (
    <CollapsibleSection
      id="extensions"
      title="Extensions"
      total={snap.extensions.length}
      rows={snap.extensions}
      render={(ext) => <ExtensionRow key={ext.name} ext={ext} now={now} />}
      empty="No extensions installed."
      explainer="extensions"
      previewCount={3}
    />
  );
}

/** An extension's one line: name · what it is · health · its newest job run; muted and job
 * counts as the hint. Tools, jobs and the rest are in its dialog. */
function ExtensionRow({ ext, now }: { ext: Extension; now: number }) {
  const muted = !!ext.muted_until && Date.parse(ext.muted_until) > now;
  const last = ext.jobs.map((j) => j.last_run_at).filter((t): t is string => !!t).sort().at(-1) ?? null;
  const failing = ext.jobs.some((j) => j.fail_streak > 0);
  const jobs = `${ext.jobs.length} job${ext.jobs.length === 1 ? "" : "s"}`;
  return (
    <ListRow
      to={{ kind: "extension", name: ext.name }}
      refText={ext.name}
      title={ext.description || `${ext.tools.length} tool${ext.tools.length === 1 ? "" : "s"}`}
      muted={!ext.description}
      pill={
        ext.status !== "enabled"
          ? { tone: "warn", label: ext.status.replace(/_/g, " ") }
          : { tone: healthTone(ext.health), label: ext.health }
      }
      hint={muted ? `muted until ${absolute(ext.muted_until)}` : failing ? `${jobs}, failing` : jobs}
      time={last}
      now={now}
      below={
        ext.panels.length > 0 &&
        ext.panels.slice(0, 2).map((p) => <PanelGlance key={p.id} ext={ext.name} panel={p} now={now} />)
      }
      testId="extension-row"
    />
  );
}

/** One scheduled job (an extension's or a core one): id · schedule · last outcome · last run. */
export function JobRow({ job: j, ext, now }: { job: Job; ext: string; now: number }) {
  const pill =
    j.fail_streak > 0
      ? { tone: "bad" as const, label: `${j.fail_streak} fail${j.fail_streak === 1 ? "" : "s"}`, title: j.last_outcome ?? undefined }
      : { tone: outcomeTone(j.last_outcome), label: j.last_outcome ?? "not run", plain: true };
  return (
    <ListRow
      to={{ kind: "job", ext, id: j.id }}
      title={j.id}
      mono
      pill={pill}
      hint={j.schedule ?? undefined}
      time={j.last_run_at}
      now={now}
      testId="job-row"
    />
  );
}
