import { ChevronDown, OctagonPause } from "lucide-react";
import { Ago, Dot, Row, Rows, StateBadge } from "@/components/bits";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Separator } from "@/components/ui/separator";
import { worstHealth } from "@/lib/snapshot";
import { duration } from "@/lib/time";
import { healthTone } from "@/lib/tone";
import type { Snapshot } from "@/types";

const HEADLINE = {
  ok: "All systems normal",
  unknown: "Some checks have no result yet",
  degraded: "Something is degraded",
  failing: "Something is failing",
} as const;

/**
 * The overall state is the worst of every component's health; the per-component rows sit
 * in a collapsible list under it.
 */
export function SystemStatus({ snap, now, stale }: { snap: Snapshot; now: number; stale: boolean }) {
  const worst = snap.health.length ? worstHealth(snap.health.map((h) => h.state)) : "unknown";
  const notOk = snap.health.filter((h) => h.state !== "ok");
  const okCount = snap.health.length - notOk.length;
  const killSwitch = snap.autonomy === "off" && snap.paused;

  return (
    <Card className="[--card-spacing:--spacing(5)]">
      <CardHeader className="gap-2">
        <p className="text-xs font-medium text-muted-foreground">System status</p>
        <div className="flex items-center gap-2.5">
          <Dot tone={healthTone(worst)} pulse={worst === "failing" && !stale} className="size-2.5" />
          <h1 className="text-xl font-semibold">
            {stale ? `Last known: ${HEADLINE[worst].toLowerCase()}` : HEADLINE[worst]}
          </h1>
        </div>
        {notOk.length > 0 && (
          <p className="text-sm text-muted-foreground">{notOk.map((h) => `${h.name} ${h.state}`).join(" · ")}</p>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {killSwitch && (
          <Alert variant="warn">
            <OctagonPause />
            <AlertTitle>Kill switch is on</AlertTitle>
            <AlertDescription>Autonomy is off and jobs are paused. /resume on iMessage turns it back.</AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap gap-1.5">
          <StateBadge tone={snap.autonomy === "on" ? "ok" : "warn"}>autonomy {snap.autonomy}</StateBadge>
          <StateBadge tone={snap.paused ? "warn" : "ok"}>{snap.paused ? "paused" : "jobs running"}</StateBadge>
          <StateBadge tone={snap.dry_run ? "info" : "neutral"}>dry run {snap.dry_run ? "on" : "off"}</StateBadge>
        </div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Last tick</dt>
          <dd className="text-right tabular-nums">
            <Ago iso={snap.last_tick} now={now} />
          </dd>
          <dt className="text-muted-foreground">Up for</dt>
          <dd className="text-right tabular-nums">{duration(snap.started_at, now)}</dd>
        </dl>
        <Separator />
        {snap.health.length === 0 ? (
          <p className="text-sm text-muted-foreground">The monitor hasn't run yet.</p>
        ) : (
          <Collapsible>
            <CollapsibleTrigger className="group/health -mx-2 flex w-[calc(100%+1rem)] items-center gap-2 rounded-lg px-2 py-1.5 text-sm font-medium outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50">
              Component health
              <span className="ml-auto text-xs font-normal text-muted-foreground tabular-nums">
                {okCount} of {snap.health.length} ok
              </span>
              <ChevronDown className="size-4 text-muted-foreground transition-transform group-data-open/health:rotate-180" />
            </CollapsibleTrigger>
            <CollapsibleContent className="pt-3">
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
            </CollapsibleContent>
          </Collapsible>
        )}
      </CardContent>
    </Card>
  );
}
