// The pure parts of the extension panels (PER-280): value formatting, tone maps and the SVG
// geometry of the series widget. Nothing here touches the DOM, so it is all unit-tested.
import type { BadgeTone } from "@/components/ui/badge";
import type { PanelFormat, PanelTone, PillTone, SeriesLine, StatusItem } from "@/types";

export const EM_DASH = "—";

/** Unix seconds as the ISO string the time helpers take. */
export function fromUnix(sec: number): string {
  const d = new Date(sec * 1000);
  return Number.isNaN(d.getTime()) ? "" : d.toISOString();
}

// --- formatting ---------------------------------------------------------------------------

const nf = (max: number, min = 0) => new Intl.NumberFormat("en-US", { maximumFractionDigits: max, minimumFractionDigits: min });

function plain(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  return nf(Math.abs(n) >= 1000 ? 0 : Math.abs(n) >= 100 ? 1 : 2).format(n);
}

function money(n: number, currency?: string | null): string {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: currency || "USD" }).format(n);
  } catch {
    return `${plain(n)} ${currency}`;
  }
}

/** Seconds as the two biggest units: "45s", "5m 30s", "2h 5m", "3d 4h". */
function duration(sec: number): string {
  const s = Math.round(Math.abs(sec));
  const sign = sec < 0 ? "-" : "";
  if (s < 60) return `${sign}${s}s`;
  const parts: [number, string][] = [
    [Math.floor(s / 86400), "d"],
    [Math.floor((s % 86400) / 3600), "h"],
    [Math.floor((s % 3600) / 60), "m"],
    [s % 60, "s"],
  ];
  const first = parts.findIndex(([n]) => n > 0);
  return sign + parts.slice(first, first + 2).filter(([n]) => n > 0).map(([n, u]) => `${n}${u}`).join(" ");
}

