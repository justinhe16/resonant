import { Ago, Empty, LocalOnly, Meta, Row, Rows, Section, StateBadge } from "@/components/bits";
import { urgencyTone } from "@/lib/tone";
import type { PulseItem, Snapshot } from "@/types";

const RANK: Record<string, number> = { urgent: 0, normal: 1, low: 2 };

export function Pulse({ snap, now }: { snap: Snapshot; now: number }) {
  const items = snap.pulse.items;
  const sorted = items
    ? [...items].sort(
        (a: PulseItem, b: PulseItem) =>
          (RANK[a.urgency] ?? 3) - (RANK[b.urgency] ?? 3) || b.updated_at.localeCompare(a.updated_at),
      )
    : undefined;
  return (
    <Section title="Pulse" description={`${snap.pulse.open} open`}>
      {sorted === undefined ? (
        snap.pulse.open === 0 ? <Empty>All quiet.</Empty> : <LocalOnly what="Pulse titles" />
      ) : sorted.length === 0 ? (
        <Empty>All quiet.</Empty>
      ) : (
        <Rows>
          {sorted.map((p) => (
            <Row key={`${p.source}/${p.key}`}>
              <div className="flex items-start justify-between gap-3">
                <p className="min-w-0 text-sm">{p.title}</p>
                <StateBadge tone={urgencyTone(p.urgency)}>{p.urgency}</StateBadge>
              </div>
              <Meta>
                <span className="truncate font-mono">
                  {p.source}/{p.key}
                </span>
                {p.state === "new" && <span>new</span>}
                <Ago iso={p.updated_at} now={now} prefix="updated " />
              </Meta>
            </Row>
          ))}
        </Rows>
      )}
    </Section>
  );
}
