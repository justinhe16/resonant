import { Ago, Empty, LocalOnly, Meta, Row, Rows, Section, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { mutedExtensions } from "@/lib/snapshot";
import { urgencyTone } from "@/lib/tone";
import type { PulseItem, Snapshot } from "@/types";

const RANK: Record<string, number> = { urgent: 0, normal: 1, low: 2 };

export function Pulse({ snap, now }: { snap: Snapshot; now: number }) {
  const items = snap.pulse.items;
  const untold = snap.pulse.untold;
  const muted = mutedExtensions(snap, now);
  const sorted = items
    ? [...items].sort(
        (a: PulseItem, b: PulseItem) =>
          (RANK[a.urgency] ?? 3) - (RANK[b.urgency] ?? 3) || b.updated_at.localeCompare(a.updated_at),
      )
    : undefined;
  return (
    <Section
      title="Pulse"
      description="What Resonant is keeping an eye on"
      action={
        <span className="flex gap-1.5">
          <Badge variant="neutral">{snap.pulse.open} open</Badge>
          {untold !== undefined && untold > 0 && (
            <Badge variant="warn" data-testid="pulse-untold" title="Items you haven't heard yet, muted ones included">
              {untold} untold
            </Badge>
          )}
        </span>
      }
    >
      {muted.length > 0 && (
        <p className="mb-3 text-xs text-muted-foreground" data-testid="pulse-muted">
          {muted.map((m, i) => (
            <span key={m.name}>
              {i > 0 && ", "}
              <span className="font-mono text-foreground">{m.name}</span> muted, ends{" "}
              <Ago iso={m.until} now={now} />
            </span>
          ))}
        </p>
      )}
      {sorted === undefined ? (
        snap.pulse.brief !== undefined && snap.pulse.brief.length > 0 ? (
          <div className="space-y-3">
            <Rows>
              {snap.pulse.brief.map((p, i) => (
                <Row key={`${p.source}-${p.updated_at}-${i}`} className="flex items-start justify-between gap-3">
                  <Meta>
                    <span className="font-mono">{p.source}</span>
                    {p.state === "new" && <span>new</span>}
                    <Ago iso={p.updated_at} now={now} prefix="updated " />
                  </Meta>
                  <StateBadge tone={urgencyTone(p.urgency)}>{p.urgency}</StateBadge>
                </Row>
              ))}
            </Rows>
            <LocalOnly what="Pulse titles" />
          </div>
        ) : snap.pulse.open === 0 && !untold ? (
          <Empty>All quiet.</Empty>
        ) : (
          <LocalOnly what="Pulse titles" />
        )
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
