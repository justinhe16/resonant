import { Info, Maximize2, Minus, Plus } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Explainer } from "@/components/Explainer";
import { Dot, Empty, Group, RouteLink, Row, Rows } from "@/components/bits";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { caption, edgePath, grouped, KIND_LABEL, layout, type Layout, NODE_H, nodeRoute, visible } from "@/lib/diagram";
import { toHash } from "@/lib/route";
import { healthTone } from "@/lib/tone";
import { cn } from "@/lib/utils";
import type { Diagram as DiagramData, DiagramNode, Snapshot } from "@/types";

type Mode = "graph" | "list";

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
  const saved = read("resonant.diagram.mode");
  if (saved === "graph" || saved === "list") return saved;
  try {
    return window.matchMedia("(min-width: 768px)").matches ? "graph" : "list";
  } catch {
    return "graph";
  }
}

const TONE_FILL: Record<string, string> = {
  ok: "fill-ok",
  warn: "fill-warn",
  bad: "fill-bad",
  info: "fill-info",
  neutral: "fill-muted-foreground/50",
};

export function Diagram({ snap }: { snap: Snapshot }) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [children, setChildren] = useState(() => read("resonant.diagram.children") !== "hide");
  const pick = (m: Mode) => {
    setMode(m);
    write("resonant.diagram.mode", m);
  };
  const toggleChildren = () => {
    setChildren((c) => {
      write("resonant.diagram.children", c ? "hide" : "show");
      return !c;
    });
  };
  const d = snap.diagram;
  return (
    <Card className="[--card-spacing:--spacing(5)]" data-testid="section-diagram">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="flex min-w-0 flex-1 items-center gap-1.5 text-sm font-medium">
            How Resonant is put together
            <Explainer name="Diagram" text="diagram" />
          </h2>
          {d && (
            <div className="flex flex-wrap items-center gap-1.5">
              {mode === "graph" && (
                <Button variant="outline" size="sm" onClick={toggleChildren} aria-pressed={children}>
                  {children ? "Hide tools, jobs and repos" : "Show tools, jobs and repos"}
                </Button>
              )}
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
        {!d ? (
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
            {mode === "graph" ? <Graph d={d} showChildren={children} /> : <List d={d} />}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// --- graph --------------------------------------------------------------------------------

interface View {
  x: number;
  y: number;
  k: number;
}

const MIN_K = 0.2;
const MAX_K = 2.5;

type Pt = { x: number; y: number };

/** The two pointers of a pinch, or null. */
function pair(points: Map<number, Pt>): [Pt, Pt] | null {
  const [a, b] = [...points.values()];
  return points.size === 2 && a && b ? [a, b] : null;
}

function clampK(k: number) {
  return Math.min(MAX_K, Math.max(MIN_K, k));
}

function fit(l: Layout, w: number, h: number): View {
  if (!l.width || !l.height || !w || !h) return { x: 0, y: 0, k: 1 };
  const k = clampK(Math.min(w / l.width, h / l.height, 1));
  return { x: (w - l.width * k) / 2, y: (h - l.height * k) / 2, k };
}

/** The first view: all of it when it fits legibly, else readable size from the left, where a
 * text comes in. */
function start(l: Layout, w: number, h: number): View {
  const all = fit(l, w, h);
  if (all.k >= START_K) return all;
  return { k: START_K, x: 8, y: (h - l.height * START_K) / 2 };
}

const START_K = 0.9;

function Graph({ d, showChildren }: { d: DiagramData; showChildren: boolean }) {
  const { nodes, edges } = useMemo(() => visible(d, showChildren), [d, showChildren]);
  const l = useMemo(() => layout(nodes, edges), [nodes, edges]);
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [view, setView] = useState<View | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const moved = useRef(0);
  const pinch = useRef<{ dist: number; k: number } | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // A new layout (children shown or hidden, a node added) fits again.
  const layoutKey = `${l.width}x${l.height}:${nodes.length}`;
  const fitted = useRef("");
  useEffect(() => {
    if (fitted.current !== layoutKey && size.w) {
      fitted.current = layoutKey;
      setView(start(l, size.w, size.h));
    }
  }, [layoutKey, l, size]);

  const v = view ?? start(l, size.w, size.h);

  const zoomAt = useCallback((factor: number, cx: number, cy: number) => {
    setView((cur) => {
      const base = cur ?? { x: 0, y: 0, k: 1 };
      const k = clampK(base.k * factor);
      const r = k / base.k;
      return { k, x: cx - (cx - base.x) * r, y: cy - (cy - base.y) * r };
    });
  }, []);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved.current = 0;
    const two = pair(pointers.current);
    if (two) pinch.current = { dist: Math.hypot(two[0].x - two[1].x, two[0].y - two[1].y), k: v.k };
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const last = pointers.current.get(e.pointerId);
    if (!last) return;
    const dx = e.clientX - last.x;
    const dy = e.clientY - last.y;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved.current += Math.abs(dx) + Math.abs(dy);
    if (moved.current > 4 && !box.current?.hasPointerCapture?.(e.pointerId)) {
      box.current?.setPointerCapture?.(e.pointerId);
    }
    const two = pair(pointers.current);
    if (two && pinch.current) {
      const [a, b] = two;
      const rect = box.current?.getBoundingClientRect();
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const target = clampK(pinch.current.k * (dist / pinch.current.dist));
      zoomAt(target / v.k, (a.x + b.x) / 2 - (rect?.left ?? 0), (a.y + b.y) / 2 - (rect?.top ?? 0));
      return;
    }
    if (pointers.current.size === 1) setView({ ...v, x: v.x + dx, y: v.y + dy });
  };
  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
  };

  const open = (n: DiagramNode) => {
    if (moved.current > 4) return; // the end of a drag, not a click
    window.location.hash = toHash(nodeRoute(n));
  };

  return (
    <div className="space-y-2">
      <div
        ref={box}
        className="relative h-[70vh] min-h-80 touch-none overflow-hidden rounded-lg border bg-background select-none"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        data-testid="diagram-graph"
      >
        <svg width="100%" height="100%" role="group" aria-label="Architecture diagram" className="block">
          <defs>
            <marker id="diagram-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" className="fill-muted-foreground" />
            </marker>
          </defs>
          <g transform={`translate(${v.x},${v.y}) scale(${v.k})`}>
            {l.edges.map((pe) => (
              <g key={`${pe.edge.source}->${pe.edge.target}`}>
                <path
                  d={edgePath(pe.points)}
                  fill="none"
                  className="stroke-muted-foreground/60"
                  strokeWidth={1.2}
                  markerEnd="url(#diagram-arrow)"
                />
                {pe.label && pe.edge.label && (
                  <text
                    x={pe.label.x}
                    y={pe.label.y}
                    textAnchor="middle"
                    dominantBaseline="middle"
                    className="fill-muted-foreground text-[10px]"
                    paintOrder="stroke"
                    stroke="var(--background)"
                    strokeWidth={3}
                  >
                    {pe.edge.label}
                  </text>
                )}
              </g>
            ))}
            {l.nodes.map((p) => (
              <NodeBox key={p.node.id} p={p} onOpen={open} />
            ))}
          </g>
        </svg>
        <div className="absolute right-2 bottom-2 flex flex-col gap-1">
          <Button variant="outline" size="icon" aria-label="Zoom in" onClick={() => zoomAt(1.25, size.w / 2, size.h / 2)}>
            <Plus />
          </Button>
          <Button variant="outline" size="icon" aria-label="Zoom out" onClick={() => zoomAt(0.8, size.w / 2, size.h / 2)}>
            <Minus />
          </Button>
          <Button variant="outline" size="icon" aria-label="Fit to view" onClick={() => setView(fit(l, size.w, size.h))}>
            <Maximize2 />
          </Button>
        </div>
      </div>
      <Legend />
    </div>
  );
}

const KIND_STROKE: Record<string, string> = {
  core: "stroke-info",
  extension: "stroke-foreground/50",
  process: "stroke-muted-foreground/60",
};

function NodeBox({ p, onOpen }: { p: { node: DiagramNode; x: number; y: number; w: number; h: number }; onOpen: (n: DiagramNode) => void }) {
  const n = p.node;
  const tone = n.health ? healthTone(n.health) : null;
  const x = p.x - p.w / 2;
  const y = p.y - p.h / 2;
  const label = n.label.length > 30 ? `${n.label.slice(0, 29)}…` : n.label;
  const sub = caption(n);
  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={`${n.label}, ${KIND_LABEL[n.kind]}${n.health ? `, ${n.health}` : ""}`}
      data-node={n.id}
      className="cursor-pointer outline-none [&:focus-visible>rect]:stroke-ring [&:hover>rect]:fill-muted"
      onClick={() => onOpen(n)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(n);
        }
      }}
    >
      <title>{n.text}</title>
      <rect
        x={x}
        y={y}
        width={p.w}
        height={NODE_H}
        rx={8}
        className={cn("fill-card", KIND_STROKE[n.kind] ?? "stroke-border")}
        strokeWidth={n.kind === "core" || n.kind === "extension" ? 1.6 : 1}
        strokeDasharray={n.kind === "process" ? "4 3" : undefined}
      />
      {tone && <circle cx={x + 12} cy={y + 16} r={4} className={TONE_FILL[tone]} />}
      <text x={x + (tone ? 22 : 10)} y={y + 19} className="fill-foreground text-[12px] font-medium">
        {label}
      </text>
      <text x={x + 10} y={y + 35} className="fill-muted-foreground text-[10px]">
        {sub.length > 40 ? `${sub.slice(0, 39)}…` : sub}
      </text>
    </g>
  );
}

