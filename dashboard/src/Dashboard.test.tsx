import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "@/Dashboard";
import { clone, empty, FIXTURE_NOW, local, remote } from "@/test/fixtures";
import type { Snapshot } from "@/types";

function mount(snap: Snapshot | (() => Promise<Snapshot | null>)) {
  const load = vi.fn(typeof snap === "function" ? snap : async () => snap);
  render(<Dashboard load={load} />);
  return load;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(FIXTURE_NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

const headings = (testId: string) =>
  within(screen.getByTestId(testId))
    .getAllByRole("heading", { level: 2 })
    .map((h) => h.textContent);

describe("Dashboard, local tier", () => {
  it("shows the system status at a glance", async () => {
    mount(local);
    expect(await screen.findByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
    expect(screen.getByText("imessage degraded · ext:github unknown")).toBeInTheDocument();
    expect(screen.getByText("autonomy on")).toBeInTheDocument();
    expect(screen.getByText("jobs running")).toBeInTheDocument();
    expect(screen.getByText("dry run off")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Slots in use" })).toHaveAttribute("aria-valuetext", "2 of 5");
    expect(screen.getByTestId("last-seen")).toHaveTextContent("seen 20s ago");
    expect(screen.getByText("22s ago")).toBeInTheDocument(); // last tick
    expect(screen.queryByTestId("stale-banner")).not.toBeInTheDocument();
  });

  it("keeps per-component health in a collapsed list", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount(local);
    const toggle = await screen.findByRole("button", { name: /Component health/ });
    expect(toggle).toHaveTextContent("3 of 5 ok");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("self-test timed out after 90s")).not.toBeInTheDocument();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("self-test timed out after 90s")).toBeInTheDocument();
    expect(screen.getByText("loop")).toBeInTheDocument();
  });

  it("lays out two columns in order", async () => {
    mount(local);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(headings("column-left")).toEqual(["Slots", "Active runs", "Active tasks"]);
    expect(headings("column-right")).toEqual([
      "Waiting on you",
      "Runs and tasks",
      "Claude",
      "Extensions",
      "Pulse",
      "Rules and repos",
    ]);
    // The status card's headline is the page's one h1.
    expect(within(screen.getByTestId("column-left")).getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("shows an approval batch as one row with its changes", async () => {
    mount(local);
    await screen.findByRole("heading", { name: "Something is degraded" });
    const row = screen.getByText("3 changes, all or none").closest("li")!;
    const changes = within(row).getByRole("list", { name: "Changes" });
    expect(within(changes).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      '1.linear: save issue title="Retry the relay push"L3',
      '2.linear: save comment issue="PER-263"L3',
      "3.restart resonantL2",
    ]);
    expect(within(row).getByText("code D7")).toBeInTheDocument();
    expect(within(row).getByText("one answer for all")).toBeInTheDocument();
    expect(within(row).getByText("expires in 55 min")).toBeInTheDocument();
    expect(within(row).getAllByText("L2")).toHaveLength(2); // the batch's strictest, and change 3
    // A lone approval has no change list.
    const lone = screen.getByText("merge_pr github: example/resonant#81").closest("li")!;
    expect(within(lone).queryByRole("list", { name: "Changes" })).not.toBeInTheDocument();
    expect(within(lone).queryByText("one answer for all")).not.toBeInTheDocument();
  });

  it("shows every section's local detail on one page", async () => {
    mount(local);
    await screen.findByRole("heading", { name: "Something is degraded" });

    // Active runs on the left, finished ones on the right.
    const left = screen.getByTestId("column-left");
    const right = screen.getByTestId("column-right");
    expect(within(left).getByText(/drops Python 3.11|Add a dashboard job to CI/)).toBeInTheDocument();
    expect(within(left).getByText("waiting (question)")).toBeInTheDocument();
    expect(within(right).getByText("R10")).toBeInTheDocument(); // ready
    expect(within(left).queryByText("R10")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "PR #81" })).toHaveAttribute(
      "href",
      "https://github.com/example/resonant/pull/81",
    );

    expect(screen.getByText(/drops Python 3.11/)).toBeInTheDocument();
    expect(screen.getByText("(a) bump runtime")).toBeInTheDocument();
    expect(screen.getByText("merge_pr github: example/resonant#81")).toBeInTheDocument();
    expect(screen.getByText("L1")).toBeInTheDocument();
    expect(screen.getByText("expires in 50 min")).toBeInTheDocument();
    // Waiting on you: two approval rows (one a batch) and one question; Pulse: three items.
    expect(screen.getAllByText("3 open")).toHaveLength(2);

    const pulse = screen.getByText("Rain after 4pm").closest("ul")!;
    expect(within(pulse).getAllByRole("listitem")[0]).toHaveTextContent("R11 asked a question"); // urgent first

    expect(screen.getByText("2 fails in a row")).toBeInTheDocument();
    expect(screen.getByText("No scheduled jobs.")).toBeInTheDocument();

    expect(screen.getByText("ask merge on trading")).toBeInTheDocument();
    expect(screen.getByText("no CI")).toBeInTheDocument();
  });

  it("says how many side effects were detected per run (PER-263)", async () => {
    mount(local);
    expect(await screen.findByText("2 side effects detected")).toBeInTheDocument();
    expect(screen.getByText("1 side effect detected")).toBeInTheDocument();
    expect(screen.getAllByText(/side effects? detected/)).toHaveLength(2); // none for a run with 0
  });

  it("refuses non-https PR links", async () => {
    const snap = clone(local);
    snap.runs.items![0]!.pr_url = "javascript:alert(1)";
    snap.runs.items![0]!.pr_number = 99;
    mount(snap);
    await screen.findByText(/Add a dashboard job/);
    expect(screen.queryByRole("link", { name: "PR #99" })).not.toBeInTheDocument();
  });

  it("toggles dark mode, light first", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    localStorage.clear();
    document.documentElement.classList.remove("dark");
    mount(local);
    const toggle = await screen.findByRole("button", { name: "Switch to dark mode" });
    expect(document.documentElement).not.toHaveClass("dark");
    await user.click(toggle);
    expect(document.documentElement).toHaveClass("dark");
    expect(localStorage.getItem("resonant.theme")).toBe("dark");
    await user.click(screen.getByRole("button", { name: "Switch to light mode" }));
    expect(document.documentElement).not.toHaveClass("dark");
    expect(localStorage.getItem("resonant.theme")).toBeNull();
  });
});

