import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "@/Dashboard";
import {
  buildTree,
  containers,
  type Item,
  itemRoute,
  nodeRoute,
  rollup,
  shownEdges,
  shownItems,
  worst,
} from "@/lib/diagram";
import { absolute, layout } from "@/lib/diagramLayout";
import { toHash } from "@/lib/route";
import { clone, FIXTURE_NOW, local, remote } from "@/test/fixtures";
import type { Diagram, Snapshot } from "@/types";

const diagram = local.diagram as Diagram;

/** The same diagram as a daemon from before PER-278 sends it: no groups. */
function withoutGroups(d: Diagram): Diagram {
  const copy = structuredClone(d);
  delete copy.groups;
  for (const n of copy.nodes) delete n.group;
  return copy;
}

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
  return screen.findByTestId("section-diagram"); // the tab is loaded on first open
}

const shape = (items: Item[]): unknown => items.map((it) => [it.id, it.label, shape(it.children)]);

describe("diagram hierarchy", () => {
  const tree = buildTree(diagram);

  it("starts from a few groups in flow order, every node somewhere under one", () => {
    expect(tree.roots.map((g) => g.id)).toEqual([
      "group:messages",
      "group:brain",
      "group:toolcore",
      "group:workers",
      "group:scheduler",
      "group:extensions",
      "group:store",
      "group:relay",
      "group:processes",
    ]);
    expect(tree.roots[0]?.label).toBe("Messages in and out");
    for (const n of diagram.nodes) expect(tree.byId.has(n.id)).toBe(true);
  });

  it("nests an extension's tools, jobs, servers and repos under it, core jobs under the scheduler", () => {
    const exts = tree.byId.get("group:extensions");
    expect(exts?.children.map((c) => c.id)).toEqual(["ext:github", "ext:weather", "ext:notes-sync"]);
    expect(tree.byId.get("ext:github")?.children.map((c) => c.id)).toEqual([
      "tool:github/merge_pr",
      "tool:github/list_prs",
      "job:github/watch",
      "job:github/digest",
      "repo:github/resonant",
      "panel:github/prs",
      "panel:github/ci",
    ]);
    expect(tree.byId.get("tool:github/merge_pr")?.depth).toBe(2);
    expect(tree.byId.get("group:scheduler")?.children.map((c) => c.id)).toEqual(["core:scheduler"]);
    expect(tree.byId.get("core:scheduler")?.children.map((c) => c.id)).toContain("job:core/pulse");
  });

  it("derives the same groups from a snapshot without them (an older daemon)", () => {
    expect(shape(buildTree(withoutGroups(diagram)).roots)).toEqual(shape(tree.roots));
  });

  it("puts a node in a group it doesn't know under that group's own name, and survives a parent loop", () => {
    const d: Diagram = {
      nodes: [
        { id: "core:new", kind: "core", label: "New", text: "", group: "future" },
        { id: "a", kind: "tool", label: "a", text: "", parent: "b" },
        { id: "b", kind: "tool", label: "b", text: "", parent: "a" },
      ],
      edges: [],
    };
    const t = buildTree(d);
    expect(t.byId.get("group:future")?.label).toBe("future");
    expect(t.byId.get("a")?.parent).toBe("group:extensions");
  });

  it("rolls the worst health and the part count up to a closed box", () => {
    expect(rollup(tree.byId.get("ext:weather")!)).toEqual({ health: "degraded", count: 3 });
    expect(rollup(tree.byId.get("ext:github")!)).toEqual({ health: "degraded", count: 7 }); // its CI panel is stale
    expect(rollup(tree.byId.get("group:extensions")!)).toEqual({ health: "degraded", count: 3 });
    expect(rollup(tree.byId.get("group:messages")!).health).toBe("degraded"); // the gateway
    expect(rollup(tree.byId.get("group:toolcore")!)).toEqual({ health: null, count: 3 }); // none known
    expect(worst(["ok", "unknown", null, "failing", "degraded"])).toBe("failing");
    expect(worst(["ok", "unknown"])).toBe("unknown");
    expect(worst([undefined, "sideways"])).toBeNull();
  });

  it("draws only group-to-group edges at first, short labels, never a loop", () => {
    const edges = shownEdges(diagram, tree, new Set());
    expect(edges.length).toBeGreaterThan(5);
    for (const e of edges) {
      expect(e.source.startsWith("group:") && e.target.startsWith("group:")).toBe(true);
      expect(e.source).not.toBe(e.target);
      expect((e.label ?? "").length).toBeLessThanOrEqual(30);
    }
    const pair = (s: string, t: string) => edges.find((e) => e.source === `group:${s}` && e.target === `group:${t}`);
    expect(pair("messages", "brain")?.label).toBe("everything else");
    expect(pair("brain", "toolcore")?.label).toBe("tool call");
    expect(pair("store", "relay")?.label).toBe("snapshot");
  });

  it("moves edges onto the opened boxes, and drops the ones inside a box", () => {
    const open = new Set(["group:extensions", "group:toolcore"]);
    const edges = shownEdges(diagram, tree, open);
    expect(edges.some((e) => e.source === "core:executor" && e.target === "ext:github")).toBe(true);
    // ext:github -> its own job is inside a closed box: not drawn.
    expect(edges.some((e) => e.target === "job:github/watch")).toBe(false);
    const inside = shownEdges(diagram, tree, new Set([...open, "ext:weather"]));
    // An MCP server to its tool, both parts of one open extension: the nesting says it.
    expect(inside.some((e) => e.source === "mcp:weather/weather-mcp")).toBe(false);
    expect(inside.some((e) => e.source === "ext:weather" && e.target.startsWith("tool:"))).toBe(false);
  });

  it("lays out the open boxes around their parts, parents first", () => {
    const open = new Set(containers(tree));
    const shown = shownItems(tree, open);
    const l = layout(tree, open, shownEdges(diagram, tree, open));
    expect(l.boxes.map((b) => b.item.id).sort()).toEqual(shown.map((it) => it.id).sort());
    const seen = new Set<string>();
    const abs = absolute(l);
    for (const b of l.boxes) {
      expect(Number.isFinite(b.x) && Number.isFinite(b.y) && b.w > 0 && b.h > 0).toBe(true);
      if (b.item.parent) {
        expect(seen.has(b.item.parent)).toBe(true); // React Flow needs the parent first
        const p = abs.get(b.item.parent)!;
        const me = abs.get(b.item.id)!;
        expect(me.x >= p.x && me.y >= p.y && me.x + me.w <= p.x + p.w + 0.5 && me.y + me.h <= p.y + p.h + 0.5).toBe(true);
      }
      seen.add(b.item.id);
    }
    const closed = layout(tree, new Set(), shownEdges(diagram, tree, new Set()));
    expect(closed.boxes).toHaveLength(tree.roots.length);
    expect(l.width * l.height).toBeGreaterThan(closed.width * closed.height);
  });

  it("opens the PER-276 views for extensions and jobs, its own for the rest and for groups", () => {
    const byId = new Map(diagram.nodes.map((n) => [n.id, n]));
    expect(toHash(nodeRoute(byId.get("ext:github")!))).toBe("#/extensions/github");
    expect(toHash(nodeRoute(byId.get("job:core/pulse")!))).toBe("#/jobs/core/pulse");
    expect(toHash(nodeRoute(byId.get("core:gate")!))).toBe("#/diagram/core%3Agate");
    expect(toHash(itemRoute(tree.byId.get("group:toolcore")!))).toBe("#/diagram/group%3Atoolcore");
  });
});

