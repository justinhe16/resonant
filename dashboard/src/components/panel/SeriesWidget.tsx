import {
  areaPath,
  barRects,
  CHART,
  compact,
  extent,
  formatValue,
  linePath,
  PLOT,
  points,
  scale,
  stackLevels,
  tickIndexes,
  timeTick,
  xAt,
  yTicks,
} from "@/lib/panels";
import type { SeriesWidget as Series } from "@/types";

// Literal class names, so Tailwind sees them. Lines past the second also get a dash pattern,
// so the legend doesn't rely on colour alone.
const STROKE = ["stroke-chart-1", "stroke-chart-2", "stroke-chart-3", "stroke-chart-4"] as const;
const FILL = ["fill-chart-1", "fill-chart-2", "fill-chart-3", "fill-chart-4"] as const;
const BG = ["bg-chart-1", "bg-chart-2", "bg-chart-3", "bg-chart-4"] as const;
const DASH = [undefined, undefined, "5 3", "1.5 3"] as const;

function summary(w: Series): string {
  const n = w.x.length;
  const names = w.series.map((s) => s.name).join(", ");
  const last = w.series
    .map((s) => {
      const v = [...s.values].reverse().find((x) => x !== null);
      return v === undefined || v === null ? null : `${s.name} ${formatValue(v, w.format, { unit: w.unit, currency: w.currency })}`;
    })
    .filter(Boolean)
    .join(", ");
  return `${w.kind} chart of ${names}, ${n} point${n === 1 ? "" : "s"}${last ? `; latest ${last}` : ""}`;
}

/** An inline SVG line, area or bar chart (up to 4 series). A null value is a gap, not a zero. */
export function SeriesWidget({ w }: { w: Series }) {
  const n = w.x.length;
  const time = w.x_type === "time";
  const bar = w.kind === "bar";
  const stacked = w.stacked && w.kind !== "line";
  const range = extent(w.series, { stacked, zero: w.kind !== "line" });
  const xs = w.x.map((_, i) => xAt(i, w.x, { time, band: bar }));
  const span = time && n > 1 ? (w.x[n - 1] as number) - (w.x[0] as number) : 0;
  const tick = (i: number) => (time ? timeTick(w.x[i] as number, span) : String(w.x[i]));
  const levels = stacked ? stackLevels(w.series, n) : null;
  const zeroY = scale(0, range, PLOT.y1, PLOT.y0);
  return (
    <div data-testid="series">
      <svg
        viewBox={`0 0 ${CHART.w} ${CHART.h}`}
        width="100%"
        role="img"
        aria-label={summary(w)}
        className="block h-auto w-full text-muted-foreground"
      >
        {yTicks(range).map((t) => {
          const y = scale(t, range, PLOT.y1, PLOT.y0);
          return (
            <g key={t}>
              <line x1={PLOT.x0} x2={PLOT.x1} y1={y} y2={y} stroke="currentColor" strokeOpacity={0.2} strokeWidth={0.6} />
              <text x={PLOT.x0 - 4} y={y + 3} textAnchor="end" fontSize={9} fill="currentColor" aria-hidden>
                {compact(t)}
              </text>
            </g>
          );
        })}
        {range[0] < 0 && range[1] > 0 && (
          <line x1={PLOT.x0} x2={PLOT.x1} y1={zeroY} y2={zeroY} stroke="currentColor" strokeOpacity={0.5} strokeWidth={0.8} />
        )}
        {tickIndexes(n, n > 1 ? 5 : 1).map((i) => (
          <text
            key={i}
            x={xs[i]}
            y={CHART.h - 6}
            textAnchor={i === 0 && !bar ? "start" : i === n - 1 && !bar ? "end" : "middle"}
            fontSize={9}
            fill="currentColor"
            aria-hidden
          >
            {tick(i)}
          </text>
        ))}
        {bar &&
          barRects(w.series, n, range, stacked).map((r) => (
            <rect
              key={`${r.s}-${r.i}`}
              data-testid="bar"
              x={r.x}
              y={r.y}
              width={r.w}
              height={r.h}
              rx={1}
              className={FILL[r.s % 4]}
            />
          ))}
        {!bar &&
          w.series.map((s, k) => {
            const pts = points(stacked && levels ? levels[k]!.top : s.values, xs, range);
            const base = stacked && levels ? points(levels[k]!.low, xs, range) : zeroY;
            return (
              <g key={s.name + k}>
                {w.kind === "area" && (
                  <path
                    data-testid="series-area"
                    d={areaPath(pts, base)}
                    className={FILL[k % 4]}
                    fillOpacity={stacked ? 0.55 : 0.18}
                    stroke="none"
                  />
                )}
                <path
                  data-testid="series-line"
                  d={linePath(pts)}
                  fill="none"
                  strokeWidth={1.6}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  strokeDasharray={DASH[k % 4]}
                  className={STROKE[k % 4]}
                />
              </g>
            );
          })}
      </svg>
      {w.series.length > 1 && (
        <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground" data-testid="series-legend">
          {w.series.map((s, k) => (
            <li key={s.name + k} className="flex items-center gap-1.5">
              <span className={`inline-block size-2.5 rounded-sm ${BG[k % 4]}`} aria-hidden />
              {s.name}
            </li>
          ))}
        </ul>
      )}
      {w.unit && <p className="mt-1 text-xs text-muted-foreground">in {w.unit}</p>}
      {w.downsampled_from ? (
        <p className="mt-1 text-xs text-muted-foreground" data-testid="series-downsampled">
          Downsampled from {w.downsampled_from} points
        </p>
      ) : null}
    </div>
  );
}
