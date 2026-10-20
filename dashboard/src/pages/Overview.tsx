import { Alert } from "@/components/ui/alert";
import { Ago, Counts, Dot, Empty, LocalOnly, Row, Rows, Section, StateBadge } from "@/components/bits";
import type { BadgeTone } from "@/components/ui/badge";
import {
  ACTIVE_RUN_STATES,
  ACTIVE_TASK_STATES,
  RUN_STATE_ORDER,
  ordered,
  safeHttpsUrl,
  sum,
  worstHealth,
} from "@/lib/snapshot";
import { duration } from "@/lib/time";
import { healthTone, runTone } from "@/lib/tone";
import { cn } from "@/lib/utils";
import type { BrainActivity, ClaudeBrain, Snapshot } from "@/types";

interface Props {
  snap: Snapshot;
  now: number;
  go: (route: string) => void;
  /** Data is old: say "last known" rather than state it as now. */
  stale?: boolean;
}

const HEADLINE = {
  ok: "All systems normal",
  unknown: "Some checks have no result yet",
  degraded: "Something is degraded",
  failing: "Something is failing",
} as const;

export function Overview({ snap, now, go, stale = false }: Props) {
  const worst = snap.health.length ? worstHealth(snap.health.map((h) => h.state)) : "unknown";
  const notOk = snap.health.filter((h) => h.state !== "ok");
  const killSwitch = snap.autonomy === "off" && snap.paused;

  return (
    <div className="space-y-4">
      {killSwitch && (
        <Alert tone="warn">
          <Dot tone="warn" className="mt-1.5" />
          <div>
            <p className="font-medium">Kill switch is on</p>
            <p className="text-muted-foreground">Autonomy is off and jobs are paused. /resume on iMessage turns it back.</p>
          </div>
        </Alert>
      )}

      <section className="rounded-lg border bg-card px-4 py-4">
        <div className="flex items-center gap-2.5">
          <Dot tone={healthTone(worst)} pulse={worst === "failing" && !stale} className="size-2.5 [&>span]:size-2.5" />
          <h1 className="text-lg font-semibold tracking-tight">
            {stale ? `Last known: ${HEADLINE[worst].toLowerCase()}` : HEADLINE[worst]}
          </h1>
        </div>
        {notOk.length > 0 && (
          <p className="mt-1 text-sm text-muted-foreground">
            {notOk.map((h) => `${h.name} ${h.state}`).join(" · ")}
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-1.5">
          <StateBadge tone={snap.autonomy === "on" ? "ok" : "warn"}>autonomy {snap.autonomy}</StateBadge>
          <StateBadge tone={snap.paused ? "warn" : "ok"}>{snap.paused ? "paused" : "jobs running"}</StateBadge>
          <StateBadge tone={snap.dry_run ? "info" : "neutral"}>dry run {snap.dry_run ? "on" : "off"}</StateBadge>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Last tick</dt>
          <dd className="text-right tabular-nums">
            <Ago iso={snap.last_tick} now={now} />
          </dd>
          <dt className="text-muted-foreground">Up for</dt>
          <dd className="text-right tabular-nums">{duration(snap.started_at, now)}</dd>
        </dl>
      </section>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <SlotTile used={snap.slots.used} max={snap.slots.max} />
        <Tile
          label="Active runs"
          value={sum(snap.runs.by_state, ACTIVE_RUN_STATES)}
          onClick={() => go("workers")}
        />
        <Tile
          label="Questions"
          value={snap.questions.open}
          tone={snap.questions.open ? "warn" : undefined}
          onClick={() => go("workers")}
        />
        <Tile
          label="Approvals"
          value={snap.approvals.open}
          tone={snap.approvals.open ? "warn" : undefined}
          onClick={() => go("approvals")}
        />
        <Tile label="Pulse open" value={snap.pulse.open} onClick={() => go("pulse")} />
        <Tile label="Active tasks" value={sum(snap.tasks.by_state, ACTIVE_TASK_STATES)} />
      </div>

      <Section title="Health" description={snap.health.length ? undefined : "No checks reported yet."}>
        {snap.health.length === 0 ? (
          <Empty>The monitor hasn't run yet.</Empty>
        ) : (
          <Rows>
            {snap.health.map((h) => (
              <Row key={h.name} className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-sm">{h.name}</p>
                  {h.reason && <p className="mt-0.5 text-xs text-muted-foreground">{h.reason}</p>}
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <StateBadge tone={healthTone(h.state)}>{h.state}</StateBadge>
                  {h.since && (
                    <span className="text-xs text-muted-foreground">
                      <Ago iso={h.since} now={now} prefix="since " />
                    </span>
                  )}
                </div>
              </Row>
            ))}
          </Rows>
        )}
      </Section>

      <Section title="Runs and tasks">
        <div className="space-y-3">
          <div>
            <p className="mb-1.5 text-xs text-muted-foreground">Runs</p>
            <Counts by={ordered(snap.runs.by_state, RUN_STATE_ORDER)} tone={runTone} />
          </div>
          <div>
            <p className="mb-1.5 text-xs text-muted-foreground">Tasks</p>
            <Counts by={ordered(snap.tasks.by_state, ACTIVE_TASK_STATES)} tone={runTone} />
          </div>
        </div>
      </Section>

      <Section title="Worker runner">
        {snap.runner.set_up ? (
          <StateBadge tone="ok">set up</StateBadge>
        ) : (
          <div className="space-y-2">
            <StateBadge tone="warn">not set up</StateBadge>
            {snap.runner.missing && snap.runner.missing.length > 0 && (
              <p className="text-sm text-muted-foreground">Missing: {snap.runner.missing.join(", ")}</p>
            )}
          </div>
        )}
      </Section>

      {(snap.claude_brain || snap.brain_activity) && (
        <ClaudeBrainSection
          brain={snap.claude_brain}
          activity={snap.brain_activity}
          local={snap.tier === "local"}
          now={now}
        />
      )}
    </div>
  );
}

const DECISION_TONE: Record<string, BadgeTone> = {
  allowed: "neutral",
  read: "neutral",
  resonant: "neutral",
  gate: "ok",
  granted: "ok",
  asked: "warn",
  refused: "bad",
  denied: "bad",
};

function ClaudeBrainSection({
  brain,
  activity,
  local,
  now,
}: {
  brain?: ClaudeBrain;
  activity?: BrainActivity;
  local: boolean;
  now: number;
}) {
  const remoteUrl = brain?.state === "up" ? safeHttpsUrl(brain.remote_url) : null;
  const tools = brain?.tools ?? [];
  return (
    <Section title="Claude brain">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {brain && (
            <>
              <StateBadge tone={brain.state === "up" ? "ok" : "neutral"}>{brain.state}</StateBadge>
              <span className="text-muted-foreground">
                {brain.turns} turn{brain.turns === 1 ? "" : "s"}
              </span>
            </>
          )}
          {activity && (
            <span className="text-muted-foreground" data-testid="brain-activity">
              {activity.calls_today} tool call{activity.calls_today === 1 ? "" : "s"} today,{" "}
              {activity.writes_today} change{activity.writes_today === 1 ? "" : "s"}
            </span>
          )}
          {remoteUrl && (
            <a
              href={remoteUrl}
              target="_blank"
              rel="noreferrer noopener"
              className="font-medium text-info underline-offset-2 hover:underline"
            >
              Remote Control
            </a>
          )}
        </div>
        {local && tools.length > 0 && (
          <Rows>
            {tools.map((t, i) => (
              <Row key={`${t.at}-${i}`} className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-sm break-all">
                    <span className="text-muted-foreground">{t.tool}</span> {t.command}
                  </p>
                  {t.cwd && <p className="mt-0.5 truncate text-xs text-muted-foreground">{t.cwd}</p>}
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <StateBadge tone={t.status === "error" ? "bad" : (DECISION_TONE[t.decision] ?? "neutral")}>
                    {t.decision}
                    {t.status === "error" ? " · error" : ""}
                  </StateBadge>
                  <span className="text-xs text-muted-foreground">
                    <Ago iso={t.at} now={now} />
                  </span>
                </div>
              </Row>
            ))}
          </Rows>
        )}
        {!local && <LocalOnly what="Its tool calls" />}
      </div>
    </Section>
  );
}

function Tile({
  label,
  value,
  tone,
  onClick,
}: {
  label: string;
  value: number;
  tone?: "warn";
  onClick?: () => void;
}) {
  const body = (
    <>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn("mt-1 block text-2xl font-semibold tabular-nums tracking-tight", tone === "warn" && "text-warn")}>
        {value}
      </span>
    </>
  );
  const cls = "flex flex-col items-start justify-start rounded-lg border bg-card px-4 py-3 text-left";
  return onClick ? (
    <button type="button" onClick={onClick} className={cn(cls, "transition-colors hover:bg-muted")}>
      {body}
    </button>
  ) : (
    <div className={cls}>{body}</div>
  );
}

function SlotTile({ used, max }: { used: number; max: number }) {
  const pct = max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0;
  return (
    <div className="rounded-lg border bg-card px-4 py-3">
      <span className="text-xs text-muted-foreground">Slots</span>
      <span className="mt-1 block text-2xl font-semibold tracking-tight tabular-nums">
        {used}
        <span className="text-base font-normal text-muted-foreground"> / {max}</span>
      </span>
      <div
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted"
        role="meter"
        aria-label="Slots in use"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={used}
      >
        <div
          className={cn("h-full rounded-full", used >= max && max > 0 ? "bg-warn" : "bg-info")}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
