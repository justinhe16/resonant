// The pure parts of the price widget (PER-282): decoding the snapshot's compact columns, the
// zoom/pan window (in candle-index space, so overnight gaps don't leave holes), the y range of
// what is visible, tick choice and the SVG paths. Nothing here touches the DOM.
import { formatValue, type Plot } from "@/lib/panels";
import type { PriceMarker, PriceTrade, PriceWidget } from "@/types";

export interface Candles {
  /** each candle's open time, unix seconds */
  t: number[];
  o: (number | null)[];
  h: (number | null)[];
  l: (number | null)[];
  c: (number | null)[];
  line: boolean;
  /** the usual seconds between candles (the median gap; 60 when there is one candle) */
  step: number;
}

/** The snapshot's delta-coded columns as plain prices (see `PriceWidget`). */
export function decodePrice(w: PriceWidget): Candles {
  const q = w.q ?? 1;
  const div = 10 ** w.dp;
  const px = (k: number) => (k * q) / div;
  const n = w.c.length;
  const t: number[] = [];
  let at = w.t0;
  for (let i = 0; i < n; i++) {
    at += w.dt[i] ?? 0;
    t.push(at);
  }
  const line = w.mode === "line";
  const o: (number | null)[] = [];
  const h: (number | null)[] = [];
  const l: (number | null)[] = [];
  const c: (number | null)[] = [];
  let prev = 0;
  for (let i = 0; i < n; i++) {
    const ci = w.c[i];
    if (ci === null || ci === undefined) {
      o.push(null);
      h.push(null);
      l.push(null);
      c.push(null);
      continue;
    }
    if (line) {
      prev += ci;
      const v = px(prev);
      o.push(v);
      h.push(v);
      l.push(v);
      c.push(v);
      continue;
    }
    const open = prev + (w.o?.[i] ?? 0);
    prev = open + ci;
    o.push(px(open));
    h.push(px(open + (w.h?.[i] ?? 0)));
    l.push(px(open - (w.l?.[i] ?? 0)));
    c.push(px(prev));
  }
  const gaps = t.slice(1).map((v, i) => v - t[i]!).sort((a, b) => a - b);
  const step = gaps.length ? gaps[Math.floor(gaps.length / 2)]! : 60;
  return { t, o, h, l, c, line, step: step > 0 ? step : 60 };
}

/** The last non-null close (and its index), or null. */
export function lastClose(d: Candles): { i: number; v: number } | null {
  for (let i = d.c.length - 1; i >= 0; i--) {
    const v = d.c[i];
    if (v !== null && v !== undefined) return { i, v };
  }
  return null;
}

// --- the window -----------------------------------------------------------------------------

/** The visible stretch in candle-index space: candle `i` covers `[i - 0.5, i + 0.5]`. */
export interface Win {
  lo: number;
  hi: number;
}

/** The fewest candles a zoom shows (all of them when there are fewer). */
export const MIN_BARS = 12;

export function fullWindow(n: number): Win {
  return { lo: -0.5, hi: Math.max(n, 1) - 0.5 };
}

export function isFull(win: Win, n: number): boolean {
  const f = fullWindow(n);
  return Math.abs(win.lo - f.lo) < 1e-6 && Math.abs(win.hi - f.hi) < 1e-6;
}

/** `win` kept inside the data and at least `MIN_BARS` wide, its width kept when it can be. */
export function clampWindow(win: Win, n: number): Win {
  const full = fullWindow(n);
  const total = full.hi - full.lo;
  const width = Math.min(Math.max(win.hi - win.lo, Math.min(MIN_BARS, total)), total);
  let lo = win.lo;
  if (lo < full.lo) lo = full.lo;
  if (lo + width > full.hi) lo = full.hi - width;
  return { lo, hi: lo + width };
}

/** Zoom by `factor` (< 1 is in) keeping the index `anchor` where it is on screen. */
export function zoomWindow(win: Win, factor: number, anchor: number, n: number): Win {
  const width = win.hi - win.lo;
  const next = width * factor;
  const lo = anchor - (anchor - win.lo) * (next / width);
  return clampWindow({ lo, hi: lo + next }, n);
}

/** Move by `by` candles (positive shows later candles). */
export function panWindow(win: Win, by: number, n: number): Win {
  return clampWindow({ lo: win.lo + by, hi: win.hi + by }, n);
}

