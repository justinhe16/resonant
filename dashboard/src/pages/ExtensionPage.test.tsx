import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "@/Dashboard";
import { clone, FIXTURE_NOW, local, remote } from "@/test/fixtures";
import priceFixture from "@/test/fixtures/price.json";
import type { DayLoader } from "@/lib/panelDays";
import type { Panel, PanelDay, PriceWidget, Snapshot } from "@/types";

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
const mount = (snap: Snapshot) => render(<Dashboard load={async () => snap} />);

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(FIXTURE_NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("extension page", () => {
  it("is what the #/ext route shows, instead of the tabs and sections, and not a dialog", async () => {
    window.location.hash = "#/ext/github";
    mount(local);
    const page = await screen.findByTestId("extension-page");
    expect(within(page).getByRole("heading", { level: 1, name: "github" })).toBeInTheDocument();
    expect(within(page).getAllByTestId("panel-card")).toHaveLength(2);
    expect(within(page).getByText("GitHub issues, PRs and CI")).toBeInTheDocument();
    expect(screen.queryByRole("tablist", { name: "Dashboard view" })).toBeNull();
    expect(screen.queryByTestId("section-extensions")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(within(page).getByRole("button", { name: "About panels" })).toBeInTheDocument();
  });

  it("goes back to the overview", async () => {
    const u = user();
    window.location.hash = "#/ext/github";
    mount(local);
    await u.click(await screen.findByRole("button", { name: "Back" }));
    expect(window.location.hash).toBe("");
    expect(await screen.findByTestId("section-extensions")).toBeInTheDocument();
    expect(screen.queryByTestId("extension-page")).toBeNull();
  });

  it("opens from a panel glance on the overview", async () => {
    const u = user();
    mount(local);
    const glances = await screen.findAllByTestId("panel-glance");
    expect(glances).toHaveLength(2);
    await u.click(glances[0]!);
    expect(window.location.hash).toBe("#/ext/github");
    expect(await screen.findByTestId("extension-page")).toBeInTheDocument();
  });

  it("has no Open locally link on the remote fixture, and one on the local", async () => {
    window.location.hash = "#/ext/github";
    const { unmount } = mount(remote);
    await screen.findByTestId("extension-page");
    expect(screen.queryByRole("link", { name: "Open locally" })).toBeNull();
    unmount();
    mount(local);
    await screen.findByTestId("extension-page");
    expect(screen.getByRole("link", { name: "Open locally" })).toBeInTheDocument();
  });

  it("scrolls to the panel in the route", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    window.location.hash = "#/ext/github/ci";
    mount(local);
    await screen.findByTestId("extension-page");
    expect(scroll).toHaveBeenCalled();
    expect(scroll.mock.contexts[0]).toBe(document.getElementById("panel-ci"));
  });

  it("says so for an unknown extension and for one with no panels", async () => {
    window.location.hash = "#/ext/nope";
    const { unmount } = mount(local);
    expect(await screen.findByTestId("ext-unknown")).toHaveTextContent("no extension called nope");
    unmount();
    window.location.hash = "#/ext/weather";
    mount(local);
    expect(await screen.findByTestId("ext-no-panels")).toBeInTheDocument();
  });

  it("draws a daemon from before panels without any panel UI", async () => {
    const old = clone(local) as unknown as { extensions: Record<string, unknown>[] };
    for (const e of old.extensions) delete e.panels;
    const { assertSnapshot } = await import("@/lib/snapshot");
    mount(assertSnapshot(old));
    await screen.findByTestId("section-extensions");
    expect(screen.queryByTestId("panel-glance")).toBeNull();
  });
});

describe("the overview", () => {
  it("shows at most two glances per extension, under its row", async () => {
    const snap = clone(local);
    const p = snap.extensions[0]!.panels;
    snap.extensions[0]!.panels = [p[0]!, p[1]!, { ...p[0]!, id: "third", title: "Third" }];
    mount(snap);
    const row = (await screen.findAllByTestId("extension-row"))[0]!;
    expect(within(row).getAllByTestId("panel-glance")).toHaveLength(2);
    expect(within(row).queryByText("Third")).toBeNull();
  });

  it("opens the diagram's panel node on the extension page", async () => {
    const u = user();
    window.localStorage.setItem("resonant.tab", "diagram");
    window.location.hash = "#/ext/github/prs";
    mount(local);
    expect(await screen.findByTestId("extension-page")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("tab", { name: "Diagram", selected: true })).toBeInTheDocument();
  });
});

describe("price panels (PER-282)", () => {
  const withChart = (order: "after" | "alone" | "middle") => {
    const snap = clone(local);
    const ext = snap.extensions[0]!;
    const [prs, ci] = ext.panels as [Panel, Panel];
    const chart: Panel = { ...prs, id: "chart", title: "NQ chart", views: [{ id: "chart", title: "Chart", widgets: [priceFixture.candles as PriceWidget] }] };
    ext.panels = order === "alone" ? [chart] : order === "after" ? [prs, ci, chart] : [prs, chart, ci];
    return snap;
  };
  const cells = async () => {
    const page = await screen.findByTestId("extension-page");
    return within(page).getAllByTestId("panel-cell");
  };

  it("a chart-only panel alone in its row takes the full width", async () => {
    window.location.hash = "#/ext/github";
    const { unmount } = mount(withChart("alone"));
    const [only] = await cells();
    expect(only).toHaveAttribute("data-wide", "true");
    expect(only).toHaveClass("lg:col-span-2");
    expect(within(only!).getByTestId("price-chart")).toBeInTheDocument();
    unmount();
    mount(withChart("after")); // the third of three: alone in the second row
    const three = await cells();
    expect(three.map((c) => c.hasAttribute("data-wide"))).toEqual([false, false, true]);
  });

  it("next to another panel it keeps its column", async () => {
    window.location.hash = "#/ext/github";
    mount(withChart("middle"));
    const three = await cells();
    expect(three.map((c) => c.hasAttribute("data-wide"))).toEqual([false, false, false]);
    expect(three.every((c) => c.classList.contains("min-w-0"))).toBe(true);
  });

  it("the overview glance of a chart-only panel is its title and age, no chart", async () => {
    mount(withChart("alone"));
    const glance = (await screen.findAllByTestId("panel-glance"))[0]!;
    expect(glance).toHaveTextContent("NQ chart");
    expect(glance).toHaveTextContent(/updated .* ago/);
    expect(within(glance).queryByTestId("price-chart")).toBeNull();
    expect(within(glance).queryByTestId("stat-value")).toBeNull();
  });
});

describe("panel history (PER-286)", () => {
  const archivedDay: PanelDay = {
    views: [{ id: "v", title: "V", widgets: [{ type: "text", text: "archived words" }] }],
    health: "warn",
    truncated: false,
  };
  afterEach(() => window.sessionStorage.clear());

  const mountDays = (snap: Snapshot, loadDay: DayLoader = vi.fn(async () => archivedDay)) => {
    render(<Dashboard load={async () => snap} loadDay={loadDay} />);
    return loadDay;
  };

  it("shows the picker only when a panel has history, on Today (live) by default", async () => {
    window.location.hash = "#/ext/github";
    const loadDay = mountDays(local);
    const picker = await screen.findByTestId("day-picker");
    expect(picker).toHaveValue("live");
    expect(screen.getByRole("combobox", { name: "Day" })).toBe(picker);
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Today (live)",
      "Thu 10/15",
      "Wed 10/14",
      "Mon 10/12",
    ]);
    expect(screen.queryByTestId("archived-day")).toBeNull();
    expect(loadDay).not.toHaveBeenCalled();
    // the live page says which days couldn't be archived
    expect(screen.getByTestId("panel-days-failed")).toHaveTextContent("Couldn't archive Tue 10/13 (tried 3 times).");
  });

  it("has no picker without history", async () => {
    window.location.hash = "#/ext/github";
    const snap = clone(local);
    for (const p of snap.extensions.find((e) => e.name === "github")!.panels) p.history = null;
    mountDays(snap);
    await screen.findByTestId("extension-page");
    expect(screen.queryByTestId("day-picker")).toBeNull();
  });

  it("an archived day draws the panels that have it and no data for the others", async () => {
    const u = user();
    window.location.hash = "#/ext/github";
    const loadDay = mountDays(local);
    await u.selectOptions(await screen.findByTestId("day-picker"), "2026-10-14");
    expect(await screen.findByTestId("archived-day")).toHaveTextContent("Wed 10/14 (archived)");
    await screen.findByText("archived words");
    expect(loadDay).toHaveBeenCalledTimes(1);
    expect(loadDay).toHaveBeenCalledWith("github", "prs", "2026-10-14");
    const cards = screen.getAllByTestId("panel-card");
    expect(within(cards[0]!).getByText("Wed 10/14 (archived)")).toBeInTheDocument();
    expect(within(cards[0]!).queryByTestId("panel-updated")).toBeNull();
    expect(within(cards[0]!).queryByTestId("panel-days-failed")).toBeNull();
    expect(within(cards[1]!).getByTestId("panel-no-day")).toHaveTextContent("No data for this day.");
    expect(screen.queryByRole("link", { name: "Open locally" })).toBeNull();
    // back to live
    await u.selectOptions(screen.getByTestId("day-picker"), "live");
    expect(screen.queryByTestId("archived-day")).toBeNull();
    expect(screen.queryByText("archived words")).toBeNull();
  });

  it("a day the source has no row for shows no data, and a failed fetch says so", async () => {
    const u = user();
    window.location.hash = "#/ext/github";
    const loadDay = vi.fn(async (_e: string, _p: string, d: string) => {
      if (d === "2026-10-12") throw new Error("HTTP 500");
      return null;
    });
    mountDays(local, loadDay);
    await u.selectOptions(await screen.findByTestId("day-picker"), "2026-10-15");
    await screen.findByTestId("archived-day");
    expect(await screen.findAllByTestId("panel-no-day")).toHaveLength(2);
    await u.selectOptions(screen.getByTestId("day-picker"), "2026-10-12");
    expect(await screen.findByTestId("day-error")).toHaveTextContent("Couldn't load some of Mon 10/12: HTTP 500");
  });

  it("remembers the pick for the session", async () => {
    const u = user();
    window.location.hash = "#/ext/github";
    const { unmount } = render(<Dashboard load={async () => local} loadDay={vi.fn(async () => archivedDay)} />);
    await u.selectOptions(await screen.findByTestId("day-picker"), "2026-10-15");
    expect(window.sessionStorage.getItem("resonant.day.github")).toBe("2026-10-15");
    unmount();
    mountDays(local);
    expect(await screen.findByTestId("day-picker")).toHaveValue("2026-10-15");
    expect(await screen.findByTestId("archived-day")).toHaveTextContent("Thu 10/15 (archived)");
  });

  it("a remembered day that is no longer listed falls back to live", async () => {
    window.sessionStorage.setItem("resonant.day.github", "2026-09-01");
    window.location.hash = "#/ext/github";
    const loadDay = mountDays(local);
    expect(await screen.findByTestId("day-picker")).toHaveValue("live");
    expect(loadDay).not.toHaveBeenCalled();
  });

  it("works on the remote fixture too", async () => {
    const u = user();
    window.location.hash = "#/ext/github";
    const loadDay = mountDays(remote);
    const picker = await screen.findByTestId("day-picker");
    // a day the relay doesn't have yet is listed only locally
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Today (live)",
      "Wed 10/14",
      "Mon 10/12",
    ]);
    await u.selectOptions(picker, "2026-10-14");
    await screen.findByText("archived words");
    expect(loadDay).toHaveBeenCalledWith("github", "prs", "2026-10-14");
  });
});
