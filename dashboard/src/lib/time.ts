/** Seconds between an ISO timestamp and `now` (positive when `iso` is in the past). */
export function ageSeconds(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (now - t) / 1000;
}

function span(seconds: number): string {
  const s = Math.round(Math.abs(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.round(s / 3600);
  if (h < 48) return `${h} h`;
  return `${Math.round(s / 86400)} d`;
}

/** "just now", "45s ago", "12 min ago", "in 5 min"; "never" for a missing time. */
export function relative(iso: string | null | undefined, now: number): string {
  const age = ageSeconds(iso, now);
  if (age === null) return "never";
  if (Math.abs(age) < 5) return "just now";
  return age >= 0 ? `${span(age)} ago` : `in ${span(age)}`;
}

/** "3 h", "2 d": how long since `iso`, without a direction. */
export function duration(iso: string | null | undefined, now: number): string {
  const age = ageSeconds(iso, now);
  return age === null ? "unknown" : span(age);
}

/** Absolute local time for a title tooltip. */
export function absolute(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? iso : t.toLocaleString();
}
