import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { credentialGroups } from "@/lib/credentials";
import { Credentials } from "@/sections/SystemStatus";
import { clone, FIXTURE_NOW, local } from "@/test/fixtures";
import type { SecretExpiry } from "@/types";

const at = (days: number) => new Date(Date.parse(local.taken_at) + days * 86_400_000).toISOString();
const secret = (ref: string, days: number, patch: Partial<SecretExpiry> = {}): SecretExpiry => ({
  ref,
  expires_at: at(days),
  days_left: days,
  source: "owner",
  probe: null,
  ...patch,
});

const NOW = FIXTURE_NOW.getTime();

describe("the credentials card (PER-315)", () => {
  it("shows each instance's soonest 3 side by side, coloured by how soon", () => {
    const mine = clone(local, {
      secrets: [
        secret("runner/github_token", -2, { source: "probe" }),
        secret("relay/supabase_key", 5),
        secret("linear/token", 40, { probe: "rejected" }),
        secret("runner/claude_token", 300, { source: "assumed" }),
      ],
    });
    const cori = clone(local, { secrets: [secret("runner/claude_token", 364, { source: "assumed" })] });
    const groups = credentialGroups([
      { name: "Resonant", snap: mine },
      { name: "Cori", snap: cori },
    ]);
    render(<Credentials groups={groups} now={NOW} />);
    const card = screen.getByTestId("credentials");
    expect(card.className).toContain("sm:grid-cols-2");

    const resonant = within(screen.getByTestId("credentials-Resonant"));
    expect(resonant.getByText("Resonant")).toBeInTheDocument();
    const rows = resonant.getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual([
      "runner/github_tokenexpired",
      "relay/supabase_key5d",
      "linear/token40d (rejected)",
    ]); // the soonest 3: the fourth is left out
    expect(rows.map((r) => r.dataset.tone)).toEqual(["bad", "warn", "bad"]);

    const coriRows = within(screen.getByTestId("credentials-Cori")).getAllByRole("listitem");
    expect(coriRows.map((r) => r.textContent)).toEqual(["runner/claude_token364d (assumed)"]);
    expect(coriRows[0]!.dataset.tone).toBe("neutral");
    expect(screen.getAllByText(/^as of /)).toHaveLength(2);
    expect(screen.getByRole("button", { name: "About Credentials" })).toBeInTheDocument();
  });

  it("one instance: no heading, one column", () => {
    const groups = credentialGroups([{ name: "Resonant", snap: clone(local, { secrets: [secret("github/token", 0)] }) }]);
    render(<Credentials groups={groups} now={NOW} />);
    expect(screen.getByTestId("credentials").className).not.toContain("sm:grid-cols-2");
    expect(screen.queryByText("Resonant")).not.toBeInTheDocument();
    const [row] = screen.getAllByRole("listitem");
    expect(row!.textContent).toBe("github/tokenexpired"); // expires_at == taken_at: expired
  });

  it("an instance without secrets (an old daemon, or none known) has no group", () => {
    expect(
      credentialGroups([
        { name: "Resonant", snap: local },
        { name: "Cori", snap: clone(local, { secrets: [] }) },
        { name: "Other", snap: null },
      ]),
    ).toEqual([]);
  });
});
