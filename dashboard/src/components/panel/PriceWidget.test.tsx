import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Widget } from "@/components/panel/Widget";
import fixture from "@/test/fixtures/price.json";
import type { PriceWidget } from "@/types";

const NOW = Date.parse("2026-10-16T15:00:00Z");
const candles = fixture.candles as PriceWidget;
const line = fixture.line as PriceWidget;
const draw = (w: PriceWidget) => render(<Widget w={w} ext="trade-jev" now={NOW} />);
const chart = () => screen.getByTestId("price-chart");
const win = () => [Number(chart().getAttribute("data-lo")), Number(chart().getAttribute("data-hi"))];
const width = () => {
  const [lo, hi] = win();
  return hi! - lo!;
};
const pointer = (type: "pointerDown" | "pointerMove" | "pointerUp", el: Element, clientX: number, pointerType = "mouse") =>
  fireEvent[type](el, { clientX, clientY: 100, pointerId: 1, pointerType, isPrimary: true });

/** The fixture with `k` more flat 1-minute candles, as the next snapshot of a live chart. */
const grown = (k: number): PriceWidget => ({
  ...candles,
  dt: [...candles.dt, ...Array<number>(k).fill(60)],
  o: [...candles.o!, ...Array<number>(k).fill(0)],
  h: [...candles.h!, ...Array<number>(k).fill(0)],
  l: [...candles.l!, ...Array<number>(k).fill(0)],
  c: [...candles.c, ...Array<number>(k).fill(0)],
});

