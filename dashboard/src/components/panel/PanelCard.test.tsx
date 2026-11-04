import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { PanelCard } from "@/components/panel/PanelCard";
import { PanelGlance } from "@/components/panel/PanelGlance";
import { clone, remote, local } from "@/test/fixtures";
import type { Panel } from "@/types";

const NOW = Date.parse("2026-10-16T09:00:20Z");
const [prs, ci] = local.extensions[0]!.panels as [Panel, Panel];
const card = (p: Panel) => render(<PanelCard ext="github" panel={p} now={NOW} />);

describe("PanelCard", () => {
  it("shows a fresh panel: title, age, its views as tabs, an Open locally link", async () => {
    const u = userEvent.setup();
    card(prs);
    expect(screen.getByRole("heading", { name: "Pull requests" })).toBeInTheDocument();
    expect(screen.getByTestId("panel-updated")).toHaveTextContent("updated 2 min ago");
    expect(screen.getByRole("link", { name: "Open locally" })).toHaveAttribute("href", "http://127.0.0.1:8765/panels/prs");
    expect(screen.getByRole("button", { name: /Outside text/ })).toBeInTheDocument();
    expect(screen.queryByText("stale")).toBeNull();
    expect(screen.getByTestId("panel-body")).not.toHaveClass("opacity-60");
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Overview", "Health"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Open PRs")).toBeInTheDocument();
    expect(screen.queryByText("GitHub API")).toBeNull();
    await u.click(tabs[1]!);
    expect(screen.getByText("GitHub API")).toBeInTheDocument();
    expect(screen.queryByText("Open PRs")).toBeNull();
  });

  it("greys a stale panel, says why, and keeps the last good copy", () => {
    card(ci);
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByTestId("panel-body")).toHaveClass("opacity-60");
    expect(screen.getByTestId("panel-error")).toHaveTextContent("Failed 3 times in a row: exit 1: gh: HTTP 502 Bad Gateway");
    expect(screen.getByTestId("panel-error")).toHaveTextContent("Showing the last good copy.");
    expect(screen.getByText(/Nightly build: 3 of 41 jobs failed/)).toBeInTheDocument();
    expect(screen.queryByRole("tab")).toBeNull(); // one view: no tabs
    expect(screen.queryByRole("link", { name: "Open locally" })).toBeNull();
  });

  it("waits for the first run", () => {
    card({ ...ci, last_run_at: null, updated_at: null, views: [], error: null, fail_streak: 0, ok: false, stale: true, outside: [] });
    expect(screen.getByText("Waiting for first run.")).toBeInTheDocument();
    expect(screen.getByTestId("panel-updated")).toHaveTextContent("no data yet");
    expect(screen.queryByText("stale")).toBeNull();
  });

  it("says a panel was too big for the relay when it has no views", () => {
    card({ ...prs, views: [], truncated: true, outside: [] });
    expect(screen.getByTestId("panel-empty")).toHaveTextContent("Too big to show this time (over the panel size budget); the next smaller update will show.");
  });

  it("only links a loopback URL", () => {
    card({ ...prs, local_url: "https://evil.example/x" });
    expect(screen.queryByRole("link", { name: "Open locally" })).toBeNull();
  });

  it("has no Open locally link on the remote tier", () => {
    card(remote.extensions[0]!.panels[0]!);
    expect(screen.queryByRole("link", { name: "Open locally" })).toBeNull();
  });
});

describe("PanelGlance", () => {
  it("shows up to four stats from the first view, links to the extension page", () => {
    const many = clone(local).extensions[0]!.panels[0]!;
    const stat = many.views[0]!.widgets[0]!;
    many.views[0]!.widgets.unshift(stat, stat); // five stats now
    render(<PanelGlance ext="github" panel={many} now={NOW} />);
    const a = screen.getByTestId("panel-glance");
    expect(a).toHaveAttribute("href", "#/ext/github");
    expect(within(a).getAllByTestId("stat")).toHaveLength(4);
    expect(a).toHaveTextContent("updated 2 min ago");
    expect(a).not.toHaveClass("opacity-60");
  });

  it("is greyed when stale", () => {
    render(<PanelGlance ext="github" panel={ci} now={NOW} />);
    expect(screen.getByTestId("panel-glance")).toHaveClass("opacity-60");
  });
});
