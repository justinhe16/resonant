import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "@/Dashboard";
import { assertSnapshot } from "@/lib/snapshot";
import { clone, empty, FIXTURE_NOW, local, remote, remoteV1 } from "@/test/fixtures";
import type { RunItem, Snapshot } from "@/types";

function mount(snap: Snapshot | (() => Promise<Snapshot | null>)) {
  const load = vi.fn(typeof snap === "function" ? snap : async () => snap);
  const view = render(<Dashboard load={load} />);
  return Object.assign(load, { view });
}

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(FIXTURE_NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const ready = () => screen.findByRole("heading", { name: "Something is degraded" });
const headings = (testId: string) =>
  within(screen.getByTestId(testId))
    .getAllByRole("heading", { level: 2 })
    .map((h) => h.textContent);
const section = (id: string) => screen.getByTestId(`section-${id}`);
/** A section's open/close button (the (i) button beside it has its own name). */
const trigger = (id: string) =>
  within(within(section(id)).getByRole("heading", { level: 2 })).getByRole("button");
const ASK12 = "Add a dashboard job to CI that rebuilds the UI and fails if dist differs";
/** PER-279: Extensions right under the approvals, then Pulse; the rest keep their order. */
const HEADS = [
  "Waiting on you (3)",
  "Extensions (3 of 3)",
  "Pulse (3 of 3)",
  "Recent runs (3 of 8)",
  "Tasks (3 of 46)",
  "Claude tool calls (2 of 4)",
  "Jobs (3 of 3)",
  "Rules and repos (6)",
];
/** The one-line row (PER-279) that holds `text`. */
const rowOf = (text: string | RegExp, el: HTMLElement = document.body) => within(el).getByText(text).closest("li")!;
/** Opens the detail dialog of the row that holds `text` (the row's one link). */
async function openRow(u: ReturnType<typeof user>, text: string | RegExp, el?: HTMLElement) {
  await u.click(within(rowOf(text, el)).getAllByRole("link")[0]!);
  return screen.findByRole("dialog");
}

describe("Dashboard, local tier", () => {
  it("shows the system status at a glance", async () => {
    mount(local);
    expect(await ready()).toBeInTheDocument();
    expect(screen.getByText("imessage degraded · ext:github unknown")).toBeInTheDocument();
    expect(screen.getByText("autonomy on")).toBeInTheDocument();
    expect(screen.getByText("jobs running")).toBeInTheDocument();
    expect(screen.getByText("dry run off")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Slots in use" })).toHaveAttribute("aria-valuetext", "2 of 5");
    expect(screen.getByTestId("last-seen")).toHaveTextContent("seen 20s ago");
    expect(screen.getByText("22s ago")).toBeInTheDocument(); // last tick
    expect(screen.queryByTestId("stale-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("legacy-banner")).not.toBeInTheDocument();
  });

  it("keeps per-component health in a collapsed list", async () => {
    const u = user();
    mount(local);
    const t = await screen.findByRole("button", { name: /Component health/ });
    expect(t).toHaveTextContent("3 of 5 ok");
    expect(t).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("self-test timed out after 90s")).not.toBeInTheDocument();
    await u.click(t);
    expect(t).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("self-test timed out after 90s")).toBeInTheDocument();
    expect(screen.getByText("loop")).toBeInTheDocument();
  });

  it("lays out two columns in order, each section with its count", async () => {
    mount(local);
    await ready();
    expect(headings("column-left")).toEqual(["Slots", "Active runs (2 of 2)", "Active tasks"]);
    expect(headings("column-right")).toEqual(HEADS);
    // The status card's headline is the page's one h1.
    expect(within(screen.getByTestId("column-left")).getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("shows an approval batch as one line, with its changes and code in the dialog", async () => {
    const u = user();
    mount(local);
    await ready();
    const row = rowOf("3 changes");
    expect(within(row).queryByRole("list", { name: "Changes" })).not.toBeInTheDocument();
    expect(within(row).queryByText(/D7/)).not.toBeInTheDocument();
    expect(within(row).getByText("L2")).toBeInTheDocument(); // the batch's strictest
    expect(within(row).getByText("expires in 55m")).toBeInTheDocument();
    expect(within(row).getByText("5m")).toBeInTheDocument(); // asked
    const dialog = await openRow(u, "3 changes");
    const changes = within(dialog).getByRole("list", { name: "Changes" });
    expect(within(changes).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      '1.linear: save issue title="Retry the relay push"outsideL3',
      '2.linear: save comment issue="PER-263"outsideL3',
      "3.restart resonantoutsideL2",
    ]);
    expect(within(dialog).getByText("D7")).toBeInTheDocument(); // the code, local tier only
    expect(within(dialog).getByText("ap_d7b")).toBeInTheDocument(); // one answer for all
  });

  it("lists recently decided approvals", async () => {
    const u = user();
    mount(local);
    await ready();
    const row = rowOf("push resonant r8-hourly");
    expect(within(row).getByText("approved")).toBeInTheDocument();
    expect(within(row).getByText("14h")).toBeInTheDocument();
    const dialog = await openRow(u, "push resonant r8-hourly");
    expect(within(dialog).getByText("owner")).toBeInTheDocument(); // decided by
  });

  it("shows run, task, question, pulse and job rows from the snapshot", async () => {
    const u = user();
    mount(local);
    await ready();
    const left = screen.getByTestId("column-left");
    expect(within(left).getByText(ASK12)).toBeInTheDocument();
    expect(within(rowOf("R11", left)).getByText("waiting").closest("[title]")).toHaveAttribute(
      "title",
      "waiting: question",
    );
    expect(within(left).queryByText("R10")).not.toBeInTheDocument();
    expect(within(section("runs")).getByText("R10")).toBeInTheDocument(); // ready
    expect(screen.queryByRole("link", { name: "PR #81" })).not.toBeInTheDocument(); // in the dialog
    expect(within(rowOf(/The new client drops Python/)).getByText("R11")).toBeInTheDocument();
    expect(screen.queryByText("(a) bump runtime")).not.toBeInTheDocument(); // in the dialog
    expect(screen.getByText("merge_pr github: example/resonant#81")).toBeInTheDocument();
    expect(screen.getByText("L1")).toBeInTheDocument();
    expect(screen.getByText("expires in 50m")).toBeInTheDocument();
    expect(screen.getByText("3 open")).toBeInTheDocument();
    // Pulse: urgent first.
    const pulse = screen.getByText("Rain after 4pm").closest("ul")!;
    expect(within(pulse).getAllByRole("listitem")[0]).toHaveTextContent("R11 asked a question");
    // Extensions: one line each; a failing job shows in the hint, the jobs are in the dialog.
    expect(within(rowOf("github", section("extensions"))).getByText("2 jobs")).toBeInTheDocument();
    expect(within(rowOf("weather", section("extensions"))).getByText(/^muted until /)).toBeInTheDocument();
    expect(within(rowOf("notes-sync", section("extensions"))).getByText("disabled")).toBeInTheDocument();
    expect(screen.queryByText("No scheduled jobs.")).not.toBeInTheDocument();
    expect(within(section("jobs")).getByText("briefing")).toBeInTheDocument();
    await u.click(trigger("rules"));
    expect(screen.getByText("ask merge on trading")).toBeInTheDocument();
    expect(screen.getByText("no CI")).toBeInTheDocument();
  });

  it("says how many side effects were detected per run, in its dialog", async () => {
    const u = user();
    mount(local);
    await ready();
    expect(screen.queryByText(/side effects? detected/)).not.toBeInTheDocument(); // not on the rows
    const dialog = await openRow(u, ASK12);
    expect(within(dialog).getByText("2 detected in its commands")).toBeInTheDocument();
  });

  it("shows the Remote Control link only where the snapshot carries it", async () => {
    const u = user();
    mount(local);
    await ready();
    expect(screen.getAllByRole("link", { name: "Remote Control" }).map((a) => a.getAttribute("href"))).toEqual([
      "https://claude.ai/code/session_brain",
    ]);
    const dialog = await openRow(u, ASK12);
    expect(within(dialog).getByRole("link", { name: "Open session" })).toHaveAttribute(
      "href",
      "https://claude.ai/code/session_r12",
    );
  });

  it("shows each task's title with its local creation time", async () => {
    mount(local);
    await ready();
    const tasks = section("tasks");
    const row = rowOf(/^Add a dashboard job to CI · /, tasks);
    expect(row).toHaveTextContent(/Add a dashboard job to CI · \d{1,2}:\d{2}(am|pm)( \S+)?, Oct 1[56]/);
    expect(within(row).getByText("4f1a9c")).toHaveAttribute("title", "t_4f1a9c"); // the id's tail
    expect(within(row).getByText("running")).toBeInTheDocument();
    expect(within(row).getAllByRole("link")).toHaveLength(1); // the whole row opens the task
  });

  it("refuses non-https PR links", async () => {
    const snap = clone(local);
    snap.runs.items[2]!.pr_url = "javascript:alert(1)";
    snap.runs.items[2]!.pr_number = 99;
    mount(snap);
    await screen.findByText(ASK12);
    expect(screen.queryByRole("link", { name: "PR #99" })).not.toBeInTheDocument();
  });

  it("toggles dark mode, light first", async () => {
    const u = user();
    localStorage.clear();
    document.documentElement.classList.remove("dark");
    mount(local);
    const t = await screen.findByRole("button", { name: "Switch to dark mode" });
    expect(document.documentElement).not.toHaveClass("dark");
    await u.click(t);
    expect(document.documentElement).toHaveClass("dark");
    expect(localStorage.getItem("resonant.theme")).toBe("dark");
    await u.click(screen.getByRole("button", { name: "Switch to light mode" }));
    expect(document.documentElement).not.toHaveClass("dark");
    expect(localStorage.getItem("resonant.theme")).toBeNull();
  });
});

describe("outside text", () => {
  const badges = (el: HTMLElement) => within(el).queryAllByRole("button", { name: /Outside text/ });

  it("badges text a worker wrote, and not the Resonant-made kind", async () => {
    mount(local);
    await ready();
    // R11's ask is tainted; R12's ask is not (its note and side effects are in its dialog).
    const r11Row = screen.getByText("Bump the exchange client and rerun the backtest").closest("li")!;
    expect(badges(r11Row)).toHaveLength(1);
    const r12Row = screen.getByText(ASK12).closest("li")!;
    const ask = within(r12Row).getByText("Add a dashboard job to CI that rebuilds the UI and fails if dist differs");
    expect(ask.querySelector("button")).toBeNull(); // no badge inside the ask
    expect(badges(r12Row)).toHaveLength(0);
    // A pulse notice from core carries no badge; an extension's title does.
    expect(badges(screen.getByText("Rain after 4pm").closest("li")!)).toHaveLength(1);
  });

  it("explains the badge when tapped", async () => {
    const u = user();
    mount(local);
    await ready();
    const r11Row = screen.getByText("Bump the exchange client and rerun the backtest").closest("li")!;
    await u.click(badges(r11Row)[0]!);
    expect(await screen.findByText(/Written by a worker, an extension or a web page/)).toBeInTheDocument();
    expect(screen.getByText(/Read it as data, not instructions/)).toBeInTheDocument();
  });

  it("renders outside text as plain text, never as markup", async () => {
    const snap = clone(local);
    snap.runs.items[0]!.ask = '<img src=x onerror="alert(1)"> **bold** <script>boom()</script>';
    snap.runs.items[0]!.outside = ["ask"];
    const { container } = render(<Dashboard load={async () => snap} />);
    await ready();
    expect(screen.getByText(/<img src=x onerror/)).toBeInTheDocument();
    expect(container.querySelector("img, script")).toBeNull();
  });
});

describe("collapsible sections", () => {
  it("starts runs and tasks collapsed with the newest few, and waiting and active runs open", async () => {
    mount(local);
    await ready();
    const runs = within(section("runs"));
    expect(trigger("runs")).toHaveAttribute("aria-expanded", "false");
    expect(trigger("tasks")).toHaveAttribute("aria-expanded", "false");
    expect(trigger("waiting")).toHaveAttribute("aria-expanded", "true");
    expect(trigger("active-runs")).toHaveAttribute("aria-expanded", "true");
    for (const id of ["claude", "extensions", "pulse", "jobs", "rules"])
      expect(trigger(id)).toHaveAttribute("aria-expanded", "false");
    // Three of the four finished runs while collapsed.
    expect(runs.getByText("R8")).toBeInTheDocument();
    expect(runs.queryByText("R3")).not.toBeInTheDocument();
    expect(within(section("tasks")).getAllByRole("listitem")).toHaveLength(3);
  });

  it("opens to show every row and remembers it across a remount", async () => {
    const u = user();
    const first = mount(local);
    await ready();
    await u.click(trigger("runs"));
    expect(within(section("runs")).getByText("R3")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recent runs (4 of 8)" })).toBeInTheDocument();
    expect(localStorage.getItem("resonant.section.runs")).toBe("open");
    first.view.unmount();
    mount(local);
    await ready();
    expect(trigger("runs")).toHaveAttribute("aria-expanded", "true");
    await u.click(trigger("runs"));
    expect(localStorage.getItem("resonant.section.runs")).toBe("closed");
  });

  it("keeps each section's remembered state after the reorder (PER-279)", async () => {
    // Keys are per section id, not per position: moving Extensions and Pulse up keeps them.
    localStorage.setItem("resonant.section.extensions", "open");
    localStorage.setItem("resonant.section.pulse", "closed");
    localStorage.setItem("resonant.section.claude", "open");
    mount(local);
    await ready();
    const order = within(screen.getByTestId("column-right"))
      .getAllByTestId(/^section-/)
      .map((el) => el.dataset.testid);
    expect(order).toEqual([
      "section-waiting",
      "section-extensions",
      "section-pulse",
      "section-runs",
      "section-tasks",
      "section-claude",
      "section-jobs",
      "section-rules",
    ]);
    expect(trigger("extensions")).toHaveAttribute("aria-expanded", "true");
    expect(trigger("pulse")).toHaveAttribute("aria-expanded", "false");
    // The renamed section keeps its id, so its stored state still applies.
    expect(trigger("claude")).toHaveAttribute("aria-expanded", "true");
    expect(within(section("claude")).getByRole("heading", { level: 2 })).toHaveTextContent(/^Claude tool calls/);
  });

  it("lets a remembered 'closed' override an open default", async () => {
    localStorage.setItem("resonant.section.waiting", "closed");
    mount(local);
    await ready();
    expect(trigger("waiting")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("3 changes")).not.toBeInTheDocument();
  });

  it("still works when localStorage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    const u = user();
    mount(local);
    await ready();
    const t = trigger("runs");
    expect(t).toHaveAttribute("aria-expanded", "false");
    await u.click(t);
    expect(t).toHaveAttribute("aria-expanded", "true");
    expect(within(section("runs")).getByText("R3")).toBeInTheDocument();
  });

  it("pages by ten with Show more", async () => {
    const u = user();
    const snap = clone(local);
    const template = snap.runs.items.find((r) => r.code === "R9")!;
    const many: RunItem[] = Array.from({ length: 25 }, (_, i) => ({ ...template, code: `R${100 + i}`, ask: `finished ${i}` }));
    snap.runs.items = many;
    snap.runs.by_state = { failed: 25 };
    mount(snap);
    await ready();
    const runs = within(section("runs"));
    await u.click(trigger("runs"));
    expect(runs.getAllByRole("listitem")).toHaveLength(10);
    await u.click(runs.getByRole("button", { name: "Show more (15 left)" }));
    expect(runs.getAllByRole("listitem")).toHaveLength(20);
    await u.click(runs.getByRole("button", { name: "Show more (5 left)" }));
    expect(runs.getAllByRole("listitem")).toHaveLength(25);
    expect(runs.queryByRole("button", { name: /Show more/ })).not.toBeInTheDocument();
  });

  it("says when the snapshot keeps fewer rows than exist", async () => {
    const u = user();
    const snap = clone(local);
    snap.runs.by_state = { merged: 60, running: 1, waiting: 1, ready: 1, failed: 1 };
    mount(snap);
    await ready();
    await u.click(trigger("runs"));
    expect(screen.getByText("The snapshot keeps the newest 50 runs; older ones aren't shown.")).toBeInTheDocument();
  });
});

describe("explainers", () => {
  const CASES: [string, RegExp][] = [
    ["System status", /Each row is a health check/],
    ["Slots", /claude\.max_concurrent/],
    ["Active runs", /one Claude Code session working on one ask/],
    ["Active tasks", /durable to-do list/],
    ["Waiting on you", /Resonant texts a prompt with a code/],
    ["Recent runs", /GitHub App token for that one repo/],
    ["Tasks", /survives restarts|survive restarts/],
    ["Claude tool calls", /Every tool call the Claude brain made/],
    ["Extensions", /resonant\.yaml/],
    ["Jobs", /run-cleanup: 04:30/],
    ["Pulse", /core\/pulse/],
    ["Rules and repos", /standing answers for workers/],
    ["relay", /dead-man|Fly app serves this page/],
  ];

  it.each(CASES)("has an (i) button for %s that opens its text", async (name, text) => {
    const u = user();
    const snap = clone(local);
    // The relay row lives in the health list.
    snap.health.push({ name: "relay", state: "ok", since: null, reason: null, outside: [] });
    mount(snap);
    await ready();
    if (name === "relay") await u.click(screen.getByRole("button", { name: /Component health/ }));
    const buttons = screen.getAllByRole("button", { name: `About ${name}` });
    await u.click(buttons[buttons.length - 1]!);
    expect(await screen.findByText(text)).toBeInTheDocument();
  });
});

describe("detail dialog", () => {
  it("opens a run from its row, sets the hash and closes back to the page", async () => {
    const u = user();
    mount(local);
    await ready();
    await u.click(within(section("active-runs")).getByRole("link", { name: ASK12 }));
    expect(window.location.hash).toBe("#/runs/R12");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Run R12" })).toBeInTheDocument();
    expect(within(dialog).getByText("a1b2c3d4")).toBeInTheDocument();
    expect(within(dialog).getByText("$0.84")).toBeInTheDocument();
    expect(within(dialog).getByText("182,000 in, 9,400 out")).toBeInTheDocument();
    expect(within(dialog).getByRole("list", { name: "History" })).toHaveTextContent("side_effect");
    expect(within(dialog).getByRole("link", { name: "Branch" })).toHaveAttribute(
      "href",
      "https://github.com/example/resonant/tree/r12-dashboard-ci",
    );
    expect(within(dialog).getByRole("link", { name: "Commit" })).toHaveAttribute(
      "href",
      "https://github.com/example/resonant/commit/a1b2c3d4",
    );
    for (const a of within(dialog).getAllByRole("link"))
      if (a.getAttribute("target") === "_blank") expect(a).toHaveAttribute("rel", "noreferrer noopener");
    await u.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(window.location.hash).toBe("");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens a deep link on mount", async () => {
    window.location.hash = "#/runs/R10";
    mount(local);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Run R10" })).toBeInTheDocument();
    expect(within(dialog).getByText("Pinned the clock in the quiet-hours test; the flake came from reading the wall time twice.")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "PR #81" })).toHaveAttribute(
      "href",
      "https://github.com/example/resonant/pull/81",
    );
    expect(within(dialog).getByText("PER-241")).toBeInTheDocument(); // Linear id as plain text
    expect(within(dialog).queryByRole("link", { name: "PER-241" })).not.toBeInTheDocument();
    expect(within(dialog).getByText(".github/workflows/ci.yml")).toBeInTheDocument();
    expect(within(dialog).getByText("high impact")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "ap_k7q2" })).toHaveAttribute("href", "#/approvals/ap_k7q2");
  });

  it("says a run past the detail window has no detail, and has no GitHub links for scratch runs", async () => {
    window.location.hash = "#/runs/R3";
    mount(local);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByTestId("no-detail")).toHaveTextContent("newest 20 runs");
    expect(within(dialog).getByText("none (scratch folder)")).toBeInTheDocument();
    expect(within(dialog).queryByRole("link", { name: "Branch" })).not.toBeInTheDocument();
    expect(within(dialog).getByText("Not kept for this run.")).toBeInTheDocument();
  });

  it("explains a run that isn't in the snapshot", async () => {
    window.location.hash = "#/runs/R99";
    mount(local);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByTestId("not-kept")).toHaveTextContent("R99 isn't in this snapshot: it keeps the newest 50 runs.");
    expect(within(dialog).queryByText(/trimmed to fit the relay/)).not.toBeInTheDocument();
  });

  it("mentions a trimmed snapshot when explaining what's missing", async () => {
    window.location.hash = "#/runs/R99";
    mount(clone(remote, { trimmed: "lists" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/trimmed to fit the relay \(lists\)/)).toBeInTheDocument();
  });

  it("ignores hashes that aren't routes", async () => {
    window.location.hash = "#access_token=abc";
    mount(local);
    await ready();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("follows the hash when it changes", async () => {
    mount(local);
    await ready();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => {
      window.location.hash = "#/tasks/t_77c0de";
    });
    expect(await screen.findByRole("heading", { name: "Bump the exchange client" })).toBeInTheDocument();
    await act(async () => {
      window.location.hash = "#/questions/7";
    });
    expect(await screen.findByRole("heading", { name: "Question from R11" })).toBeInTheDocument();
  });

  it("opens the list of a state from its pill", async () => {
    const u = user();
    mount(local);
    await ready();
    await u.click(within(section("runs")).getByRole("link", { name: "failed: 1" }));
    expect(window.location.hash).toBe("#/runs?state=failed");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Runs: failed" })).toBeInTheDocument();
    expect(within(dialog).getByText("R9")).toBeInTheDocument();
    expect(within(dialog).queryByText("R10")).not.toBeInTheDocument();
  });

  it("opens a task state list from the task pills", async () => {
    const u = user();
    mount(local);
    await ready();
    await u.click(within(section("tasks")).getByRole("link", { name: "waiting: 1" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Tasks: waiting" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Bump the exchange client · /)).toBeInTheDocument();
    expect(within(dialog).getByText(/^github\/watch · /)).toBeInTheDocument();
  });

  it("opens the runs behind a side-effect pill", async () => {
    const u = user();
    mount(local);
    await ready();
    await u.click(within(screen.getByTestId("detected-effects")).getByRole("link", { name: "push: 1" }));
    expect(window.location.hash).toBe("#/effects/push");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Side effects: push" })).toBeInTheDocument();
    expect(within(dialog).getByText("git push origin r12-dashboard-ci")).toBeInTheDocument();
  });

  it("opens an approval, an extension, a pulse item, a job and a task from their rows", async () => {
    const u = user();
    mount(local);
    await ready();

    let dialog = await openRow(u, "merge_pr github: example/resonant#81");
    expect(within(dialog).getByRole("heading", { name: "Approval" })).toBeInTheDocument();
    expect(within(dialog).getByText("K7Q2")).toBeInTheDocument(); // the code, in the local tier
    expect(within(dialog).getByRole("link", { name: "R10" })).toHaveAttribute("href", "#/runs/R10");
    await u.click(within(dialog).getByRole("button", { name: "Close" }));

    dialog = await openRow(u, "github", section("extensions"));
    expect(window.location.hash).toBe("#/extensions/github");
    expect(within(dialog).getByRole("heading", { name: "github" })).toBeInTheDocument();
    expect(within(dialog).getByText("merge_pr")).toBeInTheDocument();
    expect(within(dialog).getByText("github_token")).toBeInTheDocument(); // a secret's name
    expect(within(dialog).getByText("digest")).toBeInTheDocument(); // its jobs live here now
    await u.click(within(dialog).getByRole("button", { name: "Close" }));

    dialog = await openRow(u, "weather", section("extensions"));
    expect(within(dialog).getByText("2 fails")).toBeInTheDocument(); // a failing job's streak
    expect(within(dialog).getByText("muted")).toBeInTheDocument();
    await u.click(within(dialog).getByRole("button", { name: "Close" }));

    dialog = await openRow(u, "R11 asked a question", section("pulse"));
    expect(window.location.hash).toBe("#/pulse/core/run%2FR11");
    expect(within(dialog).getByRole("link", { name: "R11" })).toBeInTheDocument();
    await u.click(within(dialog).getByRole("button", { name: "Close" }));

    await u.click(within(section("jobs")).getByRole("link", { name: "pulse" }));
    dialog = await screen.findByRole("dialog");
    expect(window.location.hash).toBe("#/jobs/core/pulse");
    expect(within(dialog).getByText("0 * * * *")).toBeInTheDocument();
    await u.click(within(dialog).getByRole("button", { name: "Close" }));

    dialog = await openRow(u, "4f1a9c", section("tasks"));
    expect(within(dialog).getByRole("link", { name: "R12" })).toBeInTheDocument();
  });

  it("shows MCP servers by host only", async () => {
    window.location.hash = "#/extensions/weather";
    mount(local);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("weather-mcp")).toBeInTheDocument();
    expect(within(dialog).getByText(/http · mcp\.weather\.example/)).toBeInTheDocument();
  });

  it("opens a Claude brain tool call", async () => {
    window.location.hash = `#/brain/${encodeURIComponent("2026-10-16T08:59:50Z")}`;
    mount(local);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("git push origin feat")).toBeInTheDocument();
    expect(within(dialog).getByText("/Users/x/code/r")).toBeInTheDocument();
  });

  it("shows pending and decided approvals by any member id", async () => {
    window.location.hash = "#/approvals/ap_d7c";
    mount(local);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("3 changes")).toBeInTheDocument();
    expect(within(dialog).getByText("ap_d7b")).toBeInTheDocument();
  });

  it("is a dialog with a title, on both tiers", async () => {
    for (const snap of [local, remote]) {
      window.location.hash = "#/runs/R11";
      const { unmount } = render(<Dashboard load={async () => snap} />);
      const dialog = await screen.findByRole("dialog");
      expect(dialog).toHaveAccessibleName("Run R11");
      expect(within(dialog).getByRole("button", { name: "Copy link to this view" })).toBeInTheDocument();
      unmount();
    }
  });

  it("badges outside text inside the dialog too", async () => {
    window.location.hash = "#/runs/R11";
    mount(local);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByRole("button", { name: /Outside text/ }).length).toBeGreaterThan(0);
  });
});

