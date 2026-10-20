import { useEffect, useState } from "react";
import { Dashboard } from "@/Dashboard";
import { SupabaseGate } from "@/SupabaseGate";
import { TriangleAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { loadConfig } from "@/lib/config";
import { loadLocal } from "@/lib/local";
import { createRemoteClient, type RemoteClient } from "@/lib/supabase";

type State =
  | { kind: "loading" }
  | { kind: "local" }
  | { kind: "supabase"; client: RemoteClient }
  | { kind: "error"; message: string };

export interface AppProps {
  /** Injected in tests. */
  makeRemote?: (url: string, anonKey: string) => Promise<RemoteClient>;
}

export function App({ makeRemote = createRemoteClient }: AppProps) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const cfg = await loadConfig();
        const next: State =
          cfg.source === "local"
            ? { kind: "local" }
            : { kind: "supabase", client: await makeRemote(cfg.url, cfg.anon_key) };
        if (alive) setState(next);
      } catch (e) {
        if (alive) setState({ kind: "error", message: e instanceof Error ? e.message : String(e) });
      }
    })();
    return () => {
      alive = false;
    };
  }, [makeRemote]);

  switch (state.kind) {
    case "loading":
      return null;
    case "local":
      return <Dashboard load={loadLocal} />;
    case "supabase":
      return <SupabaseGate client={state.client} />;
    case "error":
      return (
        <div className="mx-auto max-w-md px-4 pt-[15vh]">
          <Alert variant="bad">
            <TriangleAlert />
            <AlertTitle>The dashboard couldn't start</AlertTitle>
            <AlertDescription>{state.message}</AlertDescription>
          </Alert>
        </div>
      );
  }
}
