import { Ago, Counts, Empty, ExternalLink, Group, LocalOnly, Meta, Row, Rows, Section, StateBadge } from "@/components/bits";
import { Progress } from "@/components/ui/progress";
import { ACTIVE_RUN_STATES, ACTIVE_TASK_STATES, RUN_STATE_ORDER, ordered, safeHttpsUrl, sum } from "@/lib/snapshot";
import { runTone } from "@/lib/tone";
import { cn } from "@/lib/utils";
import { duration } from "@/lib/time";
import type { RunBrief, RunItem, Snapshot } from "@/types";

const isActive = (r: { state: string }) => ACTIVE_RUN_STATES.includes(r.state);

/** The remote tier's runs: codes, states and times, no asks, repos or links. */
function BriefRuns({ runs, now, empty }: { runs: RunBrief[]; now: number; empty: string }) {
  if (runs.length === 0) return <Empty>{empty}</Empty>;
  return (
    <div className="space-y-3">
      <Rows>
        {runs.map((r) => (
          <BriefRunRow key={r.code} run={r} now={now} />
        ))}
      </Rows>
      <LocalOnly what="Asks, repos and links" />
    </div>
  );
}

function BriefRunRow({ run, now }: { run: RunBrief; now: number }) {
  return (
    <Row className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-xs text-muted-foreground">
          <span className="font-mono font-semibold text-foreground">{run.code}</span>
          {run.scratch && <span> · scratch</span>}
          {run.has_pr && <span> · PR open</span>}
        </p>
        <Meta>
          {run.ended_at ? (
            <Ago iso={run.ended_at} now={now} prefix="ended " />
          ) : (
            <Ago iso={run.started_at} now={now} prefix="started " />
          )}
          {run.started_at && run.ended_at && (
            <span className="tabular-nums">took {duration(run.started_at, Date.parse(run.ended_at))}</span>
          )}
        </Meta>
      </div>
      <StateBadge tone={runTone(run.state)}>
        {run.state}
        {run.waiting_reason ? ` (${run.waiting_reason.replace(/_/g, " ")})` : ""}
      </StateBadge>
    </Row>
  );
}

export function Slots({ snap }: { snap: Snapshot }) {
  const { used, max } = snap.slots;
  const pct = max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0;
  const full = max > 0 && used >= max;
  return (
    <Section title="Slots" description="Workers running at once">
      <p className="text-3xl font-semibold tracking-tight tabular-nums">
        {used}
        <span className="text-lg font-normal text-muted-foreground"> / {max}</span>
      </p>
      <Progress
        value={pct}
        getValueLabel={() => `${used} of ${max}`}
        aria-label="Slots in use"
        className="mt-3 h-1.5"
        indicatorClassName={cn(full ? "bg-warn" : "bg-info")}
      />
    </Section>
  );
}

export function ActiveRuns({ snap, now }: { snap: Snapshot; now: number }) {
  const count = sum(snap.runs.by_state, ACTIVE_RUN_STATES);
  const items = snap.runs.items?.filter(isActive);
  const brief = snap.runs.brief?.filter(isActive);
  return (
    <Section title="Active runs" description={`${count} running, waiting or queued`}>
      {items === undefined ? (
        brief !== undefined ? (
          <BriefRuns runs={brief} now={now} empty="No runs in flight." />
        ) : count === 0 ? (
          <Empty>No runs in flight.</Empty>
        ) : (
          <LocalOnly what="Run details" />
        )
      ) : items.length === 0 ? (
        <Empty>No runs in flight.</Empty>
      ) : (
        <Rows>
          {items.map((r) => (
            <RunRow key={r.code} run={r} now={now} />
          ))}
        </Rows>
      )}
    </Section>
  );
}

export function ActiveTasks({ snap }: { snap: Snapshot }) {
  return (
    <Section title="Active tasks" description={`${sum(snap.tasks.by_state, ACTIVE_TASK_STATES)} in progress`}>
      <dl className="grid grid-cols-3 gap-3">
        {ACTIVE_TASK_STATES.map((s) => (
          <div key={s} className="rounded-lg bg-muted/60 px-3 py-2.5">
            <dt className="text-xs text-muted-foreground">{s}</dt>
            <dd className="mt-0.5 text-xl font-semibold tabular-nums">{snap.tasks.by_state[s] ?? 0}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

/** Every run and task state, and the runs that have finished. Active ones are on the left. */
export function RunsAndTasks({ snap, now }: { snap: Snapshot; now: number }) {
  const finished = snap.runs.items?.filter((r) => !isActive(r));
  const finishedBrief = snap.runs.brief?.filter((r) => !isActive(r));
  return (
    <Section title="Runs and tasks">
      <div className="space-y-5">
        <Group label="Runs">
          <Counts by={ordered(snap.runs.by_state, RUN_STATE_ORDER)} tone={runTone} />
        </Group>
        <Group label="Tasks">
          <Counts by={ordered(snap.tasks.by_state, ACTIVE_TASK_STATES)} tone={runTone} />
        </Group>
        <Group label="Recent runs">
          {finished === undefined ? (
            finishedBrief !== undefined ? (
              <BriefRuns runs={finishedBrief} now={now} empty="No finished runs yet." />
            ) : (
              <LocalOnly what="Run details" />
            )
          ) : finished.length === 0 ? (
            <Empty>No finished runs yet.</Empty>
          ) : (
            <Rows>
              {finished.map((r) => (
                <RunRow key={r.code} run={r} now={now} />
              ))}
            </Rows>
          )}
        </Group>
      </div>
    </Section>
  );
}

function RunRow({ run, now }: { run: RunItem; now: number }) {
  const prUrl = safeHttpsUrl(run.pr_url);
  const remoteUrl = safeHttpsUrl(run.remote_url);
  return (
    <Row>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">
            <span className="font-mono font-semibold text-foreground">{run.code}</span>
            {run.repo && <span> · {run.repo}</span>}
          </p>
          <p className="mt-0.5 line-clamp-2 text-sm">{run.ask}</p>
        </div>
        <StateBadge tone={runTone(run.state)}>
          {run.state}
          {run.waiting_reason ? ` (${run.waiting_reason.replace(/_/g, " ")})` : ""}
        </StateBadge>
      </div>
      {run.last_note && <p className="mt-1.5 text-xs text-muted-foreground italic">“{run.last_note}”</p>}
      <Meta>
        {run.ended_at ? (
          <Ago iso={run.ended_at} now={now} prefix="ended " />
        ) : (
          <Ago iso={run.started_at} now={now} prefix="started " />
        )}
        {run.branch && <span className="truncate font-mono">{run.branch}</span>}
        {prUrl && <ExternalLink href={prUrl}>PR #{run.pr_number ?? "?"}</ExternalLink>}
        {remoteUrl && <ExternalLink href={remoteUrl}>Remote Control</ExternalLink>}
      </Meta>
    </Row>
  );
}
