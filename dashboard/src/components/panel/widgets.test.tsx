import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Widget } from "@/components/panel/Widget";
import { local } from "@/test/fixtures";
import type { PanelWidget, SeriesWidget, StatWidget, TableWidget } from "@/types";

const NOW = Date.parse("2026-10-16T09:00:20Z");
const github = local.extensions[0]!;
const views = github.panels.flatMap((p) => p.views);
const find = <T extends PanelWidget>(type: T["type"], id: string): T =>
  views.flatMap((v) => v.widgets).find((w) => w.type === type && w.id === id) as T;
const draw = (w: PanelWidget) => render(<Widget w={w} ext="github" now={NOW} />);

describe("stat", () => {
  it("shows the value in its format with a signed delta coloured by its sign", () => {
    draw(find<StatWidget>("stat", "pass"));
    expect(screen.getByTestId("stat-value")).toHaveTextContent("94.5%");
    expect(screen.getByTestId("stat-delta")).toHaveTextContent("+1.2%");
    expect(screen.getByTestId("stat-delta")).toHaveClass("text-ok");
  });

  it("colours a negative delta bad and shows a hint, a null as a dash", () => {
    const base = find<StatWidget>("stat", "wait");
    const { unmount } = draw({ ...base, delta: -600 });
    expect(screen.getByTestId("stat-delta")).toHaveTextContent("-10m");
    expect(screen.getByTestId("stat-delta")).toHaveClass("text-bad");
    expect(screen.getByText("time to first review")).toBeInTheDocument();
    unmount();
    draw({ ...base, value: null, delta: null });
    expect(screen.getByTestId("stat-value")).toHaveTextContent("—");
    expect(screen.queryByTestId("stat-delta")).toBeNull();
  });
});

describe("series", () => {
  const line = () => find<SeriesWidget>("series", "opened");

  it("draws one path per series, a legend for two, and an accessible summary", () => {
    draw(line());
    expect(screen.getAllByTestId("series-line")).toHaveLength(2);
    expect(screen.getByTestId("series-legend")).toHaveTextContent("OpenedMerged");
    expect(screen.getByRole("img")).toHaveAccessibleName(/line chart of Opened, Merged, 8 points; latest Opened 3, Merged 3/);
  });

  it("leaves a gap at a null instead of a zero", () => {
    draw(line());
    // Opened has one null (index 3) and Merged one (index 4): each line is two pieces
    for (const p of screen.getAllByTestId("series-line")) expect(p.getAttribute("d")!.match(/M/g)).toHaveLength(2);
  });

  it("draws bars, stacked, one rect per value", () => {
    draw(find<SeriesWidget>("series", "byrepo"));
    expect(screen.getAllByTestId("bar")).toHaveLength(6);
    expect(screen.getByText("resonant")).toBeInTheDocument();
  });

  it("draws an area, no legend for one series, and says when it was downsampled", () => {
    draw(find<SeriesWidget>("series", "trend"));
    expect(screen.getByTestId("series-area")).toBeInTheDocument();
    expect(screen.queryByTestId("series-legend")).toBeNull();
    expect(screen.getByTestId("series-downsampled")).toHaveTextContent("Downsampled from 600 points");
  });

  it("copes with one point and all nulls", () => {
    const w = line();
    draw({ ...w, x: [w.x[0]!], series: [{ name: "a", values: [4] }] });
    draw({ ...w, series: [{ name: "b", values: w.x.map(() => null) }] });
    expect(screen.getAllByRole("img")).toHaveLength(2);
  });
});

