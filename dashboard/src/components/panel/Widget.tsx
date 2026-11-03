import { Fragment } from "react";
import { SeriesWidget } from "@/components/panel/SeriesWidget";
import { DetailsWidget, KvWidget, StatusWidget, TextWidget } from "@/components/panel/SmallWidgets";
import { StatWidget } from "@/components/panel/StatWidget";
import { TableWidget } from "@/components/panel/TableWidget";
import type { PanelWidget } from "@/types";

/** One widget, switched on its type; a type this page doesn't know (a newer daemon) is said so. */
export function Widget({ w, ext, now }: { w: PanelWidget; ext: string; now: number }) {
  switch (w.type) {
    case "stat":
      return <StatWidget w={w} />;
    case "series":
      return <SeriesWidget w={w} />;
    case "table":
      return <TableWidget w={w} ext={ext} now={now} />;
    case "status":
      return <StatusWidget w={w} now={now} />;
    case "kv":
      return <KvWidget w={w} />;
    case "text":
      return <TextWidget w={w} />;
    case "details":
      return <DetailsWidget w={w} />;
    default:
      return (
        <p className="text-xs text-muted-foreground" data-testid="widget-unknown">
          A widget of type "{String((w as { type?: unknown }).type)}" that this page can't draw yet.
        </p>
      );
  }
}

/** A view's widgets in order, with each run of stats laid out as one grid of tiles. */
export function Widgets({ widgets, ext, now }: { widgets: PanelWidget[]; ext: string; now: number }) {
  const groups: PanelWidget[][] = [];
  for (const w of widgets) {
    const last = groups.at(-1);
    if (w.type === "stat" && last && last[0]?.type === "stat") last.push(w);
    else groups.push([w]);
  }
  return (
    <div className="space-y-4">
      {groups.map((g, gi) =>
        g[0]!.type === "stat" ? (
          <div key={gi} className="grid grid-cols-2 gap-2 sm:grid-cols-3" data-testid="stat-grid">
            {g.map((w, i) => (
              <Widget key={i} w={w} ext={ext} now={now} />
            ))}
          </div>
        ) : (
          <Fragment key={gi}>
            {g.map((w, i) => (
              <section key={i} className="min-w-0" aria-label={w.title ?? undefined}>
                {w.title && <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">{w.title}</h3>}
                <Widget w={w} ext={ext} now={now} />
              </section>
            ))}
          </Fragment>
        ),
      )}
    </div>
  );
}
