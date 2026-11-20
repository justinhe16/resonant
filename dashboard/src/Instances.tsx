import { useCallback, useMemo, useState } from "react";
import { Dashboard } from "@/Dashboard";
import { Explainer } from "@/components/Explainer";
import type { Loader } from "@/lib/hooks";
import { type InstanceRef, pickInstance, readChoice, writeChoice } from "@/lib/instances";
import type { DayLoader } from "@/lib/panelDays";
import { cn } from "@/lib/utils";

/**
 * PER-306: the dashboard of one instance at a time, with a header switcher ("Resonant · Cori")
 * when there is more than one. The choice is kept for this browser session. Each instance
 * gets its own `Dashboard` (keyed by name), so polling, errors and the staleness banner are
 * per instance: a silent Cori never touches personal Resonant's view.
 */
export function InstanceDashboard({
  instances,
  loaderFor,
  dayLoaderFor,
  onSignOut,
}: {
  instances: InstanceRef[];
  /** the loader for an instance; null: the only one there is (no list) */
  loaderFor: (inst: InstanceRef | null) => Loader;
  dayLoaderFor?: (inst: InstanceRef | null) => DayLoader | undefined;
  onSignOut?: () => void;
}) {
  const [choice, setChoice] = useState<string | null>(readChoice);
  const current = pickInstance(instances, choice);
  const many = instances.length > 1;
  // Personal Resonant and the "no list yet" state share one key, so the list arriving
  // doesn't refetch the page that is already showing.
  const key = !current || current.self || !many ? "" : current.name;
  const load = useMemo(() => loaderFor(many ? current : (instances[0] ?? null)), [loaderFor, many, current, instances]);
  const loadDay = useMemo(
    () => dayLoaderFor?.(many ? current : (instances[0] ?? null)),
    [dayLoaderFor, many, current, instances],
  );
  const pick = useCallback((name: string) => {
    setChoice(name);
    writeChoice(name);
  }, []);
  return (
    <Dashboard
      key={key}
      load={load}
      loadDay={loadDay}
      onSignOut={onSignOut}
      name={many && current ? current.display_name : undefined}
      switcher={many && current ? <InstanceSwitcher instances={instances} current={current.name} onPick={pick} /> : undefined}
    />
  );
}

/** A segmented control in the header. Buttons with `aria-pressed`, so Tab and Enter/Space
 * work as everywhere else; it fits a phone's header next to the icons. */
export function InstanceSwitcher({
  instances,
  current,
  onPick,
}: {
  instances: InstanceRef[];
  current: string;
  onPick: (name: string) => void;
}) {
  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <div role="group" aria-label="Instance" className="inline-flex shrink-0 rounded-lg border bg-card p-0.5" data-testid="instance-switcher">
        {instances.map((i) => (
          <button
            key={i.name}
            type="button"
            aria-pressed={i.name === current}
            onClick={() => onPick(i.name)}
            className={cn(
              "max-w-[7rem] truncate rounded-md px-2 py-1 text-sm font-semibold tracking-tight outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:px-2.5",
              i.name === current ? "bg-muted text-foreground" : "font-medium text-muted-foreground hover:text-foreground",
            )}
          >
            {i.display_name}
          </button>
        ))}
      </div>
      <Explainer name="instances" text="instances" />
    </div>
  );
}
