import { TriangleAlert } from "lucide-react";
import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from "react";
import { Dashboard } from "@/Dashboard";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { AuthStage, RemoteClient } from "@/lib/supabase";

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
      // A network blip shouldn't sign the owner out: keep the current stage, show why.
      setError(message(e));
      setStage((s) => (s.kind === "loading" ? { kind: "signed_out" } : s));
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
    case "mfa_missing":
      return (
        <Shell error={error}>
          <div className="space-y-4">
            <p className="text-sm">
              Two-factor authentication isn't set up for this account, so the dashboard stays locked.
              Enrol an authenticator as the go-live checklist describes, then sign in again.
            </p>
            <Button type="button" variant="outline" size="lg" className="w-full" onClick={signOut}>
              Sign out
            </Button>
          </div>
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
          <Alert variant="bad">
            <TriangleAlert />
            <AlertDescription className="break-words text-foreground">{error}</AlertDescription>
          </Alert>
        )}
        {children && (
          <Card className="[--card-spacing:--spacing(6)]">
            <CardContent>{children}</CardContent>
          </Card>
        )}
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
      <Button type="submit" size="lg" className="w-full" disabled={busy}>
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
      <Button type="submit" size="lg" className="w-full" disabled={busy || code.length !== 6}>
        {busy ? "Checking…" : "Verify"}
      </Button>
      <Button type="button" variant="ghost" size="lg" className="w-full" onClick={onCancel}>
        Sign out
      </Button>
    </form>
  );
}
