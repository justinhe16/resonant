import { CollapsibleSection } from "@/components/CollapsibleSection";
import { Ago, Counts, ExternalLink, Group, Meta, OutsideText, Row, RouteLink, Section, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
  ACTIVE_RUN_STATES,
  ACTIVE_TASK_STATES,
  RUN_STATE_ORDER,
  isOutside,
  ordered,
  safeHttpsUrl,
  sum,
} from "@/lib/snapshot";
import { runTone, taskTone } from "@/lib/tone";
import { toHash } from "@/lib/route";
import { cn } from "@/lib/utils";
import type { RunItem, Snapshot, TaskItem } from "@/types";

const isActive = (r: { state: string }) => ACTIVE_RUN_STATES.includes(r.state);
const TASK_ORDER = [...ACTIVE_TASK_STATES, "failed", "done", "cancelled"];

export function Slots({ snap }: { snap: Snapshot }) {
  const { used, max } = snap.slots;
  const pct = max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0;
  const full = max > 0 && used >= max;
  return (
    <Section title="Slots" description="Workers running at once" explainer="slots">
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
  const items = snap.runs.items.filter(isActive);
  return (
    <CollapsibleSection
      id="active-runs"
      title="Active runs"
      total={count}
      rows={items}
      render={(r) => <RunRow key={r.code} run={r} now={now} />}
      empty="No runs in flight."
      defaultOpen
      explainer="runs"
    />
  );
}

export function ActiveTasks({ snap }: { snap: Snapshot }) {
  return (
    <Section
      title="Active tasks"
      description={`${sum(snap.tasks.by_state, ACTIVE_TASK_STATES)} in progress`}
      explainer="tasks"
    >
      <dl className="grid grid-cols-3 gap-3">
        {ACTIVE_TASK_STATES.map((s) => (
          <a
            key={s}
            href={toHash({ kind: "tasks", state: s })}
            className="rounded-lg bg-muted/60 px-3 py-2.5 outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <dt className="text-xs text-muted-foreground">{s}</dt>
            <dd className="mt-0.5 text-xl font-semibold tabular-nums">{snap.tasks.by_state[s] ?? 0}</dd>
          </a>
        ))}
      </dl>
    </Section>
  );
}

/** Finished runs, with every run state and the side effects the runs showed as pills. */
export function RecentRuns({ snap, now }: { snap: Snapshot; now: number }) {
  const finished = snap.runs.items.filter((r) => !isActive(r));
  const finishedCount = sum(snap.runs.by_state) - sum(snap.runs.by_state, ACTIVE_RUN_STATES);
  return (
    <CollapsibleSection
      id="runs"
      title="Recent runs"
      total={finishedCount}
      rows={finished}
      render={(r) => <RunRow key={r.code} run={r} now={now} />}
      empty="No finished runs yet."
      keptNote={`The snapshot keeps the newest ${snap.limits.runs} runs; older ones aren't shown.`}
      explainer="runs"
      summary={
        <div className="space-y-4">
          <Group label="Runs by state">
            <Counts
              by={ordered(snap.runs.by_state, RUN_STATE_ORDER)}
              tone={runTone}
              href={(state) => ({ kind: "runs", state })}
            />
          </Group>
          <DetectedEffects detected={snap.runs.detected} />
        </div>
      }
    />
  );
}

/** Every task, newest activity first (running and waiting ones lead). */
export function Tasks({ snap, now }: { snap: Snapshot; now: number }) {
  const total = sum(snap.tasks.by_state);
  return (
    <CollapsibleSection
      id="tasks"
      title="Tasks"
      total={total}
      rows={snap.tasks.items}
      render={(t) => <TaskRow key={t.id} task={t} now={now} />}
      empty="No tasks yet."
      keptNote={`The snapshot keeps ${snap.limits.tasks} tasks: the active ones, then the newest.`}
      explainer="tasks"
      summary={
        <Group label="Tasks by state">
          <Counts
            by={ordered(snap.tasks.by_state, TASK_ORDER)}
            tone={taskTone}
            href={(state) => ({ kind: "tasks", state })}
          />
        </Group>
      }
    />
  );
}

/** What the listed runs did outside their own branch and PR, read from their commands. */
function DetectedEffects({ detected }: { detected: Snapshot["runs"]["detected"] }) {
  const by = Object.entries(detected.by_kind).sort(([a, x], [b, y]) => y - x || a.localeCompare(b));
  if (by.length === 0 && detected.high === 0) return null;
  return (
    <Group label="Detected side effects, recent runs" count={sum(detected.by_kind)}>
      <div className="space-y-1.5" data-testid="detected-effects">
        <Counts by={by} tone={() => "neutral"} href={(effect) => ({ kind: "effects", effect })} />
        {detected.high > 0 && (
          <p className="text-xs text-muted-foreground">
            <Badge variant="bad">{detected.high} high-impact</Badge> a base-branch push, a publish or a delete
          </p>
        )}
      </div>
    </Group>
  );
}

export function RunRow({ run, now }: { run: RunItem; now: number }) {
  const prUrl = safeHttpsUrl(run.pr_url);
  const remoteUrl = safeHttpsUrl(run.remote_url);
  return (
    <Row className="relative">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">
            <RouteLink to={{ kind: "run", code: run.code }} stretch className="font-mono font-semibold text-foreground">
              {run.code}
            </RouteLink>
            {run.repo && <span> · {run.repo}</span>}
          </p>
          {run.ask && (
            <p className="mt-0.5 line-clamp-2 text-sm">
              <OutsideText text={run.ask} outside={isOutside(run, "ask")} />
            </p>
          )}
        </div>
        <StateBadge tone={runTone(run.state)}>
          {run.state}
          {run.waiting_reason ? ` (${run.waiting_reason.replace(/_/g, " ")})` : ""}
        </StateBadge>
      </div>
      {run.last_note && (
        <p className="mt-1.5 text-xs text-muted-foreground italic">
          “<OutsideText text={run.last_note} outside={isOutside(run, "last_note")} />”
        </p>
      )}
      <Meta>
        {run.ended_at ? (
          <Ago iso={run.ended_at} now={now} prefix="ended " />
        ) : (
          <Ago iso={run.started_at} now={now} prefix="started " />
        )}
        {run.branch && <span className="truncate font-mono">{run.branch}</span>}
        {!!run.detected && (
          <span>
            {run.detected} side effect{run.detected === 1 ? "" : "s"} detected
          </span>
        )}
        {prUrl && (
          <span className="relative z-10">
            <ExternalLink href={prUrl}>PR #{run.pr_number ?? "?"}</ExternalLink>
          </span>
        )}
        {remoteUrl && (
          <span className="relative z-10">
            <ExternalLink href={remoteUrl}>Remote Control</ExternalLink>
          </span>
        )}
      </Meta>
    </Row>
  );
}

export function TaskRow({ task, now }: { task: TaskItem; now: number }) {
  return (
    <Row className="relative">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">
            <RouteLink to={{ kind: "task", id: task.id }} stretch className="font-mono font-semibold text-foreground">
              {task.id}
            </RouteLink>
            <span>
              {" "}
              · {task.kind}
              {task.extension ? ` · ${task.extension}` : ""}
            </span>
          </p>
          {task.label && <p className="mt-0.5 line-clamp-2 text-sm">{task.label}</p>}
        </div>
        <StateBadge tone={taskTone(task.status)}>
          {task.status}
          {task.wait_reason ? ` (${task.wait_reason.replace(/_/g, " ")})` : ""}
        </StateBadge>
      </div>
      {task.error && (
        <p className="mt-1.5 text-xs text-bad">
          <OutsideText text={task.error} outside={isOutside(task, "error")} />
        </p>
      )}
      <Meta>
        <Ago iso={task.updated_at} now={now} prefix="updated " />
        {task.attempts > 1 && <span>{task.attempts} attempts</span>}
        {task.run_code && (
          <span className="relative z-10">
            <RouteLink to={{ kind: "run", code: task.run_code }} className="font-mono font-medium text-info">
              {task.run_code}
            </RouteLink>
          </span>
        )}
      </Meta>
    </Row>
  );
}
