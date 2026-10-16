import { Ago, Empty, LocalOnly, Meta, Row, Rows, Section } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { levelTone } from "@/lib/tone";
import type { Snapshot } from "@/types";

export function Approvals({ snap, now }: { snap: Snapshot; now: number }) {
  const items = snap.approvals.items;
  return (
    <Section
      title="Approvals"
      description={`${snap.approvals.open} open · answer on iMessage`}
    >
      {items === undefined ? (
        snap.approvals.open === 0 ? <Empty>Nothing waiting for a yes.</Empty> : <LocalOnly what="Approval texts" />
      ) : items.length === 0 ? (
        <Empty>Nothing waiting for a yes.</Empty>
      ) : (
        <Rows>
          {items.map((a, i) => (
            <Row key={a.code ?? `${a.asked_at}-${i}`}>
              <div className="flex items-start justify-between gap-3">
                <p className="min-w-0 text-sm break-words">{a.summary}</p>
                <Badge tone={levelTone(a.level)}>{a.level}</Badge>
              </div>
              <Meta>
                {a.code && <span className="font-mono">code {a.code}</span>}
                <Ago iso={a.asked_at} now={now} prefix="asked " />
                {a.expires_at && <Ago iso={a.expires_at} now={now} prefix="expires " />}
              </Meta>
            </Row>
          ))}
        </Rows>
      )}
    </Section>
  );
}
