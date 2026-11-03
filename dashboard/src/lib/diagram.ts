// The Diagram tab's pure parts (PER-277): which nodes show, where they go (dagre, bundled into
// the build: nothing is fetched at runtime), and which detail view a node opens.
import dagre from "@dagrejs/dagre";
import type { Route } from "@/lib/route";
import type { Diagram, DiagramEdge, DiagramKind, DiagramNode } from "@/types";

/** An extension's children: hidden with "Show tools, jobs and repos" off. */
const CHILD: ReadonlySet<DiagramKind> = new Set(["tool", "server", "job", "repo"]);

export const KIND_LABEL: Record<DiagramKind, string> = {
  core: "core",
  process: "launchd",
  extension: "extension",
  server: "MCP server",
  tool: "tool",
  job: "job",
  repo: "repo",
};

/** The nodes and edges shown: everything, or without the extensions' children (core jobs
 * stay with the scheduler). Edges keep to the nodes shown. */
export function visible(d: Diagram, children: boolean): { nodes: DiagramNode[]; edges: DiagramEdge[] } {
  const nodes = children ? d.nodes : d.nodes.filter((n) => !CHILD.has(n.kind) || n.ext === "core");
  const ids = new Set(nodes.map((n) => n.id));
  return { nodes, edges: d.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) };
}

/** The detail view a node opens: an extension's and a job's PER-276 view, else its own. */
export function nodeRoute(n: DiagramNode): Route {
  if (n.kind === "extension" && n.ext) return { kind: "extension", name: n.ext };
  if (n.kind === "job" && n.ext && n.job) return { kind: "job", ext: n.ext, id: n.job };
  return { kind: "node", id: n.id };
}

/** The small line under a node's label: its kind and what matters about it. */
export function caption(n: DiagramNode): string {
  const tags = n.tags ?? [];
  switch (n.kind) {
    case "process":
      return `launchd · ${n.state ?? "unknown"}`;
    case "tool":
      return ["tool", tags[0], tags[1], tags[3]?.includes("ask") ? "asks" : null].filter(Boolean).join(" · ");
    case "extension":
      return ["extension", n.state && n.state !== "enabled" ? n.state.replace(/_/g, " ") : null]
        .filter(Boolean)
        .join(" · ");
    case "job":
      return n.last_outcome ? `job · last ${n.last_outcome}` : "job · not run";
    default:
      return KIND_LABEL[n.kind];
  }
}

export interface Placed {
  node: DiagramNode;
  x: number; // center
  y: number;
  w: number;
  h: number;
}

export interface PlacedEdge {
  edge: DiagramEdge;
  points: { x: number; y: number }[];
  label?: { x: number; y: number };
}

export interface Layout {
  nodes: Placed[];
  edges: PlacedEdge[];
  width: number;
  height: number;
}

export const NODE_H = 46;

/** A node's width from its label and caption (no text measuring: same in jsdom and browsers). */
export function nodeWidth(n: DiagramNode): number {
  const chars = Math.max(n.label.length * 7.4, caption(n).length * 6.2);
  return Math.round(Math.min(240, Math.max(120, chars + 34)));
}

/** Edges back into these nodes are laid out reversed (and drawn the right way round): the
 * iMessage gateway is both where a text comes in and where replies go out, and laying the
 * replies out as edges into it would push it to the far right instead of the start. */
const RETURNS_TO: ReadonlySet<string> = new Set(["core:gateway"]);

/** Left to right, in the order the snapshot lists them (core first, so text flows from the
 * gateway on the left). */
export function layout(nodes: DiagramNode[], edges: DiagramEdge[]): Layout {
  const g = new dagre.graphlib.Graph({ multigraph: false });
  g.setGraph({ rankdir: "LR", nodesep: 16, ranksep: 64, edgesep: 8, marginx: 16, marginy: 16 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: nodeWidth(n), height: NODE_H });
  const back = (e: DiagramEdge) => RETURNS_TO.has(e.target) && !RETURNS_TO.has(e.source);
  for (const e of edges) {
    if (e.source === e.target) continue;
    const [v, w] = back(e) ? [e.target, e.source] : [e.source, e.target];
    if (g.hasEdge(v, w)) continue;
    g.setEdge(v, w, e.label ? { label: e.label, width: e.label.length * 5.6 + 8, height: 14, labelpos: "c" } : {});
  }
  dagre.layout(g);
  const placed: Placed[] = nodes.map((n) => {
    const p = g.node(n.id);
    return { node: n, x: p.x, y: p.y, w: p.width, h: p.height };
  });
  const placedEdges: PlacedEdge[] = [];
  for (const e of edges) {
    if (e.source === e.target) continue;
    const [v, w] = back(e) ? [e.target, e.source] : [e.source, e.target];
    const info = g.edge(v, w) as { points?: { x: number; y: number }[]; x?: number; y?: number; label?: string } | undefined;
    if (!info?.points || (info.label ?? null) !== (e.label ?? null)) continue;
    placedEdges.push({
      edge: e,
      points: back(e) ? [...info.points].reverse() : info.points,
      label: e.label && info.x !== undefined && info.y !== undefined ? { x: info.x, y: info.y } : undefined,
    });
  }
  const graph = g.graph() as { width?: number; height?: number };
  return { nodes: placed, edges: placedEdges, width: graph.width ?? 0, height: graph.height ?? 0 };
}

/** An SVG path through dagre's points (a smooth curve through the midpoints). */
export function edgePath(points: { x: number; y: number }[]): string {
  const first = points[0];
  if (!first) return "";
  let d = `M${first.x},${first.y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const c = points[i];
    const next = points[i + 1];
    if (!c || !next) break;
    d += ` Q${c.x},${c.y} ${(c.x + next.x) / 2},${(c.y + next.y) / 2}`;
  }
  const last = points[points.length - 1];
  if (last && points.length > 1) d += ` L${last.x},${last.y}`;
  return d;
}

/** The edges into and out of a node, with the node at the other end. */
export function connections(d: Diagram, id: string): { into: [DiagramEdge, DiagramNode][]; out: [DiagramEdge, DiagramNode][] } {
  const byId = new Map(d.nodes.map((n) => [n.id, n]));
  const into: [DiagramEdge, DiagramNode][] = [];
  const out: [DiagramEdge, DiagramNode][] = [];
  for (const e of d.edges) {
    const src = byId.get(e.source);
    const dst = byId.get(e.target);
    if (!src || !dst) continue;
    if (e.target === id) into.push([e, src]);
    if (e.source === id) out.push([e, dst]);
  }
  return { into, out };
}

/** The list view's grouping: core (core jobs under the scheduler), processes, and each
 * extension with its children. */
export function grouped(d: Diagram): {
  core: DiagramNode[];
  processes: DiagramNode[];
  extensions: { ext: DiagramNode; children: DiagramNode[] }[];
  childrenOf: (id: string) => DiagramNode[];
} {
  const kids = new Map<string, DiagramNode[]>();
  for (const n of d.nodes) {
    if (n.parent) kids.set(n.parent, [...(kids.get(n.parent) ?? []), n]);
  }
  const childrenOf = (id: string) => kids.get(id) ?? [];
  return {
    core: d.nodes.filter((n) => n.kind === "core"),
    processes: d.nodes.filter((n) => n.kind === "process"),
    extensions: d.nodes.filter((n) => n.kind === "extension").map((ext) => ({ ext, children: childrenOf(ext.id) })),
    childrenOf,
  };
}
