import { LogOut, Monitor, Moon, RefreshCw, Sun } from "lucide-react";
import { useMemo } from "react";
import { Dot } from "@/components/bits";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { type Loader, useHashRoute, useNow, usePolling } from "@/lib/hooks";
import { forTier, staleness, worstHealth } from "@/lib/snapshot";
import { duration, relative } from "@/lib/time";
import { useTheme } from "@/lib/theme";
import { healthTone } from "@/lib/tone";
import { cn } from "@/lib/utils";
import { Approvals } from "@/pages/Approvals";
import { Extensions } from "@/pages/Extensions";
import { Overview } from "@/pages/Overview";
import { Pulse } from "@/pages/Pulse";
import { Rules } from "@/pages/Rules";
import { Workers } from "@/pages/Workers";
import type { Snapshot } from "@/types";

interface Tab {
  id: string;
  label: string;
  count?: number;
}

function tabsFor(snap: Snapshot | null): Tab[] {
  const tabs: Tab[] = [
    { id: "overview", label: "Overview" },
    { id: "workers", label: "Workers", count: snap?.questions.open },
    { id: "approvals", label: "Approvals", count: snap?.approvals.open },
    { id: "pulse", label: "Pulse", count: snap?.pulse.open },
    { id: "extensions", label: "Extensions" },
  ];
  // Rules and repos exist only in the local tier.
  if (snap?.tier === "local") {
    tabs.push({ id: "rules", label: "Rules & repos" });
  }
  return tabs;
}

export function Dashboard({ load, onSignOut }: { load: Loader; onSignOut?: () => void }) {
  const { snapshot: raw, error, loading, refresh } = usePolling(load);
  const snap = useMemo(() => (raw ? forTier(raw) : null), [raw]);
  const now = useNow();
  const [route, go] = useHashRoute("overview");
  const tabs = useMemo(() => tabsFor(snap), [snap]);
  const active = tabs.some((t) => t.id === route) ? route : "overview";

  const stale = snap ? staleness(snap, now) : null;
  const worst = snap?.health.length ? worstHealth(snap.health.map((h) => h.state)) : "unknown";

  return (
    <div className="min-h-dvh pb-[env(safe-area-inset-bottom)]">
      <header className="sticky top-0 z-10 border-b bg-background/85 pt-[env(safe-area-inset-top)] backdrop-blur supports-[backdrop-filter]:bg-background/70">
        <div className="mx-auto flex h-14 max-w-3xl items-center gap-3 px-4">
          <Dot tone={snap && !stale ? healthTone(worst) : "neutral"} />
          <span className="font-semibold tracking-tight">Resonant</span>
          {snap && (
            <span className="truncate text-xs text-muted-foreground" data-testid="last-seen">
              seen {relative(snap.taken_at, now)}
            </span>
          )}
          <div className="ml-auto flex items-center">
            <Button variant="ghost" size="icon" onClick={refresh} aria-label="Refresh now">
              <RefreshCw className="size-4" />
            </Button>
            <ThemeButton />
            {onSignOut && (
              <Button variant="ghost" size="icon" onClick={onSignOut} aria-label="Sign out">
                <LogOut className="size-4" />
              </Button>
            )}
          </div>
        </div>
        {snap && (
          <nav className="mx-auto max-w-3xl overflow-x-auto px-2 [scrollbar-width:none]" aria-label="Sections">
            <ul className="flex gap-1 pb-2">
              {tabs.map((t) => (
                <li key={t.id}>
                  <a
                    href={`#/${t.id}`}
                    aria-current={t.id === active ? "page" : undefined}
                    aria-label={t.count ? `${t.label}, ${t.count} open` : undefined}
                    className={cn(
                      "flex h-8 items-center gap-1.5 rounded-full px-3 text-sm whitespace-nowrap transition-colors",
                      t.id === active
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {t.label}
                    {t.count ? (
                      <span
                        className={cn(
                          "min-w-5 rounded-full px-1.5 text-center text-xs tabular-nums",
                          t.id === active ? "bg-primary-foreground/20" : "bg-warn/20 text-foreground",
                        )}
                      >
                        {t.count}
                      </span>
                    ) : null}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        )}
      </header>

      <main className="mx-auto max-w-3xl space-y-4 px-4 py-5">
        {stale && snap && (
          <Alert tone="bad" data-testid="stale-banner">
            <Dot tone="bad" className="mt-1.5" />
            <div>
              {stale.kind === "snapshot" ? (
                <>
                  <p className="font-medium">No fresh data for {duration(snap.taken_at, now)}</p>
                  <p className="text-muted-foreground">
                    {snap.tier === "remote"
                      ? "The Mini has stopped sending snapshots. It may be asleep, frozen or offline."
                      : "The daemon isn't producing new snapshots."}{" "}
                    What's below is the last thing it reported.
                  </p>
                </>
              ) : (
                <>
                  <p className="font-medium">The loop hasn't ticked for {duration(snap.last_tick ?? snap.started_at, now)}</p>
                  <p className="text-muted-foreground">
                    The daemon answers but its loop looks stuck, so nothing new is being worked on.
                  </p>
                </>
              )}
            </div>
          </Alert>
        )}
        {error && (
          <Alert tone={snap ? "warn" : "bad"} data-testid="error-banner">
            <Dot tone={snap ? "warn" : "bad"} className="mt-1.5" />
            <div className="min-w-0">
              <p className="font-medium">Can't reach Resonant</p>
              <p className="break-words text-muted-foreground">{error}</p>
            </div>
          </Alert>
        )}
        {!snap && loading && <p className="py-10 text-center text-sm text-muted-foreground">Loading…</p>}
        {!snap && !loading && !error && (
          <p className="py-10 text-center text-sm text-muted-foreground">No snapshot has been sent yet.</p>
        )}
        {snap && (
          <ErrorBoundary resetKey={`${active} ${snap.taken_at}`}>
            <Page route={active} snap={snap} now={now} go={go} stale={stale !== null} />
          </ErrorBoundary>
        )}
      </main>

      {snap && (
        <footer className="mx-auto max-w-3xl px-4 pb-8 text-xs text-muted-foreground">
          <span className="font-mono">{snap.version}</span> · {snap.tier} · updates every 10s
        </footer>
      )}
    </div>
  );
}

function Page({
  route,
  snap,
  now,
  go,
  stale,
}: {
  route: string;
  snap: Snapshot;
  now: number;
  go: (r: string) => void;
  stale: boolean;
}) {
  switch (route) {
    case "workers":
      return <Workers snap={snap} now={now} />;
    case "approvals":
      return <Approvals snap={snap} now={now} />;
    case "pulse":
      return <Pulse snap={snap} now={now} />;
    case "extensions":
      return <Extensions snap={snap} now={now} />;
    case "rules":
      return <Rules rules={snap.trust_rules ?? []} repos={snap.repos ?? []} />;
    default:
      return <Overview snap={snap} now={now} go={go} stale={stale} />;
  }
}

function ThemeButton() {
  const [pref, cycle] = useTheme();
  const Icon = pref === "light" ? Sun : pref === "dark" ? Moon : Monitor;
  return (
    <Button variant="ghost" size="icon" onClick={cycle} aria-label={`Theme: ${pref}`} title={`Theme: ${pref}`}>
      <Icon className="size-4" />
    </Button>
  );
}
