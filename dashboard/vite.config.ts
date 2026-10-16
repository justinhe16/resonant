import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vitest/config";

// Dev only: `DASHBOARD_FIXTURE=local npm run dev` serves a fixture at /api/dashboard instead
// of proxying to the daemon, so the UI can be worked on without a running Resonant.
function fixtureApi(name: string): Plugin {
  return {
    name: "resonant-fixture-api",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/api/dashboard", (_req, res) => {
        const raw = readFileSync(resolve(root, `src/test/fixtures/${name}.json`), "utf8");
        // Shift every timestamp so the fixture looks as if it was taken just now.
        const takenAt = Date.parse((JSON.parse(raw) as { taken_at: string }).taken_at);
        const shift = Date.now() - takenAt;
        const body = raw.replace(/"(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d))"/g, (_m, iso: string) =>
          JSON.stringify(new Date(Date.parse(iso) + shift).toISOString()),
        );
        res.setHeader("content-type", "application/json");
        res.end(body);
      });
    },
  };
}

const root = fileURLToPath(new URL(".", import.meta.url));
const fixture = process.env.DASHBOARD_FIXTURE;

export default defineConfig({
  // Relative asset paths: the same build is served at / by the daemon and by the Fly relay.
  base: "./",
  plugins: [react(), tailwindcss(), ...(fixture ? [fixtureApi(fixture)] : [])],
  resolve: { alias: { "@": resolve(root, "src") } },
  server: fixture ? {} : { proxy: { "/api": "http://127.0.0.1:7777" } },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // Deterministic output: content hashes only, no build time or absolute paths.
    reportCompressedSize: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
