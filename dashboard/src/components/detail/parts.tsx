import type { ReactNode } from "react";
import { Ago } from "@/components/bits";
import { absolute, relative } from "@/lib/time";
import type { Event } from "@/types";

/** A label/value list; rows whose value is empty are left out. */
export function Fields({ children }: { children: ReactNode }) {
  return <dl className="grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">{children}</dl>;
}

export function F({ label, children }: { label: string; children: ReactNode }) {
  if (children === null || children === undefined || children === false || children === "") return null;
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </>
  );
}

/** An absolute time with how long ago it was. */
export function When({ iso, now }: { iso: string | null | undefined; now: number }) {
  if (!iso) return null;
  return (
    <span className="tabular-nums">
      <time dateTime={iso}>{absolute(iso)}</time> <span className="text-muted-foreground">({relative(iso, now)})</span>
    </span>
  );
}

export function Timeline({ events, now, empty }: { events: Event[]; now: number; empty: string }) {
  if (events.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <ol aria-label="History" className="space-y-1.5 border-l pl-3 text-sm">
      {events.map((e, i) => (
        <li key={`${e.at}-${i}`} className="flex flex-wrap items-baseline gap-x-3">
          <span className="font-mono">{e.event}</span>
          <span className="text-xs text-muted-foreground">
            <Ago iso={e.at} now={now} />
          </span>
        </li>
      ))}
    </ol>
  );
}
