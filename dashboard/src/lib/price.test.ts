import { describe, expect, it } from "vitest";
import { PLOT } from "@/lib/panels";
import {
  candlePaths,
  clampWindow,
  decodePrice,
  fullWindow,
  indexAt,
  indexOfTime,
  isFull,
  lastClose,
  MIN_BARS,
  niceTicks,
  panWindow,
  priceLabel,
  rangeWindow,
  signedPoints,
  timeTicks,
  tradeMark,
  visibleRange,
  xOf,
  yRange,
  zoomWindow,
} from "@/lib/price";
import fixture from "@/test/fixtures/price.json";
import type { PriceWidget } from "@/types";

const candles = fixture.candles as PriceWidget;
const line = fixture.line as PriceWidget;
const T0 = 1792141200;

const tiny: PriceWidget = {
  type: "price",
  mode: "candles",
  t0: T0,
  dt: [0, 60, 60],
  dp: 2,
  q: 25,
  // 10.00/12.00/9.00/11.00, a gap, 11.00/11.00/10.00/10.00 in quarter points
  o: [40, null, 0],
  h: [8, null, 0],
  l: [4, null, 4],
  c: [4, null, -4],
  format: "number",
  trades: [],
  markers: [],
};

describe("decodePrice", () => {
  it("undoes the delta coding, ticks and gaps", () => {
    const d = decodePrice(tiny);
    expect(d.t).toEqual([T0, T0 + 60, T0 + 120]);
    expect([d.o, d.h, d.l, d.c]).toEqual([
      [10, null, 11],
      [12, null, 11],
      [9, null, 10],
      [11, null, 10],
    ]);
    expect(d.line).toBe(false);
    expect(d.step).toBe(60);
    expect(lastClose(d)).toEqual({ i: 2, v: 10 });
  });

  it("decodes the daemon's real output: candles valid, the line equal to the closes", () => {
    const d = decodePrice(candles);
    const l = decodePrice(line);
    expect(d.t).toHaveLength(120);
    expect(d.c[40]).toBeNull();
    for (let i = 0; i < 120; i++) {
      const [o, h, lo, c] = [d.o[i], d.h[i], d.l[i], d.c[i]];
      if (c === null || c === undefined) continue;
      expect(h!).toBeGreaterThanOrEqual(Math.max(o!, c));
      expect(lo!).toBeLessThanOrEqual(Math.min(o!, c));
      expect(l.c[i]).toBeCloseTo(c, 9);
    }
    expect(d.o[0]).toBe(25000);
    expect(l.line).toBe(true);
  });

  it("an empty widget is no candles", () => {
    const d = decodePrice({ ...tiny, t0: 0, dt: [], o: [], h: [], l: [], c: [] });
    expect(d.t).toEqual([]);
    expect(lastClose(d)).toBeNull();
  });
});

describe("the window", () => {
  it("fits everything, clamps and keeps a minimum width", () => {
    expect(fullWindow(100)).toEqual({ lo: -0.5, hi: 99.5 });
    expect(isFull(fullWindow(100), 100)).toBe(true);
    expect(clampWindow({ lo: -50, hi: 10 }, 100)).toEqual({ lo: -0.5, hi: 59.5 });
    expect(clampWindow({ lo: 95, hi: 120 }, 100)).toEqual({ lo: 74.5, hi: 99.5 });
    const narrow = clampWindow({ lo: 50, hi: 51 }, 100);
    expect(narrow.hi - narrow.lo).toBe(MIN_BARS);
    expect(clampWindow({ lo: 0, hi: 1 }, 5)).toEqual({ lo: -0.5, hi: 4.5 }); // fewer than MIN_BARS
  });

  it("zooms around an anchor that stays put, and pans within the data", () => {
    const w = fullWindow(100);
    const z = zoomWindow(w, 0.5, 49.5, 100);
    expect(z.hi - z.lo).toBe(50);
    expect((49.5 - z.lo) / (z.hi - z.lo)).toBeCloseTo((49.5 - w.lo) / (w.hi - w.lo));
    expect(zoomWindow(z, 4, 49.5, 100)).toEqual(w); // out past everything is everything
    expect(panWindow(z, 10, 100)).toEqual({ lo: z.lo + 10, hi: z.hi + 10 });
    expect(panWindow(z, 1000, 100).hi).toBe(99.5);
  });

  it("a range chip shows the last 30 minutes or hour of 1-minute candles", () => {
    const d = decodePrice(candles);
    expect(rangeWindow(d, 1800)).toEqual({ lo: 89.5, hi: 119.5 });
    expect(rangeWindow(d, 3600)).toEqual({ lo: 59.5, hi: 119.5 });
    expect(rangeWindow(d, 86400)).toEqual(fullWindow(120));
  });

  it("maps between index and screen x", () => {
    const w = { lo: 9.5, hi: 19.5 };
    expect(xOf(9.5, w, PLOT)).toBe(PLOT.x0);
    expect(xOf(19.5, w, PLOT)).toBe(PLOT.x1);
    expect(indexAt(xOf(14, w, PLOT), w, PLOT)).toBeCloseTo(14);
    expect(visibleRange(w, 120)).toEqual([10, 19]);
    expect(visibleRange(fullWindow(3), 3)).toEqual([0, 2]);
  });

  it("places a time on the candle that holds it, and past the ends fractionally", () => {
    const d = decodePrice(tiny);
    expect(indexOfTime(T0, d)).toBe(0);
    expect(indexOfTime(T0 + 90, d)).toBe(1);
    expect(indexOfTime(T0 + 150, d)).toBe(2);
    expect(indexOfTime(T0 - 120, d)).toBe(-2);
    expect(indexOfTime(T0 + 240, d)).toBe(4);
  });
});

