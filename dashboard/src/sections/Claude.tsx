import { Ago, ExternalLink, Group, LocalOnly, Row, Rows, Section, StateBadge } from "@/components/bits";
import type { BadgeTone } from "@/components/ui/badge";
import { safeHttpsUrl } from "@/lib/snapshot";
import type { Snapshot } from "@/types";

const DECISION_TONE: Record<string, BadgeTone> = {
  allowed: "neutral",
  read: "neutral",
  resonant: "neutral",
  gate: "ok",
  granted: "ok",
  asked: "warn",
  refused: "bad",
  denied: "bad",
};

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function DecisionBadge({ decision, status }: { decision: string; status: "ok" | "error" }) {
  return (
    <StateBadge tone={status === "error" ? "bad" : (DECISION_TONE[decision] ?? "neutral")}>
      {decision}
      {status === "error" ? " · error" : ""}
    </StateBadge>
  );
}

/**
 * The worker runner (Claude Code in worktrees) and the Claude brain: its session, today's
 * tool calls and its last calls (with commands on the local tier, by kind on the remote one).
 */
export function Claude({ snap, now }: { snap: Snapshot; now: number }) {
  const brain = snap.claude_brain;
  const activity = snap.brain_activity;
  const state = brain?.state ?? activity?.state ?? null;
  const turns = brain?.turns ?? activity?.turns ?? 0;
  const remoteUrl = brain?.state === "up" ? safeHttpsUrl(brain.remote_url) : null;
  const tools = brain?.tools ?? [];
  const recent = activity?.recent ?? [];
  const gaps = snap.runner.gaps ?? 0;
  return (
    <Section title="Claude">
      <div className="space-y-5">
        <div className="grid gap-5 sm:grid-cols-2">
          <Group label="Worker runner">
            {snap.runner.set_up ? (
              <StateBadge tone="ok">set up</StateBadge>
            ) : (
              <div className="space-y-2">
                <StateBadge tone="warn">not set up</StateBadge>
                {snap.runner.missing && snap.runner.missing.length > 0 ? (
                  <p className="text-sm text-muted-foreground">Missing: {snap.runner.missing.join(", ")}</p>
                ) : (
                  gaps > 0 && (
                    <p className="text-sm text-muted-foreground">{plural(gaps, "thing")} to set up</p>
                  )
                )}
              </div>
            )}
          </Group>
          {(state || activity) && (
            <Group label="Claude brain">
              <div className="flex flex-wrap items-center gap-3 text-sm">
                {state && (
                  <>
                    <StateBadge tone={state === "up" ? "ok" : "neutral"}>{state}</StateBadge>
                    <span className="text-muted-foreground">{plural(turns, "turn")}</span>
                  </>
                )}
                {activity && (
                  <span className="text-muted-foreground" data-testid="brain-activity">
                    {plural(activity.calls_today, "tool call")} today, {plural(activity.writes_today, "change")}
                  </span>
                )}
                {remoteUrl && <ExternalLink href={remoteUrl}>Remote Control</ExternalLink>}
              </div>
            </Group>
          )}
        </div>
        {snap.tier === "local" && tools.length > 0 && (
          <Group label="Last tool calls">
            <Rows>
              {tools.map((t, i) => (
                <Row key={`${t.at}-${i}`} className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-mono text-sm break-all">
                      <span className="text-muted-foreground">{t.tool}</span> {t.command}
                    </p>
                    {t.cwd && <p className="mt-0.5 truncate text-xs text-muted-foreground">{t.cwd}</p>}
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <DecisionBadge decision={t.decision} status={t.status} />
                    <span className="text-xs text-muted-foreground">
                      <Ago iso={t.at} now={now} />
                    </span>
                  </div>
                </Row>
              ))}
            </Rows>
          </Group>
        )}
        {snap.tier === "remote" && recent.length > 0 && (
          <Group label="Last tool calls">
            <div className="space-y-3">
              <Rows>
                {recent.map((t, i) => (
                  <Row key={`${t.at}-${i}`} className="flex items-center justify-between gap-3">
                    <p className="font-mono text-sm">
                      {t.tool}
                      {t.write && <span className="text-muted-foreground"> · change</span>}
                    </p>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className="text-xs text-muted-foreground">
                        <Ago iso={t.at} now={now} />
                      </span>
                      <DecisionBadge decision={t.decision} status={t.status} />
                    </div>
                  </Row>
                ))}
              </Rows>
              <LocalOnly what="Its commands" />
            </div>
          </Group>
        )}
        {snap.tier === "remote" && recent.length === 0 && activity && activity.calls_today > 0 && (
          <LocalOnly what="Its tool calls" />
        )}
      </div>
    </Section>
  );
}