const BRAIN = {
  state: "up" as const,
  turns: 3,
  tools: [
    {
      at: "2026-10-16T08:59:50Z",
      tool: "Bash",
      decision: "gate",
      command: "git push origin feat",
      cwd: "/Users/x/code/r",
      status: "ok" as const,
      write: true,
    },
    {
      at: "2026-10-16T08:59:40Z",
      tool: "Bash",
      decision: "allowed",
      command: "git rev-list --count HEAD",
      cwd: "/Users/x/code/r",
      status: "ok" as const,
      write: false,
    },
  ],
};
const ACTIVITY = { calls_today: 4, writes_today: 1 };

describe("Dashboard, the Claude brain's tool calls", () => {
  it("lists each call on the local tier", async () => {
    mount(clone(local, { claude_brain: BRAIN, brain_activity: ACTIVITY }));
    expect(await screen.findByText("git push origin feat")).toBeInTheDocument();
    expect(screen.getByText("git rev-list --count HEAD")).toBeInTheDocument();
    expect(screen.getByText("gate")).toBeInTheDocument();
    expect(screen.getByTestId("brain-activity")).toHaveTextContent("4 tool calls today, 1 change");
  });

  it("shows only the counts on the remote tier, even if calls were sent", async () => {
    mount(clone(local, { tier: "remote", claude_brain: BRAIN, brain_activity: ACTIVITY }));
    expect(await screen.findByTestId("brain-activity")).toHaveTextContent("4 tool calls today");
    expect(screen.getByText("Its tool calls are shown on the local dashboard only.")).toBeInTheDocument();
    expect(screen.queryByText("git push origin feat")).not.toBeInTheDocument();
  });

  it("shows the counts from a remote snapshot", async () => {
    mount(clone(remote, { brain_activity: ACTIVITY }));
    expect(await screen.findByTestId("brain-activity")).toHaveTextContent("1 change");
  });
});

