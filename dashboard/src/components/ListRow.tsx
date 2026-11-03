import type { ReactNode } from "react";
import { OutsideBadge, RouteLink, StateBadge } from "@/components/bits";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import type { Route } from "@/lib/route";
import { absolute, short } from "@/lib/time";
import { cn } from "@/lib/utils";

export interface RowPill {
  tone: BadgeTone;
  label: string;
  /** a tooltip with the detail the pill leaves out (e.g. why it waits) */
  title?: string;
  /** a plain badge without the status dot */
  plain?: boolean;
}

export interface ListRowProps {
  /** the detail dialog the whole row opens */
  to?: Route;
  /** short id or ref, monospace: R11, L2, a task id's tail */
  refText?: string;
  /** full id for the ref's tooltip */
  refTitle?: string;
  /** one line, cut with an ellipsis */
  title: string;
  /** the title is outside text (shows the badge) */
  outside?: boolean;
  /** the title in a quieter colour (e.g. decided approvals) */
  muted?: boolean;
  /** the title in a monospace face (a command, a job id) */
  mono?: boolean;
  pill?: RowPill;
  /** at most one small secondary hint (a repo, a cost); hidden on a phone */
  hint?: ReactNode;
  /** relative time, with the absolute one in its tooltip */
  time?: string | null;
  now: number;
  /** extra badges after the pill (rare: muted, fails) */
  extra?: ReactNode;
  /** content on a line of its own under the row (an extension's panel glances); its links
   * need `relative z-10` to stay above the row's stretched link */
  below?: ReactNode;
  testId?: string;
}

/**
 * The one glanceable row every list uses: ref · title · pill · time (and at most one hint).
 * Everything else lives in the detail dialog the row opens.
 */
export function ListRow({
  to,
  refText,
  refTitle,
  title,
  outside,
  muted,
  mono,
  pill,
  hint,
  time,
  now,
  extra,
  below,
  testId = "list-row",
}: ListRowProps) {
  const titleCls = cn(
    "min-w-0 truncate text-sm",
    muted ? "text-muted-foreground" : "text-foreground",
    mono && "font-mono text-[0.8rem]",
  );
  return (
    <li
      className={cn("relative flex min-h-10 items-center gap-2 py-2 first:pt-0 last:pb-0 sm:gap-3", below && "flex-wrap")}
      data-testid={testId}
    >
      {refText && (
        <span
          className="max-w-[7rem] shrink-0 truncate font-mono text-xs font-semibold text-foreground tabular-nums"
          title={refTitle ?? refText}
        >
          {refText}
        </span>
      )}
      <span className="flex min-w-0 flex-1 items-center">
        {to ? (
          <RouteLink to={to} stretch className={cn(titleCls, "hover:no-underline")} title={title}>
            {title}
          </RouteLink>
        ) : (
          <span className={titleCls} title={title}>
            {title}
          </span>
        )}
        {outside && <OutsideBadge />}
      </span>
      {hint && (
        <span className="hidden max-w-[9rem] shrink-0 truncate text-xs text-muted-foreground sm:inline" data-testid="row-hint">
          {hint}
        </span>
      )}
      {extra}
      {pill && (
        <span className="shrink-0" title={pill.title}>
          {pill.plain ? <Badge variant={pill.tone}>{pill.label}</Badge> : <StateBadge tone={pill.tone}>{pill.label}</StateBadge>}
        </span>
      )}
      {time !== undefined && (
        <time
          dateTime={time ?? undefined}
          title={absolute(time)}
          className="w-12 shrink-0 text-right text-xs text-muted-foreground tabular-nums"
        >
          {short(time, now)}
        </time>
      )}
      {below && <div className="basis-full">{below}</div>}
    </li>
  );
}
