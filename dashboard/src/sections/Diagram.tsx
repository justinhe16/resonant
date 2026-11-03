import "@xyflow/react/dist/base.css";
import {
  Background,
  BackgroundVariant,
  BaseEdge,
  type Edge,
  type EdgeProps,
  getBezierPath,
  Handle,
  type InternalNode,
  MarkerType,
  type Node,
  type NodeMouseHandler,
  type NodeProps,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useInternalNode,
  useReactFlow,
} from "@xyflow/react";
import { ChevronDown, ChevronRight, Info, Maximize2, Minus, Plus } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Explainer } from "@/components/Explainer";
import { Dot, RouteLink } from "@/components/bits";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  buildTree,
  containers,
  isOpen,
  type Item,
  itemRoute,
  KIND_LABEL,
  type Rollup,
  rollup,
  shownEdges,
  subline,
  type Tree,
} from "@/lib/diagram";
import { absolute, layout, type Layout } from "@/lib/diagramLayout";
import { toHash } from "@/lib/route";
import { healthTone } from "@/lib/tone";
import { cn } from "@/lib/utils";
import type { Diagram as DiagramData, Snapshot } from "@/types";

type Mode = "graph" | "list";

const MODE_KEY = "resonant.diagram.mode";
const OPEN_KEY = "resonant.diagram.open";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* not remembered */
  }
}

/** Graph from `md` up, the stacked list on a phone; the owner's pick is remembered. */
function initialMode(): Mode {
  const saved = read(MODE_KEY);
  if (saved === "graph" || saved === "list") return saved;
  try {
    return window.matchMedia("(min-width: 768px)").matches ? "graph" : "list";
  } catch {
    return "graph";
  }
}

