import { Ago, Empty, Row, Rows, Section, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { absolute } from "@/lib/time";
import { healthTone, outcomeTone } from "@/lib/tone";
import type { Extension, Snapshot } from "@/types";

export function Extensions({ snap, now }: { snap: Snapshot; now: number }) {
  return (
    <Section title="Extensions" description={`${snap.extensions.length} installed`}>
      {snap.extensions.length === 0 ? (
        <Empty>No extensions installed.</Empty>
      ) : (
        <div className="space-y-3">
          {snap.extensions.map((ext) => (
            <ExtensionBlock key={ext.name} ext={ext} now={now} />
          ))}
        </div>
      )}
    </Section>
  );
}

function ExtensionBlock({ ext, now }: { ext: Extension; now: number }) {
  const muted = !!ext.muted_until && Date.parse(ext.muted_until) > now;
  return (
    <div className="rounded-lg border px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-mono text-sm font-medium">{ext.name}</h3>
          <p className="text-xs text-muted-foreground">{ext.status.replace(/_/g, " ")}</p>
        </div>
        <span className="flex shrink-0 items-center gap-1.5">
          {muted && (
            <Badge variant="warn" title={`muted until ${absolute(ext.muted_until)}`}>
              muted
            </Badge>
          )}
          <StateBadge tone={healthTone(ext.health)}>{ext.health}</StateBadge>
        </span>
      </div>
      <div className="mt-3">
        {ext.jobs.length === 0 ? (
          <Empty>No scheduled jobs.</Empty>
        ) : (
          <Rows>
            {ext.jobs.map((j) => (
              <Row key={j.id} className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 py-2.5 text-sm">
                <span className="font-mono text-xs">{j.id}</span>
                <span className="flex items-center justify-end gap-1.5">
                  {j.fail_streak > 0 && (
                    <Badge variant="bad">
                      {j.fail_streak} fail{j.fail_streak === 1 ? "" : "s"} in a row
                    </Badge>
                  )}
                  <Badge variant={outcomeTone(j.last_outcome)}>{j.last_outcome ?? "not run"}</Badge>
                </span>
                <span className="col-span-2 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                  <Ago iso={j.last_run_at} now={now} prefix="last " />
                  {j.next_due && <Ago iso={j.next_due} now={now} prefix="next " />}
                </span>
              </Row>
            ))}
          </Rows>
        )}
      </div>
    </div>
  );
}
