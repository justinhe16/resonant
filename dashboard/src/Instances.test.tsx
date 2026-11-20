import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import { SupabaseGate } from "@/SupabaseGate";
import { INSTANCE_KEY } from "@/lib/instances";
import type { RemoteClient } from "@/lib/supabase";
import { clone, cori, FIXTURE_NOW, local, remote } from "@/test/fixtures";
import type { Snapshot } from "@/types";

type Route = () => Response;
const json =
  (body: unknown, status = 200): Route =>
  () =>
    new Response(JSON.stringify(body), { status });

function stubFetch(routes: Record<string, Route>) {
  const f = vi.fn(async (input: RequestInfo | URL) => {
    const hit = routes[String(input)];
    return hit ? hit() : new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", f);
  return f;
}

const BOTH = {
  instances: [
    { name: "resonant", display_name: "Resonant", self: true },
    { name: "cori", display_name: "Cori", self: false },
  ],
};
/** React's generated ids (`radix-_r_3_`) differ between two mounts; nothing else may. */
const withoutIds = (html: string) => html.replace(/_r_[0-9a-z]+_/g, "_r_");
const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
const ready = () => screen.findByRole("heading", { name: "Something is degraded" });
const switcher = () => screen.findByRole("group", { name: "Instance" });

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(FIXTURE_NOW);
  window.sessionStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe("instances on the local page", () => {
  it("without other instances the page is what it was, with no switcher", async () => {
    // An older daemon (no /api/instances) and one that lists only itself render the same page.
    stubFetch({ "./config.json": json({ source: "local" }), "/api/dashboard": json(local) });
    const before = render(<App />);
    await ready();
    const html = withoutIds(before.container.innerHTML);
    before.unmount();

    const f = stubFetch({
      "./config.json": json({ source: "local" }),
      "/api/dashboard": json(local),
      "/api/instances": json({ instances: [BOTH.instances[0]] }),
    });
    const after = render(<App />);
    await ready();
    await waitFor(() => expect(f).toHaveBeenCalledWith("/api/instances", expect.anything()));
    expect(withoutIds(after.container.innerHTML)).toBe(html);
    expect(screen.queryByRole("group", { name: "Instance" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("section-registered-jobs")).not.toBeInTheDocument();
    expect(screen.queryByTestId("section-slack")).not.toBeInTheDocument();
  });

  it("switches to Cori through the proxy and remembers the choice for the session", async () => {
    const u = user();
    const f = stubFetch({
      "./config.json": json({ source: "local" }),
      "/api/dashboard": json(local),
      "/api/instances": json(BOTH),
      "/api/instances/cori/dashboard": json(cori),
    });
    render(<App />);
    await ready();
    const group = await switcher();
    expect(within(group).getByRole("button", { name: "Resonant" })).toHaveAttribute("aria-pressed", "true");
    await u.click(within(group).getByRole("button", { name: "Cori" }));

    expect(await screen.findByTestId("section-registered-jobs")).toBeInTheDocument();
    expect(f).toHaveBeenCalledWith("/api/instances/cori/dashboard", expect.objectContaining({ cache: "no-store" }));
    expect(within(await switcher()).getByRole("button", { name: "Cori" })).toHaveAttribute("aria-pressed", "true");
    expect(window.sessionStorage.getItem(INSTANCE_KEY)).toBe("cori");

    const jobs = screen.getAllByTestId("registered-job-row");
    expect(jobs.map((r) => within(r).getByText(/^J\d$/).textContent)).toEqual(["J1", "J2"]);
    expect(within(jobs[0]!).getByText("Flaky CI sweep")).toBeInTheDocument();
    expect(within(jobs[1]!).getByText("paused")).toBeInTheDocument();
    expect(within(jobs[1]!).getByText("outside")).toBeInTheDocument(); // a member's words
    const slack = screen.getByTestId("slack-row");
    expect(within(slack).getByText("connected")).toBeInTheDocument();
    expect(within(slack).getByText("3 followed threads")).toBeInTheDocument();

    // The job's last run opens with who asked for it.
    await u.click(within(jobs[0]!).getByRole("link"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("slack:U07TEAM")).toBeInTheDocument();
  });

  it("opens on the remembered instance and switches back with the keyboard", async () => {
    window.sessionStorage.setItem(INSTANCE_KEY, "cori");
    const u = user();
    stubFetch({
      "./config.json": json({ source: "local" }),
      "/api/dashboard": json(local),
      "/api/instances": json(BOTH),
      "/api/instances/cori/dashboard": json(cori),
    });
    render(<App />);
    expect(await screen.findByTestId("section-slack")).toBeInTheDocument();
    const resonant = within(await switcher()).getByRole("button", { name: "Resonant" });
    resonant.focus();
    await u.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByTestId("section-slack")).not.toBeInTheDocument());
    expect(await ready()).toBeInTheDocument();
    expect(window.sessionStorage.getItem(INSTANCE_KEY)).toBe("resonant");
  });

  it("says since when Cori isn't answering and leaves personal Resonant alone", async () => {
    const u = user();
    stubFetch({
      "./config.json": json({ source: "local" }),
      "/api/dashboard": json(local),
      "/api/instances": json(BOTH),
      "/api/instances/cori/dashboard": json({ detail: "cori isn't answering", since: "2026-10-16T08:40:00Z" }, 502),
    });
    render(<App />);
    await ready();
    await u.click(within(await switcher()).getByRole("button", { name: "Cori" }));
    const banner = await screen.findByTestId("error-banner");
    expect(within(banner).getByText("Can't reach Cori")).toBeInTheDocument();
    expect(banner).toHaveTextContent(/Cori isn't answering \(since .+\)/);
    expect(banner.textContent).toContain(new Date("2026-10-16T08:40:00Z").toLocaleString());

    await u.click(within(await switcher()).getByRole("button", { name: "Resonant" }));
    expect(await ready()).toBeInTheDocument();
    expect(screen.queryByTestId("error-banner")).not.toBeInTheDocument();
  });
});

describe("instances on the remote page", () => {
  function twoRows(rows: Record<string, Snapshot>) {
    const client: RemoteClient = {
      stage: vi.fn(async () => ({ kind: "ready" as const })),
      signIn: vi.fn(),
      verify: vi.fn(),
      signOut: vi.fn(async () => {}),
      instances: vi.fn(async () =>
        Object.keys(rows).map((name) => ({ name, display_name: name.charAt(0).toUpperCase() + name.slice(1) })),
      ),
      latest: vi.fn(async (instance?: string) => (instance ? (rows[instance] ?? null) : null)),
      panelDay: vi.fn(async () => null),
      onAuthChange: vi.fn(() => () => {}),
    };
    return client;
  }

  it("reads one instance's row at a time and switches between them", async () => {
    const u = user();
    const coriRemote = clone(remote, {
      instance: { name: "cori", display_name: "Cori" },
      jobs: cori.jobs,
      channels: cori.channels,
    });
    const client = twoRows({ resonant: remote, cori: coriRemote });
    render(<SupabaseGate client={client} />);
    expect(await ready()).toBeInTheDocument();
    expect(client.latest).toHaveBeenCalledWith("resonant");
    expect(client.latest).not.toHaveBeenCalledWith(undefined);
    await u.click(within(await switcher()).getByRole("button", { name: "Cori" }));
    expect(await screen.findByTestId("section-registered-jobs")).toBeInTheDocument();
    expect(screen.getByTestId("section-slack")).toBeInTheDocument();
    expect(client.latest).toHaveBeenCalledWith("cori");
  });

  it("a stale Cori row shows its own staleness banner, not personal Resonant's", async () => {
    const u = user();
    const old = clone(remote, {
      instance: { name: "cori", display_name: "Cori" },
      taken_at: "2026-10-16T08:30:00Z",
    });
    render(<SupabaseGate client={twoRows({ resonant: remote, cori: old })} />);
    expect(await ready()).toBeInTheDocument();
    expect(screen.queryByTestId("stale-banner")).not.toBeInTheDocument();
    await u.click(within(await switcher()).getByRole("button", { name: "Cori" }));
    expect(await screen.findByTestId("stale-banner")).toBeInTheDocument();
    await u.click(within(await switcher()).getByRole("button", { name: "Resonant" }));
    await waitFor(() => expect(screen.queryByTestId("stale-banner")).not.toBeInTheDocument());
  });

  it("one row means no switcher", async () => {
    const client = twoRows({ resonant: remote });
    render(<SupabaseGate client={client} />);
    expect(await ready()).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Instance" })).not.toBeInTheDocument();
    expect(client.latest).toHaveBeenCalledWith("resonant");
  });
});
