import type { ReactNode } from "react";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { absolute, relative } from "@/lib/time";
import { cn } from "@/lib/utils";

const DOT: Record<BadgeTone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-info",
  neutral: "bg-muted-foreground/50",
  solid: "bg-primary",
};

export function Dot({ tone, pulse, className }: { tone: BadgeTone; pulse?: boolean; className?: string }) {
  return (
    <span className={cn("relative inline-flex size-2 shrink-0", className)} aria-hidden>
      {pulse && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", DOT[tone])} />}
      <span className={cn("relative inline-flex size-2 rounded-full", DOT[tone])} />
    </span>
  );
}

export function StateBadge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return (
    <Badge tone={tone}>
      <Dot tone={tone} className="size-1.5 [&>span]:size-1.5" />
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
    <Card className={className}>
      <CardHeader>
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          {description && <CardDescription className="mt-0.5">{description}</CardDescription>}
        </div>
        {action}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-2 text-sm text-muted-foreground">{children}</p>;
}

/** Shown where the remote tier carries only counts. */
export function LocalOnly({ what }: { what: string }) {
  return <p className="py-1 text-xs text-muted-foreground">{what} are shown on the local dashboard only.</p>;
}

export function Rows({ children }: { children: ReactNode }) {
  return <ul className="-my-1 divide-y">{children}</ul>;
}

export function Row({ children, className }: { children: ReactNode; className?: string }) {
  return <li className={cn("py-3 first:pt-1 last:pb-1", className)}>{children}</li>;
}

export function Counts({ by, tone }: { by: [string, number][]; tone: (s: string) => BadgeTone }) {
  if (by.length === 0) return <Empty>None yet.</Empty>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {by.map(([state, n]) => (
        <Badge key={state} tone={tone(state)} className="gap-1.5">
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
