// The Diagram tab's pure parts (PER-277, PER-278): the hierarchy (a few high-level groups, each
// opening into its parts, an extension into its tools, jobs, servers and repos), the health
// roll-up of a closed box, the edges between whatever is shown, and which detail view a box
// opens. The layout is in diagramLayout.ts, loaded with the tab.
import type { Route } from "@/lib/route";
import type { Diagram, DiagramEdge, DiagramGroup, DiagramKind, DiagramNode, HealthState } from "@/types";

export const KIND_LABEL: Record<DiagramKind, string> = {
  core: "core",
  process: "launchd",
  extension: "extension",
  server: "MCP server",
  tool: "tool",
  job: "job",
  repo: "repo",
};

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

// --- groups -------------------------------------------------------------------------------

/** The groups a daemon from before PER-278 doesn't send (as `dashboard.diagram.GROUPS`). */
export const DEFAULT_GROUPS: readonly DiagramGroup[] = [
  {
    id: "messages",
    label: "Messages in and out",
    text: "Texts come in over iMessage, the event loop queues them and the qwen router triages them; replies, approval asks, pulses and briefings go back out the same way.",
  },
  {
    id: "brain",
    label: "Claude brain",
    text: "One long-lived Claude Code session answers every text the router hands on and calls Resonant's tools through the gate.",
  },
  {
    id: "toolcore",
    label: "Gate and tool core",
    text: "Every tool call takes one path: the registry, the gate (which asks you by text when it must) and the executor, which runs it once and audits it.",
  },
  {
    id: "workers",
    label: "Workers",
    text: "Claude Code runs on their own branch copies of a repo; they push PRs and report back.",
  },
  {
    id: "scheduler",
    label: "Scheduler and jobs",
    text: "In-daemon cron for Resonant's own jobs and every extension's jobs, all through the tool core.",
  },
  {
    id: "extensions",
    label: "Extensions",
    text: "What you installed: each extension with its MCP servers, tools, jobs and repos, from the manifest you approved.",
  },
  {
    id: "store",
    label: "Store and backups",
    text: "One SQLite file holds tasks, approvals, runs and the audit log, copied every day.",
  },
  {
    id: "relay",
    label: "Dashboard and relay",
    text: "This page, served on the Mini and, from the snapshot pushed to Supabase every minute, by the Fly app that also watches for silence.",
  },
  {
    id: "processes",
    label: "launchd agents",
    text: "The Mac's launchd agents Resonant knows: its own, those an extension declares and those named after a registered repo.",
  },
];

/** Where an older daemon's core components go (its nodes carry no `group`). */
const CORE_GROUP: Record<string, string> = {
  gateway: "messages",
  loop: "messages",
  router: "messages",
  pulse: "messages",
  brain: "brain",
  toolcore: "toolcore",
  gate: "toolcore",
  executor: "toolcore",
  workers: "workers",
  scheduler: "scheduler",
  store: "store",
  backups: "store",
  dashboard: "relay",
  relay: "relay",
  supabase: "relay",
  fly: "relay",
};

/** A top-level node's group: its own, else derived the way the daemon assigns it. */
export function groupOf(n: DiagramNode): string {
  if (n.group) return n.group;
  switch (n.kind) {
    case "core":
      return CORE_GROUP[n.id.replace(/^core:/, "")] ?? "other";
    case "process":
      return "processes";
    default:
      return n.ext === "core" ? "scheduler" : "extensions";
  }
}

export const GROUP_PREFIX = "group:";

/** One box of the hierarchy: a group (`group:<id>`) or a diagram node. */
export interface Item {
  id: string;
  label: string;
  text: string;
  /** absent for a group */
  node?: DiagramNode;
  parent: string | null;
  children: Item[];
  depth: number;
}

export interface Tree {
  roots: Item[];
  byId: Map<string, Item>;
}

/** A node's parent when it is a node of this diagram and following parents never loops. */
function parentOf(n: DiagramNode, byId: Map<string, DiagramNode>): DiagramNode | null {
  const p = n.parent ? byId.get(n.parent) : undefined;
  if (!p) return null;
  const seen = new Set([n.id]);
  for (let at: DiagramNode | undefined = p; at; at = at.parent ? byId.get(at.parent) : undefined) {
    if (seen.has(at.id)) return null;
    seen.add(at.id);
  }
  return p;
}

/** Groups in flow order (the snapshot's, else the page's own), each with its top-level nodes
 * and their children nested under them, in snapshot order. Empty groups are left out. */
export function buildTree(d: Diagram): Tree {
  const nodesById = new Map(d.nodes.map((n) => [n.id, n]));
  const defs = d.groups?.length ? d.groups : DEFAULT_GROUPS;
  const groups = new Map<string, Item>();
  const group = (gid: string): Item => {
    let g = groups.get(gid);
    if (!g) {
      const def = defs.find((x) => x.id === gid) ?? DEFAULT_GROUPS.find((x) => x.id === gid);
      g = { id: GROUP_PREFIX + gid, label: def?.label ?? gid, text: def?.text ?? "", parent: null, children: [], depth: 0 };
      groups.set(gid, g);
    }
    return g;
  };
  for (const def of defs) group(def.id);
  const items = new Map<string, Item>();
  for (const n of d.nodes) {
    if (!items.has(n.id)) {
      items.set(n.id, { id: n.id, label: n.label, text: n.text, node: n, parent: null, children: [], depth: 0 });
    }
  }
  for (const [id, item] of items) {
    const n = nodesById.get(id);
    if (!n) continue;
    const p = parentOf(n, nodesById);
    const parent = (p && items.get(p.id)) || group(groupOf(n));
    item.parent = parent.id;
    parent.children.push(item);
  }
  const roots = [...groups.values()].filter((g) => g.children.length > 0);
  const byId = new Map<string, Item>();
  const index = (it: Item, depth: number) => {
    it.depth = depth;
    byId.set(it.id, it);
    for (const c of it.children) index(c, depth + 1);
  };
  for (const r of roots) index(r, 0);
  return { roots, byId };
}

