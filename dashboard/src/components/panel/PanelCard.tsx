import { useState } from "react";
import { Ago, Dot, ExternalLink, OutsideBadge, OutsideText } from "@/components/bits";
import { Widgets } from "@/components/panel/Widget";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { dayLabel } from "@/lib/panelDays";
import { panelTone } from "@/lib/panels";
import { cn } from "@/lib/utils";
import type { Panel } from "@/types";

/** Only a loopback URL is linked, whatever the snapshot says. */
const isLocalUrl = (u: string) => {
  const m = /^http:\/\/127\.0\.0\.1:(\d{1,5})(\/[!-~]*)?$/.exec(u);
  return m !== null && Number(m[1]) >= 1 && Number(m[1]) <= 65535;
};

/** One panel of an extension: title and age, the stale state with its error, and the views
 * (tabs when there are several). Everything in it was drawn from data the extension printed.
 * `archived` (PER-286, a YYYY-MM-DD) shows an archived day's copy instead of the live one:
 * no run state, and "No data for this day" when it has no views. */
export function PanelCard({ ext, panel: p, now, archived }: { ext: string; panel: Panel; now: number; archived?: string }) {
  const [tab, setTab] = useState(0);
  const view = p.views[Math.min(tab, p.views.length - 1)];
  const neverRun = !archived && p.last_run_at === null && p.views.length === 0;
  const failed = archived ? [] : (p.history?.failed ?? []);
  return (
    <Card className="[--card-spacing:--spacing(5)]" id={`panel-${p.id}`} data-testid="panel-card" data-stale={p.stale || undefined}>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {p.health && <Dot tone={panelTone(p.health)} />}
          <h2 className="text-sm font-medium">{p.title}</h2>
          {p.outside.length > 0 && <OutsideBadge />}
          {p.stale && !neverRun && !archived && <Badge variant="warn">stale</Badge>}
          {archived && <Badge variant="info">{dayLabel(archived)} (archived)</Badge>}
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          {p.description && <span>{p.description}</span>}
          {!archived && (
            <span data-testid="panel-updated">
              {p.updated_at ? <Ago iso={p.updated_at} now={now} prefix="updated " /> : "no data yet"}
            </span>
          )}
          {!archived && p.local_url && isLocalUrl(p.local_url) && (
            <ExternalLink href={p.local_url}>Open locally</ExternalLink>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {!archived && p.error && (
          <p className="rounded-md border border-bad/30 bg-bad/10 px-2.5 py-1.5 text-xs text-bad" data-testid="panel-error">
            {p.fail_streak > 0 ? `Failed ${p.fail_streak} time${p.fail_streak === 1 ? "" : "s"} in a row: ` : "Last run: "}
            <OutsideText text={p.error} outside={false} />
            {p.views.length > 0 && " Showing the last good copy."}
          </p>
        )}
        {neverRun && <p className="text-sm text-muted-foreground">Waiting for first run.</p>}
        {archived && p.views.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="panel-no-day">
            No data for this day.
          </p>
        )}
        {!archived && !neverRun && p.views.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="panel-empty">
            {p.truncated
              ? "Too big to show this time (over the panel size budget); the next smaller update will show."
              : "Nothing to show yet."}
          </p>
        )}
        {p.views.length > 1 && (
          <div role="tablist" aria-label={`${p.title} views`} className="inline-flex flex-wrap rounded-lg border p-0.5">
            {p.views.map((v, i) => (
              <button
                key={v.id}
                type="button"
                role="tab"
                aria-selected={i === tab}
                onClick={() => setTab(i)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                  i === tab ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {v.title}
              </button>
            ))}
          </div>
        )}
        {view && (
          <div className={cn(p.stale && !archived && "opacity-60")} data-testid="panel-body" role={p.views.length > 1 ? "tabpanel" : undefined}>
            <Widgets widgets={view.widgets} ext={ext} now={now} />
          </div>
        )}
        {p.truncated && p.views.length > 0 && (
          <p className="text-xs text-muted-foreground">Some of this panel was cut to fit its size limit.</p>
        )}
        {failed.length > 0 && (
          <p className="text-xs text-muted-foreground" data-testid="panel-days-failed">
            Couldn't archive {failed.slice(0, 3).map(dayLabel).join(", ")}
            {failed.length > 3 ? ` and ${failed.length - 3} more` : ""} (tried 3 times).
          </p>
        )}
      </CardContent>
    </Card>
  );
}
