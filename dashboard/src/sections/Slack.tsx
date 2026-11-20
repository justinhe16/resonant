import { CollapsibleSection } from "@/components/CollapsibleSection";
import { ListRow } from "@/components/ListRow";
import type { SlackStatus } from "@/types";

/** PER-306: the Slack adapter (Socket Mode) as the daemon sees it: connected or not, its last
 * event, and how many threads it follows. */
export function Slack({ slack, now }: { slack: SlackStatus; now: number }) {
  const threads = `${slack.followed_threads} followed thread${slack.followed_threads === 1 ? "" : "s"}`;
  return (
    <CollapsibleSection
      id="slack"
      title="Slack"
      total={1}
      rows={[slack]}
      previewCount={1}
      defaultOpen
      render={(s) => (
        <ListRow
          key="slack"
          title={s.connected ? "Socket connected" : "Socket disconnected"}
          pill={s.connected ? { tone: "ok", label: "connected" } : { tone: "bad", label: "disconnected" }}
          hint={threads}
          time={s.last_event_at}
          now={now}
          testId="slack-row"
        />
      )}
      summary={<p className="text-xs text-muted-foreground sm:hidden">{threads}</p>}
      explainer="slack"
    />
  );
}
