import { ChevronDown } from "lucide-react";
import { type ReactNode, useCallback, useState } from "react";
import { Explainer } from "@/components/Explainer";
import { Empty, Rows } from "@/components/bits";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { ExplainerKey } from "@/lib/explainers";

const KEY = (id: string) => `resonant.section.${id}`;

/** Remembered open/closed per section; storage that throws (private mode, blocked) is ignored. */
function readOpen(id: string, fallback: boolean): boolean {
  try {
    const v = window.localStorage.getItem(KEY(id));
    return v === "open" ? true : v === "closed" ? false : fallback;
  } catch {
    return fallback;
  }
}

function writeOpen(id: string, open: boolean) {
  try {
    window.localStorage.setItem(KEY(id), open ? "open" : "closed");
  } catch {
    /* not remembered */
  }
}

export interface CollapsibleSectionProps<T> {
  /** Storage key suffix and test id: resonant.section.<id> */
  id: string;
  title: string;
  /** How many exist (for runs: from the counts, which can exceed the rows the snapshot keeps). */
  total: number;
  /** Newest first; collapsed shows `previewCount`, open pages by `pageSize`. */
  rows?: T[];
  render?: (row: T, index: number) => ReactNode;
  previewCount?: number;
  pageSize?: number;
  defaultOpen?: boolean;
  explainer?: ExplainerKey;
  /** Shown in both states under the header (counts, pills). */
  summary?: ReactNode;
  /** Shown when there are no rows. */
  empty?: ReactNode;
  /** Shown only while open (other groups, details). */
  children?: ReactNode;
  action?: ReactNode;
  /** Told after the last row when the snapshot keeps fewer rows than exist. */
  keptNote?: ReactNode;
}

/**
 * A card whose header is a button (aria-expanded) that opens and closes it. Closed it still
 * shows the newest few rows; open it shows a page of them and a "Show more" button.
 */
export function CollapsibleSection<T>({
  id,
  title,
  total,
  rows,
  render,
  previewCount = 3,
  pageSize = 10,
  defaultOpen = false,
  explainer,
  summary,
  empty,
  children,
  action,
  keptNote,
}: CollapsibleSectionProps<T>) {
  const [open, setOpen] = useState(() => readOpen(id, defaultOpen));
  const [pages, setPages] = useState(1);
  const toggle = useCallback(
    (next: boolean) => {
      setOpen(next);
      writeOpen(id, next);
      if (!next) setPages(1);
    },
    [id],
  );
  const all = rows ?? [];
  const visible = open ? Math.min(all.length, pages * pageSize) : Math.min(all.length, previewCount);
  const shown = all.slice(0, visible);
  const left = all.length - visible;
  const count = rows ? `${visible} of ${total}` : `${total}`;
  return (
    <Collapsible open={open} onOpenChange={toggle} data-testid={`section-${id}`}>
      <Card className="[--card-spacing:--spacing(5)]">
        <CardHeader>
          <div className="flex items-center gap-1.5">
            <h2 className="min-w-0 flex-1 text-sm font-medium">
              <CollapsibleTrigger className="group/section -mx-2 flex w-[calc(100%+1rem)] items-center gap-2 rounded-lg px-2 py-1 text-left outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50">
                <span className="min-w-0 truncate">
                  {title} <span className="font-normal text-muted-foreground tabular-nums">({count})</span>
                </span>
                <ChevronDown className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform group-data-open/section:rotate-180" />
              </CollapsibleTrigger>
            </h2>
            {action}
            {explainer && <Explainer name={title} text={explainer} />}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {summary}
          {rows &&
            render &&
            (all.length === 0 ? empty && <Empty>{empty}</Empty> : <Rows>{shown.map((r, i) => render(r, i))}</Rows>)}
          <CollapsibleContent className="space-y-4">
            {open && left > 0 && (
              <Button variant="outline" size="sm" onClick={() => setPages((p) => p + 1)}>
                Show more ({left} left)
              </Button>
            )}
            {open && left === 0 && all.length < total && keptNote && (
              <p className="text-xs text-muted-foreground">{keptNote}</p>
            )}
            {children}
          </CollapsibleContent>
        </CardContent>
      </Card>
    </Collapsible>
  );
}
