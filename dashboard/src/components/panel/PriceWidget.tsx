import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { formatValue, fromUnix, timeTick, type Plot } from "@/lib/panels";
import {
  candlePaths,
  type Candles,
  decodePrice,
  fullWindow,
  indexAt,
  isFull,
  lastClose,
  markerIndex,
  niceTicks,
  panWindow,
  priceLabel,
  priceText,
  rangeWindow,
  signedPoints,
  timeTicks,
  type TradeMark,
  tradeMark,
  triangle,
  visibleRange,
  type Win,
  xOf,
  yRange,
  zoomWindow,
} from "@/lib/price";
import { cn } from "@/lib/utils";
import type { PriceWidget as Price } from "@/types";

const HEIGHT = 260;
const PAD = { left: 8, right: 64, top: 10, bottom: 24 } as const;
const DEFAULT_WIDTH = 640;
const TAP_MS = 300;

type Chip = "30m" | "1h" | "all";
const CHIPS: { id: Chip; label: string; seconds: number | null }[] = [
  { id: "30m", label: "30m", seconds: 30 * 60 },
  { id: "1h", label: "1h", seconds: 60 * 60 },
  { id: "all", label: "All", seconds: null },
];

// Literal class names, so Tailwind sees them.
const TONE_STROKE = { ok: "stroke-ok", bad: "stroke-bad", neutral: "stroke-muted-foreground" } as const;
const TONE_FILL = { ok: "fill-ok", bad: "fill-bad", warn: "fill-warn", neutral: "fill-muted-foreground" } as const;

