import { Dot } from "@/components/bits";
import { deltaTone, formatDelta, formatValue, panelTone } from "@/lib/panels";
import { cn } from "@/lib/utils";
import type { StatWidget as Stat } from "@/types";

const DELTA = { ok: "text-ok", bad: "text-bad", neutral: "text-muted-foreground", warn: "text-warn-foreground", info: "text-info" } as const;

/** A big number with its label: unit, a signed delta coloured by its sign, a health dot, a hint. */
export function StatWidget({ w, compact }: { w: Stat; compact?: boolean }) {
  const value = formatValue(w.value, w.format, { unit: w.unit, currency: w.currency });
  const delta = typeof w.delta === "number" ? w.delta : null;
  return (
    <div className="min-w-0 rounded-lg border bg-background/50 px-3 py-2" data-testid="stat">
      {w.title && <p className="truncate text-[0.65rem] tracking-wide text-muted-foreground uppercase">{w.title}</p>}
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {w.health && <Dot tone={panelTone(w.health)} />}
        <span className="truncate">{w.label}</span>
      </p>
      <p className={cn("truncate font-semibold tracking-tight tabular-nums", compact ? "text-lg" : "text-2xl")} data-testid="stat-value">
        {value}
      </p>
      {delta !== null && (
        <p className={cn("text-xs font-medium tabular-nums", DELTA[deltaTone(delta)])} data-testid="stat-delta">
          {formatDelta(delta, w.delta_format ?? w.format, { currency: w.currency })}
        </p>
      )}
      {w.hint && !compact && <p className="mt-0.5 text-xs text-muted-foreground">{w.hint}</p>}
    </div>
  );
}
