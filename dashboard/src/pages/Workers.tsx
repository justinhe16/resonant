import { Ago, Counts, Empty, LocalOnly, Meta, Row, Rows, Section, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { RUN_STATE_ORDER, ordered } from "@/lib/snapshot";
import { runTone } from "@/lib/tone";
import type { RunItem, Snapshot } from "@/types";

export function Workers({ snap, now }: { snap: Snapshot; now: number }) {
  const runs = snap.runs.items;
  const questions = snap.questions.items;
  return (
    <div className="space-y-4">
      <Section
        title="Questions"
        description={snap.questions.open === 1 ? "1 open" : `${snap.questions.open} open`}
      >
        {questions === undefined ? (
          <LocalOnly what="Question texts" />
        ) : questions.length === 0 ? (
          <Empty>No worker is waiting on you.</Empty>
        ) : (
          <Rows>
            {questions.map((q) => (
              <Row key={`${q.run_code}-${q.asked_at}`}>
                <p className="text-sm">
                  <span className="mr-2 font-mono text-xs font-semibold">{q.run_code}</span>
                  {q.text}
                </p>
                {q.options.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {q.options.map((o, i) => (
                      <Badge key={o}>
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
        )}
      </Section>

      <Section title="Runs">
        <Counts by={ordered(snap.runs.by_state, RUN_STATE_ORDER)} tone={runTone} />
        {runs === undefined ? (
          <div className="mt-3">
            <LocalOnly what="Run details" />
          </div>
        ) : runs.length > 0 ? (
          <div className="mt-3 border-t pt-2">
            <Rows>
              {runs.map((r) => (
                <RunRow key={r.code} run={r} now={now} />
              ))}
            </Rows>
          </div>
        ) : null}
      </Section>
    </div>
  );
}

function RunRow({ run, now }: { run: RunItem; now: number }) {
  return (
    <Row>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">
            <span className="font-mono font-semibold text-foreground">{run.code}</span>
            {run.repo && <span> · {run.repo}</span>}
          </p>
          <p className="mt-0.5 line-clamp-2 text-sm">{run.ask}</p>
        </div>
        <StateBadge tone={runTone(run.state)}>
          {run.state}
          {run.waiting_reason ? ` (${run.waiting_reason.replace(/_/g, " ")})` : ""}
        </StateBadge>
      </div>
      {run.last_note && <p className="mt-1.5 text-xs text-muted-foreground italic">“{run.last_note}”</p>}
      <Meta>
        {run.ended_at ? (
          <Ago iso={run.ended_at} now={now} prefix="ended " />
        ) : (
          <Ago iso={run.started_at} now={now} prefix="started " />
        )}
        {run.branch && <span className="truncate font-mono">{run.branch}</span>}
        {run.pr_url && /^https:\/\//.test(run.pr_url) && (
          <a
            href={run.pr_url}
            target="_blank"
            rel="noreferrer noopener"
            className="font-medium text-info underline-offset-2 hover:underline"
          >
            PR #{run.pr_number ?? "?"}
          </a>
        )}
      </Meta>
    </Row>
  );
}
