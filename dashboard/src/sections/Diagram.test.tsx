import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "@/Dashboard";
import { layout, nodeRoute, visible } from "@/lib/diagram";
import { toHash } from "@/lib/route";
import { clone, FIXTURE_NOW, local, remote } from "@/test/fixtures";
import type { Diagram, Snapshot } from "@/types";

const diagram = local.diagram as Diagram;

function mount(snap: Snapshot) {
  return render(<Dashboard load={async () => snap} />);
}

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(FIXTURE_NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function openDiagram(mode?: "graph" | "list") {
  if (mode) window.localStorage.setItem("resonant.diagram.mode", mode);
  await user().click(await screen.findByRole("tab", { name: "Diagram" }));
  return screen.getByTestId("section-diagram");
}

describe("diagram layout", () => {
  it("places every node and routes every edge", () => {
    const { nodes, edges } = visible(diagram, true);
    const l = layout(nodes, edges);
    expect(l.nodes).toHaveLength(diagram.nodes.length);
    expect(l.width).toBeGreaterThan(0);
    expect(l.height).toBeGreaterThan(0);
    for (const p of l.nodes) {
      expect(Number.isFinite(p.x) && Number.isFinite(p.y)).toBe(true);
    }
    expect(l.edges.length).toBeGreaterThan(0);
    expect(l.edges.every((e) => e.points.length >= 2)).toBe(true);
  });

  it("hides the extensions' children but keeps the core jobs", () => {
    const { nodes, edges } = visible(diagram, false);
    expect(nodes.some((n) => n.kind === "tool" || n.kind === "repo" || n.kind === "server")).toBe(false);
    expect(nodes.some((n) => n.id === "job:core/pulse")).toBe(true);
    expect(nodes.some((n) => n.id === "job:github/watch")).toBe(false);
    const ids = new Set(nodes.map((n) => n.id));
    expect(edges.every((e) => ids.has(e.source) && ids.has(e.target))).toBe(true);
  });

  it("opens the PER-276 views for extensions and jobs, its own for the rest", () => {
    const byId = new Map(diagram.nodes.map((n) => [n.id, n]));
    expect(toHash(nodeRoute(byId.get("ext:github")!))).toBe("#/extensions/github");
    expect(toHash(nodeRoute(byId.get("job:core/pulse")!))).toBe("#/jobs/core/pulse");
    expect(toHash(nodeRoute(byId.get("core:gate")!))).toBe("#/diagram/core%3Agate");
  });
});

describe("Diagram tab", () => {
  it("renders the graph as SVG and opens a node's details", async () => {
    mount(local);
    const card = await openDiagram("graph");
    const graph = within(card).getByTestId("diagram-graph");
    const boxes = graph.querySelectorAll("[data-node]");
    expect(boxes).toHaveLength(diagram.nodes.length);
    expect(graph.querySelectorAll("path[marker-end]").length).toBeGreaterThan(10);
    await user().click(within(graph).getByRole("button", { name: /^Gate and policy, core/ }));
    const dialog = await screen.findByTestId("detail-dialog");
    expect(within(dialog).getByRole("heading", { name: "Gate and policy" })).toBeInTheDocument();
    expect(within(dialog).getByText("resonant/gate/")).toBeInTheDocument();
    expect(within(dialog).getByText(/Weighs each call's level/)).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Executor" })).toHaveAttribute("href", "#/diagram/core%3Aexecutor");
  });

  it("an extension box opens the extension's own view, with its sentence", async () => {
    mount(local);
    const card = await openDiagram("graph");
    await user().click(within(card).getByRole("button", { name: /^weather, extension/ }));
    const dialog = await screen.findByTestId("detail-dialog");
    expect(window.location.hash).toBe("#/extensions/weather");
    expect(within(dialog).getByText(/MCP connector to mcp\.weather\.example/)).toBeInTheDocument();
  });

  it("hides and shows the extensions' children", async () => {
    mount(local);
    const card = await openDiagram("graph");
    const count = () => within(card).getByTestId("diagram-graph").querySelectorAll("[data-node]").length;
    const all = count();
    await user().click(within(card).getByRole("button", { name: "Hide tools, jobs and repos" }));
    expect(count()).toBeLessThan(all);
    await user().click(within(card).getByRole("button", { name: "Show tools, jobs and repos" }));
    expect(count()).toBe(all);
  });

  it("has a stacked list for phones, grouped core, processes and extensions", async () => {
    mount(local);
    const card = await openDiagram("list");
    const list = within(card).getByTestId("diagram-list");
    expect(within(list).getByText("Core")).toBeInTheDocument();
    expect(within(list).getByRole("link", { name: "com.resonant.daemon" })).toHaveAttribute(
      "href",
      "#/diagram/proc%3Acom.resonant.daemon",
    );
    expect(within(list).getByRole("link", { name: "com.notessync.agent" })).toBeInTheDocument();
    expect(within(list).queryByText("com.apple.private")).not.toBeInTheDocument();
    expect(within(list).getByRole("link", { name: "merge_pr" })).toBeInTheDocument();
    expect(within(list).getByText(/github\/watch: every 5 min job/)).toBeInTheDocument();
  });

  it("is on the remote tier too", async () => {
    mount(remote);
    const card = await openDiagram("list");
    expect(within(card).getByRole("link", { name: "Relay pusher" })).toBeInTheDocument();
  });

  it("says when the daemon predates the diagram", async () => {
    mount(clone(local, { diagram: null }));
    const card = await openDiagram();
    expect(within(card).getByTestId("diagram-missing")).toBeInTheDocument();
  });

  it("remembers the tab", async () => {
    mount(local);
    await openDiagram("list");
    expect(window.localStorage.getItem("resonant.tab")).toBe("diagram");
    expect(screen.getByRole("tab", { name: "Diagram" })).toHaveAttribute("aria-selected", "true");
  });
});
