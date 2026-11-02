import { Check, Link as LinkIcon } from "lucide-react";
import { type Ref, useEffect, useRef, useState } from "react";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { describeRoute, type View } from "@/components/detail/views";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { closeRoute, toHash, useHashRoute } from "@/lib/route";
import type { Snapshot } from "@/types";

/**
 * The one detail view, for both tiers: a centered modal from `sm` up, a bottom sheet below.
 * What it shows comes from the URL hash (`#/runs/R12`), so a view can be linked.
 */
export function DetailDialog({ snap, now }: { snap: Snapshot; now: number }) {
  const route = useHashRoute();
  const hash = route ? toHash(route) : "";
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    body.current?.scrollTo?.({ top: 0 });
  }, [hash]);
  return (
    <Dialog open={route !== null} onOpenChange={(open) => !open && closeRoute()}>
      {route && <Content route={route} snap={snap} now={now} hash={hash} contentRef={body} />}
    </Dialog>
  );
}

function Content({
  route,
  snap,
  now,
  hash,
  contentRef,
}: {
  route: NonNullable<ReturnType<typeof useHashRoute>>;
  snap: Snapshot;
  now: number;
  hash: string;
  contentRef: Ref<HTMLDivElement>;
}) {
  const view = safeDescribe(route, snap, now);
  return (
    <DialogContent ref={contentRef} data-testid="detail-dialog">
      <DialogHeader>
        <DialogTitle>{view.title}</DialogTitle>
        <DialogDescription className="break-words">{view.description ?? "Details from the latest snapshot"}</DialogDescription>
      </DialogHeader>
      <ErrorBoundary resetKey={`${hash}-${snap.taken_at}`}>{view.body}</ErrorBoundary>
      <div className="flex justify-start">
        <CopyLink hash={hash} />
      </div>
    </DialogContent>
  );
}

function safeDescribe(route: NonNullable<ReturnType<typeof useHashRoute>>, snap: Snapshot, now: number): View {
  try {
    return describeRoute(route, { snap, now });
  } catch (e) {
    return { title: "Details", body: <p className="text-sm text-bad">{e instanceof Error ? e.message : String(e)}</p> };
  }
}

function CopyLink({ hash }: { hash: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(id);
  }, [copied]);
  const copy = async () => {
    try {
      const url = `${window.location.origin}${window.location.pathname}${window.location.search}${hash}`;
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      /* no clipboard permission: the address bar has the link */
    }
  };
  return (
    <Button variant="outline" size="sm" onClick={() => void copy()} aria-label="Copy link to this view">
      {copied ? <Check /> : <LinkIcon />}
      {copied ? "Copied" : "Copy link"}
    </Button>
  );
}
