import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SupabaseGate } from "@/SupabaseGate";
import type { AuthStage, RemoteClient } from "@/lib/supabase";
import { remote } from "@/test/fixtures";

function fakeClient(initial: AuthStage) {
  let stage = initial;
  const client: RemoteClient = {
    stage: vi.fn(async () => stage),
    signIn: vi.fn(async (email: string, password: string) => {
      if (password !== "right") throw new Error("Invalid login credentials");
      void email;
      stage = { kind: "mfa_challenge", factorId: "f1" };
    }),
    verify: vi.fn(async (_factorId: string, code: string) => {
      if (code !== "123456") throw new Error("Invalid TOTP code entered");
      stage = { kind: "ready" };
    }),
    enroll: vi.fn(async () => ({ factorId: "f-new", qrCode: "data:image/svg+xml;utf8,<svg/>", secret: "SECRETKEY" })),
    signOut: vi.fn(async () => {
      stage = { kind: "signed_out" };
    }),
    latest: vi.fn(async () => remote),
    onAuthChange: vi.fn(() => () => {}),
  };
  return client;
}

describe("SupabaseGate", () => {
  it("requires password then TOTP before reading snapshots", async () => {
    const user = userEvent.setup();
    const client = fakeClient({ kind: "signed_out" });
    render(<SupabaseGate client={client} />);

    await user.type(await screen.findByLabelText("Email"), "owner@example.com");
    await user.type(screen.getByLabelText("Password"), "right");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(client.signIn).toHaveBeenCalledWith("owner@example.com", "right");

    const code = await screen.findByLabelText("Authentication code");
    expect(client.latest).not.toHaveBeenCalled();
    await user.type(code, "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(client.verify).toHaveBeenCalledWith("f1", "123456");

    expect(await screen.findByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
    expect(client.latest).toHaveBeenCalled();
  });

  it("shows sign-in errors and stays on the form", async () => {
    const user = userEvent.setup();
    render(<SupabaseGate client={fakeClient({ kind: "signed_out" })} />);
    await user.type(await screen.findByLabelText("Email"), "owner@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid login credentials");
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("rejects a wrong code", async () => {
    const user = userEvent.setup();
    const client = fakeClient({ kind: "mfa_challenge", factorId: "f1" });
    render(<SupabaseGate client={client} />);
    await user.type(await screen.findByLabelText("Authentication code"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid TOTP code");
    expect(client.latest).not.toHaveBeenCalled();
  });

  it("enrolls an authenticator when none is set up", async () => {
    const user = userEvent.setup();
    const client = fakeClient({ kind: "mfa_enroll" });
    render(<SupabaseGate client={client} />);
    await user.click(await screen.findByRole("button", { name: "Set up authenticator" }));
    expect(await screen.findByAltText("QR code for your authenticator app")).toBeInTheDocument();
    expect(screen.getByText("SECRETKEY")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(client.verify).toHaveBeenCalledWith("f-new", "123456");
    expect(await screen.findByRole("heading", { name: "Something is degraded" })).toBeInTheDocument();
  });

  it("signs out from the dashboard", async () => {
    const user = userEvent.setup();
    const client = fakeClient({ kind: "ready" });
    render(<SupabaseGate client={client} />);
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(client.signOut).toHaveBeenCalled();
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
  });
});
