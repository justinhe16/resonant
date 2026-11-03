import { useState } from "react";
import { CollapsibleSection } from "@/components/CollapsibleSection";
import { Ago, Empty, Group, Meta, OutsideText, Row, RouteLink, Rows, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { approvalTitle, isOutside } from "@/lib/snapshot";
import { approvalTone, levelTone } from "@/lib/tone";
import type { ApprovalItem, QuestionItem, Snapshot } from "@/types";

const RECENT_PREVIEW = 5;

/** Approvals and worker questions: everything waiting for the owner's answer. */
export function WaitingOnYou({ snap, now }: { snap: Snapshot; now: number }) {
  const open = snap.approvals.open + snap.questions.open;
  return (
    <CollapsibleSection
      id="waiting"
      title="Waiting on you"
      total={open}
      defaultOpen
      explainer="approvals"
      action={<Badge variant={open ? "warn" : "neutral"}>{open} open</Badge>}
      summary={<p className="text-xs text-muted-foreground">Answer on iMessage</p>}
    >
      <div className="space-y-5">
        <Group label="Approvals" count={snap.approvals.open}>
          <Approvals items={snap.approvals.items} now={now} />
        </Group>
        <Group label="Questions" count={snap.questions.open}>
          <Questions items={snap.questions.items} now={now} />
        </Group>
        {snap.approvals.recent.length > 0 && (
          <Group label="Recently decided" count={snap.approvals.recent.length}>
            <Recent items={snap.approvals.recent} now={now} />
          </Group>
        )}
      </div>
    </CollapsibleSection>
  );
}

function Approvals({ items, now }: { items: ApprovalItem[]; now: number }) {
  if (items.length === 0) return <Empty>Nothing waiting for a yes.</Empty>;
  return (
    <Rows>
      {items.map((a) => (
        <Row key={a.id} className="relative">
          <div className="flex items-start justify-between gap-3">
            <p className="min-w-0 text-sm">
              <OutsideText text={approvalTitle(a)} outside={isOutside(a, "summary")} />
            </p>
            <Badge variant={levelTone(a.level)}>{a.level}</Badge>
          </div>
          <Changes approval={a} />
          <Meta>
            {a.code && <span className="font-mono">code {a.code}</span>}
            {a.changes && a.changes.length > 0 && <span>one answer for all</span>}
            <Ago iso={a.asked_at} now={now} prefix="asked " />
            {a.expires_at && <Ago iso={a.expires_at} now={now} prefix="expires " />}
            <RouteLink to={{ kind: "approval", id: a.id }} stretch className="font-medium text-info">
              details
            </RouteLink>
          </Meta>
        </Row>
      ))}
    </Rows>
  );
}

export function Changes({ approval: a }: { approval: ApprovalItem }) {
  if (!a.changes || a.changes.length === 0) return null;
  const outside = isOutside(a, "changes");
  return (
    <ol aria-label="Changes" className="mt-2 space-y-1.5">
      {a.changes.map((c, ci) => (
        <li key={ci} className="flex items-start justify-between gap-3 text-sm text-muted-foreground">
          <span className="min-w-0">
            <span className="mr-1.5 tabular-nums">{ci + 1}.</span>
            <OutsideText text={c.summary} outside={outside} />
          </span>
          <Badge variant={levelTone(c.level)} className="opacity-80">
            {c.level}
          </Badge>
        </li>
      ))}
    </ol>
  );
}

function Recent({ items, now }: { items: ApprovalItem[]; now: number }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, RECENT_PREVIEW);
  return (
    <div className="space-y-3">
      <Rows>
        {shown.map((a) => (
          <Row key={a.id} className="relative">
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 text-sm text-muted-foreground">
                <OutsideText text={approvalTitle(a)} outside={isOutside(a, "summary")} />
              </p>
              <StateBadge tone={approvalTone(a.status)}>{a.status}</StateBadge>
            </div>
            <Meta>
              <Badge variant={levelTone(a.level)}>{a.level}</Badge>
              {a.decided_at && (
                <Ago iso={a.decided_at} now={now} prefix={a.decided_by ? `${a.decided_by} ` : "decided "} />
              )}
              <RouteLink to={{ kind: "approval", id: a.id }} stretch className="font-medium text-info">
                details
              </RouteLink>
            </Meta>
          </Row>
        ))}
      </Rows>
      {items.length > RECENT_PREVIEW && (
        <Button variant="ghost" size="sm" onClick={() => setAll((v) => !v)}>
          {all ? "Show fewer" : `Show all ${items.length}`}
        </Button>
      )}
    </div>
  );
}

function Questions({ items, now }: { items: QuestionItem[]; now: number }) {
  if (items.length === 0) return <Empty>No worker is waiting on you.</Empty>;
  return (
    <Rows>
      {items.map((q) => (
        <Row key={q.id} className="relative">
          <p className="text-sm">
            <RouteLink to={{ kind: "question", id: q.id }} stretch className="mr-2 font-mono text-xs font-semibold">
              {q.run_code}
            </RouteLink>
            {q.text && <OutsideText text={q.text} outside={isOutside(q, "text")} />}
          </p>
          {q.options.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {q.options.map((o, i) => (
                <Badge key={i} variant="outline" className="h-auto whitespace-normal">
                  ({String.fromCharCode(97 + i)}) {o}
                </Badge>
              ))}
            </div>
          )}
          <Meta>
            {q.urgent && <Badge variant="bad">urgent</Badge>}
            <Ago iso={q.asked_at} now={now} prefix="asked " />
          </Meta>
        </Row>
      ))}
    </Rows>
  );
}
