import { Ago } from "@/components/bits";
import { StatWidget } from "@/components/panel/StatWidget";
import { type Route, toHash } from "@/lib/route";
import { cn } from "@/lib/utils";
import type { Panel, StatWidget as Stat } from "@/types";

/** A panel at a glance on the overview: up to 4 stats from its first view and its age. It is
 * greyed when stale, and opens the extension's page. */
export function PanelGlance({ ext, panel: p, now }: { ext: string; panel: Panel; now: number }) {
  const stats = (p.views[0]?.widgets.filter((w): w is Stat => w.type === "stat") ?? []).slice(0, 4);
  const to: Route = { kind: "ext", name: ext };
  return (
    <a
      href={toHash(to)}
      className={cn(
        "relative z-10 mt-1 mb-1 block rounded-lg border bg-background/50 px-3 py-2 outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50",
        p.stale && "opacity-60",
      )}
      data-testid="panel-glance"
      data-stale={p.stale || undefined}
      aria-label={`${p.title}, open the ${ext} dashboard`}
    >
      <div className="flex items-baseline justify-between gap-2 text-xs text-muted-foreground">
        <span className="truncate font-medium text-foreground">{p.title}</span>
        <span className="shrink-0">
          {p.last_run_at === null && !p.updated_at ? "waiting for first run" : <Ago iso={p.updated_at} now={now} prefix="updated " />}
        </span>
      </div>
      {stats.length > 0 && (
        <div className="mt-1.5 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {stats.map((w, i) => (
            <StatWidget key={i} w={w} compact />
          ))}
        </div>
      )}
    </a>
  );
}
