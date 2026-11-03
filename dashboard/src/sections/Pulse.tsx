import { CollapsibleSection } from "@/components/CollapsibleSection";
import { Ago } from "@/components/bits";
import { ListRow } from "@/components/ListRow";
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
        <ListRow
          key={`${p.source}/${p.key}`}
          to={p.key ? { kind: "pulse", source: p.source, key: p.key } : undefined}
          title={p.title || p.source}
          outside={!!p.title && isOutside(p, "title")}
          pill={{ tone: urgencyTone(p.urgency), label: p.urgency }}
          hint={p.state === "new" ? `new · ${p.source}` : p.source}
          time={p.updated_at}
          now={now}
          testId="pulse-row"
        />
      )}
    />
  );
}