/** The last `seconds` of the data (a 30m or 1h chip), or everything when it is shorter. */
export function rangeWindow(d: Candles, seconds: number): Win {
  const n = d.t.length;
  if (n === 0) return fullWindow(0);
  const from = d.t[n - 1]! - seconds;
  let i = 0;
  while (i < n - 1 && d.t[i]! <= from) i++;
  return clampWindow({ lo: i - 0.5, hi: n - 0.5 }, n);
}

/** The first and last candle at least partly inside `win`. */
export function visibleRange(win: Win, n: number): [number, number] {
  return [Math.max(0, Math.floor(win.lo + 0.5)), Math.min(n - 1, Math.ceil(win.hi - 0.5))];
}

export function xOf(i: number, win: Win, plot: Plot): number {
  return plot.x0 + ((i - win.lo) / (win.hi - win.lo)) * (plot.x1 - plot.x0);
}

/** The index at screen x (fractional). */
export function indexAt(x: number, win: Win, plot: Plot): number {
  return win.lo + ((x - plot.x0) / (plot.x1 - plot.x0)) * (win.hi - win.lo);
}

/** Where time `sec` falls on the index axis: the candle that holds it, fractional past the
 * ends (so a trade before the first candle is off the left edge). */
export function indexOfTime(sec: number, d: Candles): number {
  const { t, step } = d;
  const n = t.length;
  if (n === 0) return 0;
  if (sec < t[0]!) return (sec - t[0]!) / step;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (t[mid]! <= sec) lo = mid;
    else hi = mid - 1;
  }
  if (lo === n - 1 && sec - t[lo]! >= step) return lo + (sec - t[lo]!) / step;
  return lo;
}

// --- the y range and ticks ------------------------------------------------------------------

/** The price range of the visible candles, plus any visible trade or marker prices, padded. */
export function yRange(d: Candles, win: Win, extra: { i: number; v: number }[] = []): [number, number] {
  const [a, b] = visibleRange(win, d.t.length);
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = a; i <= b; i++) {
    const l = d.l[i];
    const h = d.h[i];
    if (l !== null && l !== undefined) lo = Math.min(lo, l);
    if (h !== null && h !== undefined) hi = Math.max(hi, h);
  }
  for (const p of extra) {
    if (p.i >= win.lo && p.i <= win.hi && Number.isFinite(p.v)) {
      lo = Math.min(lo, p.v);
      hi = Math.max(hi, p.v);
    }
  }
  if (lo === Infinity) return [0, 1];
  if (lo === hi) {
    const pad = lo === 0 ? 1 : Math.abs(lo) * 0.001;
    return [lo - pad, hi + pad];
  }
  const pad = (hi - lo) * 0.06;
  return [lo - pad, hi + pad];
}

/** About `count` round ticks inside `[lo, hi]` and their step (1, 2, 2.5 or 5 × 10^k). */
export function niceTicks(lo: number, hi: number, count = 4): { ticks: number[]; step: number } {
  const span = hi - lo;
  if (!(span > 0) || !Number.isFinite(span)) return { ticks: [lo], step: 1 };
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((m) => m * mag >= raw) ?? 10) * mag;
  const ticks: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) ticks.push(Number(v.toPrecision(12)));
  return { ticks, step };
}

/** A y label: as many decimals as the tick step needs, in the widget's format. */
export function priceLabel(v: number, step: number, w: Pick<PriceWidget, "format" | "currency">): string {
  if (w.format === "money") return formatValue(v, "money", { currency: w.currency });
  if (w.format !== "number") return formatValue(v, w.format);
  const decimals = Math.min(6, (String(Number(step.toPrecision(6))).split(".")[1] ?? "").length);
  return new Intl.NumberFormat("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(v);
}

/** A price in a tooltip or the table: the data's own precision, up to 6 decimals. */
export function priceText(v: number | null | undefined, w: Pick<PriceWidget, "format" | "currency" | "unit">): string {
  if (v === null || v === undefined) return "—";
  if (w.format === "money") return formatValue(v, "money", { currency: w.currency });
  if (w.format !== "number") return formatValue(v, w.format, { unit: w.unit });
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 }).format(v);
}

/** Up to `max` candle indexes in `[a, b]` to label on the time axis, evenly spaced. */
export function timeTicks(a: number, b: number, max = 5): number[] {
  if (b < a) return [];
  const n = b - a + 1;
  if (n <= max) return Array.from({ length: n }, (_, k) => a + k);
  return Array.from({ length: max }, (_, k) => a + Math.round((k * (n - 1)) / (max - 1)));
}

// --- marks ----------------------------------------------------------------------------------

const r = (n: number) => Math.round(n * 100) / 100;

