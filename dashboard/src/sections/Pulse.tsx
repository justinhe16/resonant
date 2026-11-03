import { CollapsibleSection } from "@/components/CollapsibleSection";
import { Ago, Meta, OutsideText, Row, RouteLink, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { isOutside, mutedExtensions } from "@/lib/snapshot";
import { urgencyTone } from "@/lib/tone";
import type { PulseItem, Snapshot } from "@/types";

const RANK: Record<string, number> = { urgent: 0, normal: 1, low: 2 };

export function Pulse({ snap, now }: { snap: Snapshot; now: number }) {
  const untold = snap.pulse.untold;
  const muted = mutedExtensions(snap, now);
  const sorted = [...snap.pulse.items].sort(
    (a: PulseItem, b: PulseItem) =>
      (RANK[a.urgency] ?? 3) - (RANK[b.urgency] ?? 3) || b.updated_at.localeCompare(a.updated_at),
  );
  return (
    <CollapsibleSection
      id="pulse"
      title="Pulse"
      total={snap.pulse.open}
      rows={sorted}
      explainer="pulse"
      empty="All quiet."
      keptNote={`The snapshot keeps ${snap.limits.pulse} items.`}
      action={
        untold > 0 ? (
          <Badge variant="warn" data-testid="pulse-untold" title="Items you haven't heard yet, muted ones included">
            {untold} untold
          </Badge>
        ) : undefined
      }
      summary={
        muted.length > 0 ? (
          <p className="text-xs text-muted-foreground" data-testid="pulse-muted">
            {muted.map((m, i) => (
              <span key={m.name}>
                {i > 0 && ", "}
                <span className="font-mono text-foreground">{m.name}</span> muted, ends <Ago iso={m.until} now={now} />
              </span>
            ))}
          </p>
        ) : undefined
      }
      render={(p) => (
        <Row key={`${p.source}/${p.key}`} className="relative">
          <div className="flex items-start justify-between gap-3">
            <p className="min-w-0 text-sm">
              <OutsideText text={p.title || p.source} outside={isOutside(p, "title")} />
            </p>
            <StateBadge tone={urgencyTone(p.urgency)}>{p.urgency}</StateBadge>
          </div>
          <Meta>
            {p.key ? (
              <RouteLink
                to={{ kind: "pulse", source: p.source, key: p.key }}
                stretch
                className="truncate font-mono font-medium text-info"
              >
                {p.source}/{p.key}
              </RouteLink>
            ) : (
              <span className="font-mono">{p.source}</span>
            )}
            {p.state === "new" && <span>new</span>}
            <Ago iso={p.updated_at} now={now} prefix="updated " />
          </Meta>
        </Row>
      )}
    />
  );
}