describe("Dashboard, remote tier", () => {
  it("hides what the tier doesn't carry", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount(remote);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(screen.queryByRole("heading", { name: "Rules and repos" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Component health/ }));
    expect(screen.queryByText("self-test timed out after 90s")).not.toBeInTheDocument();
    expect(screen.getByText("imessage")).toBeInTheDocument();
  });

  it("shows counts but not texts for runs, approvals, questions and pulse", async () => {
    mount(remote);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(screen.getAllByText("Run details are shown on the local dashboard only.")).toHaveLength(2);
    expect(screen.getByText("Question texts are shown on the local dashboard only.")).toBeInTheDocument();
    expect(screen.getByText("Approval texts are shown on the local dashboard only.")).toBeInTheDocument();
    expect(screen.getByText("Pulse titles are shown on the local dashboard only.")).toBeInTheDocument();
    expect(screen.getAllByText("running").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Add a dashboard job/)).not.toBeInTheDocument();
  });

  it("shows the pulse's untold count and a muted reporter (PER-263)", async () => {
    mount(remote);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(screen.getByTestId("pulse-untold")).toHaveTextContent("2 untold");
    expect(screen.getByTestId("pulse-muted")).toHaveTextContent("weather muted, ends in 3 h");
    expect(screen.getAllByText("muted")).toHaveLength(1); // the weather extension's badge
  });

  it("shows detected side effects as counts (PER-263)", async () => {
    mount(remote);
    await screen.findByRole("heading", { name: "Something is degraded" });
    const effects = screen.getByTestId("detected-effects");
    expect(effects).toHaveTextContent("push1");
    expect(effects).toHaveTextContent("merge1");
    expect(effects).toHaveTextContent("1 high-impact");
  });

  it("works with a snapshot from before PER-263", async () => {
    const old = clone(remote);
    delete old.pulse.untold;
    delete old.runs.detected;
    for (const e of old.extensions) delete e.muted_until;
    mount(old);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(screen.queryByTestId("pulse-untold")).not.toBeInTheDocument();
    expect(screen.queryByTestId("pulse-muted")).not.toBeInTheDocument();
    expect(screen.queryByTestId("detected-effects")).not.toBeInTheDocument();
  });

  it("shows no muted badge once the mute has ended", async () => {
    const ended = clone(remote);
    for (const e of ended.extensions) if (e.muted_until) e.muted_until = "2026-10-16T08:00:00+00:00";
    mount(ended);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(screen.queryByTestId("pulse-muted")).not.toBeInTheDocument();
    expect(screen.queryByText("muted")).not.toBeInTheDocument();
  });
});

