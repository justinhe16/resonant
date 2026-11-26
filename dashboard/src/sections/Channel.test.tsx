import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { PanelGlance } from "@/components/panel/PanelGlance";
import { channelName } from "@/lib/snapshot";
import { Claude } from "@/sections/Claude";
import { Rules } from "@/sections/Rules";
import { Slack } from "@/sections/Slack";
import { Credentials } from "@/sections/SystemStatus";
import { WaitingOnYou } from "@/sections/WaitingOnYou";
import { clone, cori, FIXTURE_NOW, local } from "@/test/fixtures";
import { openSections } from "@/test/sections";
import { credentialGroups } from "@/lib/credentials";
import type { Panel, SecretExpiry, Snapshot } from "@/types";

const NOW = FIXTURE_NOW.getTime();

beforeEach(openSections);

/** Cori as it runs: Slack on, iMessage off (so no imessage health row). */
const slackOnly: Snapshot = clone(cori, {
  health: cori.health.filter((h) => h.name !== "imessage"),
});

describe("the instance's channel", () => {
  it("is Slack with a Slack adapter and no iMessage row, else iMessage", () => {
    expect(channelName(slackOnly)).toBe("Slack");
    expect(channelName(local)).toBe("iMessage");
    expect(channelName(clone(local, { channels: null }))).toBe("iMessage");
  });

  it("words the waiting and rules hints with it", () => {
    const { unmount } = render(<WaitingOnYou snap={slackOnly} now={NOW} />);
    expect(screen.getByText("Answer on Slack")).toBeInTheDocument();
    unmount();
    render(<WaitingOnYou snap={local} now={NOW} />);
    expect(screen.getByText("Answer on iMessage")).toBeInTheDocument();
  });

  it("words the rules hint with it", () => {
    const { unmount } = render(<Rules rules={[]} repos={[]} channel="Slack" />);
    expect(screen.getByText(/\/rules on Slack\./)).toBeInTheDocument();
    unmount();
    render(<Rules rules={[]} repos={[]} />);
    expect(screen.getByText(/\/rules on iMessage\./)).toBeInTheDocument();
  });
});

describe("the Claude brain between turns", () => {
  it("shows idle as a neutral pill, not down", () => {
    const snap = clone(local);
    snap.claude_brain = { state: "idle", turns: 15, tools: [] };
    snap.brain_activity = { ...snap.brain_activity, state: "idle", turns: 15 };
    render(<Claude snap={snap} now={NOW} />);
    expect(screen.getByText("idle")).toBeInTheDocument();
    expect(screen.queryByText("down")).toBeNull();
  });
});

describe("extension panel preview", () => {
  const panel = (widgets: unknown[]): Panel =>
    ({
      id: "p",
      title: "Deploys",
      stale: false,
      last_run_at: "2026-10-16T08:59:00Z",
      updated_at: "2026-10-16T08:59:00Z",
      views: [{ id: "v", title: "V", widgets }],
    }) as unknown as Panel;

  it("shows the first rows of a table", () => {
    const table = {
      type: "table",
      columns: [
        { key: "a", label: "Service", type: "text" },
        { key: "b", label: "Status", type: "pill" },
      ],
      rows: [
        ["api", { label: "ok", tone: "ok" }],
        ["web", { label: "slow", tone: "warn" }],
        ["db", "ok"],
        ["queue", "ok"],
      ],
      truncated: false,
    };
    render(<PanelGlance ext="dori" panel={panel([table])} now={NOW} />);
    const t = screen.getByTestId("glance-table");
    expect(t).toHaveTextContent("apiok");
    expect(t).toHaveTextContent("webslow");
    expect(t).toHaveTextContent("+1 more");
    expect(t).not.toHaveTextContent("queue");
  });

  it("shows status items next to a stat", () => {
    const stat = { type: "stat", label: "Prod 5xx", value: 0, format: "integer" };
    const status = {
      type: "status",
      items: [
        { name: "prod", state: "up", detail: "120ms" },
        { name: "staging", state: "down" },
      ],
      truncated: false,
    };
    render(<PanelGlance ext="dori" panel={panel([stat, status])} now={NOW} />);
    expect(screen.getByTestId("stat")).toBeInTheDocument();
    const s = screen.getByTestId("glance-status");
    expect(s).toHaveTextContent("prod");
    expect(s).toHaveTextContent("120ms");
    expect(s).toHaveTextContent("staging");
  });

  it("is never an empty box when its widgets have nothing to preview", () => {
    render(<PanelGlance ext="dori" panel={panel([{ type: "text", text: "hello" }])} now={NOW} />);
    expect(screen.getByTestId("panel-glance")).toHaveTextContent("Open for the details.");
  });
});

describe("the credentials card shows every credential", () => {
  it("keeps the soonest 3 and expands the rest under N more", async () => {
    const at = (days: number) => new Date(Date.parse(local.taken_at) + days * 86_400_000).toISOString();
    const secrets: SecretExpiry[] = Array.from({ length: 7 }, (_, i) => ({
      ref: `ref/${i}`,
      expires_at: at(10 + i),
      days_left: 10 + i,
      source: "owner",
      probe: null,
    }));
    const groups = credentialGroups([{ name: "Cori", snap: clone(local, { secrets }) }]);
    render(<Credentials groups={groups} now={NOW} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    const more = screen.getByRole("button", { name: "4 more" });
    await userEvent.setup().click(more);
    expect(await screen.findAllByRole("listitem")).toHaveLength(7);
    expect(screen.getByRole("button", { name: "Show fewer" })).toBeInTheDocument();
  });
});

describe("collapsed headers (PER-345)", () => {
  beforeEach(() => window.localStorage.clear());

  it("shows Slack collapsed, its connection as a header badge", () => {
    const slack = { connected: true, last_event_at: null, followed_threads: 2 };
    const { unmount } = render(<Slack slack={{ ...slack, connected: true }} now={NOW} />);
    expect(screen.getByRole("button", { name: /^Slack/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("slack-row")).not.toBeInTheDocument();
    expect(screen.getByTestId("slack-state")).toHaveTextContent("connected");
    unmount();
    render(<Slack slack={{ ...slack, connected: false }} now={NOW} />);
    expect(screen.getByTestId("slack-state")).toHaveTextContent("disconnected");
  });

  it("keeps a worker runner that isn't set up visible on the collapsed Claude header", () => {
    const { unmount } = render(<Claude snap={clone(local, { runner: { ...local.runner, set_up: false } })} now={NOW} />);
    expect(screen.getByTestId("runner-not-set-up")).toBeInTheDocument();
    expect(screen.queryByText("Worker runner")).not.toBeInTheDocument();
    unmount();
    render(<Claude snap={clone(local, { runner: { ...local.runner, set_up: true } })} now={NOW} />);
    expect(screen.queryByTestId("runner-not-set-up")).not.toBeInTheDocument();
  });
});
