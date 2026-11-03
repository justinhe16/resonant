import { CollapsibleSection } from "@/components/CollapsibleSection";
import { ExternalLink, Group, StateBadge } from "@/components/bits";
import { ListRow } from "@/components/ListRow";
import type { BadgeTone } from "@/components/ui/badge";
import { isOutside, safeHttpsUrl } from "@/lib/snapshot";
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

export function DecisionBadge({ decision, status }: { decision: string; status: "ok" | "error" }) {
  return (
    <StateBadge tone={status === "error" ? "bad" : (DECISION_TONE[decision] ?? "neutral")}>
      {decision}
      {status === "error" ? " · error" : ""}
    </StateBadge>
  );
}

/**
 * "Claude tool calls": the Claude brain's last tool calls, one line each, with its session
 * and today's counts, and whether the worker runner is set up. The same on both tiers.
 */
export function Claude({ snap, now }: { snap: Snapshot; now: number }) {
  const brain = snap.claude_brain;
  const activity = snap.brain_activity;
  const state = brain?.state ?? activity.state;
  const turns = brain?.turns ?? activity.turns;
  const remoteUrl = brain?.state === "up" ? safeHttpsUrl(brain.remote_url) : null;
  const tools = brain?.tools ?? [];
  const gaps = snap.runner.gaps;
  return (
    <CollapsibleSection
      id="claude"
      title="Claude tool calls"
      total={Math.max(activity.calls_today, tools.length)}
      rows={tools}
      explainer="claude"
      keptNote={`The snapshot keeps the last ${snap.limits.brain_tools} tool calls.`}
      summary={
        <div className="grid gap-5 sm:grid-cols-2">
          <Group label="Worker runner">
            {snap.runner.set_up ? (
              <StateBadge tone="ok">set up</StateBadge>
            ) : (
              <div className="space-y-2">
                <StateBadge tone="warn">not set up</StateBadge>
                {snap.runner.missing.length > 0 ? (
                  <p className="text-sm text-muted-foreground">Missing: {snap.runner.missing.join(", ")}</p>
                ) : (
                  gaps > 0 && <p className="text-sm text-muted-foreground">{plural(gaps, "thing")} to set up</p>
                )}
              </div>
            )}
          </Group>
          <Group label="Claude brain">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              {state && (
                <>
                  <StateBadge tone={state === "up" ? "ok" : "neutral"}>{state}</StateBadge>
                  <span className="text-muted-foreground">{plural(turns, "turn")}</span>
                </>
              )}
              <span className="text-muted-foreground" data-testid="brain-activity">
                {plural(activity.calls_today, "tool call")} today, {plural(activity.writes_today, "change")}
              </span>
              {remoteUrl && <ExternalLink href={remoteUrl}>Remote Control</ExternalLink>}
            </div>
          </Group>
        </div>
      }
      render={(t, i) => (
        <ListRow
          key={`${t.at}-${i}`}
          to={{ kind: "brain-tool", at: t.at }}
          refText={t.tool}
          title={t.command || t.cwd || t.tool}
          outside={!!t.command && isOutside(t, "command")}
          mono
          pill={{
            tone: t.status === "error" ? "bad" : (DECISION_TONE[t.decision] ?? "neutral"),
            label: t.status === "error" ? `${t.decision} · error` : t.decision,
          }}
          time={t.at}
          now={now}
          testId="tool-row"
        />
      )}
    />
  );
}