function Legend() {
  return (
    <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-3 w-5 rounded-sm border-[1.6px] border-info" /> core
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-3 w-5 rounded-sm border-[1.6px] border-foreground/50" /> extension
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-3 w-5 rounded-sm border border-dashed border-muted-foreground/60" /> launchd agent
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Dot tone="ok" /> healthy <Dot tone="warn" /> degraded <Dot tone="bad" /> failing
      </span>
      <span>Drag to pan, scroll or pinch to zoom; click a box for its details.</span>
    </p>
  );
}

// --- list (phones) ------------------------------------------------------------------------

function NodeRow({ n, d, nested }: { n: DiagramNode; d: DiagramData; nested?: DiagramNode[] }) {
  const byId = useMemo(() => new Map(d.nodes.map((x) => [x.id, x])), [d]);
  const out = d.edges.filter((e) => e.source === n.id && byId.get(e.target) && byId.get(e.target)?.parent !== n.id);
  return (
    <Row className="relative">
      <div className="flex items-center gap-2">
        {n.health && <Dot tone={healthTone(n.health)} />}
        <RouteLink to={nodeRoute(n)} stretch className="min-w-0 truncate text-sm font-medium">
          {n.label}
        </RouteLink>
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">{caption(n)}</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{n.text}</p>
      {out.length > 0 && (
        <p className="mt-1 text-xs">
          <span className="text-muted-foreground">→ </span>
          {out.map((e, i) => (
            <span key={`${e.source}-${e.target}`}>
              {i > 0 && ", "}
              {byId.get(e.target)?.label}
              {e.label && <span className="text-muted-foreground"> ({e.label})</span>}
            </span>
          ))}
        </p>
      )}
      {nested && nested.length > 0 && (
        <div className="relative z-10 mt-2 border-l pl-3">
          <Rows>
            {nested.map((c) => (
              <NodeRow key={c.id} n={c} d={d} />
            ))}
          </Rows>
        </div>
      )}
    </Row>
  );
}

function List({ d }: { d: DiagramData }) {
  const g = useMemo(() => grouped(d), [d]);
  return (
    <div className="space-y-5" data-testid="diagram-list">
      <Group label="Core" count={g.core.length}>
        <Rows>
          {g.core.map((n) => (
            <NodeRow key={n.id} n={n} d={d} nested={g.childrenOf(n.id)} />
          ))}
        </Rows>
      </Group>
      <Group label="Processes" count={g.processes.length}>
        {g.processes.length === 0 ? (
          <Empty>No launchd agents found.</Empty>
        ) : (
          <Rows>
            {g.processes.map((n) => (
              <NodeRow key={n.id} n={n} d={d} />
            ))}
          </Rows>
        )}
      </Group>
      <Group label="Extensions" count={g.extensions.length}>
        {g.extensions.length === 0 ? (
          <Empty>No extensions installed.</Empty>
        ) : (
          <Rows>
            {g.extensions.map(({ ext, children }) => (
              <NodeRow key={ext.id} n={ext} d={d} nested={children} />
            ))}
          </Rows>
        )}
      </Group>
    </div>
  );
}