describe("y range and ticks", () => {
  it("autoscales to the visible candles and visible trade prices", () => {
    const d = decodePrice(tiny);
    const [lo, hi] = yRange(d, fullWindow(3));
    expect(lo).toBeLessThan(9);
    expect(hi).toBeGreaterThan(12);
    const zoomed = yRange(d, { lo: 1.5, hi: 2.5 });
    expect(zoomed[0]).toBeGreaterThan(9.5); // candle 0's low is off screen
    const withTrade = yRange(d, { lo: 1.5, hi: 2.5 }, [{ i: 2, v: 20 }]);
    expect(withTrade[1]).toBeGreaterThan(20);
    expect(yRange(d, { lo: 1.5, hi: 2.5 }, [{ i: 0, v: 99 }])).toEqual(zoomed); // off screen
  });

  it("picks round ticks and labels them with the decimals the step needs", () => {
    expect(niceTicks(24990, 25040, 4)).toEqual({ ticks: [25000, 25020, 25040], step: 20 });
    expect(niceTicks(0.1, 0.9, 4).step).toBe(0.2);
    expect(priceLabel(25000, 20, { format: "number" })).toBe("25,000");
    expect(priceLabel(25000.25, 0.25, { format: "number" })).toBe("25,000.25");
    expect(priceLabel(12.5, 2.5, { format: "money", currency: "USD" })).toBe("$12.50");
    expect(timeTicks(0, 2, 5)).toEqual([0, 1, 2]);
    expect(timeTicks(10, 110, 3)).toEqual([10, 60, 110]);
  });
});

describe("marks", () => {
  it("batches candles into one path per kind and hides bodies when they get too thin", () => {
    const d = decodePrice(candles);
    const yr = yRange(d, fullWindow(120));
    const wideWin = { lo: 9.5, hi: 29.5 };
    const wide = candlePaths(d, wideWin, PLOT, yRange(d, wideWin));
    expect(wide.bodies).toBe(true);
    expect((wide.upBody.match(/Z/g) ?? []).length + (wide.downBody.match(/Z/g) ?? []).length).toBe(20);
    const all = candlePaths(d, fullWindow(120), { ...PLOT, x1: PLOT.x0 + 200 }, yr);
    expect(all.bodies).toBe(false);
    expect((all.upWick.match(/M/g) ?? []).length + (all.downWick.match(/M/g) ?? []).length).toBe(118); // 2 gaps
    expect(all.line).toBe("");
    const l = decodePrice(line);
    const lp = candlePaths(l, fullWindow(120), PLOT, yRange(l, fullWindow(120)));
    expect((lp.line.match(/M/g) ?? []).length).toBe(2); // the gap lifts the pen
    expect(lp.upBody + lp.upWick + lp.downWick).toBe("");
  });

  it("a trade's tone follows its pnl, an open one runs to the last close", () => {
    const d = decodePrice(candles);
    const [win, loss, open] = candles.trades.map((t) => tradeMark(t, d));
    expect([win!.tone, win!.points, win!.open, win!.i0, win!.i1]).toEqual(["ok", 7.25, false, 10, 25]);
    expect([loss!.tone, loss!.points]).toEqual(["bad", -3.5]); // a short that went up
    expect(open!.open).toBe(true);
    expect(open!.i1).toBe(119);
    expect(open!.y1).toBe(lastClose(d)!.v);
    expect(tradeMark({ side: "long", entry_t: T0, entry_px: 10, exit_t: T0 + 60, exit_px: 9 }, decodePrice(tiny)).tone).toBe("bad");
    expect(tradeMark({ side: "long", entry_t: T0, entry_px: 10, exit_t: T0 + 60, exit_px: 10 }, decodePrice(tiny)).tone).toBe("neutral");
  });

  it("writes signed points", () => {
    expect([signedPoints(7.25), signedPoints(-3), signedPoints(0), signedPoints(null)]).toEqual(["+7.25", "-3", "0", "—"]);
  });
});