describe("table", () => {
  const table = () => find<TableWidget>("table", "list");

  it("has a real table for wide screens and stacked rows for phones, both rendered", () => {
    draw(table());
    const wide = screen.getByTestId("table-wide");
    expect(within(wide).getAllByRole("row")).toHaveLength(4); // header + 3
    expect(within(wide).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "PR",
      "State",
      "Lines",
      "CI cost",
      "Opened",
      "Check",
    ]);
    expect(screen.getAllByTestId("table-row")).toHaveLength(3);
    expect(screen.getByTestId("table-rows")).toHaveClass("sm:hidden");
    expect(wide.parentElement).toHaveClass("hidden", "sm:block");
  });

  it("aligns numbers and money right and formats them", () => {
    draw(table());
    const row = within(screen.getByTestId("table-wide")).getAllByRole("row")[1]!;
    const cells = within(row).getAllByRole("cell");
    expect(cells[2]).toHaveClass("text-right");
    expect(cells[2]).toHaveTextContent("340");
    expect(cells[3]).toHaveTextContent("$0.42");
  });

  it("draws a pill as a badge, a link as a mono chip titled ext/tool, a time relative with the absolute one in the tooltip", () => {
    draw(table());
    const row = within(screen.getByTestId("table-wide")).getAllByRole("row")[2]!;
    expect(within(row).getByText("CI failing")).toHaveAttribute("data-slot", "badge");
    expect(within(row).getByText("CI failing")).toHaveAttribute("data-variant", "bad");
    expect(within(row).getByText("List PRs")).toHaveAttribute("title", "github/list_prs");
    const t = row.querySelector("time")!;
    expect(t).toHaveTextContent("16 h ago");
    expect(t.getAttribute("title")).not.toBe("");
    // a missing value is a dash
    expect(within(within(screen.getByTestId("table-wide")).getAllByRole("row")[3]!).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("says when rows were cut", () => {
    draw(table());
    expect(screen.getByTestId("table-truncated")).toHaveTextContent("Showing 3 of 7 rows");
  });

  it("says so for no rows", () => {
    draw({ ...table(), rows: [], truncated: false });
    expect(screen.getByText("No rows.")).toBeInTheDocument();
  });
});

describe("status, kv and text", () => {
  it("lists status items with their state", () => {
    draw(find("status", "checks"));
    const items = within(screen.getByTestId("status")).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(items[1]).toHaveTextContent("CI runners");
    expect(within(items[1]!).getByText("warn")).toHaveAttribute("data-variant", "warn");
    expect(within(items[2]!).getByText("down")).toHaveAttribute("data-variant", "bad");
    expect(within(items[0]!).getByText("200 in 180 ms")).toBeInTheDocument();
  });

  it("formats kv values by their format and shows null as a dash", () => {
    draw(find("kv", "limits"));
    const kv = within(screen.getByTestId("kv"));
    expect(kv.getByText("Remaining").nextElementSibling).toHaveTextContent("4,210");
    expect(kv.getByText("Resets in").nextElementSibling).toHaveTextContent("30m");
    expect(kv.getByText("Cached").nextElementSibling).toHaveTextContent("yes");
    expect(kv.getByText("Last error").nextElementSibling).toHaveTextContent("—");
  });

  it("shows text literally: markup is never interpreted", () => {
    const { container } = draw(find("text", "note"));
    expect(screen.getByTestId("text")).toHaveTextContent("Look at <b>docs</b> first");
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByTestId("text")).toHaveClass("whitespace-pre-wrap");
  });

  it("names a widget type it doesn't know", () => {
    draw({ type: "gauge" } as unknown as PanelWidget);
    expect(screen.getByTestId("widget-unknown")).toHaveTextContent('"gauge"');
  });
});

describe("details", () => {
  it("starts collapsed and expands a level at a time", async () => {
    const u = userEvent.setup();
    draw(find("details", "raw"));
    expect(screen.getAllByRole("button")).toHaveLength(1); // only the root shows
    expect(screen.queryByText("failed")).toBeNull();
    await u.click(screen.getByRole("button", { name: /data/ }));
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("total")).toBeInTheDocument();
    expect(screen.queryByText('"docs"')).toBeNull();
    const failed = screen.getByRole("button", { name: /failed/ });
    expect(failed).toHaveAttribute("aria-expanded", "false");
    await u.click(failed);
    expect(failed).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText('"docs"')).toBeInTheDocument();
    expect(screen.getByText("null")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: /data/ }));
    expect(screen.queryByText("failed")).toBeNull();
  });

  it("starts open when not collapsed", () => {
    draw({ ...(find("details", "raw") as PanelWidget & { type: "details" }), collapsed: false });
    expect(screen.getByText('"docs"')).toBeInTheDocument();
  });
});