/** The view a box's details action opens: a node's own, or the group's. */
export function itemRoute(item: Item): Route {
  return item.node ? nodeRoute(item.node) : { kind: "node", id: item.id };
}

const HEALTH_RANK: Record<string, number> = { ok: 0, unknown: 1, degraded: 2, failing: 3 };

/** The worst of these (as the daemon's `worst_health`), null when none is known. */
export function worst(states: Iterable<string | null | undefined>): HealthState | null {
  let out: HealthState | null = null;
  for (const s of states) {
    const rank = s ? HEALTH_RANK[s] : undefined;
    if (rank === undefined) continue;
    if (out === null || rank > (HEALTH_RANK[out] ?? 0)) out = s as HealthState;
  }
  return out;
}

export interface Rollup {
  /** the worst health of the box and everything inside it */
  health: HealthState | null;
  /** its direct parts */
  count: number;
}

export function rollup(item: Item): Rollup {
  const states: (string | null | undefined)[] = [];
  const walk = (it: Item) => {
    states.push(it.node?.health);
    it.children.forEach(walk);
  };
  walk(item);
  return { health: worst(states), count: item.children.length };
}

/** Every box with parts (what "Expand all" opens). */
export function containers(tree: Tree): string[] {
  return [...tree.byId.values()].filter((it) => it.children.length > 0).map((it) => it.id);
}

export function isOpen(item: Item, open: ReadonlySet<string>): boolean {
  return item.children.length > 0 && open.has(item.id);
}

// --- what is shown ------------------------------------------------------------------------

/** The boxes shown: the groups, and the parts of every open box (whose parents are open). */
export function shownItems(tree: Tree, open: ReadonlySet<string>): Item[] {
  const out: Item[] = [];
  const walk = (it: Item) => {
    out.push(it);
    if (isOpen(it, open)) it.children.forEach(walk);
  };
  tree.roots.forEach(walk);
  return out;
}

/** An open box whose parts go on a grid: anything but core components (an extension's tools,
 * the extensions, the launchd agents), whose parts mostly don't talk to each other. Edges
 * between two parts of one grid are not drawn (an MCP server to each of its tools): the
 * nesting says it. */
export function gridded(item: Item | undefined): boolean {
  return !!item && !item.children.every((c) => c.node?.kind === "core");
}

export interface ShownEdge {
  id: string;
  source: string;
  target: string;
  /** both ways: the pair's two edges drawn as one, with a head at each end */
  both?: boolean;
  label?: string;
}

function within(item: Item, other: Item, tree: Tree): boolean {
  for (let p = item.parent ? tree.byId.get(item.parent) : undefined; p; p = p.parent ? tree.byId.get(p.parent) : undefined) {
    if (p === other) return true;
  }
  return false;
}

/** A short label for edges folded into one: the one label, or the first and how many more. */
function foldLabel(labels: string[]): string | undefined {
  const uniq = [...new Set(labels.filter(Boolean))];
  if (uniq.length <= 1) return uniq[0];
  return `${uniq[0]} +${uniq.length - 1}`;
}

/** The diagram's edges between the boxes shown: each end moves up to its nearest shown box;
 * an edge inside one box, into a box's own part, or between two parts of a grid is dropped;
 * edges that land on the same pair are folded into one, and (with `twoWay`, for the graph) a
 * pair that talks both ways into one edge with both labels. */
export function shownEdges(d: Diagram, tree: Tree, open: ReadonlySet<string>, twoWay = false): ShownEdge[] {
  const shown = new Set(shownItems(tree, open).map((it) => it.id));
  const up = (id: string): Item | null => {
    for (let it = tree.byId.get(id); it; it = it.parent ? tree.byId.get(it.parent) : undefined) {
      if (shown.has(it.id)) return it;
    }
    return null;
  };
  const folded = new Map<string, { source: string; target: string; labels: string[] }>();
  for (const e of d.edges) {
    const s = up(e.source);
    const t = up(e.target);
    if (!s || !t || s === t || within(s, t, tree) || within(t, s, tree)) continue;
    if (s.parent && s.parent === t.parent && gridded(tree.byId.get(s.parent))) continue;
    const key = `${s.id}->${t.id}`;
    const f = folded.get(key) ?? { source: s.id, target: t.id, labels: [] };
    if (e.label) f.labels.push(e.label);
    folded.set(key, f);
  }
  const out: ShownEdge[] = [];
  const merged = new Set<string>();
  for (const [id, f] of folded) {
    if (merged.has(id)) continue;
    const backId = `${f.target}->${f.source}`;
    const back = twoWay ? folded.get(backId) : undefined;
    if (back) {
      merged.add(backId);
      const labels = [foldLabel(f.labels), foldLabel(back.labels)].filter(Boolean);
      out.push({ id, source: f.source, target: f.target, both: true, label: labels.join(" / ") || undefined });
    } else {
      out.push({ id, source: f.source, target: f.target, label: foldLabel(f.labels) });
    }
  }
  return out;
}

/** A closed box's second line: a node's caption, a group's part count. */
export function subline(item: Item): string {
  if (item.node) return caption(item.node);
  const n = item.children.length;
  return `${n} part${n === 1 ? "" : "s"}`;
}
