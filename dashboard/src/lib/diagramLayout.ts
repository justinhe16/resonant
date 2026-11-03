// The Diagram tab's layout (PER-278): each open box sized around its parts, by dagre (bundled
// into the build: nothing is fetched at runtime) or on a grid. Loaded with the tab, so the
// overview doesn't carry dagre.
import dagre from "@dagrejs/dagre";
import { GROUP_PREFIX, gridded, isOpen, type Item, type ShownEdge, subline, type Tree } from "@/lib/diagram";

export const BOX_H = 58;
/** An open box: its header, then its parts inside this padding. */
export const HEADER_H = 40;
export const PAD = 14;
const GAP = 12;

/** A closed box's width from its label and second line (no text measuring: the same in jsdom
 * and browsers). Room for the health dot, the buttons and the roll-up badge. */
export function boxWidth(item: Item): number {
  // A group's label is text-sm, a node's text-xs; then the dot, the buttons and the padding.
  const label = item.label.length * (item.node ? 7.4 : 8.2) + 34 + (item.children.length ? 22 : 0) + 22;
  const second = subline(item).length * 6 + 24 + (item.node && item.children.length ? 40 : 0);
  return Math.round(Math.min(280, Math.max(item.node ? 150 : 190, label, second)));
}

export interface Box {
  item: Item;
  /** relative to the open box it sits in, or the canvas */
  x: number;
  y: number;
  w: number;
  h: number;
  open: boolean;
}

export interface Layout {
  /** parents before their parts (React Flow needs that order) */
  boxes: Box[];
  width: number;
  height: number;
}

/** Edges back into these are laid out reversed (and drawn the right way round): the iMessage
 * side is both where a text comes in and where replies go out, and laying the replies out as
 * edges into it would push it to the far right instead of the start. */
const RETURNS_TO: ReadonlySet<string> = new Set([`${GROUP_PREFIX}messages`, "core:gateway"]);

interface Sized {
  item: Item;
  w: number;
  h: number;
  open: boolean;
  inner: Box[];
}

interface Placed {
  pos: Map<string, { x: number; y: number }>;
  w: number;
  h: number;
}

/** An edge between two boxes laid out side by side, with the label drawn on it (if any). */
type LocalEdge = [string, string, string | undefined];

function dagrePlace(sized: Sized[], edges: LocalEdge[]): Placed {
  const g = new dagre.graphlib.Graph({ multigraph: false });
  g.setGraph({ rankdir: "LR", nodesep: 24, ranksep: 64, marginx: 0, marginy: 0 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const s of sized) g.setNode(s.item.id, { width: s.w, height: s.h });
  for (const [a, b, label] of edges) {
    const back = RETURNS_TO.has(b) && !RETURNS_TO.has(a);
    const [v, w] = back ? [b, a] : [a, b];
    if (v === w || g.hasEdge(v, w)) continue;
    // Room for the label between the two ranks, so a box doesn't sit on it.
    g.setEdge(v, w, label ? { label, width: label.length * 5.4 + 16, height: 14, labelpos: "c" } : {});
  }
  dagre.layout(g);
  const pos = new Map<string, { x: number; y: number }>();
  for (const s of sized) {
    const p = g.node(s.item.id);
    pos.set(s.item.id, { x: p.x - s.w / 2, y: p.y - s.h / 2 });
  }
  // Start at 0, 0 whatever margin dagre left.
  const all = [...pos.values()];
  const minX = Math.min(0, ...all.map((p) => p.x));
  const minY = Math.min(0, ...all.map((p) => p.y));
  let w = 0;
  let h = 0;
  for (const s of sized) {
    const p = pos.get(s.item.id);
    if (!p) continue;
    p.x -= minX;
    p.y -= minY;
    w = Math.max(w, p.x + s.w);
    h = Math.max(h, p.y + s.h);
  }
  return { pos, w, h };
}

function gridPlace(sized: Sized[]): Placed {
  const cols = Math.max(1, Math.min(4, Math.ceil(Math.sqrt(sized.length))));
  const colW: number[] = [];
  const rowH: number[] = [];
  sized.forEach((s, i) => {
    const c = i % cols;
    const r = Math.floor(i / cols);
    colW[c] = Math.max(colW[c] ?? 0, s.w);
    rowH[r] = Math.max(rowH[r] ?? 0, s.h);
  });
  const offset = (sizes: number[], k: number) => sizes.slice(0, k).reduce((a, b) => a + b + GAP, 0);
  const pos = new Map<string, { x: number; y: number }>();
  sized.forEach((s, i) => pos.set(s.item.id, { x: offset(colW, i % cols), y: offset(rowH, Math.floor(i / cols)) }));
  return { pos, w: offset(colW, colW.length) - GAP, h: offset(rowH, rowH.length) - GAP };
}

/** The shown boxes, each open box sized around its parts: the top level and a group's core
 * components by dagre (left to right, a text flowing from the iMessage side), any other open
 * box's parts on a grid. */
export function layout(tree: Tree, open: ReadonlySet<string>, edges: ShownEdge[]): Layout {
  const place = (items: Item[], container: Item | undefined): { boxes: Box[]; w: number; h: number } => {
    const sized: Sized[] = items.map((item) => {
      if (isOpen(item, open)) {
        const inner = place(item.children, item);
        const w = Math.max(boxWidth(item), inner.w + PAD * 2);
        return { item, w, h: inner.h + HEADER_H + PAD, open: true, inner: inner.boxes };
      }
      return { item, w: boxWidth(item), h: BOX_H, open: false, inner: [] };
    });
    const ids = new Set(items.map((it) => it.id));
    const at = (id: string): string | null => {
      for (let it = tree.byId.get(id); it; it = it.parent ? tree.byId.get(it.parent) : undefined) {
        if (ids.has(it.id)) return it.id;
      }
      return null;
    };
    const local: LocalEdge[] = [];
    for (const e of edges) {
      const a = at(e.source);
      const b = at(e.target);
      // The label only where the edge is drawn between exactly these two boxes.
      if (a && b && a !== b) local.push([a, b, a === e.source && b === e.target ? e.label : undefined]);
    }
    const placed = gridded(container) ? gridPlace(sized) : dagrePlace(sized, local);
    const boxes: Box[] = [];
    for (const s of sized) {
      const p = placed.pos.get(s.item.id) ?? { x: 0, y: 0 };
      boxes.push({ item: s.item, x: p.x, y: p.y, w: s.w, h: s.h, open: s.open });
      // Parts sit below their box's header, inside its padding.
      for (const b of s.inner) boxes.push(b.item.parent === s.item.id ? { ...b, x: b.x + PAD, y: b.y + HEADER_H } : b);
    }
    return { boxes, w: placed.w, h: placed.h };
  };
  const top = place(tree.roots, undefined);
  return { boxes: top.boxes, width: top.w, height: top.h };
}

/** Where each box is on the canvas (its open parents' offsets added up). */
export function absolute(l: Layout): Map<string, { x: number; y: number; w: number; h: number }> {
  const rel = new Map(l.boxes.map((b) => [b.item.id, b]));
  const out = new Map<string, { x: number; y: number; w: number; h: number }>();
  const get = (id: string): { x: number; y: number } => {
    const hit = out.get(id);
    if (hit) return hit;
    const b = rel.get(id);
    if (!b) return { x: 0, y: 0 };
    const base = b.item.parent ? get(b.item.parent) : { x: 0, y: 0 };
    const abs = { x: base.x + b.x, y: base.y + b.y, w: b.w, h: b.h };
    out.set(id, abs);
    return abs;
  };
  for (const b of l.boxes) get(b.item.id);
  return out;
}