describe("Dashboard, remote tier", () => {
  it("has the same sections as the local tier", async () => {
    const a = mount(local);
    await ready();
    const localHeads = [headings("column-left"), headings("column-right")];
    a.view.unmount();
    mount(remote);
    await ready();
    expect([headings("column-left"), headings("column-right")]).toEqual(localHeads);
  });

  it("shows the full detail the relay carries, without the code or the Remote Control link", async () => {
    const u = user();
    mount(remote);
    await ready();
    expect(screen.getByText(ASK12)).toBeInTheDocument();
    expect(screen.getByText("merge_pr github: example/resonant#81")).toBeInTheDocument();
    expect(screen.getByText(/The new client drops Python/)).toBeInTheDocument();
    expect(screen.getByText("Rain after 4pm")).toBeInTheDocument();
    expect(screen.getByText(/^Add a dashboard job to CI · /)).toBeInTheDocument(); // a task's title
    expect(screen.queryByText(/^code /)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Remote Control" })).not.toBeInTheDocument();
    expect(screen.queryByText(/shown on the local dashboard only/)).not.toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: /Component health/ }));
    expect(screen.getByText("self-test timed out after 90s")).toBeInTheDocument();
    expect(screen.getByTestId("brain-activity")).toHaveTextContent("4 tool calls today, 1 change");
  });

  it("never shows the code or Remote Control link even if the relay sent them", async () => {
    const leaky = assertSnapshot(clone(local, { tier: "remote" }));
    mount(leaky);
    await ready();
    expect(screen.queryByText("code K7Q2")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Remote Control" })).not.toBeInTheDocument();
  });

  it("shows the pulse's untold count and a muted reporter", async () => {
    mount(remote);
    await ready();
    expect(screen.getByTestId("pulse-untold")).toHaveTextContent("2 untold");
    expect(screen.getByTestId("pulse-muted")).toHaveTextContent("weather muted, ends in 3 h");
    expect(screen.getAllByText(/^muted until /)).toHaveLength(1); // the weather extension's hint
  });

  it("shows detected side effects as counts", async () => {
    mount(remote);
    await ready();
    const effects = screen.getByTestId("detected-effects");
    expect(effects).toHaveTextContent("push1");
    expect(effects).toHaveTextContent("merge1");
    expect(effects).toHaveTextContent("1 high-impact");
  });

  it("shows no muted badge once the mute has ended", async () => {
    const ended = clone(remote);
    for (const e of ended.extensions) if (e.muted_until) e.muted_until = "2026-10-16T08:00:00+00:00";
    mount(ended);
    await ready();
    expect(screen.queryByTestId("pulse-muted")).not.toBeInTheDocument();
    expect(screen.queryByText("muted")).not.toBeInTheDocument();
  });
});