describe("Diagram tab", () => {
  const boxes = (card: HTMLElement) =>
    [...within(card).getByTestId("diagram-graph").querySelectorAll("[data-node]")].map((el) => el.getAttribute("data-node"));

  it("starts with the groups only, each with its roll-up", async () => {
    mount(local);
    const card = await openDiagram("graph");
    expect(boxes(card)).toEqual(buildTree(diagram).roots.map((g) => g.id));
    const graph = within(card).getByTestId("diagram-graph");
    expect(within(graph).getByRole("group", { name: "Extensions, group, degraded" })).toBeInTheDocument();
    expect(within(graph).getAllByText("3 parts").length).toBeGreaterThan(0);
  });

  it("opens a group in place, then an extension into its tools, and folds them back", async () => {
    mount(local);
    const card = await openDiagram("graph");
    const graph = within(card).getByTestId("diagram-graph");
    await user().click(within(graph).getByRole("button", { name: "Expand Extensions" }));
    expect(boxes(card)).toEqual(expect.arrayContaining(["ext:github", "ext:weather", "ext:notes-sync"]));
    expect(boxes(card)).not.toContain("tool:github/merge_pr");
    // A closed extension shows its parts' roll-up.
    const weather = graph.querySelector('[data-node="ext:weather"]') as HTMLElement;
    expect(within(weather).getByTestId("rollup")).toHaveAttribute("data-health", "degraded");
    expect(within(weather).getByTestId("rollup")).toHaveTextContent("3");

    await user().click(within(graph).getByRole("button", { name: "Expand github" }));
    expect(boxes(card)).toContain("tool:github/merge_pr");
    expect(within(graph).getByRole("button", { name: "Collapse github" })).toHaveAttribute("aria-expanded", "true");
    expect(JSON.parse(window.localStorage.getItem("resonant.diagram.open") ?? "[]")).toEqual(["group:extensions", "ext:github"]);

    await user().click(within(graph).getByRole("button", { name: "Collapse Extensions" }));
    expect(boxes(card)).not.toContain("ext:github");
  });

  it("clicking a closed box opens it; clicking a leaf opens its details", async () => {
    mount(local);
    const card = await openDiagram("graph");
    const graph = within(card).getByTestId("diagram-graph");
    // A plain click: user-event's mousedown has no view, which d3-zoom's pan handler needs.
    fireEvent.click(within(graph).getByText("Gate and tool core"));
    expect(boxes(card)).toContain("core:gate");
    fireEvent.click(within(graph).getByText("Gate and policy"));
    const dialog = await screen.findByTestId("detail-dialog");
    expect(within(dialog).getByRole("heading", { name: "Gate and policy" })).toBeInTheDocument();
    expect(within(dialog).getByText("resonant/gate/")).toBeInTheDocument();
    expect(within(dialog).getByText(/Weighs each call's level/)).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Executor" })).toHaveAttribute("href", "#/diagram/core%3Aexecutor");
    expect(within(dialog).getByRole("link", { name: "Gate and tool core" })).toHaveAttribute("href", "#/diagram/group%3Atoolcore");
  });

  it("expand all and collapse all", async () => {
    mount(local);
    const card = await openDiagram("graph");
    await user().click(within(card).getByRole("button", { name: "Expand all" }));
    expect(boxes(card)).toHaveLength(diagram.nodes.length + buildTree(diagram).roots.length);
    await user().click(within(card).getByRole("button", { name: "Collapse all" }));
    expect(boxes(card)).toHaveLength(buildTree(diagram).roots.length);
  });

  it("a remembered box that no longer exists doesn't count as open", async () => {
    window.localStorage.setItem("resonant.diagram.open", JSON.stringify(["ext:removed"]));
    mount(local);
    const card = await openDiagram("graph");
    expect(within(card).getByRole("button", { name: "Collapse all" })).toBeDisabled();
    expect(boxes(card)).toHaveLength(buildTree(diagram).roots.length);
  });

  it("a group's details say what it does, its worst health and what is inside", async () => {
    mount(local);
    const card = await openDiagram("graph");
    await user().click(within(card).getByRole("button", { name: "Details: Extensions" }));
    const dialog = await screen.findByTestId("detail-dialog");
    expect(window.location.hash).toBe("#/diagram/group%3Aextensions");
    expect(within(dialog).getByRole("heading", { name: "Extensions" })).toBeInTheDocument();
    expect(within(dialog).getByText("degraded")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "weather" })).toHaveAttribute("href", "#/extensions/weather");
    expect(within(dialog).getByRole("link", { name: "Gate and tool core" })).toBeInTheDocument();
  });

  it("an extension's details open the extension's own view, with its sentence", async () => {
    mount(local);
    const card = await openDiagram("graph");
    const graph = within(card).getByTestId("diagram-graph");
    await user().click(within(graph).getByRole("button", { name: "Expand Extensions" }));
    await user().click(within(graph).getByRole("button", { name: "Details: weather" }));
    const dialog = await screen.findByTestId("detail-dialog");
    expect(window.location.hash).toBe("#/extensions/weather");
    expect(within(dialog).getByText(/MCP connector to mcp\.weather\.example/)).toBeInTheDocument();
  });

  it("renders a snapshot without groups the same way", async () => {
    mount(clone(local, { diagram: withoutGroups(diagram) }));
    const card = await openDiagram("graph");
    expect(boxes(card)).toEqual(buildTree(diagram).roots.map((g) => g.id));
    expect(within(card).getByText("Messages in and out")).toBeInTheDocument();
  });

  it("has a stacked accordion for phones, with the same hierarchy", async () => {
    mount(local);
    const card = await openDiagram("list");
    const list = within(card).getByTestId("diagram-list");
    expect(within(list).getByRole("link", { name: "launchd agents" })).toHaveAttribute("href", "#/diagram/group%3Aprocesses");
    expect(within(list).queryByRole("link", { name: "com.resonant.daemon" })).not.toBeInTheDocument();
    await user().click(within(list).getByRole("button", { name: "Expand launchd agents" }));
    expect(within(list).getByRole("link", { name: "com.resonant.daemon" })).toHaveAttribute(
      "href",
      "#/diagram/proc%3Acom.resonant.daemon",
    );
    expect(within(list).getByRole("link", { name: "com.notessync.agent" })).toBeInTheDocument();
    expect(within(list).queryByText("com.apple.private")).not.toBeInTheDocument();
    await user().click(within(list).getByRole("button", { name: "Expand Extensions" }));
    await user().click(within(list).getByRole("button", { name: "Expand github" }));
    expect(within(list).getByRole("link", { name: "merge_pr" })).toBeInTheDocument();
    expect(within(list).getByText(/github\/watch: every 5 min job/)).toBeInTheDocument();
    await user().click(within(list).getByRole("button", { name: "Collapse Extensions" }));
    expect(within(list).queryByRole("link", { name: "merge_pr" })).not.toBeInTheDocument();
    // The groups' flows, as on the graph.
    expect(within(list).getAllByText("(everything else)").length).toBe(1);
  });

  it("is on the remote tier too", async () => {
    mount(remote);
    const card = await openDiagram("list");
    await user().click(within(card).getByRole("button", { name: "Expand Dashboard and relay" }));
    expect(within(card).getByRole("link", { name: "Relay pusher" })).toBeInTheDocument();
  });

  it("says when the daemon predates the diagram", async () => {
    mount(clone(local, { diagram: null }));
    const card = await openDiagram();
    expect(within(card).getByTestId("diagram-missing")).toBeInTheDocument();
  });

  it("says when the daemon cut the diagram to its caps", async () => {
    mount(clone(local, { diagram: { ...diagram, truncated: true } }));
    const card = await openDiagram("list");
    expect(within(card).getByTestId("diagram-truncated")).toBeInTheDocument();
  });

  it("remembers the tab", async () => {
    mount(local);
    await openDiagram("list");
    expect(window.localStorage.getItem("resonant.tab")).toBe("diagram");
    expect(screen.getByRole("tab", { name: "Diagram" })).toHaveAttribute("aria-selected", "true");
  });
});
