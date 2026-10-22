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
  const brief = snap.approvals.brief;
  if (items === undefined && brief !== undefined && brief.length > 0)
    return (
      <div className="space-y-3">
        <Rows>
          {brief.map((a, i) => (
            <Row key={`${a.asked_at}-${i}`}>
              <div className="flex items-start justify-between gap-3">
                <p className="min-w-0 text-sm">
                  {a.effect === "pay" ? "A payment" : a.effect === "read" ? "A read" : "A change"}
                  {a.needs_code ? ", needs its code" : ""}
                </p>
                <Badge variant={levelTone(a.level)}>{a.level}</Badge>
              </div>
              <Meta>
                <Ago iso={a.asked_at} now={now} prefix="asked " />
                {a.expires_at && <Ago iso={a.expires_at} now={now} prefix="expires " />}
              </Meta>
            </Row>
          ))}
        </Rows>
        <LocalOnly what="Approval texts and codes" />
      </div>
    );
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
          {a.changes && a.changes.length > 0 && (
            <ol aria-label="Changes" className="mt-2 space-y-1.5">
              {a.changes.map((c, ci) => (
                <li key={ci} className="flex items-start justify-between gap-3 text-sm text-muted-foreground">
                  <span className="min-w-0 break-words">
                    <span className="mr-1.5 tabular-nums">{ci + 1}.</span>
                    {c.summary}
                  </span>
                  <Badge variant={levelTone(c.level)} className="opacity-80">
                    {c.level}
                  </Badge>
                </li>
              ))}
            </ol>
          )}
          <Meta>
            {a.code && <span className="font-mono">code {a.code}</span>}
            {a.changes && a.changes.length > 0 && <span>one answer for all</span>}
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
  const brief = snap.questions.brief;
  if (items === undefined && brief !== undefined && brief.length > 0)
    return (
      <div className="space-y-3">
        <Rows>
          {brief.map((q, i) => (
            <Row key={`${q.run_code}-${i}`} className="flex items-start justify-between gap-3">
              <p className="text-sm">
                <span className="mr-2 font-mono text-xs font-semibold">{q.run_code}</span>
                {q.options > 0 ? `${q.options} option${q.options === 1 ? "" : "s"}` : "open answer"}
              </p>
              <span className="shrink-0 text-xs text-muted-foreground">
                <Ago iso={q.asked_at} now={now} prefix="asked " />
              </span>
            </Row>
          ))}
        </Rows>
        <LocalOnly what="Question texts" />
      </div>
    );
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
