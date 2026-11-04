import { Badge } from "@/components/ui/badge";
import { EM_DASH, formatValue, fromUnix, panelTone } from "@/lib/panels";
import { absolute, relative, short } from "@/lib/time";
import { cn } from "@/lib/utils";
import type { Cell, PillCell, TableColumn, TableWidget as Table } from "@/types";

const isPill = (c: Cell): c is PillCell => typeof c === "object" && c !== null && "tone" in c;

function Time({ sec, now, compact }: { sec: number; now: number; compact?: boolean }) {
  const iso = fromUnix(sec);
  return (
    <time dateTime={iso} title={absolute(iso)} className="tabular-nums">
      {compact ? short(iso, now) : relative(iso, now)}
    </time>
  );
}

/** One cell by its column's type. A cell that doesn't fit the column still shows as text. */
function CellView({ cell, col, ext, now }: { cell: Cell; col: TableColumn; ext: string; now: number }) {
  if (cell === null || cell === undefined) return <span className="text-muted-foreground">{EM_DASH}</span>;
  if (typeof cell === "object") {
    if (isPill(cell)) return <Badge variant={panelTone(cell.tone)}>{cell.label}</Badge>;
    return (
      <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs" title={`${ext}/${cell.tool}`}>
        {cell.label}
      </span>
    );
  }
  if (typeof cell === "number") {
    if (col.type === "time") return <Time sec={cell} now={now} />;
    return <>{formatValue(cell, col.type === "money" ? "money" : "number", { currency: col.currency })}</>;
  }
  if (col.type === "pill") return <Badge variant="neutral">{cell}</Badge>;
  return <>{cell}</>;
}

const rightAligned = (c: TableColumn) => (c.align ?? (c.type === "number" || c.type === "money" ? "right" : "left")) === "right";

/** A table from `sm` up; below it, stacked rows in the list-row pattern (first column as the
 * title, a pill and a time on the right, the other columns as a quiet line underneath). */
export function TableWidget({ w, ext, now }: { w: Table; ext: string; now: number }) {
  const cols = w.columns;
  const pillAt = cols.findIndex((c) => c.type === "pill");
  const timeAt = cols.findIndex((c) => c.type === "time");
  const rest = cols.map((c, i) => [c, i] as const).filter(([, i]) => i !== 0 && i !== pillAt && i !== timeAt);
  const shown = w.rows.length;
  return (
    <div data-testid="table">
      {shown === 0 ? (
        <p className="text-sm text-muted-foreground">No rows.</p>
      ) : (
        <>
          <div className="hidden overflow-x-auto sm:block">
            <table className="w-full text-sm" data-testid="table-wide">
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  {cols.map((c) => (
                    <th key={c.key} scope="col" className={cn("px-2 py-1.5 font-medium", rightAligned(c) ? "text-right" : "text-left")}>
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {w.rows.map((row, ri) => (
                  <tr key={ri}>
                    {cols.map((c, ci) => (
                      <td key={c.key} className={cn("px-2 py-1.5", rightAligned(c) ? "text-right tabular-nums" : "text-left")}>
                        <CellView cell={row[ci] ?? null} col={c} ext={ext} now={now} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="divide-y sm:hidden" data-testid="table-rows">
            {w.rows.map((row, ri) => {
              const first = row[0] ?? null;
              const t = timeAt >= 0 ? row[timeAt] : null;
              return (
                <li key={ri} className="py-2 first:pt-0 last:pb-0" data-testid="table-row">
                  <div className="flex min-h-8 items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm font-medium">
                      <CellView cell={first} col={cols[0]!} ext={ext} now={now} />
                    </span>
                    {pillAt > 0 && row[pillAt] != null && (
                      <span className="shrink-0">
                        <CellView cell={row[pillAt]!} col={cols[pillAt]!} ext={ext} now={now} />
                      </span>
                    )}
                    {typeof t === "number" && (
                      <span className="w-12 shrink-0 text-right text-xs text-muted-foreground">
                        <Time sec={t} now={now} compact />
                      </span>
                    )}
                  </div>
                  {rest.some(([, i]) => row[i] != null) && (
                    <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                      {rest.map(([c, i]) =>
                        row[i] == null ? null : (
                          <span key={c.key} className="tabular-nums">
                            {c.label}: <CellView cell={row[i]!} col={c} ext={ext} now={now} />
                          </span>
                        ),
                      )}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
      {w.truncated && (
        <p className="mt-2 text-xs text-muted-foreground" data-testid="table-truncated">
          Showing {shown} of {w.total_rows ?? "more"} rows
        </p>
      )}
    </div>
  );
}