export interface CandlePaths {
  upBody: string;
  downBody: string;
  upWick: string;
  downWick: string;
  /** the close line (line mode, or candles too narrow for bodies) */
  line: string;
  /** candles were drawn as bodies (wide enough) */
  bodies: boolean;
}

/** One path per kind of mark, for the visible candles: a few elements however many candles,
 * so 1440 of them draw fast. A candle under 3px wide is just its wick. */
export function candlePaths(d: Candles, win: Win, plot: Plot, yr: [number, number]): CandlePaths {
  const n = d.t.length;
  const out = { upBody: [] as string[], downBody: [] as string[], upWick: [] as string[], downWick: [] as string[], line: [] as string[] };
  const y = (v: number) => plot.y1 - ((v - yr[0]) / (yr[1] - yr[0])) * (plot.y1 - plot.y0);
  const per = (plot.x1 - plot.x0) / (win.hi - win.lo);
  const bodies = !d.line && per >= 3;
  const bw = Math.max(1, Math.min(per * 0.7, 14));
  const [a, b] = visibleRange(win, n);
  let pen = false;
  for (let i = a; i <= b; i++) {
    const o = d.o[i];
    const h = d.h[i];
    const l = d.l[i];
    const c = d.c[i];
    if (o === null || h === null || l === null || c === null || o === undefined || h === undefined || l === undefined || c === undefined) {
      pen = false;
      continue;
    }
    const x = xOf(i, win, plot);
    if (d.line) {
      out.line.push(`${pen ? "L" : "M"}${r(x)} ${r(y(c))}`);
      pen = true;
      continue;
    }
    const up = c >= o;
    (up ? out.upWick : out.downWick).push(`M${r(x)} ${r(y(h))}V${r(y(l))}`);
    if (bodies) {
      const top = y(Math.max(o, c));
      const height = Math.max(y(Math.min(o, c)) - top, 1);
      (up ? out.upBody : out.downBody).push(`M${r(x - bw / 2)} ${r(top)}h${r(bw)}v${r(height)}h${r(-bw)}Z`);
    }
  }
  return {
    upBody: out.upBody.join(""),
    downBody: out.downBody.join(""),
    upWick: out.upWick.join(""),
    downWick: out.downWick.join(""),
    line: out.line.join(""),
    bodies,
  };
}

export type TradeTone = "ok" | "bad" | "neutral";

export interface TradeMark {
  trade: PriceTrade;
  /** entry and exit (or, while open, the last candle) on the index axis */
  i0: number;
  i1: number;
  y0: number;
  y1: number;
  open: boolean;
  /** exit minus entry, in the trade's favour (long: up is good); null without a price */
  points: number | null;
  tone: TradeTone;
}

/** A trade as marks: open trades run to the last close. Its tone is the sign of `pnl`, or of
 * the points when there is no `pnl`. */
export function tradeMark(tr: PriceTrade, d: Candles): TradeMark {
  const last = lastClose(d);
  const open = tr.exit_t === null || tr.exit_t === undefined || tr.exit_px === null || tr.exit_px === undefined;
  const exitPx = open ? (last?.v ?? tr.entry_px) : (tr.exit_px as number);
  const i1 = open ? (last?.i ?? d.t.length - 1) : indexOfTime(tr.exit_t as number, d);
  const dir = tr.side === "long" ? 1 : -1;
  const points = open && !last ? null : (exitPx - tr.entry_px) * dir;
  const basis = tr.pnl ?? points ?? 0;
  return {
    trade: tr,
    i0: indexOfTime(tr.entry_t, d),
    i1,
    y0: tr.entry_px,
    y1: exitPx,
    open,
    points,
    tone: basis > 0 ? "ok" : basis < 0 ? "bad" : "neutral",
  };
}

export function markerIndex(m: PriceMarker, d: Candles): number {
  return indexOfTime(m.t, d);
}

/** A triangle centred on (x, y), pointing up or down, `s` tall. */
export function triangle(x: number, y: number, up: boolean, s = 9): string {
  const h = s / 2;
  return up
    ? `M${r(x)} ${r(y - h)}L${r(x + h)} ${r(y + h)}L${r(x - h)} ${r(y + h)}Z`
    : `M${r(x)} ${r(y + h)}L${r(x + h)} ${r(y - h)}L${r(x - h)} ${r(y - h)}Z`;
}

/** A signed number of points: "+7.25", "-3", "0". */
export function signedPoints(v: number | null): string {
  if (v === null) return "—";
  const body = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(Math.abs(v));
  return v > 0 ? `+${body}` : v < 0 ? `-${body}` : "0";
}