describe("Dashboard, a schema 1 snapshot from an un-updated daemon", () => {
  it("renders counts and rows with a legacy banner", async () => {
    mount(remoteV1);
    await ready();
    expect(screen.getByTestId("legacy-banner")).toHaveTextContent("Older snapshot format");
    const left = screen.getByTestId("column-left");
    expect(within(left).getByText("R12")).toBeInTheDocument();
    expect(within(left).getByText("R11")).toBeInTheDocument();
    expect(within(screen.getByTestId("column-right")).getByText("R10")).toBeInTheDocument();
    expect(screen.getByText("A change, needs its code")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "failed: 1" })).toBeInTheDocument();
    expect(screen.getByTestId("pulse-untold")).toHaveTextContent("2 untold");
    expect(screen.getByTestId("brain-activity")).toHaveTextContent("4 tool calls today, 1 change");
  });

  it("opens a run with the note that details are missing", async () => {
    window.location.hash = "#/runs/R10";
    mount(remoteV1);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByTestId("no-detail")).toBeInTheDocument();
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
    expect(screen.getByText("No finished runs yet.")).toBeInTheDocument();
    expect(screen.getByText("No tasks yet.")).toBeInTheDocument();
    expect(screen.getByText("All quiet.")).toBeInTheDocument();
    expect(screen.getByText("No extensions installed.")).toBeInTheDocument();
    expect(screen.getByText("No core jobs reported.")).toBeInTheDocument();
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

  it("contains a render error to its section", async () => {
    const bad = clone(local);
    (bad.extensions[0]!.jobs as unknown) = null;
    mount(bad);
    expect(await screen.findByText("This section couldn't be shown")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /^Pulse/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Extensions/ })).not.toBeInTheDocument();
  });

  it("goes stale while the page sits open", async () => {
    mount(remote);
    await ready();
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
    await ready();
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
