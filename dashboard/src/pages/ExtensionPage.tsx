import { ArrowLeft } from "lucide-react";
import { useEffect } from "react";
import { Explainer } from "@/components/Explainer";
import { StateBadge } from "@/components/bits";
import { PanelCard } from "@/components/panel/PanelCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { closeRoute, type Route } from "@/lib/route";
import { healthTone } from "@/lib/tone";
import type { Snapshot } from "@/types";

/** An extension's dashboard (`#/ext/<name>[/<panel>]`): every panel it declares as a card,
 * one column on a phone and two from `lg`. */
export function ExtensionPage({ snap, now, route }: { snap: Snapshot; now: number; route: Extract<Route, { kind: "ext" }> }) {
  const ext = snap.extensions.find((e) => e.name === route.name);
  const target = route.panel;
  useEffect(() => {
    if (target) document.getElementById(`panel-${target}`)?.scrollIntoView?.({ block: "start" });
  }, [target, ext?.panels.length]);
  return (
    <div className="space-y-5" data-testid="extension-page">
      <div className="space-y-2">
        <Button variant="ghost" size="sm" onClick={closeRoute} className="-ml-2">
          <ArrowLeft />
          Back
        </Button>
        {ext ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">{ext.name}</h1>
              <StateBadge tone={healthTone(ext.health)}>{ext.health}</StateBadge>
              {ext.status !== "enabled" && <Badge variant="warn">{ext.status.replace(/_/g, " ")}</Badge>}
              <Explainer name="panels" text="panels" />
            </div>
            {ext.description && <p className="text-sm text-muted-foreground">{ext.description}</p>}
          </>
        ) : (
          <h1 className="text-xl font-semibold">{route.name}</h1>
        )}
      </div>
      {!ext ? (
        <p className="text-sm text-muted-foreground" data-testid="ext-unknown">
          There is no extension called {route.name} in this snapshot.
        </p>
      ) : ext.panels.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="ext-no-panels">
          {ext.name} has no dashboard panels.
        </p>
      ) : (
        <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
          {ext.panels.map((p) => (
            <PanelCard key={p.id} ext={ext.name} panel={p} now={now} />
          ))}
        </div>
      )}
    </div>
  );
}
