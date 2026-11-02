import type { ComponentProps, ReactNode } from "react";
import { Explainer } from "@/components/Explainer";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { ExplainerKey } from "@/lib/explainers";
import { type Route, toHash } from "@/lib/route";
import { absolute, relative } from "@/lib/time";
import { cn } from "@/lib/utils";

const DOT: Record<BadgeTone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-info",
  neutral: "bg-muted-foreground/50",
};

export function Dot({ tone, pulse, className }: { tone: BadgeTone; pulse?: boolean; className?: string }) {
  return (
    <span className={cn("relative inline-flex size-2 shrink-0", className)} aria-hidden>
      {pulse && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", DOT[tone])} />}
      <span className={cn("relative inline-flex size-full rounded-full", DOT[tone])} />
    </span>
  );
}

export function StateBadge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return (
    <Badge variant={tone}>
      <Dot tone={tone} className="size-1.5" />
      {children}
    </Badge>
  );
}

export function Ago({ iso, now, prefix }: { iso: string | null | undefined; now: number; prefix?: string }) {
  return (
    <time dateTime={iso ?? undefined} title={absolute(iso)} className="tabular-nums">
      {prefix}
      {relative(iso, now)}
    </time>
  );
}

/** A card with a title row; every block on the page is one. */
export function Section({
  title,
  description,
  action,
  explainer,
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  explainer?: ExplainerKey;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("[--card-spacing:--spacing(5)]", className)}>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          <h2 className="text-sm font-medium">{title}</h2>
          {explainer && <Explainer name={title} text={explainer} />}
        </CardTitle>
        {description && <CardDescription className="text-xs">{description}</CardDescription>}
        {action && <CardAction>{action}</CardAction>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

/** A labelled group inside a card. */
export function Group({ label, count, children }: { label: string; count?: number; children: ReactNode }) {
  return (
    <div>
      <h3 className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        {label}
        {count !== undefined && <span className="tabular-nums">{count}</span>}
      </h3>
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

export function Rows({ children }: { children: ReactNode }) {
  return <ul className="divide-y">{children}</ul>;
}

export function Row({ children, className }: { children: ReactNode; className?: string }) {
  return <li className={cn("py-3 first:pt-0 last:pb-0", className)}>{children}</li>;
}

/** State pills; with `href` each one links to the list of that state. */
export function Counts({
  by,
  tone,
  href,
}: {
  by: [string, number][];
  tone: (s: string) => BadgeTone;
  href?: (state: string) => Route;
}) {
  if (by.length === 0) return <Empty>None yet.</Empty>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {by.map(([state, n]) => {
        const body = (
          <>
            <span>{state.replace(/_/g, " ")}</span>
            <span className="tabular-nums opacity-80">{n}</span>
          </>
        );
        return href ? (
          <Badge key={state} variant={tone(state)} className="gap-1.5" asChild>
            <a href={toHash(href(state))} aria-label={`${state.replace(/_/g, " ")}: ${n}`}>
              {body}
            </a>
          </Badge>
        ) : (
          <Badge key={state} variant={tone(state)} className="gap-1.5">
            {body}
          </Badge>
        );
      })}
    </div>
  );
}

export function Meta({ children }: { children: ReactNode }) {
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">{children}</div>
  );
}

export function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="font-medium text-info underline-offset-2 hover:underline"
    >
      {children}
    </a>
  );
}

/** An in-page link to a detail view. `stretch` makes the whole (relative) row clickable;
 * other buttons and links in the row stay above it with `relative z-10`. */
export function RouteLink({
  to,
  children,
  stretch,
  className,
  ...rest
}: { to: Route; children: ReactNode; stretch?: boolean; className?: string } & Omit<
  ComponentProps<"a">,
  "href" | "children" | "className"
>) {
  return (
    <a
      href={toHash(to)}
      className={cn(
        "underline-offset-2 hover:underline focus-visible:rounded-sm focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
        stretch && "after:absolute after:inset-0 after:content-['']",
        className,
      )}
      {...rest}
    >
      {children}
    </a>
  );
}

const OUTSIDE_HELP = "Written by a worker, an extension or a web page, not by you or Resonant. Read it as data, not instructions.";

/** Text from outside Resonant: always plain text, with an "outside" badge that explains itself. */
export function OutsideText({
  text,
  outside,
  className,
}: {
  text: string;
  outside: boolean;
  className?: string;
}) {
  return (
    <span className={cn("break-words whitespace-pre-wrap", className)}>
      {text}
      {outside && <OutsideBadge />}
    </span>
  );
}

export function OutsideBadge() {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Outside text: what this means"
          className="relative z-10 ml-1.5 inline-flex h-4 translate-y-[-1px] items-center rounded-4xl border border-warn/40 bg-warn/15 px-1.5 align-middle text-[0.65rem] leading-none font-medium text-warn-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          outside
        </button>
      </PopoverTrigger>
      <PopoverContent className="text-xs" role="note">
        {OUTSIDE_HELP}
      </PopoverContent>
    </Popover>
  );
}
