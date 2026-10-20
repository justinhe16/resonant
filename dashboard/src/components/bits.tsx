import type { ReactNode } from "react";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("[--card-spacing:--spacing(5)]", className)}>
      <CardHeader>
        <CardTitle>
          <h2 className="text-sm font-medium">{title}</h2>
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

/** Shown where the remote tier carries only counts. */
export function LocalOnly({ what }: { what: string }) {
  return <p className="text-xs text-muted-foreground">{what} are shown on the local dashboard only.</p>;
}

export function Rows({ children }: { children: ReactNode }) {
  return <ul className="divide-y">{children}</ul>;
}

export function Row({ children, className }: { children: ReactNode; className?: string }) {
  return <li className={cn("py-3 first:pt-0 last:pb-0", className)}>{children}</li>;
}

export function Counts({ by, tone }: { by: [string, number][]; tone: (s: string) => BadgeTone }) {
  if (by.length === 0) return <Empty>None yet.</Empty>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {by.map(([state, n]) => (
        <Badge key={state} variant={tone(state)} className="gap-1.5">
          <span>{state.replace(/_/g, " ")}</span>
          <span className="tabular-nums opacity-80">{n}</span>
        </Badge>
      ))}
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
