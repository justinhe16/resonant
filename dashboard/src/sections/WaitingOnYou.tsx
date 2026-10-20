import { Ago, Empty, Group, LocalOnly, Meta, Row, Rows, Section } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { levelTone } from "@/lib/tone";
import type { Snapshot } from "@/types";

/** Approvals and worker questions: everything waiting for the owner's answer. */
export function WaitingOnYou({ snap, now }: { snap: Snapshot; now: number }) {
  const open = snap.approvals.open + snap.questions.open;
  return (
    <Section
      title="Waiting on you"
      description="Answer on iMessage"
      action={<Badge variant={open ? "warn" : "neutral"}>{open} open</Badge>}
    >
      <div className="space-y-5">
        <Group label="Approvals" count={snap.approvals.open}>
          <Approvals snap={snap} now={now} />
        </Group>
        <Group label="Questions" count={snap.questions.open}>
          <Questions snap={snap} now={now} />
        </Group>
      </div>
    </Section>
  );
}

function Approvals({ snap, now }: { snap: Snapshot; now: number }) {
  const items = snap.approvals.items;
  if (items === undefined)
    return snap.approvals.open === 0 ? <Empty>Nothing waiting for a yes.</Empty> : <LocalOnly what="Approval texts" />;
  if (items.length === 0) return <Empty>Nothing waiting for a yes.</Empty>;
  return (
    <Rows>
      {items.map((a, i) => (
        <Row key={a.code ?? `${a.asked_at}-${i}`}>
          <div className="flex items-start justify-between gap-3">
            <p className="min-w-0 text-sm break-words">{a.summary}</p>
            <Badge variant={levelTone(a.level)}>{a.level}</Badge>
          </div>
          <Meta>
            {a.code && <span className="font-mono">code {a.code}</span>}
            <Ago iso={a.asked_at} now={now} prefix="asked " />
            {a.expires_at && <Ago iso={a.expires_at} now={now} prefix="expires " />}
          </Meta>
        </Row>
      ))}
    </Rows>
  );
}

function Questions({ snap, now }: { snap: Snapshot; now: number }) {
  const items = snap.questions.items;
  if (items === undefined)
    return snap.questions.open === 0 ? <Empty>No worker is waiting on you.</Empty> : <LocalOnly what="Question texts" />;
  if (items.length === 0) return <Empty>No worker is waiting on you.</Empty>;
  return (
    <Rows>
      {items.map((q, qi) => (
        <Row key={`${q.run_code}-${qi}`}>
          <p className="text-sm">
            <span className="mr-2 font-mono text-xs font-semibold">{q.run_code}</span>
            {q.text}
          </p>
          {q.options.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {q.options.map((o, i) => (
                <Badge key={i} variant="outline">
                  ({String.fromCharCode(97 + i)}) {o}
                </Badge>
              ))}
            </div>
          )}
          <Meta>
            <Ago iso={q.asked_at} now={now} prefix="asked " />
          </Meta>
        </Row>
      ))}
    </Rows>
  );
}
