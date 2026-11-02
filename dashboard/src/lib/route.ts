import { useSyncExternalStore } from "react";

/** What the detail dialog shows, kept in the URL hash so a view can be linked and bookmarked. */
export type Route =
  | { kind: "run"; code: string }
  | { kind: "runs"; state?: string }
  | { kind: "task"; id: string }
  | { kind: "tasks"; state?: string }
  | { kind: "effects"; effect: string }
  | { kind: "approval"; id: string }
  | { kind: "question"; id: number }
  | { kind: "extension"; name: string }
  | { kind: "job"; ext: string; id: string }
  | { kind: "pulse"; source: string; key: string }
  | { kind: "health"; name: string }
  | { kind: "brain-tool"; at: string };

const enc = encodeURIComponent;

function dec(s: string | undefined): string | null {
  if (!s) return null;
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
}

/** Parses `#/runs/R12`, `#/runs?state=failed`, …; anything else (an empty hash, a login
 * fragment) is not a route and returns null. */
export function parseHash(hash: string): Route | null {
  if (!hash.startsWith("#/")) return null;
  const [path = "", query = ""] = hash.slice(2).split("?", 2);
  const seg: string[] = [];
  for (const raw of path.split("/")) {
    if (raw === "") continue;
    const d = dec(raw);
    if (d === null) return null;
    seg.push(d);
  }
  const [head, a, b] = seg;
  const state = new URLSearchParams(query).get("state") || undefined;
  switch (head) {
    case "runs":
      return a ? { kind: "run", code: a } : { kind: "runs", ...(state && { state }) };
    case "tasks":
      return a ? { kind: "task", id: a } : { kind: "tasks", ...(state && { state }) };
    case "effects":
      return a ? { kind: "effects", effect: a } : null;
    case "approvals":
      return a ? { kind: "approval", id: a } : null;
    case "questions": {
      const id = a && /^\d+$/.test(a) ? Number(a) : null;
      return id === null ? null : { kind: "question", id };
    }
    case "extensions":
      return a ? { kind: "extension", name: a } : null;
    case "jobs":
      return a && b ? { kind: "job", ext: a, id: b } : null;
    case "pulse":
      return a && b ? { kind: "pulse", source: a, key: b } : null;
    case "health":
      return a ? { kind: "health", name: a } : null;
    case "brain":
      return a ? { kind: "brain-tool", at: a } : null;
    default:
      return null;
  }
}

export function toHash(r: Route): string {
  switch (r.kind) {
    case "run":
      return `#/runs/${enc(r.code)}`;
    case "runs":
      return r.state ? `#/runs?state=${enc(r.state)}` : "#/runs";
    case "task":
      return `#/tasks/${enc(r.id)}`;
    case "tasks":
      return r.state ? `#/tasks?state=${enc(r.state)}` : "#/tasks";
    case "effects":
      return `#/effects/${enc(r.effect)}`;
    case "approval":
      return `#/approvals/${enc(r.id)}`;
    case "question":
      return `#/questions/${r.id}`;
    case "extension":
      return `#/extensions/${enc(r.name)}`;
    case "job":
      return `#/jobs/${enc(r.ext)}/${enc(r.id)}`;
    case "pulse":
      return `#/pulse/${enc(r.source)}/${enc(r.key)}`;
    case "health":
      return `#/health/${enc(r.name)}`;
    case "brain-tool":
      return `#/brain/${enc(r.at)}`;
  }
}

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}
const snapshot = () => window.location.hash;

/** The route in the hash now (null when there is none); re-renders on `hashchange`. */
export function useHashRoute(): Route | null {
  const hash = useSyncExternalStore(subscribe, snapshot, () => "");
  return parseHash(hash);
}

/** Closes the dialog: drops the hash without scrolling, and tells listeners. It replaces the
 *  history entry rather than adding one, so Back does not reopen the dialog just closed. */
export function closeRoute(): void {
  try {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  } catch {
    window.location.hash = "";
    return;
  }
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}