/** The boxes left open last time (both views share them); everything closed at first. */
function initialOpen(): Set<string> {
  try {
    const raw: unknown = JSON.parse(read(OPEN_KEY) ?? "[]");
    return new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

/** Animate re-layouts unless the owner asked for less motion (or there is no screen). */
function motionOk(): boolean {
  try {
    return typeof window.requestAnimationFrame === "function" && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

export function Diagram({ snap }: { snap: Snapshot }) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [open, setOpen] = useState<Set<string>>(initialOpen);
  // What the last toggle asks the graph to show: the box just opened, or everything.
  const [focus, setFocus] = useState<{ id: string | null; n: number }>({ id: null, n: 0 });
  const d = snap.diagram;
  const tree = useMemo(() => (d ? buildTree(d) : null), [d]);

  const change = (next: Set<string>, focusId: string | null) => {
    write(OPEN_KEY, JSON.stringify([...next]));
    setOpen(next);
    setFocus((f) => ({ id: focusId, n: f.n + 1 }));
  };
  const toggle = (id: string) => {
    const next = new Set(open);
    const opening = !next.has(id);
    if (opening) next.add(id);
    else next.delete(id);
    change(next, opening ? id : null);
  };
  const pick = (m: Mode) => {
    setMode(m);
    write(MODE_KEY, m);
  };

  return (
    <Card className="[--card-spacing:--spacing(5)]" data-testid="section-diagram">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="flex min-w-48 flex-1 items-center gap-1.5 text-sm font-medium">
            How Resonant is put together
            <Explainer name="Diagram" text="diagram" />
          </h2>
          {tree && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Button variant="outline" size="sm" onClick={() => change(new Set(containers(tree)), null)}>
                Expand all
              </Button>
              <Button variant="outline" size="sm" onClick={() => change(new Set(), null)} disabled={open.size === 0}>
                Collapse all
              </Button>
              <div role="radiogroup" aria-label="Diagram view" className="inline-flex rounded-lg border p-0.5">
                {(["graph", "list"] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    role="radio"
                    aria-checked={mode === m}
                    onClick={() => pick(m)}
                    className={cn(
                      "rounded-md px-2.5 py-1 text-xs font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                      mode === m ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {m === "graph" ? "Graph" : "List"}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {!d || !tree ? (
          <Alert variant="warn" data-testid="diagram-missing">
            <Info />
            <AlertTitle>No diagram in this snapshot</AlertTitle>
            <AlertDescription>
              The Mini is running a version from before the diagram. Restart the daemon after the update to get it.
            </AlertDescription>
          </Alert>
        ) : (
          <>
            {d.processes_scanned === false && (
              <p className="text-xs text-muted-foreground">
                The daemon couldn't read launchd, so the processes' states are unknown.
              </p>
            )}
            {d.truncated && (
              <p className="text-xs text-muted-foreground" data-testid="diagram-truncated">
                There is more than the diagram can carry, so the last extensions or their tools are left out.
              </p>
            )}
            {mode === "graph" ? (
              <ReactFlowProvider>
                <Graph d={d} tree={tree} open={open} toggle={toggle} focus={focus} />
              </ReactFlowProvider>
            ) : (
              <List d={d} tree={tree} open={open} toggle={toggle} />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function details(item: Item) {
  window.location.hash = toHash(itemRoute(item));
}

function RollupBadge({ roll }: { roll: Rollup }) {
  const parts = `${roll.count} part${roll.count === 1 ? "" : "s"}`;
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] leading-none text-muted-foreground tabular-nums"
      title={roll.health ? `${parts}, worst health ${roll.health}` : parts}
      data-testid="rollup"
      data-health={roll.health ?? undefined}
    >
      {roll.health && <Dot tone={healthTone(roll.health)} className="size-1.5" />}
      {roll.count}
    </span>
  );
}

// --- graph --------------------------------------------------------------------------------

interface BoxData extends Record<string, unknown> {
  item: Item;
  open: boolean;
  roll: Rollup;
}

type BoxNode = Node<BoxData, "box">;

const Toggle = createContext<(id: string) => void>(() => {});

const KIND_BORDER: Record<string, string> = {
  group: "border-[1.5px] border-foreground/30",
  core: "border-info/70",
  extension: "border-foreground/40",
  process: "border-dashed border-muted-foreground/60",
};

const ICON_BUTTON =
  "nodrag nopan rounded p-0.5 text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50";
const HANDLE = "!size-1 !min-h-0 !min-w-0 !border-0 !bg-transparent";

function BoxView({ data }: NodeProps<BoxNode>) {
  const toggle = useContext(Toggle);
  const { item, open, roll } = data;
  const n = item.node;
  const parts = item.children.length > 0;
  // A node shows its own health; a closed group, the worst of its parts.
  const health = n ? n.health : open ? null : roll.health;
  return (
    <div
      className={cn(
        "flex h-full w-full flex-col rounded-lg border text-card-foreground transition-colors",
        open ? "bg-muted/40" : "cursor-pointer bg-card hover:bg-muted",
        KIND_BORDER[n?.kind ?? "group"] ?? "border-border",
      )}
      data-node={item.id}
      role="group"
      aria-label={`${item.label}, ${n ? KIND_LABEL[n.kind] : "group"}${health ? `, ${health}` : ""}`}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className={HANDLE} />
      <div className={cn("flex items-center gap-1.5 px-2.5 pt-2", open && "cursor-pointer")} data-box-header>
        {health && <Dot tone={healthTone(health)} />}
        <span className={cn("min-w-0 flex-1 truncate font-medium", n ? "text-xs" : "text-sm")} title={item.text || item.label}>
          {item.label}
        </span>
        {parts && (
          <button
            type="button"
            className={ICON_BUTTON}
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} ${item.label}`}
            onClick={(e) => {
              e.stopPropagation();
              toggle(item.id);
            }}
          >
            {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          </button>
        )}
        <button
          type="button"
          className={ICON_BUTTON}
          aria-label={`Details: ${item.label}`}
          onClick={(e) => {
            e.stopPropagation();
            details(item);
          }}
        >
          <Info className="size-3.5" />
        </button>
      </div>
      {!open && (
        <div className="flex items-center gap-1.5 px-2.5 pt-1">
          <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground">{subline(item)}</span>
          {parts && n && <RollupBadge roll={roll} />}
        </div>
      )}
      <Handle type="source" position={Position.Right} isConnectable={false} className={HANDLE} />
    </div>
  );
}

const NODE_TYPES = { box: BoxView };

type Rect = { x: number; y: number; w: number; h: number };

function rectOf(n: InternalNode<BoxNode>): Rect {
  const { x, y } = n.internals.positionAbsolute;
  return { x, y, w: n.measured.width ?? n.width ?? 0, h: n.measured.height ?? n.height ?? 0 };
}

/** The middle of the side of `a` that faces `b`, and which side that is. */
function facing(a: Rect, b: Rect): [number, number, Position] {
  const dx = b.x + b.w / 2 - (a.x + a.w / 2);
  const dy = b.y + b.h / 2 - (a.y + a.h / 2);
  if (Math.abs(dx) / Math.max(1, a.w + b.w) >= Math.abs(dy) / Math.max(1, a.h + b.h)) {
    return dx >= 0 ? [a.x + a.w, a.y + a.h / 2, Position.Right] : [a.x, a.y + a.h / 2, Position.Left];
  }
  return dy >= 0 ? [a.x + a.w / 2, a.y + a.h, Position.Bottom] : [a.x + a.w / 2, a.y, Position.Top];
}

/** An edge from the side of one box that faces the other to the side of that one facing back,
 * so it never loops round a box whichever way the layout put them. */
function FloatingEdge({ id, source, target, markerStart, markerEnd, label, style }: EdgeProps) {
  const s = useInternalNode<BoxNode>(source);
  const t = useInternalNode<BoxNode>(target);
  if (!s || !t) return null;
  const a = rectOf(s);
  const b = rectOf(t);
  const [sourceX, sourceY, sourcePosition] = facing(a, b);
  const [targetX, targetY, targetPosition] = facing(b, a);
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  return (
    <BaseEdge
      id={id}
      path={path}
      markerStart={markerStart}
      markerEnd={markerEnd}
      label={label}
      labelX={labelX}
      labelY={labelY}
      labelBgPadding={[3, 1]}
      labelBgBorderRadius={3}
      style={style}
    />
  );
}

const EDGE_TYPES = { floating: FloatingEdge };
const ARROW = { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "var(--muted-foreground)" } as const;

const TWEEN_MS = 280;
const MIN_ZOOM = 0.15;

type Frame = Map<string, { x: number; y: number; w: number; h: number }>;
interface Tween {
  target: BoxNode[];
  from: Frame | null;
  k: number;
}

function frameOf(nodes: BoxNode[]): Frame {
  return new Map(nodes.map((n) => [n.id, { x: n.position.x, y: n.position.y, w: n.width ?? 0, h: n.height ?? 0 }]));
}

function tweened({ target, from, k }: Tween): BoxNode[] {
  if (!from || k >= 1) return target;
  const e = 1 - (1 - k) ** 3;
  const mix = (a: number, b: number) => a + (b - a) * e;
  return target.map((n) => {
    const f = from.get(n.id);
    if (!f) return n;
    return {
      ...n,
      position: { x: mix(f.x, n.position.x), y: mix(f.y, n.position.y) },
      width: mix(f.w, n.width ?? 0),
      height: mix(f.h, n.height ?? 0),
    };
  });
}

/** `target`, reached smoothly from where the boxes were (a box new to the screen starts where
 * it ends; with reduced motion, or no screen, everything jumps). */
function useTween(target: BoxNode[]): BoxNode[] {
  const [anim, setAnim] = useState<Tween>({ target, from: null, k: 1 });
  let current = anim;
  if (anim.target !== target) {
    // A new layout starts from what is on screen now (adjusting state on a prop change).
    current = { target, from: motionOk() ? frameOf(tweened(anim)) : null, k: 0 };
    setAnim(current);
  }
  const { from, target: goal } = current;
  useEffect(() => {
    if (!from) return;
    let raf = 0;
    const start = performance.now();
    const step = (now: number) => {
      const k = Math.min(1, (now - start) / TWEEN_MS);
      setAnim((a) => (a.target === goal ? { ...a, k } : a));
      if (k < 1) raf = window.requestAnimationFrame(step);
    };
    raf = window.requestAnimationFrame(step);
    return () => window.cancelAnimationFrame(raf);
  }, [from, goal]);
  return tweened(current);
}

function toNodes(l: Layout, open: ReadonlySet<string>): BoxNode[] {
  return l.boxes.map((b) => ({
    id: b.item.id,
    type: "box",
    position: { x: b.x, y: b.y },
    width: b.w,
    height: b.h,
    parentId: b.item.parent ?? undefined,
    zIndex: b.item.depth,
    draggable: false,
    selectable: false,
    data: { item: b.item, open: isOpen(b.item, open), roll: rollup(b.item) },
  }));
}

function Graph({
  d,
  tree,
  open,
  toggle,
  focus,
}: {
  d: DiagramData;
  tree: Tree;
  open: ReadonlySet<string>;
  toggle: (id: string) => void;
  focus: { id: string | null; n: number };
}) {
  const edges = useMemo(() => shownEdges(d, tree, open, true), [d, tree, open]);
  const l = useMemo(() => layout(tree, open, edges), [tree, open, edges]);
  const nodes = useTween(useMemo(() => toNodes(l, open), [l, open]));
  const rfEdges: Edge[] = useMemo(
    () =>
      edges.map((e) => ({
        id: e.id,
        type: "floating",
        source: e.source,
        target: e.target,
        label: e.label,
        markerStart: e.both ? ARROW : undefined,
        // Over the open box an end sits in, under the boxes themselves.
        zIndex: Math.max(tree.byId.get(e.source)?.depth ?? 0, tree.byId.get(e.target)?.depth ?? 0),
        markerEnd: ARROW,
        focusable: false,
      })),
    [edges, tree],
  );
  const rf = useReactFlow<BoxNode>();
  const box = useRef<HTMLDivElement>(null);

  /** Show this part of the canvas, at most at 100%. */
  const show = useCallback(
    (r: { x: number; y: number; w: number; h: number }, duration: number) => {
      const el = box.current;
      const W = el?.clientWidth ?? 0;
      const H = el?.clientHeight ?? 0;
      if (!W || !H || !r.w || !r.h) return;
      const pad = 24;
      const zoom = Math.max(MIN_ZOOM, Math.min(1, (W - pad * 2) / r.w, (H - pad * 2) / r.h));
      void rf.setViewport({ x: W / 2 - (r.x + r.w / 2) * zoom, y: H / 2 - (r.y + r.h / 2) * zoom, zoom }, { duration });
    },
    [rf],
  );
  const fitAll = (duration: number) => show({ x: 0, y: 0, w: l.width, h: l.height }, duration);

  // After a toggle: the box just opened, or everything.
  const seen = useRef(focus.n);
  useEffect(() => {
    if (seen.current === focus.n) return;
    seen.current = focus.n;
    const at = focus.id ? absolute(l).get(focus.id) : undefined;
    show(at ?? { x: 0, y: 0, w: l.width, h: l.height }, motionOk() ? TWEEN_MS : 0);
  }, [focus, l, show]);

  const onNodeClick: NodeMouseHandler<BoxNode> = (event, node) => {
    const { item, open: shownOpen } = node.data;
    // An open box closes from its header only: its body is where its parts are.
    if (shownOpen) {
      if (event.target instanceof Element && event.target.closest("[data-box-header]")) toggle(item.id);
      return;
    }
    if (item.children.length > 0) toggle(item.id);
    else details(item);
  };

  return (
    <div className="space-y-2">
      <Toggle.Provider value={toggle}>
        <div ref={box} className="relative h-[60vh] min-h-80 overflow-hidden rounded-lg border bg-background" data-testid="diagram-graph">
          <ReactFlow<BoxNode>
            nodes={nodes}
            edges={rfEdges}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
            onNodeClick={onNodeClick}
            onInit={() => fitAll(0)}
            nodesDraggable={false}
            nodesConnectable={false}
            nodesFocusable={false}
            edgesFocusable={false}
            elementsSelectable={false}
            minZoom={MIN_ZOOM}
            maxZoom={2.5}
            proOptions={{ hideAttribution: true }}
            aria-label="Architecture diagram"
          >
            <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--border)" />
            <Panel position="bottom-right" className="flex flex-col gap-1">
              <Button variant="outline" size="icon" aria-label="Zoom in" onClick={() => void rf.zoomIn({ duration: 150 })}>
                <Plus />
              </Button>
              <Button variant="outline" size="icon" aria-label="Zoom out" onClick={() => void rf.zoomOut({ duration: 150 })}>
                <Minus />
              </Button>
              <Button variant="outline" size="icon" aria-label="Fit to view" onClick={() => fitAll(motionOk() ? 200 : 0)}>
                <Maximize2 />
              </Button>
            </Panel>
          </ReactFlow>
        </div>
      </Toggle.Provider>
      <Legend />
    </div>
  );
}

function Legend() {
  return (
    <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-3 w-5 rounded-sm border-[1.5px] border-foreground/30" /> group
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-3 w-5 rounded-sm border border-info/70" /> core
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-3 w-5 rounded-sm border border-dashed border-muted-foreground/60" /> launchd agent
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Dot tone="ok" /> healthy <Dot tone="warn" /> degraded <Dot tone="bad" /> failing
      </span>
      <span>Click a box to open it into its parts, (i) for its details. Drag to pan, scroll or pinch to zoom.</span>
    </p>
  );
}

// --- list (phones) ------------------------------------------------------------------------

function ListItem({
  item,
  open,
  toggle,
  outs,
}: {
  item: Item;
  open: ReadonlySet<string>;
  toggle: (id: string) => void;
  outs: Map<string, ReactNode>;
}) {
  const n = item.node;
  const parts = item.children.length > 0;
  const shown = isOpen(item, open);
  const roll = rollup(item);
  const health = n ? n.health : roll.health;
  return (
    <li className="py-2" data-item={item.id}>
      <div className="flex items-center gap-2">
        {parts ? (
          <button
            type="button"
            className="-ml-1 rounded p-0.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50"
            aria-expanded={shown}
            aria-label={`${shown ? "Collapse" : "Expand"} ${item.label}`}
            onClick={() => toggle(item.id)}
          >
            {shown ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </button>
        ) : (
          <span className="w-4 shrink-0" aria-hidden />
        )}
        {health && <Dot tone={healthTone(health)} />}
        <RouteLink to={itemRoute(item)} className={cn("min-w-0 truncate text-sm", n ? "font-medium" : "font-semibold")}>
          {item.label}
        </RouteLink>
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          {n && subline(item)}
          {parts && !shown && <RollupBadge roll={roll} />}
        </span>
      </div>
      {item.text && <p className="mt-1 ml-6 text-xs text-muted-foreground">{item.text}</p>}
      {outs.get(item.id)}
      {shown && (
        <ul className="mt-1 ml-3 divide-y border-l pl-3">
          {item.children.map((c) => (
            <ListItem key={c.id} item={c} open={open} toggle={toggle} outs={outs} />
          ))}
        </ul>
      )}
    </li>
  );
}

function List({ d, tree, open, toggle }: { d: DiagramData; tree: Tree; open: ReadonlySet<string>; toggle: (id: string) => void }) {
  // Where each group sends things, as the graph shows them with every box closed.
  const outs = useMemo(() => {
    const out = new Map<string, ReactNode>();
    const edges = shownEdges(d, tree, new Set());
    for (const g of tree.roots) {
      const mine = edges.filter((e) => e.source === g.id);
      if (!mine.length) continue;
      out.set(
        g.id,
        <p className="mt-1 ml-6 text-xs">
          <span className="text-muted-foreground">→ </span>
          {mine.map((e, i) => (
            <span key={e.id}>
              {i > 0 && ", "}
              {tree.byId.get(e.target)?.label}
              {e.label && <span className="text-muted-foreground"> ({e.label})</span>}
            </span>
          ))}
        </p>,
      );
    }
    return out;
  }, [d, tree]);
  return (
    <ul className="divide-y" data-testid="diagram-list">
      {tree.roots.map((g) => (
        <ListItem key={g.id} item={g} open={open} toggle={toggle} outs={outs} />
      ))}
    </ul>
  );
}