const clock = (sec: number) =>
  new Date(sec * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

function summary(w: Price, d: Candles): string {
  const n = d.t.length;
  const last = lastClose(d);
  const what = w.mode === "line" ? "line" : "candlestick";
  const latest = last ? `; last ${priceText(last.v, w)}${w.unit ? ` ${w.unit}` : ""}` : "";
  const trades = w.trades?.length ? `; ${w.trades.length} trade${w.trades.length === 1 ? "" : "s"} (see the Trades table)` : "";
  return `Price ${what} chart${w.title ? ` of ${w.title}` : ""}, ${n} candle${n === 1 ? "" : "s"}${latest}${trades}`;
}

/** Measures the element's width (a fixed default where nothing is laid out, like jsdom). */
function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => {
      const got = el.getBoundingClientRect().width;
      if (got > 0) setWidth(Math.round(got));
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** A price chart: candles (or a close line), trades as ▲/▼ at entry, a dot at exit and a thin
 * line between coloured by result, and markers. ctrl/⌘ + wheel or a pinch zooms the time axis,
 * a drag pans, a double click or double tap fits everything; the y axis fits what is visible.
 * Every value is also in the Trades table under it. */
export function PriceWidget({ w }: { w: Price }) {
  const d = useMemo(() => decodePrice(w), [w]);
  const n = d.t.length;
  const trades = useMemo(() => (w.trades ?? []).map((t) => tradeMark(t, d)), [w.trades, d]);
  const markers = useMemo(() => (w.markers ?? []).map((m) => ({ m, i: markerIndex(m, d) })), [w.markers, d]);
  const [boxRef, width] = useWidth();
  const plot: Plot = useMemo(() => ({ x0: PAD.left, x1: Math.max(width - PAD.right, PAD.left + 40), y0: PAD.top, y1: HEIGHT - PAD.bottom }), [width]);

  const [win, setWin] = useState<Win>(() => fullWindow(n));
  const [chip, setChip] = useState<Chip | null>("all");
  const [hover, setHover] = useState<number | null>(null); // a candle index
  const [picked, setPicked] = useState<number | null>(null); // a trade tapped or clicked
  const [pointed, setPointed] = useState<number | null>(null); // a trade under the mouse
  const svgRef = useRef<SVGSVGElement | null>(null);
  const clipId = useId().replace(/:/g, "");

  // A new snapshot with another number of candles refits.
  const seen = useRef(n);
  useEffect(() => {
    if (seen.current !== n) {
      seen.current = n;
      setWin(fullWindow(n));
      setChip("all");
    }
  }, [n]);

  const apply = useCallback((next: Win) => {
    setWin(next);
    setChip(null);
  }, []);
  const fit = useCallback(() => {
    setWin(fullWindow(n));
    setChip("all");
  }, [n]);
  const pickChip = (c: (typeof CHIPS)[number]) => {
    setWin(c.seconds === null ? fullWindow(n) : rangeWindow(d, c.seconds));
    setChip(c.id);
  };

  // y fits the visible candles and the visible trade and marker prices.
  const yr = useMemo(() => {
    const extra = [
      ...trades.flatMap((t) => [
        { i: t.i0, v: t.y0 },
        { i: t.i1, v: t.y1 },
      ]),
      ...markers.map(({ m, i }) => ({ i, v: m.price })),
    ];
    return yRange(d, win, extra);
  }, [d, win, trades, markers]);
  const paths = useMemo(() => candlePaths(d, win, plot, yr), [d, win, plot, yr]);
  const y = (v: number) => plot.y1 - ((v - yr[0]) / (yr[1] - yr[0])) * (plot.y1 - plot.y0);
  const x = (i: number) => xOf(i, win, plot);
  const { ticks, step } = niceTicks(yr[0], yr[1], 4);
  const [a, b] = visibleRange(win, n);
  const span = n > 0 ? (d.t[b] ?? 0) - (d.t[a] ?? 0) : 0;

  // --- zoom (ctrl/⌘ + wheel, which is also a trackpad pinch); a plain wheel scrolls the page
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const box = svg.getBoundingClientRect();
      const px = e.clientX - box.left;
      setWin((cur) => zoomWindow(cur, Math.exp(e.deltaY * 0.004), indexAt(px, cur, plot), n));
      setChip(null);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [plot, n]);

  // --- drag to pan, two fingers to pinch, tap for the crosshair, double tap to fit
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const drag = useRef<{ x: number; win: Win; moved: boolean } | null>(null);
  const pinch = useRef<{ dist: number; win: Win; anchor: number } | null>(null);
  const lastTap = useRef<{ at: number; x: number } | null>(null);
  const localX = (e: { clientX: number }) => e.clientX - (svgRef.current?.getBoundingClientRect().left ?? 0);
  const nearest = (px: number) => Math.min(n - 1, Math.max(0, Math.round(indexAt(px, win, plot))));

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (n === 0) return;
    pointers.current.set(e.pointerId, { x: localX(e), y: e.clientY });
    svgRef.current?.setPointerCapture?.(e.pointerId);
    if (pointers.current.size === 2) {
      const [p, q] = [...pointers.current.values()] as [{ x: number; y: number }, { x: number; y: number }];
      pinch.current = { dist: Math.hypot(p.x - q.x, p.y - q.y) || 1, win, anchor: indexAt((p.x + q.x) / 2, win, plot) };
      drag.current = null;
    } else {
      drag.current = { x: localX(e), win, moved: false };
    }
  };
  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (n === 0) return;
    const px = localX(e);
    if (pointers.current.has(e.pointerId)) pointers.current.set(e.pointerId, { x: px, y: e.clientY });
    if (pinch.current && pointers.current.size === 2) {
      const [p, q] = [...pointers.current.values()] as [{ x: number; y: number }, { x: number; y: number }];
      const dist = Math.hypot(p.x - q.x, p.y - q.y) || 1;
      apply(zoomWindow(pinch.current.win, pinch.current.dist / dist, pinch.current.anchor, n));
      return;
    }
    const dr = drag.current;
    if (dr) {
      const dx = px - dr.x;
      if (Math.abs(dx) > 3) dr.moved = true;
      if (dr.moved) {
        const perPx = (dr.win.hi - dr.win.lo) / (plot.x1 - plot.x0);
        apply(panWindow(dr.win, -dx * perPx, n));
        setHover(null);
        return;
      }
    }
    if (e.pointerType === "mouse" && px >= plot.x0 && px <= plot.x1) setHover(nearest(px));
  };
  const onPointerUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    const dr = drag.current;
    drag.current = null;
    if (!dr || dr.moved || n === 0) return;
    const px = localX(e);
    if (e.pointerType !== "mouse") {
      const now = e.timeStamp || Date.now();
      const prev = lastTap.current;
      if (prev && now - prev.at < TAP_MS && Math.abs(prev.x - px) < 24) {
        lastTap.current = null;
        fit();
        return;
      }
      lastTap.current = { at: now, x: px };
      if (px >= plot.x0 && px <= plot.x1) setHover(nearest(px));
    }
  };
  const onKeyDown = (e: ReactKeyboardEvent<SVGSVGElement>) => {
    if (n === 0) return;
    const width = win.hi - win.lo;
    const centre = (win.lo + win.hi) / 2;
    const keys: Record<string, () => void> = {
      ArrowLeft: () => apply(panWindow(win, -width * 0.1, n)),
      ArrowRight: () => apply(panWindow(win, width * 0.1, n)),
      "+": () => apply(zoomWindow(win, 0.8, centre, n)),
      "=": () => apply(zoomWindow(win, 0.8, centre, n)),
      "-": () => apply(zoomWindow(win, 1.25, centre, n)),
      "0": fit,
      Escape: () => {
        setPicked(null);
        setPointed(null);
        setHover(null);
      },
    };
    const act = keys[e.key];
    if (act) {
      e.preventDefault();
      act();
    }
  };

  const current = pointed ?? picked;
  const shown = current !== null ? trades[current] : undefined;
  const tipX = shown ? x(shown.i0) : hover !== null ? x(hover) : null;
  const full = isFull(win, n);

  return (
    <div data-testid="price" className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Time range" className="inline-flex rounded-lg border p-0.5">
          {CHIPS.map((c) => (
            <button
              key={c.id}
              type="button"
              aria-pressed={chip === c.id}
              onClick={() => pickChip(c)}
              disabled={n === 0}
              className={cn(
                "rounded-md px-2 py-0.5 text-xs font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50",
                chip === c.id ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {c.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={fit}
          disabled={full}
          className="rounded-md border px-2 py-0.5 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50"
        >
          Reset
        </button>
        <span className="ml-auto text-xs text-muted-foreground">
          {w.unit ? `in ${w.unit} · ` : ""}
          <span className="hidden sm:inline">ctrl/⌘ + scroll or pinch to zoom, drag to pan</span>
          <span className="sm:hidden">pinch to zoom, drag to pan</span>
        </span>
      </div>
      <div ref={boxRef} className="relative min-w-0">
        {n === 0 ? (
          <p className="flex h-40 items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground" data-testid="price-empty">
            No price data yet.
          </p>
        ) : (
          <svg
            ref={svgRef}
            width={width}
            height={HEIGHT}
            viewBox={`0 0 ${width} ${HEIGHT}`}
            role="img"
            tabIndex={0}
            aria-label={summary(w, d)}
            data-testid="price-chart"
            data-lo={win.lo}
            data-hi={win.hi}
            className="block w-full cursor-crosshair touch-pan-y text-muted-foreground outline-none select-none focus-visible:ring-3 focus-visible:ring-ring/50"
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onPointerLeave={(e) => e.pointerType === "mouse" && setHover(null)}
            onDoubleClick={fit}
            onKeyDown={onKeyDown}
          >
            <defs>
              <clipPath id={clipId}>
                <rect x={plot.x0} y={plot.y0 - 6} width={plot.x1 - plot.x0} height={plot.y1 - plot.y0 + 12} />
              </clipPath>
            </defs>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={plot.x0} x2={plot.x1} y1={y(t)} y2={y(t)} stroke="currentColor" strokeOpacity={0.18} strokeWidth={0.6} />
                <text x={plot.x1 + 6} y={y(t) + 3} fontSize={10} fill="currentColor" aria-hidden>
                  {priceLabel(t, step, w)}
                </text>
              </g>
            ))}
            {timeTicks(a, b, Math.max(2, Math.floor((plot.x1 - plot.x0) / 110))).map((i) => (
              <text key={i} x={Math.min(Math.max(x(i), plot.x0 + 16), plot.x1 + PAD.right - 18)} y={HEIGHT - 7} textAnchor="middle" fontSize={10} fill="currentColor" aria-hidden>
                {timeTick(d.t[i]!, span)}
              </text>
            ))}
            <g clipPath={`url(#${clipId})`}>
              {paths.line ? (
                <path data-testid="price-line" d={paths.line} fill="none" strokeWidth={1.75} strokeLinejoin="round" className="stroke-chart-1" />
              ) : null}
              <path data-testid="price-wicks-up" d={paths.upWick} strokeWidth={1} className="stroke-up" fill="none" />
              <path data-testid="price-wicks-down" d={paths.downWick} strokeWidth={1} className="stroke-down" fill="none" />
              {paths.bodies && (
                <>
                  {/* up candles hollow, down candles filled: direction isn't colour alone */}
                  <path data-testid="price-bodies-up" d={paths.upBody} strokeWidth={1} className="fill-card stroke-up" />
                  <path data-testid="price-bodies-down" d={paths.downBody} strokeWidth={1} className="fill-down stroke-down" />
                </>
              )}
              {markers.map(({ m, i }, k) => {
                const mx = x(i);
                const my = y(m.price);
                return (
                  <g key={`m${k}`} data-testid="price-marker" className={TONE_FILL[m.tone]}>
                    {m.kind === "dot" ? (
                      <circle cx={mx} cy={my} r={4} className="stroke-card" strokeWidth={1.5} />
                    ) : (
                      <path d={triangle(mx, m.kind === "up" ? my + 8 : my - 8, m.kind === "up", 9)} className="stroke-card" strokeWidth={1.5} />
                    )}
                    {m.label ? <title>{m.label}</title> : null}
                  </g>
                );
              })}
              {trades.map((t, k) => (
                <TradeMarks
                  key={`t${k}`}
                  t={t}
                  x0={x(t.i0)}
                  x1={x(t.i1)}
                  y0={y(t.y0)}
                  y1={y(t.y1)}
                  active={current === k}
                  onPick={() => setPicked((p) => (p === k ? null : k))}
                  onHover={(on) => setPointed(on ? k : null)}
                />
              ))}
              {hover !== null && !shown && (
                <line
                  data-testid="price-crosshair"
                  x1={x(hover)}
                  x2={x(hover)}
                  y1={plot.y0}
                  y2={plot.y1}
                  stroke="currentColor"
                  strokeOpacity={0.6}
                  strokeWidth={1}
                  strokeDasharray="3 3"
                  pointerEvents="none"
                />
              )}
            </g>
          </svg>
        )}
        {tipX !== null && (shown || hover !== null) && (
          <div
            aria-hidden
            data-testid="price-tooltip"
            className="pointer-events-none absolute top-1 z-10 max-w-[16rem] rounded-md border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-md"
            style={tipX > width / 2 ? { right: Math.max(width - tipX + 8, 4) } : { left: tipX + 8 }}
          >
            {shown ? <TradeTip t={shown} w={w} /> : <CandleTip i={hover!} d={d} w={w} />}
          </div>
        )}
      </div>
      {(trades.length > 0 || markers.length > 0) && (
        <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground" data-testid="price-legend">
          {trades.length > 0 && (
            <>
              <li>▲ long entry</li>
              <li>▼ short entry</li>
              <li>● exit</li>
              <li className="flex items-center gap-1">
                <span className="inline-block h-0.5 w-3 bg-ok" aria-hidden /> win
              </li>
              <li className="flex items-center gap-1">
                <span className="inline-block h-0.5 w-3 bg-bad" aria-hidden /> loss
              </li>
              <li>dashed: still open</li>
            </>
          )}
        </ul>
      )}
      {w.aggregated_from ? (
        <p className="text-xs text-muted-foreground" data-testid="price-aggregated">
          {w.aggregated_from} candles combined into {n} wider ones
        </p>
      ) : null}
      {trades.length > 0 && <TradesTable trades={trades} w={w} />}
    </div>
  );
}

function TradeMarks(props: {
  t: TradeMark;
  x0: number;
  x1: number;
  y0: number;
  y1: number;
  active: boolean;
  onPick: () => void;
  onHover: (on: boolean) => void;
}) {
  const { t, x0, x1, y0, y1, active } = props;
  const long = t.trade.side === "long";
  const hover = {
    onPointerEnter: (e: ReactPointerEvent) => e.pointerType === "mouse" && props.onHover(true),
    onPointerLeave: (e: ReactPointerEvent) => e.pointerType === "mouse" && props.onHover(false),
    onClick: (e: React.MouseEvent) => {
      e.stopPropagation();
      props.onPick();
    },
    onPointerDown: (e: ReactPointerEvent) => e.stopPropagation(),
  };
  return (
    <g data-testid="price-trade" data-tone={t.tone} data-open={t.open || undefined}>
      <line
        data-testid="price-trade-line"
        x1={x0}
        y1={y0}
        x2={x1}
        y2={y1}
        strokeWidth={active ? 3 : 2}
        strokeDasharray={t.open ? "4 3" : undefined}
        className={TONE_STROKE[t.tone]}
      />
      <path
        data-testid="price-trade-entry"
        d={triangle(x0, long ? y0 + 7 : y0 - 7, long, 10)}
        className={cn(TONE_FILL[t.tone], "stroke-card")}
        strokeWidth={1.5}
      />
      {!t.open && <circle data-testid="price-trade-exit" cx={x1} cy={y1} r={4} className={cn(TONE_FILL[t.tone], "stroke-card")} strokeWidth={1.5} />}
      {/* hit targets bigger than the marks */}
      <circle cx={x0} cy={long ? y0 + 7 : y0 - 7} r={12} fill="transparent" className="cursor-pointer" data-testid="price-trade-hit" {...hover} />
      {!t.open && <circle cx={x1} cy={y1} r={12} fill="transparent" className="cursor-pointer" {...hover} />}
    </g>
  );
}

function TradeTip({ t, w }: { t: TradeMark; w: Price }) {
  const tr = t.trade;
  return (
    <div className="space-y-0.5">
      <p className="text-muted-foreground">
        {clock(tr.entry_t)}
        {tr.exit_t !== null && tr.exit_t !== undefined ? ` → ${clock(tr.exit_t)}` : " → open"}
      </p>
      <p>
        <span className="font-medium">{tr.side === "long" ? "Long" : "Short"}</span> {priceText(tr.entry_px, w)} → {t.open ? `${priceText(t.y1, w)} (last)` : priceText(tr.exit_px, w)}
      </p>
      <p>
        <span className="font-semibold">{signedPoints(t.points)}</span> {w.unit ?? "pts"}
        {tr.pnl !== null && tr.pnl !== undefined && (
          <>
            {" · "}
            <span className={cn("font-semibold", tr.pnl > 0 ? "text-ok" : tr.pnl < 0 ? "text-bad" : undefined)}>
              {tr.pnl > 0 ? "+" : ""}
              {formatValue(tr.pnl, "money", { currency: tr.currency })}
            </span>
          </>
        )}
      </p>
      {(tr.why || tr.label) && <p className="text-muted-foreground">{[tr.why, tr.label].filter(Boolean).join(" · ")}</p>}
    </div>
  );
}

function CandleTip({ i, d, w }: { i: number; d: Candles; w: Price }) {
  const t = d.t[i];
  const c = d.c[i];
  return (
    <div className="space-y-0.5">
      <p className="text-muted-foreground">{t !== undefined ? clock(t) : ""}</p>
      {c === null || c === undefined ? (
        <p>no trading</p>
      ) : d.line ? (
        <p>
          <span className="font-semibold">{priceText(c, w)}</span>
        </p>
      ) : (
        <p className="grid grid-cols-[auto_auto] gap-x-2 tabular-nums">
          <span className="text-muted-foreground">O</span>
          <span className="font-semibold">{priceText(d.o[i], w)}</span>
          <span className="text-muted-foreground">H</span>
          <span className="font-semibold">{priceText(d.h[i], w)}</span>
          <span className="text-muted-foreground">L</span>
          <span className="font-semibold">{priceText(d.l[i], w)}</span>
          <span className="text-muted-foreground">C</span>
          <span className="font-semibold">{priceText(c, w)}</span>
        </p>
      )}
    </div>
  );
}

function TradesTable({ trades, w }: { trades: TradeMark[]; w: Price }) {
  return (
    <details className="rounded-md border px-2.5 py-1.5 text-xs" data-testid="price-trades">
      <summary className="cursor-pointer text-muted-foreground select-none">Trades ({trades.length})</summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left tabular-nums">
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 pr-3 font-medium">Entry</th>
              <th className="py-1 pr-3 font-medium">Side</th>
              <th className="py-1 pr-3 text-right font-medium">In</th>
              <th className="py-1 pr-3 text-right font-medium">Out</th>
              <th className="py-1 pr-3 text-right font-medium">{w.unit ?? "Points"}</th>
              <th className="py-1 pr-3 text-right font-medium">P&amp;L</th>
              <th className="py-1 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t, k) => {
              const tr = t.trade;
              return (
                <tr key={k} className="border-t">
                  <td className="py-1 pr-3 whitespace-nowrap">
                    <time dateTime={fromUnix(tr.entry_t)}>{clock(tr.entry_t)}</time>
                  </td>
                  <td className="py-1 pr-3">
                    {tr.side}
                    {tr.label ? <span className="text-muted-foreground"> · {tr.label}</span> : null}
                  </td>
                  <td className="py-1 pr-3 text-right">{priceText(tr.entry_px, w)}</td>
                  <td className="py-1 pr-3 text-right">{t.open ? "open" : priceText(tr.exit_px, w)}</td>
                  <td className="py-1 pr-3 text-right">{signedPoints(t.points)}</td>
                  <td className={cn("py-1 pr-3 text-right", t.tone === "ok" && "text-ok", t.tone === "bad" && "text-bad")}>
                    {tr.pnl === null || tr.pnl === undefined ? "—" : formatValue(tr.pnl, "money", { currency: tr.currency })}
                  </td>
                  <td className="py-1">{tr.why ?? ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}
