import { CircleAlert, LogOut, Moon, RefreshCw, Sun, WifiOff } from "lucide-react";
import { type ReactNode, useMemo } from "react";
import { Dot } from "@/components/bits";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { type Loader, useNow, usePolling } from "@/lib/hooks";
import { forTier, staleness, worstHealth } from "@/lib/snapshot";
import { duration, relative } from "@/lib/time";
import { useTheme } from "@/lib/theme";
import { healthTone } from "@/lib/tone";
import { Claude } from "@/sections/Claude";
import { Extensions } from "@/sections/Extensions";
import { Pulse } from "@/sections/Pulse";
import { Rules } from "@/sections/Rules";
import { SystemStatus } from "@/sections/SystemStatus";
import { WaitingOnYou } from "@/sections/WaitingOnYou";
import { ActiveRuns, ActiveTasks, RunsAndTasks, Slots } from "@/sections/Work";

export function Dashboard({ load, onSignOut }: { load: Loader; onSignOut?: () => void }) {
  const { snapshot: raw, error, loading, refresh } = usePolling(load);
  const snap = useMemo(() => (raw ? forTier(raw) : null), [raw]);
  const now = useNow();

  const stale = snap ? staleness(snap, now) : null;
  const worst = snap?.health.length ? worstHealth(snap.health.map((h) => h.state)) : "unknown";
  // One bad section shows its own error; the next snapshot retries it.
  const guard = (node: ReactNode) => <ErrorBoundary resetKey={snap?.taken_at ?? ""}>{node}</ErrorBoundary>;

  return (
    <div className="min-h-dvh pb-[env(safe-area-inset-bottom)]">
      <header className="sticky top-0 z-10 border-b bg-background/80 pt-[env(safe-area-inset-top)] backdrop-blur supports-[backdrop-filter]:bg-background/70">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4 sm:px-6 lg:px-8">
          <Dot tone={snap && !stale ? healthTone(worst) : "neutral"} />
          <span className="font-semibold tracking-tight">Resonant</span>
          {snap && (
            <span className="truncate text-xs text-muted-foreground" data-testid="last-seen">
              seen {relative(snap.taken_at, now)}
            </span>
          )}
          <div className="ml-auto flex items-center gap-0.5">
            <Button variant="ghost" size="icon" onClick={refresh} aria-label="Refresh now">
              <RefreshCw />
            </Button>
            <ThemeButton />
            {onSignOut && (
              <Button variant="ghost" size="icon" onClick={onSignOut} aria-label="Sign out">
                <LogOut />
              </Button>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
        {stale && snap && (
          <Alert variant="bad" data-testid="stale-banner">
            <CircleAlert />
            {stale.kind === "snapshot" ? (
              <>
                <AlertTitle>No fresh data for {duration(snap.taken_at, now)}</AlertTitle>
                <AlertDescription>
                  {snap.tier === "remote"
                    ? "The Mini has stopped sending snapshots. It may be asleep, frozen or offline."
                    : "The daemon isn't producing new snapshots."}{" "}
                  What's below is the last thing it reported.
                </AlertDescription>
              </>
            ) : (
              <>
                <AlertTitle>The loop hasn't ticked for {duration(snap.last_tick ?? snap.started_at, now)}</AlertTitle>
                <AlertDescription>
                  The daemon answers but its loop looks stuck, so nothing new is being worked on.
                </AlertDescription>
              </>
            )}
          </Alert>
        )}
        {error && (
          <Alert variant={snap ? "warn" : "bad"} data-testid="error-banner">
            <WifiOff />
            <AlertTitle>Can't reach Resonant</AlertTitle>
            <AlertDescription className="break-words">{error}</AlertDescription>
          </Alert>
        )}
        {!snap && loading && <p className="py-10 text-center text-sm text-muted-foreground">Loading…</p>}
        {!snap && !loading && !error && (
          <p className="py-10 text-center text-sm text-muted-foreground">No snapshot has been sent yet.</p>
        )}
        {snap && (
          <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
            <div className="space-y-6" data-testid="column-left">
              {guard(<SystemStatus snap={snap} now={now} stale={stale !== null} />)}
              {guard(<Slots snap={snap} />)}
              {guard(<ActiveRuns snap={snap} now={now} />)}
              {guard(<ActiveTasks snap={snap} />)}
            </div>
            <div className="space-y-6" data-testid="column-right">
              {guard(<WaitingOnYou snap={snap} now={now} />)}
              {guard(<RunsAndTasks snap={snap} now={now} />)}
              {guard(<Claude snap={snap} now={now} />)}
              {guard(<Extensions snap={snap} now={now} />)}
              {guard(<Pulse snap={snap} now={now} />)}
              {/* Rules and repos exist only in the local tier. */}
              {snap.tier === "local" && guard(<Rules rules={snap.trust_rules ?? []} repos={snap.repos ?? []} />)}
            </div>
          </div>
        )}
      </main>

      {snap && (
        <footer className="mx-auto max-w-6xl px-4 pb-10 text-xs text-muted-foreground sm:px-6 lg:px-8">
          <span className="font-mono">{snap.version}</span> · {snap.tier} · updates every 10s
        </footer>
      )}
    </div>
  );
}

function ThemeButton() {
  const [theme, toggle] = useTheme();
  const next = theme === "light" ? "dark" : "light";
  return (
    <Button variant="ghost" size="icon" onClick={toggle} aria-label={`Switch to ${next} mode`} title={`Switch to ${next} mode`}>
      {theme === "light" ? <Moon /> : <Sun />}
    </Button>
  );
}
