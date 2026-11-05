import { ArrowLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { Explainer } from "@/components/Explainer";
import { StateBadge } from "@/components/bits";
import { PanelCard } from "@/components/panel/PanelCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { archivedDays, dayLabel, type DayLoader, hasHistory, LIVE, readPick, savePick } from "@/lib/panelDays";
import { closeRoute, type Route } from "@/lib/route";
import { healthTone } from "@/lib/tone";
import { cn } from "@/lib/utils";
import type { Extension, Panel, PanelDay, Snapshot } from "@/types";

/** A panel whose every view is just a price chart. */
const chartOnly = (p: Panel) => p.views.length > 0 && p.views.every((v) => v.widgets.length === 1 && v.widgets[0]!.type === "price");

/** Panels fill two columns in order; a chart-only panel that would sit alone in its row (the
 * only panel, or the last of an odd count) takes the whole row. */
function wide(panels: Panel[], i: number): boolean {
  return i === panels.length - 1 && i % 2 === 0 && chartOnly(panels[i]!);
}

/** A panel's archived copy for one day: what PanelCard draws in place of the live one. */
function asArchived(p: Panel, d: PanelDay | null): Panel {
  const views = d?.views ?? [];
  return { ...p, views, health: d?.health ?? null, truncated: d?.truncated === true, stale: false, error: null,
    fail_streak: 0, local_url: undefined, outside: views.length ? ["views"] : [] };
}

type DayState = { key: string; byPanel: Record<string, PanelDay | null>; error?: string };

/** PER-286: the archived copies of `day` for the panels that list it (the others have none). */
function useDay(ext: Extension | undefined, day: string, loadDay: DayLoader | undefined): DayState | null {
  const ids = ext && day !== LIVE ? ext.panels.filter((p) => p.history?.days.includes(day)).map((p) => p.id) : [];
  const key = ext && day !== LIVE ? `${ext.name}|${day}|${ids.join(",")}` : "";
  const [state, setState] = useState<DayState | null>(null);
  useEffect(() => {
    if (!key || !ext) return;
    let alive = true;
    const name = ext.name;
    const wanted = key.split("|")[2]!.split(",").filter(Boolean);
    void (async () => {
      const byPanel: Record<string, PanelDay | null> = {};
      let error: string | undefined;
      await Promise.all(
        wanted.map(async (id) => {
          try {
            byPanel[id] = loadDay ? await loadDay(name, id, day) : null;
          } catch (e) {
            byPanel[id] = null;
            error = e instanceof Error ? e.message : String(e);
          }
        }),
      );
      if (alive) setState({ key, byPanel, error });
    })();
    return () => {
      alive = false;
    };
    // `ext` changes on every poll; `key` is what decides a refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, loadDay]);
  return state && state.key === key ? state : null;
}

/** The day picker: Today (live) and every archived day, newest first. A native select, so it
 * works by keyboard and opens the phone's own picker. */
function DayPicker({ days, value, onChange }: { days: string[]; value: string; onChange: (d: string) => void }) {
  const newest = days[0]?.slice(0, 4);
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-muted-foreground">Day</span>
      <select
        className="h-9 rounded-md border bg-background px-2 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid="day-picker"
      >
        <option value={LIVE}>Today (live)</option>
        {days.map((d) => (
          <option key={d} value={d}>
            {dayLabel(d)}
            {d.slice(0, 4) !== newest ? `/${d.slice(0, 4)}` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

/** An extension's dashboard (`#/ext/<name>[/<panel>]`): every panel it declares as a card,
 * one column on a phone and two from `lg`. With panel history (PER-286) a day picker shows an
 * archived day instead of the live copies; the pick lasts for this browser session. */
export function ExtensionPage({
  snap,
  now,
  route,
  loadDay,
}: {
  snap: Snapshot;
  now: number;
  route: Extract<Route, { kind: "ext" }>;
  loadDay?: DayLoader;
}) {
  const ext = snap.extensions.find((e) => e.name === route.name);
  const target = route.panel;
  const [pick, setPick] = useState(() => readPick(route.name));
  const days = ext ? archivedDays(ext) : [];
  const day = pick !== LIVE && days.includes(pick) ? pick : LIVE; // a day no longer listed: live
  const choose = (d: string) => {
    setPick(d);
    savePick(route.name, d);
  };
  const archived = useDay(ext, day, loadDay);
  useEffect(() => {
    if (target) document.getElementById(`panel-${target}`)?.scrollIntoView?.({ block: "start" });
  }, [target, ext?.panels.length]);
  return (
    <div className="space-y-5" data-testid="extension-page">
      <div className="space-y-2">
        <Button variant="ghost" size="sm" onClick={closeRoute} className="-ml-2">
          <ArrowLeft />
          Back
        </Button>
        {ext ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">{ext.name}</h1>
              <StateBadge tone={healthTone(ext.health)}>{ext.health}</StateBadge>
              {ext.status !== "enabled" && <Badge variant="warn">{ext.status.replace(/_/g, " ")}</Badge>}
              <Explainer name="panels" text="panels" />
              {day !== LIVE && (
                <Badge variant="info" data-testid="archived-day">
                  {dayLabel(day)} (archived)
                </Badge>
              )}
            </div>
            {hasHistory(ext) && <DayPicker days={days} value={day} onChange={choose} />}
            {ext.description && <p className="text-sm text-muted-foreground">{ext.description}</p>}
          </>
        ) : (
          <h1 className="text-xl font-semibold">{route.name}</h1>
        )}
      </div>
      {!ext ? (
        <p className="text-sm text-muted-foreground" data-testid="ext-unknown">
          There is no extension called {route.name} in this snapshot.
        </p>
      ) : ext.panels.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="ext-no-panels">
          {ext.name} has no dashboard panels.
        </p>
      ) : day !== LIVE && !archived ? (
        <p className="text-sm text-muted-foreground" data-testid="day-loading">
          Loading {dayLabel(day)}…
        </p>
      ) : (
        <>
          {archived?.error && (
            <p className="text-sm text-bad" data-testid="day-error">
              Couldn't load some of {dayLabel(day)}: {archived.error}
            </p>
          )}
          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
            {(archived ? ext.panels.map((p) => asArchived(p, archived.byPanel[p.id] ?? null)) : ext.panels).map((p, i, all) => (
              <div key={p.id} className={cn("min-w-0", wide(all, i) && "lg:col-span-2")} data-testid="panel-cell" data-wide={wide(all, i) || undefined}>
                <PanelCard ext={ext.name} panel={p} now={now} archived={archived ? day : undefined} />
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