describe("price widget", () => {
  it("draws candles hollow up and filled down, markers, and an accessible summary", () => {
    draw(candles);
    expect(chart()).toHaveAttribute("role", "img");
    expect(chart().getAttribute("aria-label")).toMatch(/^Price candlestick chart of NQZ6, 1-min, 120 candles; last [\d,.]+ pts; 3 trades/);
    expect(screen.getByTestId("price-bodies-up").getAttribute("d")).toMatch(/Z/);
    expect(screen.getByTestId("price-bodies-up")).toHaveClass("fill-card", "stroke-up");
    expect(screen.getByTestId("price-bodies-down")).toHaveClass("fill-down");
    expect(screen.getByTestId("price-wicks-up").getAttribute("d")).toMatch(/V/);
    expect(screen.queryByTestId("price-line")).toBeNull();
    expect(screen.getAllByTestId("price-marker")).toHaveLength(2);
    expect(screen.getByText("ctrl/⌘ + scroll or pinch to zoom, drag to pan").parentElement).toHaveTextContent(/^in pts · /);
  });

  it("draws a close line when the widget is a line", () => {
    draw(line);
    expect(screen.getByTestId("price-line").getAttribute("d")).toMatch(/^M/);
    expect(screen.queryByTestId("price-bodies-up")).toBeNull();
    expect(chart().getAttribute("aria-label")).toMatch(/^Price line chart of NQZ6 close, 120 candles/);
    expect(screen.queryByTestId("price-trades")).toBeNull(); // no trades, no table
  });

  it("draws each trade as an entry triangle, an exit dot and a line coloured by its result", () => {
    draw(candles);
    const trades = screen.getAllByTestId("price-trade");
    expect(trades.map((t) => t.getAttribute("data-tone"))).toEqual(["ok", "bad", expect.stringMatching(/ok|bad|neutral/)]);
    const [win_, loss, open] = trades as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(win_).getByTestId("price-trade-line")).toHaveClass("stroke-ok");
    expect(within(loss).getByTestId("price-trade-line")).toHaveClass("stroke-bad");
    expect(within(win_).getByTestId("price-trade-entry")).toHaveClass("fill-ok");
    expect(within(win_).getByTestId("price-trade-exit")).toBeInTheDocument();
    // the open one: dashed to the last candle, no exit dot
    expect(open).toHaveAttribute("data-open", "true");
    expect(within(open).getByTestId("price-trade-line")).toHaveAttribute("stroke-dasharray", "4 3");
    expect(within(open).queryByTestId("price-trade-exit")).toBeNull();
    expect(screen.getByTestId("price-legend")).toHaveTextContent("▲ long entry▼ short entry● exit win lossdashed: still open");
  });

  it("the range chips show the last 30 minutes, the last hour, or everything", async () => {
    const u = userEvent.setup();
    draw(candles);
    const all = screen.getByRole("button", { name: "All" });
    expect(all).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Reset" })).toBeDisabled();
    await u.click(screen.getByRole("button", { name: "30m" }));
    expect(win()).toEqual([89.5, 119.5]);
    expect(screen.getByRole("button", { name: "30m" })).toHaveAttribute("aria-pressed", "true");
    expect(all).toHaveAttribute("aria-pressed", "false");
    await u.click(screen.getByRole("button", { name: "1h" }));
    expect(win()).toEqual([59.5, 119.5]);
    await u.click(screen.getByRole("button", { name: "Reset" }));
    expect(win()).toEqual([-0.5, 119.5]);
    expect(all).toHaveAttribute("aria-pressed", "true");
  });

  it("zooms on ctrl/⌘ + wheel only; a plain wheel is left to the page", () => {
    draw(candles);
    const plain = fireEvent.wheel(chart(), { deltaY: -200, clientX: 300 });
    expect(plain).toBe(true); // not prevented: the page scrolls
    expect(width()).toBe(120);
    const ctrl = fireEvent.wheel(chart(), { deltaY: -200, clientX: 300, ctrlKey: true });
    expect(ctrl).toBe(false);
    expect(width()).toBeLessThan(120);
    const before = width();
    fireEvent.wheel(chart(), { deltaY: -200, clientX: 300, metaKey: true });
    expect(width()).toBeLessThan(before);
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Reset" })).toBeEnabled();
  });

  it("drags to pan, and a double click fits everything again", async () => {
    draw(candles);
    fireEvent.wheel(chart(), { deltaY: -400, clientX: 300, ctrlKey: true });
    const [lo] = win();
    pointer("pointerDown", chart(), 300);
    pointer("pointerMove", chart(), 200); // drag left: later candles come in
    pointer("pointerUp", chart(), 200);
    expect(win()[0]).toBeGreaterThan(lo!);
    fireEvent.doubleClick(chart());
    expect(win()).toEqual([-0.5, 119.5]);
  });

  it("pinches with two fingers and fits on a double tap", () => {
    draw(candles);
    fireEvent.pointerDown(chart(), { clientX: 250, clientY: 100, pointerId: 1, pointerType: "touch" });
    fireEvent.pointerDown(chart(), { clientX: 350, clientY: 100, pointerId: 2, pointerType: "touch" });
    fireEvent.pointerMove(chart(), { clientX: 150, clientY: 100, pointerId: 1, pointerType: "touch" });
    fireEvent.pointerMove(chart(), { clientX: 450, clientY: 100, pointerId: 2, pointerType: "touch" });
    expect(width()).toBeLessThan(60); // fingers three times apart: a third of the width
    fireEvent.pointerUp(chart(), { clientX: 150, pointerId: 1, pointerType: "touch" });
    fireEvent.pointerUp(chart(), { clientX: 450, pointerId: 2, pointerType: "touch" });
    pointer("pointerDown", chart(), 300, "touch");
    pointer("pointerUp", chart(), 300, "touch");
    expect(screen.getByTestId("price-tooltip")).toBeInTheDocument(); // a tap shows the crosshair
    pointer("pointerDown", chart(), 302, "touch");
    pointer("pointerUp", chart(), 302, "touch");
    expect(win()).toEqual([-0.5, 119.5]);
  });

  it("the keyboard pans and zooms when the chart has focus", async () => {
    const u = userEvent.setup();
    draw(candles);
    chart().focus();
    await u.keyboard("+");
    expect(width()).toBeCloseTo(96);
    const [lo] = win();
    await u.keyboard("{ArrowLeft}");
    expect(win()[0]).toBeLessThan(lo!);
    await u.keyboard("0");
    expect(win()).toEqual([-0.5, 119.5]);
  });

  it("a crosshair shows the candle's OHLC on hover", () => {
    draw(candles);
    expect(screen.queryByTestId("price-tooltip")).toBeNull();
    pointer("pointerMove", chart(), 300);
    expect(screen.getByTestId("price-crosshair")).toBeInTheDocument();
    const tip = screen.getByTestId("price-tooltip");
    expect(tip).toHaveTextContent(/O[\d,.]+H[\d,.]+L[\d,.]+C[\d,.]+/);
    fireEvent.pointerLeave(chart(), { pointerType: "mouse" });
    expect(screen.queryByTestId("price-tooltip")).toBeNull();
  });

  it("hovering or tapping a trade shows its time, side, prices, points, money and why", async () => {
    const u = userEvent.setup();
    draw(candles);
    const [first, second] = screen.getAllByTestId("price-trade-hit") as [HTMLElement, HTMLElement, ...HTMLElement[]];
    fireEvent.pointerEnter(first, { pointerType: "mouse" });
    const tip = screen.getByTestId("price-tooltip");
    expect(tip).toHaveTextContent(/[A-Z][a-z]{2} \d{1,2}, \d\d:10 → [A-Z][a-z]{2} \d{1,2}, \d\d:25/);
    expect(tip).toHaveTextContent(/Long [\d,.]+ → [\d,.]+/);
    expect(tip).toHaveTextContent("+7.25 pts · +$145.00");
    expect(tip).toHaveTextContent("target · jev");
    fireEvent.pointerLeave(first, { pointerType: "mouse" });
    expect(screen.queryByTestId("price-tooltip")).toBeNull();
    await u.click(second);
    expect(screen.getByTestId("price-tooltip")).toHaveTextContent("-3.5 pts · -$70.00");
    expect(screen.getByTestId("price-tooltip")).toHaveTextContent("stop");
    fireEvent.pointerLeave(second, { pointerType: "mouse" });
    expect(screen.getByTestId("price-tooltip")).toHaveTextContent("stop"); // a click pins it
    await u.click(second);
    fireEvent.pointerLeave(second, { pointerType: "mouse" });
    expect(screen.queryByTestId("price-tooltip")).toBeNull(); // a second click unpins
  });

  it("says so when there is no data", () => {
    draw({ ...line, t0: 0, dt: [], c: [] });
    expect(screen.getByTestId("price-empty")).toHaveTextContent("No price data yet.");
    expect(screen.queryByTestId("price-chart")).toBeNull();
    expect(screen.getByRole("button", { name: "30m" })).toBeDisabled();
  });

  it("lists the trades in a table under the chart, collapsed by default", async () => {
    const u = userEvent.setup();
    draw(candles);
    const details = screen.getByTestId("price-trades");
    expect(details).not.toHaveAttribute("open");
    await u.click(within(details).getByText("Trades (3)"));
    expect(details).toHaveAttribute("open");
    const rows = within(details).getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(rows[1]).toHaveTextContent(/long · jev.*\+7\.25\$145\.00target/);
    expect(rows[2]).toHaveTextContent(/short · jev.*-3\.5-\$70\.00stop/);
    expect(rows[3]).toHaveTextContent(/long · jev.*open/);
  });

  it("says when candles were combined", () => {
    draw({ ...candles, aggregated_from: 3000 });
    expect(screen.getByTestId("price-aggregated")).toHaveTextContent("3000 candles combined into 120 wider ones");
  });

  it("a new candle keeps the owner's view: All stays all, a chip re-applies, a zoom stays", async () => {
    const u = userEvent.setup();
    const { rerender } = draw(candles);
    const again = (w: PriceWidget) => rerender(<Widget w={w} ext="trade-jev" now={NOW} />);
    again(grown(1));
    expect(win()).toEqual([-0.5, 120.5]); // All follows
    await u.click(screen.getByRole("button", { name: "30m" }));
    again(grown(2));
    expect(win()).toEqual([91.5, 121.5]); // still the last 30 minutes
    fireEvent.wheel(chart(), { deltaY: -400, clientX: 100, ctrlKey: true }); // zoom on the left
    const zoomed = win();
    again(grown(5));
    expect(win()).toEqual(zoomed); // left alone
    await u.click(screen.getByRole("button", { name: "1h" }));
    pointer("pointerDown", chart(), 300);
    pointer("pointerMove", chart(), 400); // drag right: earlier candles, off the right edge
    pointer("pointerUp", chart(), 400);
    const panned = win();
    again(grown(6));
    expect(win()).toEqual(panned);
    fireEvent.keyDown(chart(), { key: "0" });
    fireEvent.wheel(chart(), { deltaY: -400, clientX: 576, ctrlKey: true }); // zoom at the right edge: follows
    const [lo, hi] = win();
    again(grown(8));
    expect(win()).toEqual([lo! + 2, hi! + 2]);
  });

  it("a tap or click off the trades unpins one; a cancelled touch is not a tap", async () => {
    const u = userEvent.setup();
    draw(candles);
    const [first] = screen.getAllByTestId("price-trade-hit") as [HTMLElement, ...HTMLElement[]];
    await u.click(first);
    fireEvent.pointerLeave(first, { pointerType: "mouse" });
    expect(screen.getByTestId("price-tooltip")).toHaveTextContent("target");
    pointer("pointerDown", chart(), 500, "touch");
    pointer("pointerUp", chart(), 500, "touch");
    expect(screen.getByTestId("price-tooltip")).toHaveTextContent(/O[\d,.]+H/); // the crosshair now
    cleanupTip();
    await u.click(first);
    fireEvent.pointerLeave(first, { pointerType: "mouse" });
    pointer("pointerDown", chart(), 500);
    pointer("pointerUp", chart(), 500);
    fireEvent.pointerLeave(chart(), { pointerType: "mouse" });
    expect(screen.queryByTestId("price-tooltip")).toBeNull();
    // a page scroll that began on the chart: down, then cancel, twice; no crosshair, no fit
    fireEvent.wheel(chart(), { deltaY: -400, clientX: 300, ctrlKey: true });
    const before = win();
    for (let k = 0; k < 2; k++) {
      pointer("pointerDown", chart(), 300, "touch");
      fireEvent.pointerCancel(chart(), { pointerId: 1, pointerType: "touch" });
    }
    expect(screen.queryByTestId("price-crosshair")).toBeNull();
    expect(win()).toEqual(before);
  });
});

function cleanupTip() {
  fireEvent.keyDown(screen.getByTestId("price-chart"), { key: "Escape" });
}

