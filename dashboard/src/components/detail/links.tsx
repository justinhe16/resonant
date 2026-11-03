import { RouteLink, Rows } from "@/components/bits";
import { nodeRoute } from "@/lib/diagram";
import { findApproval } from "@/lib/snapshot";
import { RunRow } from "@/sections/Work";
import type { DiagramNode, Snapshot } from "@/types";

export const idChip = "rounded bg-muted px-1.5 py-0.5 font-mono text-xs";

export function Missing({ what, keeps, snap }: { what: string; keeps: string; snap: Snapshot }) {
  return (
    <div className="space-y-2 text-sm" data-testid="not-kept">
      <p>
        {what} isn't in this snapshot: {keeps}.
      </p>
      {snap.trimmed !== "none" && (
        <p className="text-muted-foreground">
          This snapshot was also trimmed to fit the relay ({snap.trimmed}), so some lists are shorter than usual.
        </p>
      )}
    </div>
  );
}

export function ApprovalLinks({ ids, snap }: { ids: string[]; snap: Snapshot }) {
  if (ids.length === 0) return null;
  const seen = new Set<string>();
  return (
    <span className="flex flex-wrap gap-1.5">
      {ids.map((id) => {
        const a = findApproval(snap, id);
        const key = a?.id ?? id;
        if (seen.has(key)) return null;
        seen.add(key);
        return a ? (
          <RouteLink key={key} to={{ kind: "approval", id: a.id }} className={`${idChip} text-info`}>
            {a.id}
          </RouteLink>
        ) : (
          <span key={key} className={idChip} title="Not in this snapshot">
            {id}
          </span>
        );
      })}
    </span>
  );
}

export function RunLink({ code }: { code: string | null }) {
  if (!code) return null;
  return (
    <RouteLink to={{ kind: "run", code }} className="font-mono font-medium text-info">
      {code}
    </RouteLink>
  );
}

export function TaskLink({ id }: { id: string | null }) {
  if (!id) return null;
  return (
    <RouteLink to={{ kind: "task", id }} className="font-mono font-medium text-info">
      {id}
    </RouteLink>
  );
}

export function RunLines({ runs, now }: { runs: Snapshot["runs"]["items"]; now: number }) {
  return (
    <Rows>
      {runs.map((r) => (
        <RunRow key={r.code} run={r} now={now} />
      ))}
    </Rows>
  );
}

/** The diagram's one sentence on what a component does, when the snapshot has it. */
export function DiagramText({ snap, id }: { snap: Snapshot; id: string }) {
  const n = snap.diagram?.nodes.find((x) => x.id === id);
  if (!n?.text) return null;
  return <p className="text-sm">{n.text}</p>;
}

export function NodeLink({ n }: { n: DiagramNode }) {
  return (
    <RouteLink to={nodeRoute(n)} className="font-medium text-info">
      {n.label}
    </RouteLink>
  );
}

