# Brain plan (Phase 6a): a general agent loop on the local model

Status: **planned** (2026-10-18). Built with robot-surf in the waves of §5.

Texting Resonant should feel like texting Claude: general questions and conversation, looking around the Mini read-only, Resonant's own tools (still gated), extensions, other programs, Claude workers with Remote Control. Today every non-slash text is one intent label (`router/labeler.py`) → that intent's fixed toolset (`router/intents.py` `INTENT_TOOLS`) → `runners/local.py` capped at 3 model calls and 3 tool calls → a number guard that swaps the reply for a template. History is 5 earlier messages as user turns, nothing reads files or logs (`tools/builtin.py`), and nothing escalates (`escalate=True` is written and never read). "Where is mycelium?" had no route, so it failed.

The local qwen3-30B stays the brain. The harness changes, modeled on Claude Code's: one loop, an environment briefing, a memory index, general read tools, tools loaded on demand, a subagent for hard questions.

## 1. Owner decisions

- **Use cases:** questions about the Mini; personal assistant (calendar, email, reminders, notes); research and the web; named programs; photos, screenshots and voice; links; long-running asks ("watch X, tell me when Y"); other people with per-person grants (today's principals model). Group chats later.
- **Fast first, escalate:** the local loop answers; when it's stuck or the ask needs the web, email, calendar or an image, it hands it to a read-only Claude session and sends "looking into it…" first.
- **Email and calendar:** read and draft. Sending an email or creating, changing or answering an event asks first.
- **Programs:** each one a small extension with start, stop and status tools (e.g. trade-jev), not a generic runner.
- **Memory:** recent turns plus a small store of long-term facts.
- **Read scope:** `~/code`, `~/resonant`, Resonant's state, logs and docs, run transcripts. Never `~/Documents`, Mail, Messages, the Keychain, `~/.ssh`, `.env`-like files.
- **Ambiguity:** best guess for reads (say what it assumed), ask for writes.
- **Style:** a terse coworker.
- **Proactive:** yes, sparingly: a few notices a day, through the pulse.
- Everything that exists stays: fast path, gate, approvals, standing allows, kill switch, audit, extensions, MCP, workers and Remote Control, trust rules, pulse, dashboard.

## 2. Decisions

- **D1. One loop.** `runners/brain.py` handles every non-fast-path chat task. The fast path is unchanged (slash commands, `status`, `ping`, approval replies, the relay's `R4 …`, `/kill`). `brain.mode: intents | loop`, default `intents` until the evals pass (§4), then `loop` (BR-8). In loop mode the labeler is skipped (two prefixes would evict each other's KV cache with Ollama's one slot). A `ModelOutputError` in the loop falls back to the intents path for that turn.
- **D2. Prompt order for the cache.** Qwen's template puts the system text before the tool definitions in one turn, so anything that changes per turn goes after them: fixed system prompt (role, style, rules) → core tool schemas → the deferred-tool catalog (one line each; changes only on extension install) → facts index → rolling history (append-only, older part summarised) → one wrapped user turn with the **briefing** (time, repos and paths, runs with state and Remote Control links, extensions, open asks, health; from `dashboard/snapshot.py build_snapshot` local tier and `status.collect_status`; ≤1.5k tokens) and any "since we last talked" notes → the current message. A CI test pins the prefix hash.
- **D3. History as real turns.** The last ~10 exchanges as user/assistant turns (not `<prior_reply>` user turns), each capped; older ones folded into a running summary per principal. Tool results inside a task are still replayed as untrusted user turns.
- **D4. Tools: core plus deferred.** About 14 core tools always loaded: the read tools (D5), `remember`/`forget`, `ask_claude`, `load_tools`, notes and reminders add/list, `list_runs`, `run_activity`, `start_run`, `system_status`. Every other builtin, extension and MCP tool the principal may use is listed by name and one line; `load_tools(names)` adds up to 8 for the rest of the task. Per-principal filtering happens before anything is listed.
- **D5. Read tools** (`tools/files.py`, L0): `read_file`, `list_dir`, `search` (ripgrep), `tail_log`, `read_transcript`. Real path resolved, then the deny list, then the allowed roots (§1); symlinks out of a root and `..` refused; byte and line caps; output redacted (`redact.py`) and wrapped as untrusted data. Owner-only by default; members only through a grant.
- **D6. Taint.** A task that has read untrusted content (files, logs, transcripts, web, email, extension results, `ask_claude` output) is tainted; Resonant's own state reads and the facts index don't taint. A tainted task's model-chosen calls use a new origin `tainted`: the gate asks for L2 and L3 (standing allows don't apply, since they require `inferred`). The approval summary is code-built (`gate/summary.py`), so injected text can't reword what the owner approves. This is the one gate change in this plan.
- **D7. Limits.** At most 8 model rounds per task, reads free inside them; side effects ≤3 per task and one approval stops the task (both unchanged). Replies up to ~1500 chars. The hard number guard stays on the intents path only; in loop mode it logs and counts in the evals, and the prompt says to state only what it read. If the loop passes ~6 s, a short progress text goes out.
- **D8. `ask_claude`** runs as its own task (`runner="ask"`), not inside the chat step: "looking into it…" goes out, then the answer when it's back. `claude -p --restricted --tools Read,Grep,Glob,WebSearch,WebFetch --permission-mode dontAsk` with an **allowlist** of connector read (and draft, if the connector has one) tools, write tools never allowed (Gmail send, Calendar create, update, delete, respond), `Read` denied on the deny list, `--add-dir` for the read roots, no session persistence, `--max-turns 15`, 120 s timeout. The real connector tool names are read from the `init` event; it refuses to start when an expected read tool is missing. Concurrency 1 (`brain.ask_claude.concurrency`), a daily cap (~30), separate from worker slots; a usage limit backs off and tells the owner (`runners/claude.py` `usage_limit`). Sends and event changes come back as a code-built proposal and go through a gated tool. **Unverified:** whether `--restricted` keeps claude.ai connectors; BR-5 probes it first. Gmail and Drive need the owner to finish connecting them in claude.ai.
- **D9. Facts.** `~/.resonant/brain/facts/<slug>.md`, one fact each, with an index rendered into the prompt. `remember` and `forget` are L3 (do, then report), owner only.
- **D10. Media, links, watches, proactive.** Image and voice attachments from chat.db go to `ask_claude` (voice to local whisper when present). A link is summarised through `ask_claude` (WebFetch). "Watch X, tell me when Y" becomes a proposed job on an extension or a `watch` builtin, and asks first. Brain-authored notices go through the pulse with a daily cap on top of the hourly one.

## 3. Out of scope

Group chats; a generic "run any program" tool (programs are extensions); writes by `ask_claude`; replacing qwen; new sandboxes for workers (they stay unguarded).

## 4. Evals and verification

- `evals/task_eval.py` and `evals/task.example.jsonl`: at least 40 real texts across the use cases, each scored on the right tool, a grounded answer (expected facts), no unapproved write when tainted, and latency; run in both modes. `brain.mode: loop` becomes the default when it scores ≥85% graded correct, escalations counted apart.
- Latency: fast path p50 ≤1 s; a simple loop read ~5 s, a three-step task <15 s; "looking into it…" within 2 s of escalating.
- `ScriptedModel` (`e2e/fakes.py`) gains a scripted mode (tool calls and a final reply per test); the label mode stays.
- On the Mini, by text: "where is mycelium", "what's R4 doing", "and where's that?", "what's on my calendar tomorrow", "draft a reply to the last email from X" (draft only), "look up X", a screenshot, "start trade-jev" (asks).

## 5. Tickets and waves

1. Wave 1, in parallel:
   - **BR-2 Read tools** (D5).
   - **BR-4 Context and memory**: briefing builder, rolling history and summary, "since we last talked" notes, facts store with `remember`/`forget`, the prefix-hash test (D2, D3, D9).
   - **BR-5 `ask_claude` and taint**: the `ask` runner, the argv, connector discovery, limits, the progress text; the `tainted` origin in `gate/types.py` and `gate/policy.py` (D6, D8).
2. Wave 2: **BR-1 Brain loop**: `runners/brain.py`, `brain.mode`, core and deferred tools with `load_tools`, limits, taint tracking, fallback, the scripted fake (D1, D4, D7).
3. Wave 3, in parallel: **BR-6 Task evals** (§4); **BR-7 Media, links, watches, proactive** (D10).
4. Wave 4: **BR-8 Cut-over**: default `loop` once the evals pass, docs (architecture, interfaces, go-live), drop unused intent few-shots, the architecture artifact.
