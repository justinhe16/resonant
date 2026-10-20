import { Ago, ExternalLink, Group, LocalOnly, Row, Rows, Section, StateBadge } from "@/components/bits";
import type { BadgeTone } from "@/components/ui/badge";
import { safeHttpsUrl } from "@/lib/snapshot";
import type { BrainActivity, ClaudeBrain, Snapshot } from "@/types";

/** The worker runner (Claude Code in worktrees) and the Claude brain: its tool calls on the local tier, counts in both. */
export function Claude({ snap, now }: { snap: Snapshot; now: number }) {
  const brain = snap.claude_brain;
  const activity = snap.brain_activity;
  return (
    <Section title="Claude">
      <div className="grid gap-5 sm:grid-cols-2">
        <Group label="Worker runner">
          {snap.runner.set_up ? (
            <StateBadge tone="ok">set up</StateBadge>
          ) : (
            <div className="space-y-2">
              <StateBadge tone="warn">not set up</StateBadge>
              {snap.runner.missing && snap.runner.missing.length > 0 && (
                <p className="text-sm text-muted-foreground">Missing: {snap.runner.missing.join(", ")}</p>
              )}
            </div>
          )}
        </Group>
        {(brain || activity) && (
          <Group label="Claude brain">
            <BrainSummary brain={brain} activity={activity} />
          </Group>
        )}
      </div>
      {(brain || activity) && <BrainTools tools={brain?.tools ?? []} local={snap.tier === "local"} now={now} />}
    </Section>
  );
}

function BrainSummary({ brain, activity }: { brain?: ClaudeBrain; activity?: BrainActivity }) {
  const remoteUrl = brain?.state === "up" ? safeHttpsUrl(brain.remote_url) : null;
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      {brain && (
        <>
          <StateBadge tone={brain.state === "up" ? "ok" : "neutral"}>{brain.state}</StateBadge>
          <span className="text-muted-foreground">
            {brain.turns} turn{brain.turns === 1 ? "" : "s"}
          </span>
        </>
      )}
      {activity && (
        <span className="text-muted-foreground" data-testid="brain-activity">
          {activity.calls_today} tool call{activity.calls_today === 1 ? "" : "s"} today, {activity.writes_today} change
          {activity.writes_today === 1 ? "" : "s"}
        </span>
      )}
      {remoteUrl && <ExternalLink href={remoteUrl}>Remote Control</ExternalLink>}
    </div>
  );
}

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

function BrainTools({ tools, local, now }: { tools: NonNullable<ClaudeBrain["tools"]>; local: boolean; now: number }) {
  if (!local) {
    return (
      <div className="mt-4">
        <LocalOnly what="Its tool calls" />
      </div>
    );
  }
  if (tools.length === 0) return null;
  return (
    <div className="mt-5">
      <Group label="Brain tool calls" count={tools.length}>
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
                <StateBadge tone={t.status === "error" ? "bad" : (DECISION_TONE[t.decision] ?? "neutral")}>
                  {t.decision}
                  {t.status === "error" ? " · error" : ""}
                </StateBadge>
                <span className="text-xs text-muted-foreground">
                  <Ago iso={t.at} now={now} />
                </span>
              </div>
            </Row>
          ))}
        </Rows>
      </Group>
    </div>
  );
}
