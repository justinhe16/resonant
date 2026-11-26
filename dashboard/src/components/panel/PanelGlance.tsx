import { Ago, Dot } from "@/components/bits";
import { StatWidget } from "@/components/panel/StatWidget";
import { statusTone } from "@/lib/panels";
import { type Route, toHash } from "@/lib/route";
import { cn } from "@/lib/utils";
import type { Cell, Panel, StatusWidget, StatWidget as Stat, TableWidget } from "@/types";

const GLANCE_ROWS = 3;

/** A table cell as plain text for the compact preview. */
function cellText(c: Cell): string {
  if (c === null || c === undefined) return "";
  return typeof c === "object" ? c.label : String(c);
}

/** The first rows of a table, first two columns, one line each. */
function TableGlance({ w }: { w: TableWidget }) {
  const rows = w.rows.slice(0, GLANCE_ROWS);
  if (rows.length === 0) return null;
  const more = (w.total_rows ?? w.rows.length) - rows.length;
  return (
    <ul className="mt-1.5 space-y-0.5 text-xs" data-testid="glance-table">
      {rows.map((r, i) => (
        <li key={i} className="flex justify-between gap-2">
          <span className="min-w-0 truncate">{cellText(r[0] ?? null)}</span>
          <span className="shrink-0 text-muted-foreground">{cellText(r[1] ?? null)}</span>
        </li>
      ))}
      {more > 0 && <li className="text-muted-foreground">+{more} more</li>}
    </ul>
  );
}

/** A status list's items as dot, name and detail, a few at most. */
function StatusGlance({ w }: { w: StatusWidget }) {
  const items = w.items.slice(0, 4);
  if (items.length === 0) return null;
  return (
    <ul className="mt-1.5 grid grid-cols-1 gap-x-3 gap-y-0.5 text-xs sm:grid-cols-2" data-testid="glance-status">
      {items.map((it, i) => (
        <li key={i} className="flex min-w-0 items-center gap-1.5">
          <Dot tone={statusTone(it.state)} className="size-1.5" />
          <span className="truncate">{it.name}</span>
          {it.detail && <span className="shrink-0 text-muted-foreground">{it.detail}</span>}
        </li>
      ))}
    </ul>
  );
}

/** A panel at a glance on the overview: up to 4 stats from its first view, then its first
 * status list and table in a compact form, and its age. It is greyed when stale, and opens the
 * extension's page. */
export function PanelGlance({ ext, panel: p, now }: { ext: string; panel: Panel; now: number }) {
  const stats = (p.views[0]?.widgets.filter((w): w is Stat => w.type === "stat") ?? []).slice(0, 4);
  const widgets = p.views[0]?.widgets ?? [];
  const status = widgets.find((w): w is StatusWidget => w.type === "status" && w.items.length > 0);
  const table = widgets.find((w): w is TableWidget => w.type === "table" && w.rows.length > 0);
  const shown = stats.length > 0 || !!status || !!table;
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
      {status && <StatusGlance w={status} />}
      {table && <TableGlance w={table} />}
      {!shown && widgets.length > 0 && <p className="mt-1 text-xs text-muted-foreground">Open for the details.</p>}
    </a>
  );
}
