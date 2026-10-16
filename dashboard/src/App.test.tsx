import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import type { RemoteClient } from "@/lib/supabase";
import { local } from "@/test/fixtures";

function stubFetch(routes: Record<string, () => Response>) {
  const f = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const hit = routes[url];
    return hit ? hit() : new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", f);
  return f;
}

const json = (body: unknown) => () => new Response(JSON.stringify(body), { status: 200 });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("reads /api/dashboard in local mode", async () => {
    const f = stubFetch({ "./config.json": json({ source: "local" }), "/api/dashboard": json(local) });
    const makeRemote = vi.fn();
    render(<App makeRemote={makeRemote} />);
    expect(await screen.findByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
    expect(f).toHaveBeenCalledWith("/api/dashboard", expect.objectContaining({ cache: "no-store" }));
    expect(makeRemote).not.toHaveBeenCalled();
  });

  it("uses Supabase behind a login in supabase mode", async () => {
    const f = stubFetch({
      "./config.json": json({ source: "supabase", url: "https://p.supabase.co", anon_key: "anon" }),
    });
    const client: RemoteClient = {
      stage: vi.fn(async () => ({ kind: "signed_out" as const })),
      signIn: vi.fn(),
      verify: vi.fn(),
      enroll: vi.fn(),
      signOut: vi.fn(),
      latest: vi.fn(),
      onAuthChange: vi.fn(() => () => {}),
    };
    const makeRemote = vi.fn(async () => client);
    render(<App makeRemote={makeRemote} />);
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(makeRemote).toHaveBeenCalledWith("https://p.supabase.co", "anon");
    expect(f).not.toHaveBeenCalledWith("/api/dashboard", expect.anything());
  });

  it("explains a broken config", async () => {
    stubFetch({ "./config.json": json({ source: "ftp" }) });
    render(<App />);
    expect(await screen.findByText("The dashboard is misconfigured")).toBeInTheDocument();
  });

  it("explains a daemon without the dashboard API", async () => {
    stubFetch({ "./config.json": json({ source: "local" }) });
    render(<App />);
    expect(await screen.findByTestId("error-banner")).toHaveTextContent("HTTP 404");
  });
});
