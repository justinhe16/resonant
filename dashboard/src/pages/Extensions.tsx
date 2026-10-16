import { Ago, Empty, Section, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { healthTone, outcomeTone } from "@/lib/tone";
import type { Snapshot } from "@/types";

export function Extensions({ snap, now }: { snap: Snapshot; now: number }) {
  if (snap.extensions.length === 0) {
    return (
      <Section title="Extensions">
        <Empty>No extensions installed.</Empty>
      </Section>
    );
  }
  return (
    <div className="space-y-4">
      {snap.extensions.map((ext) => (
        <Section
          key={ext.name}
          title={ext.name}
          description={ext.status}
          action={<StateBadge tone={healthTone(ext.health)}>{ext.health}</StateBadge>}
        >
          {ext.jobs.length === 0 ? (
            <Empty>No scheduled jobs.</Empty>
          ) : (
            <ul className="-my-1 divide-y">
              {ext.jobs.map((j) => (
                <li key={j.id} className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 py-2.5 text-sm">
                  <span className="font-mono">{j.id}</span>
                  <span className="flex items-center justify-end gap-1.5">
                    {j.fail_streak > 0 && (
                      <Badge tone="bad">
                        {j.fail_streak} fail{j.fail_streak === 1 ? "" : "s"} in a row
                      </Badge>
                    )}
                    <Badge tone={outcomeTone(j.last_outcome)}>{j.last_outcome ?? "not run"}</Badge>
                  </span>
                  <span className="col-span-2 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                    <Ago iso={j.last_run_at} now={now} prefix="last " />
                    {j.next_due && <Ago iso={j.next_due} now={now} prefix="next " />}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>
      ))}
    </div>
  );
}
