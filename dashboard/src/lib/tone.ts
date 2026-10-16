import type { BadgeTone } from "@/components/ui/badge";

export function healthTone(state: string): BadgeTone {
  switch (state) {
    case "ok":
      return "ok";
    case "degraded":
      return "warn";
    case "failing":
      return "bad";
    default:
      return "neutral";
  }
}

export function runTone(state: string): BadgeTone {
  switch (state) {
    case "running":
      return "info";
    case "waiting":
    case "ready":
      return "warn";
    case "failed":
      return "bad";
    case "merged":
    case "done":
      return "ok";
    default:
      return "neutral";
  }
}

export function urgencyTone(urgency: string): BadgeTone {
  return urgency === "urgent" ? "bad" : urgency === "normal" ? "info" : "neutral";
}

export function levelTone(level: string): BadgeTone {
  return level === "L1" ? "bad" : level === "L2" ? "warn" : "neutral";
}

export function outcomeTone(outcome: string | null): BadgeTone {
  if (!outcome) return "neutral";
  if (/^(ok|success|done)$/i.test(outcome)) return "ok";
  if (/fail|error|timeout/i.test(outcome)) return "bad";
  return "neutral";
}
