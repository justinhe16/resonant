import { ChevronDown, OctagonPause } from "lucide-react";
import { useState } from "react";
import { Ago, Dot, OutsideText, Row, RouteLink, Rows, Section, StateBadge } from "@/components/bits";
import type { BadgeTone } from "@/components/ui/badge";
import { Explainer } from "@/components/Explainer";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Separator } from "@/components/ui/separator";
import { CREDENTIALS_SHOWN, type CredentialGroup, credentialLabel, credentialTone } from "@/lib/credentials";
import type { ExplainerKey } from "@/lib/explainers";
import { channelName, isOutside, worstHealth } from "@/lib/snapshot";
import { duration } from "@/lib/time";
import { healthTone } from "@/lib/tone";
import { cn } from "@/lib/utils";
import type { Snapshot } from "@/types";

/** Health rows with their own explainer (the relay is the dead-man switch). */
const ROW_EXPLAINER: Record<string, ExplainerKey> = { relay: "relay", backup: "backups" };

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
        <div className="flex items-center gap-1.5">
          <p className="text-xs font-medium text-muted-foreground">System status</p>
          <Explainer name="System status" text="status" />
        </div>
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
            <AlertDescription>Autonomy is off and jobs are paused. /resume on {channelName(snap)} turns it back.</AlertDescription>
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
                      <p className="flex items-center gap-1 font-mono text-sm">
                        <RouteLink to={{ kind: "health", name: h.name }}>{h.name}</RouteLink>
                        {ROW_EXPLAINER[h.name] && <Explainer name={h.name} text={ROW_EXPLAINER[h.name]!} />}
                      </p>
                      {h.reason && (
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          <OutsideText text={h.reason} outside={isOutside(h, "reason")} />
                        </p>
                      )}
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

/** The soonest few expiries, with the rest behind "N more". */
function CredentialList({ group: g }: { group: CredentialGroup }) {
  const [all, setAll] = useState(false);
  const hidden = g.secrets.length - CREDENTIALS_SHOWN;
  const shown = all || hidden <= 0 ? g.secrets : g.secrets.slice(0, CREDENTIALS_SHOWN);
  return (
    <>
      <ul className="space-y-1">
        {shown.map((e) => {
          const tone = credentialTone(e, g.taken_at);
          return (
            <li key={e.ref} className="flex items-center gap-2 text-sm" data-tone={tone}>
              <Dot tone={tone} className="size-1.5" />
              <span className="min-w-0 truncate font-mono text-xs" title={e.expires_at}>
                {e.ref}
              </span>
              <span className={cn("ml-auto shrink-0 tabular-nums", TONE_TEXT[tone])}>{credentialLabel(e, g.taken_at)}</span>
            </li>
          );
        })}
      </ul>
      {hidden > 0 && (
        <button
          type="button"
          className="mt-1 text-xs text-muted-foreground underline-offset-2 hover:underline"
          onClick={() => setAll((v) => !v)}
        >
          {all ? "Show fewer" : `${hidden} more`}
        </button>
      )}
    </>
  );
}

const TONE_TEXT: Partial<Record<BadgeTone, string>> = { bad: "text-bad", warn: "text-warn-foreground" };

/** The soonest credential expiries of each instance, side by side when there is more than one:
 * "Resonant: runner/claude_token 300d · Cori: runner/claude_token 364d (assumed)". */
export function Credentials({ groups, now }: { groups: CredentialGroup[]; now: number }) {
  return (
    <Section title="Credentials" explainer="credentials">
      <div className={cn("grid gap-4", groups.length > 1 && "sm:grid-cols-2")} data-testid="credentials">
        {groups.map((g) => (
          <div key={g.name} className="min-w-0" data-testid={`credentials-${g.name}`}>
            {groups.length > 1 && <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">{g.name}</h3>}
            <CredentialList group={g} />
            <p className="mt-1.5 text-xs text-muted-foreground">
              <Ago iso={g.taken_at} now={now} prefix="as of " />
            </p>
          </div>
        ))}
      </div>
    </Section>
  );
}
