import type { BadgeTone } from "@/components/ui/badge";
import type { SecretExpiry, Snapshot } from "@/types";

/** PER-315: one instance's credential expiries, from its newest snapshot. */
export interface CredentialGroup {
  name: string;
  taken_at: string;
  secrets: SecretExpiry[];
}

/** How many of each instance's soonest expiries the card shows at first; the rest expand
 * under "N more" (the snapshot keeps up to 16). */
export const CREDENTIALS_SHOWN = 3;
const SOON_DAYS = 7;

/** The groups worth a row: an instance whose snapshot has no `secrets` (an old daemon, or
 * none known) is left out, and with none left there is no card. */
export function credentialGroups(entries: { name: string; snap: Snapshot | null | undefined }[]): CredentialGroup[] {
  return entries.flatMap(({ name, snap }) =>
    snap?.secrets?.length ? [{ name, taken_at: snap.taken_at, secrets: snap.secrets }] : [],
  );
}

function expired(e: SecretExpiry, takenAt: string): boolean {
  return e.days_left < 0 || Date.parse(e.expires_at) <= Date.parse(takenAt);
}

/** Red when expired or rejected, amber within 7 days, plain otherwise. */
export function credentialTone(e: SecretExpiry, takenAt: string): BadgeTone {
  if (expired(e, takenAt) || e.probe === "rejected") return "bad";
  return e.days_left <= SOON_DAYS ? "warn" : "neutral";
}

export function credentialLabel(e: SecretExpiry, takenAt: string): string {
  if (expired(e, takenAt)) return "expired";
  const days = e.days_left === 0 ? "today" : `${e.days_left}d`;
  const notes = [e.source === "assumed" && "assumed", e.probe === "rejected" && "rejected"].filter(Boolean);
  return notes.length ? `${days} (${notes.join(", ")})` : days;
}
