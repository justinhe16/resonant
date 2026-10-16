import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from "react";
import { Dashboard } from "@/Dashboard";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { AuthStage, Enrollment, RemoteClient } from "@/lib/supabase";

type Stage = AuthStage | { kind: "loading" };

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Supabase mode: email + password, then a TOTP code. The snapshot is only read once the
 * session is at AAL2; row-level security enforces the same on the server.
 */
export function SupabaseGate({ client }: { client: RemoteClient }) {
  const [stage, setStage] = useState<Stage>({ kind: "loading" });
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setStage(await client.stage());
    } catch (e) {
      setError(message(e));
      setStage({ kind: "signed_out" });
    }
  }, [client]);

  useEffect(() => {
    // Initial load plus every auth change (sign-out elsewhere, expired refresh token).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
    return client.onAuthChange(() => void reload());
  }, [client, reload]);

  const run = async (fn: () => Promise<void>) => {
    setError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(message(e));
    }
  };

  const signOut = () => void run(() => client.signOut());
  const load = useCallback(() => client.latest(), [client]);

  switch (stage.kind) {
    case "loading":
      return <Shell>{null}</Shell>;
    case "ready":
      return <Dashboard load={load} onSignOut={signOut} />;
    case "signed_out":
      return (
        <Shell error={error}>
          <PasswordForm onSubmit={(email, pw) => run(() => client.signIn(email, pw))} />
        </Shell>
      );
    case "mfa_challenge":
      return (
        <Shell error={error}>
          <CodeForm
            hint="Enter the 6-digit code from your authenticator app."
            onSubmit={(code) => run(() => client.verify(stage.factorId, code))}
            onCancel={signOut}
          />
        </Shell>
      );
    case "mfa_enroll":
      return (
        <Shell error={error}>
          <Enroll client={client} run={run} onCancel={signOut} />
        </Shell>
      );
  }
}

function Shell({ children, error }: { children: ReactNode; error?: string | null }) {
  return (
    <div className="flex min-h-dvh items-start justify-center px-4 pt-[15vh]">
      <div className="w-full max-w-sm space-y-4">
        <div className="text-center">
          <p className="text-lg font-semibold tracking-tight">Resonant</p>
          <p className="text-sm text-muted-foreground">Status dashboard</p>
        </div>
        {error && (
          <Alert tone="bad">
            <p className="break-words">{error}</p>
          </Alert>
        )}
        {children && <Card className="p-5">{children}</Card>}
      </div>
    </div>
  );
}

function PasswordForm({ onSubmit }: { onSubmit: (email: string, password: string) => Promise<void> }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    await onSubmit(email.trim(), password);
    setBusy(false);
  };
  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      <Button type="submit" className="w-full" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}

function CodeForm({
  hint,
  onSubmit,
  onCancel,
}: {
  hint: string;
  onSubmit: (code: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    await onSubmit(code);
    setBusy(false);
    setCode("");
  };
  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="totp">Authentication code</Label>
        <p className="text-xs text-muted-foreground">{hint}</p>
        <Input
          id="totp"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          maxLength={6}
          required
          autoFocus
          className="text-center font-mono text-lg tracking-[0.4em]"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
        />
      </div>
      <Button type="submit" className="w-full" disabled={busy || code.length !== 6}>
        {busy ? "Checking…" : "Verify"}
      </Button>
      <Button variant="ghost" className="w-full" onClick={onCancel}>
        Sign out
      </Button>
    </form>
  );
}

function Enroll({
  client,
  run,
  onCancel,
}: {
  client: RemoteClient;
  run: (fn: () => Promise<void>) => Promise<void>;
  onCancel: () => void;
}) {
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  if (!enrollment) {
    return (
      <div className="space-y-4">
        <p className="text-sm">
          Two-factor authentication is required. Set up an authenticator app to continue.
        </p>
        <Button
          className="w-full"
          onClick={() =>
            void run(async () => {
              setEnrollment(await client.enroll());
            })
          }
        >
          Set up authenticator
        </Button>
        <Button variant="ghost" className="w-full" onClick={onCancel}>
          Sign out
        </Button>
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <img
        src={enrollment.qrCode}
        alt="QR code for your authenticator app"
        className="mx-auto size-44 rounded-md bg-white p-2"
      />
      <p className="text-center text-xs break-all text-muted-foreground">
        Or enter this key: <span className="font-mono text-foreground">{enrollment.secret}</span>
      </p>
      <CodeForm
        hint="Scan the code, then enter the 6 digits it shows."
        onSubmit={(code) => run(() => client.verify(enrollment.factorId, code))}
        onCancel={onCancel}
      />
    </div>
  );
}
