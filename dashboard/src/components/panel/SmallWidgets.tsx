import { type ReactNode, useState } from "react";
import { Dot } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { formatValue, statusTone } from "@/lib/panels";
import { absolute, short } from "@/lib/time";
import { cn } from "@/lib/utils";
import type { DetailRow, DetailsWidget as Details, KvWidget as Kv, StatusWidget as Status, TextWidget as Text } from "@/types";

const more = (truncated: boolean) =>
  truncated ? <p className="mt-2 text-xs text-muted-foreground">Some items were left out (the list is capped).</p> : null;

export function StatusWidget({ w, now }: { w: Status; now: number }) {
  return (
    <div data-testid="status">
      <ul className="divide-y">
        {w.items.map((it, i) => (
          <li key={i} className="flex items-center gap-2 py-1.5 first:pt-0 last:pb-0">
            <Dot tone={statusTone(it.state)} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm">{it.name}</span>
              {it.detail && <span className="block text-xs break-words text-muted-foreground">{it.detail}</span>}
            </span>
            <Badge variant={statusTone(it.state)}>{it.state}</Badge>
            {it.since && (
              <time dateTime={it.since} title={absolute(it.since)} className="w-10 shrink-0 text-right text-xs text-muted-foreground tabular-nums">
                {short(it.since, now)}
              </time>
            )}
          </li>
        ))}
      </ul>
      {more(w.truncated)}
    </div>
  );
}

export function KvWidget({ w }: { w: Kv }) {
  return (
    <div data-testid="kv">
      <dl className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-x-3 gap-y-1.5 text-sm">
        {w.items.map((it, i) => (
          <div key={i} className="col-span-2 grid grid-cols-subgrid">
            <dt className="truncate text-muted-foreground">{it.key}</dt>
            <dd className="text-right break-words tabular-nums">{formatValue(it.value, it.format ?? "number", { unit: it.unit })}</dd>
          </div>
        ))}
      </dl>
      {more(w.truncated)}
    </div>
  );
}

/** Plain text only: React escapes it, and there is no HTML path. */
export function TextWidget({ w }: { w: Text }) {
  return (
    <p className="text-sm break-words whitespace-pre-wrap" data-testid="text">
      {w.text}
    </p>
  );
}

const isContainer = (r: DetailRow) => r.kind === "object" || r.kind === "array";

/** How many rows sit under row `i` (the following rows that are deeper). */
function childCount(rows: DetailRow[], i: number): number {
  let n = 0;
  for (let j = i + 1; j < rows.length && rows[j]!.depth > rows[i]!.depth; j++) if (rows[j]!.depth === rows[i]!.depth + 1) n++;
  return n;
}

/** A flattened JSON value as an indented outline; an object or array row folds its children. */
export function DetailsWidget({ w }: { w: Details }) {
  const [open, setOpen] = useState<Set<number>>(
    () => new Set(w.collapsed ? [] : w.rows.flatMap((r, i) => (isContainer(r) ? [i] : []))),
  );
  const toggle = (i: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(i)) next.add(i);
      return next;
    });
  const out: ReactNode[] = [];
  let hideBelow: number | null = null;
  w.rows.forEach((r, i) => {
    if (hideBelow !== null) {
      if (r.depth > hideBelow) return;
      hideBelow = null;
    }
    const isOpen = open.has(i);
    if (isContainer(r) && !isOpen) hideBelow = r.depth;
    const key = r.key || (r.depth === 0 ? "data" : "");
    const indent = Math.min(r.depth, 5) * 0.9;
    const pad = { paddingLeft: `${indent}rem` };
    if (isContainer(r)) {
      const n = childCount(w.rows, i);
      out.push(
        <li key={i} style={pad}>
          <button
            type="button"
            aria-expanded={isOpen}
            onClick={() => toggle(i)}
            className="flex w-full items-center gap-1.5 rounded py-0.5 text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring/50"
          >
            <span className="w-3 text-muted-foreground" aria-hidden>
              {isOpen ? "▾" : "▸"}
            </span>
            <span className="font-mono text-xs">{key}</span>
            <span className="text-xs text-muted-foreground">
              {r.kind === "array" ? `[${n}]` : `{${n}}`}
            </span>
          </button>
        </li>,
      );
    } else {
      out.push(
        <li key={i} style={{ paddingLeft: `${indent + 1.4}rem` }} className="flex gap-1.5 py-0.5">
          <span className="shrink-0 font-mono text-xs text-muted-foreground">{key}</span>
          <span className={cn("min-w-0 font-mono text-xs break-words", r.kind === "null" && "text-muted-foreground")}>
            {r.kind === "null" ? "null" : r.kind === "string" ? JSON.stringify(r.value ?? "") : (r.value ?? "")}
          </span>
        </li>,
      );
    }
  });
  return (
    <div data-testid="details">
      <ul>{out}</ul>
      {w.truncated && <p className="mt-2 text-xs text-muted-foreground">Cut after 200 rows.</p>}
    </div>
  );
}
