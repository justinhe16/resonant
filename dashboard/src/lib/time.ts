/** Seconds between an ISO timestamp and `now` (positive when `iso` is in the past). */
export function ageSeconds(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (now - t) / 1000;
}

export function span(seconds: number): string {
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

/** "now", "45s", "12m", "3h", "2d", "in 5m": a row's compact relative time. */
export function short(iso: string | null | undefined, now: number): string {
  const age = ageSeconds(iso, now);
  if (age === null) return "never";
  const s = Math.round(Math.abs(age));
  if (s < 5) return "now";
  const v =
    s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
  return age >= 0 ? v : `in ${v}`;
}

/** "9:30am PT, Oct 26": a local wall-clock time with its zone (a task's creation time). */
export function localStamp(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  const parts = (zoneName: "shortGeneric" | "short") =>
    new Intl.DateTimeFormat("en-US", {
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
      month: "short",
      day: "numeric",
      timeZoneName: zoneName,
      timeZone,
    }).formatToParts(t);
  let got: Intl.DateTimeFormatPart[];
  try {
    got = parts("shortGeneric");
  } catch {
    got = parts("short"); // an engine without generic zone names
  }
  const p = (type: Intl.DateTimeFormatPartTypes) => got.find((x) => x.type === type)?.value ?? "";
  const zone = p("timeZoneName");
  return `${p("hour")}:${p("minute")}${p("dayPeriod").toLowerCase()}${zone ? ` ${zone}` : ""}, ${p("month")} ${p("day")}`;
}
