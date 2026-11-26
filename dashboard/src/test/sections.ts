/**
 * Every collapsible list section's id. Sections start collapsed to their header (PER-345), so
 * tests about a section's rows open them first, as an owner who had opened them would see them.
 */
export const SECTION_IDS = [
  "waiting",
  "active-runs",
  "extensions",
  "pulse",
  "runs",
  "tasks",
  "claude",
  "jobs",
  "rules",
  "registered-jobs",
  "slack",
] as const;

/** Remembers every section as open, as CollapsibleSection stores it. */
export function openSections() {
  for (const id of SECTION_IDS) window.localStorage.setItem(`resonant.section.${id}`, "open");
}
