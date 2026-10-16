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

describe("Dashboard, local tier", () => {
  it("shows the overview at a glance", async () => {
    mount(local);
    expect(await screen.findByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
    expect(screen.getByText("imessage degraded · ext:github unknown")).toBeInTheDocument();
    expect(screen.getByText("autonomy on")).toBeInTheDocument();
    expect(screen.getByText("jobs running")).toBeInTheDocument();
    expect(screen.getByText("dry run off")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Slots in use" })).toHaveAttribute("aria-valuenow", "2");
    expect(screen.getByText("self-test timed out after 90s")).toBeInTheDocument();
    expect(screen.getByTestId("last-seen")).toHaveTextContent("seen 20s ago");
    expect(screen.getByText("22s ago")).toBeInTheDocument(); // last tick
    expect(screen.queryByTestId("stale-banner")).not.toBeInTheDocument();
  });

  it("navigates to every local page", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount(local);
    const nav = await screen.findByRole("navigation", { name: "Sections" });

    await user.click(within(nav).getByRole("link", { name: /Workers/ }));
    expect(await screen.findByText(/Add a dashboard job to CI/)).toBeInTheDocument();
    expect(screen.getByText(/drops Python 3.11/)).toBeInTheDocument();
    expect(screen.getByText("(a) bump runtime")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "PR #81" })).toHaveAttribute(
      "href",
      "https://github.com/example/resonant/pull/81",
    );
    expect(screen.getByText("waiting (question)")).toBeInTheDocument();

    await user.click(within(nav).getByRole("link", { name: /Approvals/ }));
    expect(await screen.findByText("merge_pr github: example/resonant#81")).toBeInTheDocument();
    expect(screen.getByText("L1")).toBeInTheDocument();
    expect(screen.getByText("expires in 50 min")).toBeInTheDocument();

    await user.click(within(nav).getByRole("link", { name: /Pulse/ }));
    await screen.findByText("Rain after 4pm");
    const pulse = within(screen.getByRole("main")).getAllByRole("listitem");
    expect(pulse[0]).toHaveTextContent("R11 asked a question"); // urgent first

    await user.click(within(nav).getByRole("link", { name: /Extensions/ }));
    expect(await screen.findByText("2 fails in a row")).toBeInTheDocument();
    expect(screen.getByText("No scheduled jobs.")).toBeInTheDocument();

    await user.click(within(nav).getByRole("link", { name: /Rules/ }));
    expect(await screen.findByText("ask merge on trading")).toBeInTheDocument();
    expect(screen.getByText("unprotected")).toBeInTheDocument();
  });

  it("shows open counts on the tabs", async () => {
    mount(local);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    expect(within(nav).getByRole("link", { name: "Approvals, 1 open" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Pulse, 3 open" })).toBeInTheDocument();
  });

  it("refuses non-https PR links", async () => {
    const snap = clone(local);
    snap.runs.items![0]!.pr_url = "javascript:alert(1)";
    snap.runs.items![0]!.pr_number = 99;
    window.location.hash = "/workers";
    mount(snap);
    await screen.findByText(/Add a dashboard job/);
    expect(screen.queryByRole("link", { name: "PR #99" })).not.toBeInTheDocument();
  });
});

describe("Dashboard, remote tier", () => {
  it("hides what the tier doesn't carry", async () => {
    mount(remote);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    expect(within(nav).queryByRole("link", { name: /Rules/ })).not.toBeInTheDocument();
    expect(screen.queryByText("self-test timed out after 90s")).not.toBeInTheDocument();
    expect(screen.getByText("imessage")).toBeInTheDocument();
  });

  it("shows counts but not texts on Workers, Approvals and Pulse", async () => {
    window.location.hash = "/workers";
    mount(remote);
    expect(await screen.findByText("Run details are shown on the local dashboard only.")).toBeInTheDocument();
    expect(screen.getByText("Question texts are shown on the local dashboard only.")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.queryByText(/Add a dashboard job/)).not.toBeInTheDocument();

    act(() => {
      window.location.hash = "/approvals";
    });
    expect(await screen.findByText("Approval texts are shown on the local dashboard only.")).toBeInTheDocument();
  });

  it("falls back to the overview for a local-only route", async () => {
    window.location.hash = "/rules";
    mount(remote);
    expect(await screen.findByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
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

  it("shows empty lists on each page", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount(empty);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    await user.click(within(nav).getByRole("link", { name: /Workers/ }));
    expect(await screen.findByText("No worker is waiting on you.")).toBeInTheDocument();
    await user.click(within(nav).getByRole("link", { name: /Approvals/ }));
    expect(await screen.findByText("Nothing waiting for a yes.")).toBeInTheDocument();
    await user.click(within(nav).getByRole("link", { name: /Pulse/ }));
    expect(await screen.findByText("All quiet.")).toBeInTheDocument();
    await user.click(within(nav).getByRole("link", { name: /Extensions/ }));
    expect(await screen.findByText("No extensions installed.")).toBeInTheDocument();
    await user.click(within(nav).getByRole("link", { name: /Rules/ }));
    expect(await screen.findByText(/No rules yet/)).toBeInTheDocument();
  });

  it("shows a stale banner when taken_at is old", async () => {
    mount(clone(remote, { taken_at: "2026-10-16T08:50:00+00:00" }));
    const banner = await screen.findByTestId("stale-banner");
    expect(banner).toHaveTextContent("No fresh data for 10 min");
    expect(banner).toHaveTextContent("stopped sending snapshots");
    expect(screen.getByTestId("last-seen")).toHaveTextContent("seen 10 min ago");
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

  it("shows new data on the next poll", async () => {
    let snap = local;
    mount(async () => snap);
    await screen.findByText("autonomy on");
    snap = clone(local, { autonomy: "off", taken_at: "2026-10-16T09:00:10+00:00" });
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(screen.getByText("autonomy off")).toBeInTheDocument();
  });
});
