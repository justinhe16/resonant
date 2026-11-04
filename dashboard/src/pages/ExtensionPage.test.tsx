import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "@/Dashboard";
import { clone, FIXTURE_NOW, local, remote } from "@/test/fixtures";
import type { Snapshot } from "@/types";

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
const mount = (snap: Snapshot) => render(<Dashboard load={async () => snap} />);

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(FIXTURE_NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("extension page", () => {
  it("is what the #/ext route shows, instead of the tabs and sections, and not a dialog", async () => {
    window.location.hash = "#/ext/github";
    mount(local);
    const page = await screen.findByTestId("extension-page");
    expect(within(page).getByRole("heading", { level: 1, name: "github" })).toBeInTheDocument();
    expect(within(page).getAllByTestId("panel-card")).toHaveLength(2);
    expect(within(page).getByText("GitHub issues, PRs and CI")).toBeInTheDocument();
    expect(screen.queryByRole("tablist", { name: "Dashboard view" })).toBeNull();
    expect(screen.queryByTestId("section-extensions")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(within(page).getByRole("button", { name: "About panels" })).toBeInTheDocument();
  });

  it("goes back to the overview", async () => {
    const u = user();
    window.location.hash = "#/ext/github";
    mount(local);
    await u.click(await screen.findByRole("button", { name: "Back" }));
    expect(window.location.hash).toBe("");
    expect(await screen.findByTestId("section-extensions")).toBeInTheDocument();
    expect(screen.queryByTestId("extension-page")).toBeNull();
  });

  it("opens from a panel glance on the overview", async () => {
    const u = user();
    mount(local);
    const glances = await screen.findAllByTestId("panel-glance");
    expect(glances).toHaveLength(2);
    await u.click(glances[0]!);
    expect(window.location.hash).toBe("#/ext/github");
    expect(await screen.findByTestId("extension-page")).toBeInTheDocument();
  });

  it("has no Open locally link on the remote fixture, and one on the local", async () => {
    window.location.hash = "#/ext/github";
    const { unmount } = mount(remote);
    await screen.findByTestId("extension-page");
    expect(screen.queryByRole("link", { name: "Open locally" })).toBeNull();
    unmount();
    mount(local);
    await screen.findByTestId("extension-page");
    expect(screen.getByRole("link", { name: "Open locally" })).toBeInTheDocument();
  });

  it("scrolls to the panel in the route", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    window.location.hash = "#/ext/github/ci";
    mount(local);
    await screen.findByTestId("extension-page");
    expect(scroll).toHaveBeenCalled();
    expect(scroll.mock.contexts[0]).toBe(document.getElementById("panel-ci"));
  });

  it("says so for an unknown extension and for one with no panels", async () => {
    window.location.hash = "#/ext/nope";
    const { unmount } = mount(local);
    expect(await screen.findByTestId("ext-unknown")).toHaveTextContent("no extension called nope");
    unmount();
    window.location.hash = "#/ext/weather";
    mount(local);
    expect(await screen.findByTestId("ext-no-panels")).toBeInTheDocument();
  });

  it("draws a daemon from before panels without any panel UI", async () => {
    const old = clone(local) as unknown as { extensions: Record<string, unknown>[] };
    for (const e of old.extensions) delete e.panels;
    const { assertSnapshot } = await import("@/lib/snapshot");
    mount(assertSnapshot(old));
    await screen.findByTestId("section-extensions");
    expect(screen.queryByTestId("panel-glance")).toBeNull();
  });
});

describe("the overview", () => {
  it("shows at most two glances per extension, under its row", async () => {
    const snap = clone(local);
    const p = snap.extensions[0]!.panels;
    snap.extensions[0]!.panels = [p[0]!, p[1]!, { ...p[0]!, id: "third", title: "Third" }];
    mount(snap);
    const row = (await screen.findAllByTestId("extension-row"))[0]!;
    expect(within(row).getAllByTestId("panel-glance")).toHaveLength(2);
    expect(within(row).queryByText("Third")).toBeNull();
  });

  it("opens the diagram's panel node on the extension page", async () => {
    const u = user();
    window.localStorage.setItem("resonant.tab", "diagram");
    window.location.hash = "#/ext/github/prs";
    mount(local);
    expect(await screen.findByTestId("extension-page")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("tab", { name: "Diagram", selected: true })).toBeInTheDocument();
  });
});