describe("Dashboard, empty and failure states", () => {
  it("renders a fresh install calmly", async () => {
    mount(empty);
    expect(await screen.findByRole("heading", { name: "Some checks have no result yet" })).toBeInTheDocument();
    expect(screen.getByText("Kill switch is on")).toBeInTheDocument();
    expect(screen.getByText("autonomy off")).toBeInTheDocument();
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getByText("dry run on")).toBeInTheDocument();
    expect(screen.getByText("The monitor hasn't run yet.")).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument(); // last tick
    expect(screen.getByText("Missing: claude, gh auth")).toBeInTheDocument();
  });

  it("shows empty lists in each section", async () => {
    mount(empty);
    expect(await screen.findByText("No worker is waiting on you.")).toBeInTheDocument();
    expect(screen.getByText("Nothing waiting for a yes.")).toBeInTheDocument();
    expect(screen.getByText("No runs in flight.")).toBeInTheDocument();
    expect(screen.getByText("All quiet.")).toBeInTheDocument();
    expect(screen.getByText("No extensions installed.")).toBeInTheDocument();
    expect(screen.getByText(/No rules yet/)).toBeInTheDocument();
  });

  it("shows a stale banner when taken_at is old", async () => {
    mount(clone(remote, { taken_at: "2026-10-16T08:50:00+00:00" }));
    const banner = await screen.findByTestId("stale-banner");
    expect(banner).toHaveTextContent("No fresh data for 10 min");
    expect(banner).toHaveTextContent("stopped sending snapshots");
    expect(screen.getByTestId("last-seen")).toHaveTextContent("seen 10 min ago");
  });

  it("says 'last known' in the status card while stale", async () => {
    mount(clone(remote, { taken_at: "2026-10-16T08:50:00+00:00" }));
    expect(await screen.findByRole("heading", { name: "Last known: something is degraded" })).toBeInTheDocument();
  });

  it("flags a stuck loop on the local tier", async () => {
    mount(clone(local, { last_tick: "2026-10-16T08:50:00+00:00" }));
    expect(await screen.findByTestId("stale-banner")).toHaveTextContent("The loop hasn't ticked for 10 min");
  });

  it("never shows local-only texts on the remote tier, even if sent", async () => {
    mount(clone(local, { tier: "remote" }));
    expect((await screen.findAllByText("Run details are shown on the local dashboard only.")).length).toBe(2);
    expect(screen.queryByText(/Add a dashboard job/)).not.toBeInTheDocument();
    expect(screen.queryByText(/drops Python 3.11/)).not.toBeInTheDocument();
    expect(screen.queryByText("merge_pr github: example/resonant#81")).not.toBeInTheDocument();
    expect(screen.queryByText("Rain after 4pm")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Rules and repos" })).not.toBeInTheDocument();
  });

  it("contains a render error to its section", async () => {
    const bad = clone(local);
    (bad.extensions[0]!.jobs as unknown) = null;
    mount(bad);
    expect(await screen.findByText("This section couldn't be shown")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Pulse" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Extensions" })).not.toBeInTheDocument();
  });

  it("goes stale while the page sits open", async () => {
    mount(remote);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(screen.queryByTestId("stale-banner")).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(4 * 60_000));
    // The loader keeps returning the same snapshot, so it ages past the threshold.
    expect(screen.getByTestId("stale-banner")).toBeInTheDocument();
  });

  it("keeps the last snapshot when a poll fails", async () => {
    let fail = false;
    mount(async () => {
      if (fail) throw new Error("Resonant answered HTTP 502");
      return local;
    });
    await screen.findByRole("heading", { name: "Something is degraded" });
    fail = true;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.getByTestId("error-banner")).toHaveTextContent("HTTP 502");
    expect(screen.getByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
  });

  it("says so when no snapshot exists yet", async () => {
    mount(async () => null);
    expect(await screen.findByText("No snapshot has been sent yet.")).toBeInTheDocument();
  });
});

describe("polling", () => {
  it("fetches every 10 seconds", async () => {
    const load = mount(local);
    await screen.findByRole("heading", { name: "Something is degraded" });
    expect(load).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(load).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("pauses while the tab is hidden and catches up on return", async () => {
    const load = mount(local);
    await screen.findByRole("heading", { name: "Something is degraded" });
    const vis = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(() => vi.advanceTimersByTimeAsync(10_000)); // the pending tick runs once
    const calls = load.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(load).toHaveBeenCalledTimes(calls);
    vis.mockReturnValue("visible");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(load).toHaveBeenCalledTimes(calls + 1);
    vis.mockRestore();
  });

  it("shows new data on the next poll", async () => {
    let snap = local;
    mount(async () => snap);
    await screen.findByText("autonomy on");
    snap = clone(local, { autonomy: "off", taken_at: "2026-10-16T09:00:10+00:00" });
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.getByText("autonomy off")).toBeInTheDocument();
  });
});