function bytes(n: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Math.abs(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${n < 0 ? "-" : ""}${i === 0 ? Math.round(v) : nf(1).format(v)} ${units[i]}`;
}

export interface FormatOpts {
  unit?: string | null;
  currency?: string | null;
}

/** A number as text in its declared format; a string is shown as it is, null as a dash. */
export function formatValue(v: number | string | boolean | null | undefined, format: PanelFormat = "number", opts: FormatOpts = {}): string {
  if (v === null || v === undefined) return EM_DASH;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return opts.unit ? `${v} ${opts.unit}` : v;
  let out: string;
  switch (format) {
    case "integer":
      out = nf(0).format(Math.round(v));
      break;
    case "money":
      return money(v, opts.currency);
    case "percent":
      return `${nf(1).format(v)}%`;
    case "duration":
      return duration(v);
    case "bytes":
      return bytes(v);
    default:
      out = plain(v);
  }
  return opts.unit ? `${out} ${opts.unit}` : out;
}

/** "+1.2%", "-$3.00", "0": a delta with its sign; `format` is the delta's own, else the value's. */
export function formatDelta(d: number, format: PanelFormat = "number", opts: FormatOpts = {}): string {
  if (d === 0) return "0";
  const body = formatValue(Math.abs(d), format, opts);
  return `${d > 0 ? "+" : "-"}${body}`;
}

export function deltaTone(d: number): BadgeTone {
  return d > 0 ? "ok" : d < 0 ? "bad" : "neutral";
}

// --- tones --------------------------------------------------------------------------------

export function panelTone(t: PanelTone | PillTone | null | undefined): BadgeTone {
  return t === "ok" || t === "warn" || t === "bad" ? t : "neutral";
}

export function statusTone(state: StatusItem["state"] | string): BadgeTone {
  switch (state) {
    case "up":
      return "ok";
    case "down":
      return "bad";
    case "warn":
      return "warn";
    default:
      return "neutral";
  }
}

// --- series geometry ----------------------------------------------------------------------

export const CHART = { w: 320, h: 140, left: 40, right: 8, top: 8, bottom: 22 } as const;

export interface Plot {
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

/** The drawing area inside the viewBox (y0 is the top, y1 the bottom). */
export const PLOT: Plot = { x0: CHART.left, x1: CHART.w - CHART.right, y0: CHART.top, y1: CHART.h - CHART.bottom };

export type Pt = { x: number; y: number } | null;

/** The y range to draw: the data's, with 0 included for bars and areas, and a little room. A
 * stacked chart sums its series (a null counts as 0). */
export function extent(series: SeriesLine[], opts: { stacked?: boolean; zero?: boolean } = {}): [number, number] {
  const n = Math.max(0, ...series.map((s) => s.values.length));
  let lo = Infinity;
  let hi = -Infinity;
  const see = (v: number) => {
    if (Number.isFinite(v)) {
      lo = Math.min(lo, v);
      hi = Math.max(hi, v);
    }
  };
  if (opts.stacked) {
    for (let i = 0; i < n; i++) {
      let pos = 0;
      let neg = 0;
      for (const s of series) {
        const v = s.values[i] ?? 0;
        if (v >= 0) pos += v;
        else neg += v;
      }
      see(pos);
      see(neg);
    }
  } else {
    for (const s of series) for (const v of s.values) if (v !== null) see(v);
  }
  if (lo === Infinity) return [0, 1];
  if (opts.zero || opts.stacked) {
    lo = Math.min(lo, 0);
    hi = Math.max(hi, 0);
  }
  if (lo === hi) {
    const pad = lo === 0 ? 1 : Math.abs(lo) * 0.1;
    return [lo - pad, hi + pad];
  }
  const pad = opts.zero || opts.stacked ? 0 : (hi - lo) * 0.08;
  return [lo - (lo === 0 ? 0 : pad), hi + pad];
}

export function scale(v: number, [lo, hi]: [number, number], from: number, to: number): number {
  return hi === lo ? (from + to) / 2 : from + ((v - lo) / (hi - lo)) * (to - from);
}

const r = (n: number) => Math.round(n * 100) / 100;

/** The x of point `i` of `n`. A line over time is placed by its time; everything else sits on
 * evenly spaced bands (a bar's centre, a category). */
export function xAt(i: number, x: (number | string)[], opts: { time: boolean; band: boolean }, plot: Plot = PLOT): number {
  const n = x.length;
  if (opts.band) return plot.x0 + ((i + 0.5) / n) * (plot.x1 - plot.x0);
  if (opts.time) {
    const t = x as number[];
    const lo = t[0] ?? 0;
    const hi = t[n - 1] ?? 0;
    return hi === lo ? (plot.x0 + plot.x1) / 2 : plot.x0 + (((t[i] ?? lo) - lo) / (hi - lo)) * (plot.x1 - plot.x0);
  }
  return n === 1 ? (plot.x0 + plot.x1) / 2 : plot.x0 + (i / (n - 1)) * (plot.x1 - plot.x0);
}

/** Points of one series (null where the value is missing). */
export function points(values: (number | null)[], xs: number[], range: [number, number], plot: Plot = PLOT): Pt[] {
  return values.map((v, i) => (v === null || xs[i] === undefined ? null : { x: xs[i]!, y: scale(v, range, plot.y1, plot.y0) }));
}

/** A polyline that lifts the pen at nulls ("M x y L x y M …"); a lone point is a tiny dash. */
export function linePath(pts: Pt[]): string {
  const out: string[] = [];
  let pen = false;
  pts.forEach((p, i) => {
    if (!p) {
      pen = false;
      return;
    }
    const next = pts[i + 1];
    if (!pen) {
      out.push(`M${r(p.x)} ${r(p.y)}`);
      pen = true;
      if (!next) out.push(`h0.01`);
    } else {
      out.push(`L${r(p.x)} ${r(p.y)}`);
    }
  });
  return out.join(" ");
}

/** The filled shape under a line, one piece per run of points; `base` is the lower edge (the
 * same x's, e.g. the series below in a stack) or a y. */
export function areaPath(top: Pt[], base: Pt[] | number): string {
  const out: string[] = [];
  let run: number[] = [];
  const flush = () => {
    if (run.length > 0) {
      const fwd = run.map((i, k) => `${k === 0 ? "M" : "L"}${r(top[i]!.x)} ${r(top[i]!.y)}`);
      const back = [...run].reverse().map((i) => {
        const b = typeof base === "number" ? { x: top[i]!.x, y: base } : base[i];
        return `L${r(b?.x ?? top[i]!.x)} ${r(b?.y ?? (typeof base === "number" ? base : top[i]!.y))}`;
      });
      out.push(`${fwd.join(" ")} ${back.join(" ")} Z`);
    }
    run = [];
  };
  top.forEach((p, i) => {
    if (p) run.push(i);
    else flush();
  });
  flush();
  return out.join(" ");
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
  /** the series and the point it belongs to */
  s: number;
  i: number;
}

/** Bars, grouped side by side or stacked; a null draws nothing. */
export function barRects(
  series: SeriesLine[],
  n: number,
  range: [number, number],
  stacked: boolean,
  plot: Plot = PLOT,
): Rect[] {
  const band = (plot.x1 - plot.x0) / Math.max(n, 1);
  const inner = band * 0.76;
  const out: Rect[] = [];
  for (let i = 0; i < n; i++) {
    const left = plot.x0 + i * band + (band - inner) / 2;
    let pos = 0;
    let neg = 0;
    series.forEach((s, k) => {
      const v = s.values[i];
      if (v === null || v === undefined) return;
      let from = 0;
      if (stacked) {
        from = v >= 0 ? pos : neg;
        if (v >= 0) pos += v;
        else neg += v;
      }
      const ya = scale(from, range, plot.y1, plot.y0);
      const yb = scale(from + v, range, plot.y1, plot.y0);
      const w = stacked ? inner : inner / series.length;
      out.push({
        x: r(left + (stacked ? 0 : k * w)),
        y: r(Math.min(ya, yb)),
        w: r(Math.max(w - (stacked ? 0 : 1), 1)),
        h: r(Math.max(Math.abs(ya - yb), v === 0 ? 0 : 1)),
        s: k,
        i,
      });
    });
  }
  return out;
}

/** Cumulative tops of a stack: for series k, the top and the line below it (null stays null). */
export function stackLevels(series: SeriesLine[], n: number): { top: (number | null)[]; low: number[] }[] {
  const acc = new Array<number>(n).fill(0);
  return series.map((s) => {
    const low = [...acc];
    const top = s.values.map((v, i) => {
      acc[i] = (acc[i] ?? 0) + (v ?? 0);
      return v === null ? null : acc[i]!;
    });
    return { top, low };
  });
}

/** Up to `max` evenly spaced indexes, first and last included. */
export function tickIndexes(n: number, max = 6): number[] {
  if (n <= 0) return [];
  if (n <= max) return Array.from({ length: n }, (_, i) => i);
  return Array.from({ length: max }, (_, k) => Math.round((k * (n - 1)) / (max - 1)));
}

/** "14:30" within two days, else "Oct 16"; unix seconds. */
export function timeTick(sec: number, spanSeconds: number): string {
  const d = new Date(sec * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return spanSeconds <= 2 * 86400
    ? d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false })
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** Three y gridlines: the low end, the middle and the high end. */
export function yTicks(range: [number, number]): number[] {
  const [lo, hi] = range;
  return [lo, (lo + hi) / 2, hi];
}

/** A y tick label: compact, so a 40px gutter holds it ("1.2k", "3.4M"). */
export function compact(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e9) return `${nf(1).format(n / 1e9)}B`;
  if (a >= 1e6) return `${nf(1).format(n / 1e6)}M`;
  if (a >= 1e4) return `${nf(0).format(n / 1e3)}k`;
  if (a >= 1e3) return `${nf(1).format(n / 1e3)}k`;
  return nf(a >= 100 ? 0 : a >= 1 ? 1 : 2).format(n);
}
