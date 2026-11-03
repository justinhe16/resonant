import { useState } from "react";
import { CollapsibleSection } from "@/components/CollapsibleSection";
import { Empty, Group, OutsideText, Rows } from "@/components/bits";
import { ListRow } from "@/components/ListRow";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { approvalTitle, isOutside } from "@/lib/snapshot";
import { short } from "@/lib/time";
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
        <ListRow
          key={a.id}
          to={{ kind: "approval", id: a.id }}
          refText={a.run_code ?? undefined}
          title={approvalTitle(a)}
          outside={isOutside(a, "summary")}
          pill={{ tone: levelTone(a.level), label: a.level, plain: true, title: a.needs_code ? "needs its code" : undefined }}
          hint={a.expires_at ? `expires ${short(a.expires_at, now)}` : undefined}
          time={a.asked_at}
          now={now}
          testId="approval-row"
        />
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
          <ListRow
            key={a.id}
            to={{ kind: "approval", id: a.id }}
            refText={a.run_code ?? undefined}
            title={approvalTitle(a)}
            outside={isOutside(a, "summary")}
            muted
            pill={{ tone: approvalTone(a.status), label: a.status, title: a.decided_by ? `by ${a.decided_by}` : undefined }}
            time={a.decided_at ?? a.asked_at}
            now={now}
            testId="approval-row"
          />
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
        <ListRow
          key={q.id}
          to={{ kind: "question", id: q.id }}
          refText={q.run_code}
          title={q.text || "A question"}
          outside={!!q.text && isOutside(q, "text")}
          pill={q.urgent ? { tone: "bad", label: "urgent" } : { tone: "warn", label: "asks" }}
          hint={q.options.length > 0 ? `${q.options.length} options` : undefined}
          time={q.asked_at}
          now={now}
          testId="question-row"
        />
      ))}
    </Rows>
  );
}
