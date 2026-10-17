#!/usr/bin/env node
// One-off owner step: enroll the dashboard owner's TOTP factor, outside the dashboard.
//
// The dashboard (DB-2) deliberately can't enroll a factor, so a stolen password can't add its
// own. Run this once from the owner's own machine, after creating the one Supabase user:
//
//   cd dashboard && npm ci && cd ..
//   node supabase/enroll_mfa.mjs --url https://<ref>.supabase.co --anon-key <anon key> --email <you>
//
// It signs in with the password (asked, hidden), refuses if a verified TOTP factor already
// exists, clears unverified leftovers, enrolls a new one, shows the otpauth URI (and a QR code
// when `qrencode` is installed, otherwise an SVG file only you can read, deleted afterwards),
// then verifies a code from your authenticator app. On success it prints the user id to put in
// relay_private.config.owner_id and signs out. Nothing is stored. The TOTP secret is printed
// only here, on your terminal: clear the scrollback when done.
//
// supabase-js comes from dashboard/node_modules, so this needs no install of its own.

import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import readline from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

function usage(code) {
  console.log(
    "usage: node supabase/enroll_mfa.mjs --url https://<ref>.supabase.co --anon-key <key> --email <email>\n" +
      "       (or SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_EMAIL in the environment)",
  );
  process.exit(code);
}

function parseArgs(argv) {
  const opts = {
    url: process.env.SUPABASE_URL,
    anonKey: process.env.SUPABASE_ANON_KEY,
    email: process.env.SUPABASE_EMAIL,
  };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "-h" || flag === "--help") usage(0);
    const value = argv[i + 1];
    if (value === undefined) usage(2);
    if (flag === "--url") opts.url = value;
    else if (flag === "--anon-key") opts.anonKey = value;
    else if (flag === "--email") opts.email = value;
    else usage(2);
    i++;
  }
  if (!opts.url || !opts.anonKey || !opts.email) usage(2);
  const u = new URL(opts.url);
  if (u.protocol !== "https:") {
    console.error("error: --url must be https");
    process.exit(2);
  }
  return opts;
}

// One readline for the whole run, with its lines queued: a second interface, or a line that
// arrives before its prompt, would otherwise be lost. EOF before an answer fails the run (so
// the finally below still cleans up).
let rl = null;
let muted = false;
let closed = false;
const lines = [];
const waiting = [];

function ask(question, { hidden = false } = {}) {
  if (rl === null) {
    rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    // Echo prompts, never what is typed while muted.
    rl._writeToOutput = (s) => {
      if (!muted) rl.output.write(s);
    };
    rl.on("line", (line) => {
      const next = waiting.shift();
      if (next) next.resolve(line);
      else lines.push(line);
    });
    rl.on("close", () => {
      closed = true;
      for (const w of waiting.splice(0)) w.reject(new Fail("input closed before an answer"));
    });
  }
  process.stdout.write(question);
  muted = hidden;
  const done = (answer) => {
    muted = false;
    if (hidden) process.stdout.write("\n");
    return answer.trim();
  };
  if (lines.length) return Promise.resolve(done(lines.shift()));
  if (closed) return Promise.reject(new Fail("input closed before an answer"));
  return new Promise((resolve, reject) => waiting.push({ resolve: (l) => resolve(done(l)), reject }));
}

async function loadSupabase() {
  const require = createRequire(join(here, "..", "dashboard", "package.json"));
  let entry;
  try {
    entry = require.resolve("@supabase/supabase-js");
  } catch {
    console.error("error: supabase-js not found; run `npm ci` in dashboard/ first");
    process.exit(2);
  }
  return import(pathToFileURL(entry).href);
}

class Fail extends Error {}

function check({ data, error }, what) {
  if (error) throw new Fail(`${what}: ${error.message}`);
  return data;
}

function showQr(totp) {
  // The URI on stdin, not in argv: argv is visible to other users in `ps`.
  const qr = spawnSync("qrencode", ["-t", "ansiutf8"], { input: totp.uri, encoding: "utf8" });
  if (qr.status === 0) {
    console.log(qr.stdout);
    return () => {};
  }
  // No qrencode: write Supabase's SVG to a private temp file and remove it afterwards.
  const dir = mkdtempSync(join(tmpdir(), "resonant-mfa-"));
  const file = join(dir, "totp.svg");
  const svg = totp.qr_code.startsWith("data:image/svg+xml;utf-8,")
    ? totp.qr_code.slice("data:image/svg+xml;utf-8,".length)
    : totp.qr_code;
  writeFileSync(file, svg, { mode: 0o600 });
  console.log(`QR code: open ${file} (deleted when this script ends)`);
  return () => rmSync(dir, { recursive: true, force: true });
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const { createClient } = await loadSupabase();
  const sb = createClient(opts.url, opts.anonKey, {
    auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false },
  });
  const password = await ask(`Password for ${opts.email}: `, { hidden: true });
  const { user } = check(
    await sb.auth.signInWithPassword({ email: opts.email, password }),
    "sign-in failed",
  );

  let cleanup = () => {};
  try {
    const factors = check(await sb.auth.mfa.listFactors(), "listing factors failed");
    const all = factors.all ?? factors.totp;
    if (all.some((f) => f.factor_type === "totp" && f.status === "verified")) {
      throw new Fail(
        "a verified TOTP factor already exists. To replace it, delete it in the Supabase " +
          "dashboard (Authentication > Users > the user > MFA factors), then run this again.",
      );
    }
    for (const f of all.filter((f) => f.status !== "verified")) {
      check(await sb.auth.mfa.unenroll({ factorId: f.id }), "removing an unverified factor failed");
    }

    const enrolled = check(
      await sb.auth.mfa.enroll({ factorType: "totp", friendlyName: "resonant dashboard" }),
      "enrolling failed",
    );
    console.log("\nAdd this to your authenticator app:\n");
    console.log(`  ${enrolled.totp.uri}\n`);
    console.log(`  secret: ${enrolled.totp.secret}\n`);
    cleanup = showQr(enrolled.totp);

    for (let tries = 1; ; tries++) {
      const code = await ask("6-digit code from the app: ");
      const { error } = await sb.auth.mfa.challengeAndVerify({ factorId: enrolled.id, code });
      if (!error) break;
      console.error(`  not accepted: ${error.message}`);
      if (tries === 3) {
        await sb.auth.mfa.unenroll({ factorId: enrolled.id });
        throw new Fail("three wrong codes; the factor was removed. Check the app's clock and retry.");
      }
    }
    const aal = check(await sb.auth.mfa.getAuthenticatorAssuranceLevel(), "checking aal failed");
    console.log(`\nTOTP enrolled and verified (session ${aal.currentLevel}).`);
    console.log(`Owner user id, for relay_private.config.owner_id: ${user.id}`);
    console.log("Clear this terminal's scrollback: it shows the TOTP secret.");
  } finally {
    cleanup();
    rl?.close();
    await sb.auth.signOut();
  }
}

main().catch((e) => {
  console.error(`error: ${e instanceof Error ? e.message : String(e)}`);
  process.exitCode = 1;
});
