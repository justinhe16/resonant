/** What each section is, in plain words (the (i) button on every header shows these). */
export const EXPLAINERS = {
  runs: "Each run is one Claude Code session working on one ask. A repo run works in its own git worktree of your checkout in ~/code, on a branch resonant/r<n>-…; an ask with no repo gets a scratch folder. It pushes, opens the PR and watches CI with a GitHub App token for that one repo, valid an hour. A run that asks you something waits with its slot freed until you answer. Side effects are what it did outside its branch and PR: the ones it noted (did:) and the ones Resonant read in its commands (detected).",
  slots:
    "How many Claude sessions may run at once (claude.max_concurrent, 5 by default), with one kept free for on-call work; the rest queue.",
  tasks:
    "Resonant's durable to-do list: each chat turn, run, job that needs approval, reminder and watch check is a task that survives restarts. Tasks go queued → running → waiting (for an approval, your reply, a usage reset, CI, a veto, or a free slot or repo) → done, failed or cancelled. Queued tasks start by priority, then age; one cut off by a restart is queued again.",
  approvals:
    "When the gate needs your yes, Resonant texts a prompt with a code. Reads (L0) never ask. L3 acts, then tells you. L2 acts when you typed the command, but asks first when Claude or a job chose it (yes, or yes always to stop asking for that tool). L1, used for destructive actions and every payment, always asks and only yes <code> approves. While the kill switch is on, everything above L0 asks. An approval expires after 30 minutes by default and gets one reminder halfway. The code is shown in iMessage, not on the remote page.",
  questions:
    "A worker asks with resonant ask. You get 'R4 asks: …' and your reply goes back by code, by option or by what it says. The run waits, its slot freed, until you answer.",
  claude:
    "Your harder texts go to one long-lived Claude Code session, the Claude brain, one turn at a time. It starts on first use, resumes after a daemon restart and starts fresh after 50 turns or 12 hours by default. It has Claude Code's shell, file and web tools: reads on a pre-approved list run straight away; every other call goes through Resonant's gate, and changes wait for your yes. Every tool call is recorded and listed here. 'Worker runner' says whether Claude workers can start, and what's missing if not.",
  extensions:
    "Add-on tool packs. Each one declares its tools in a manifest (resonant.yaml) that you review at install, pinned to a commit. A tool is either a command the extension ships (stdlib-only Python, run as a separate process) or a tool of an MCP server, local or hosted, whose tool list is pinned at install; anything unlisted is never called. Each tool's effect and level come from the manifest, never the model: reads run without asking, writes ask first when Claude or a job chose them, and payments always need your code. Calls go through Resonant's gate, Keychain secrets and audit like built-in tools. Jobs run on cron inside the daemon.",
  pulse:
    "What Resonant is keeping an eye on: items reported by extensions and by Resonant itself (worker progress, worker questions, stalled runs and PRs), each with an urgency. Urgent items text you right away, at any hour. Normal ones wait for the hourly pulse: core/pulse, a job inside the daemon that runs at the top of every hour (cron 0 * * * *, not launchd). It sends at most one short message, stays quiet from 00:00 to 06:00 by default and during the briefing's hour, and lists only what you haven't heard. Low items, and everything still open, go into the daily briefing (08:00 by default). Each item is told once per change. 'Untold' counts what you haven't heard yet, muted extensions included.",
  jobs: "Resonant's own scheduled jobs, run by the daemon's scheduler. briefing: daily at 08:00 by default. pulse: hourly. watches: every 5 minutes. remind: every minute (approval reminders). mcp: every minute (MCP tool re-checks). backup: 03:30 by default. prune: 30 minutes after the backup. run-cleanup: 04:30 (old run folders and worktrees). They are trusted core code and run without the gate. While Resonant is paused, due runs are skipped (backup still runs).",
  relay:
    "The Mini pushes this snapshot to Supabase every minute; nothing connects into the Mini. A small Fly app serves this page and checks the newest snapshot every minute. If none arrives for 5 minutes, a health check is failing, or it can't read Supabase, it alerts you on ntfy or a webhook, and again on recovery. Reading this page needs your password and TOTP.",
  backups:
    "core/backup copies the store every day at 03:30 (by default) with SQLite's online backup, checks it, and keeps 7 daily and 4 Sunday copies in ~/.resonant/backups. The backup check fails after 36 hours without a good copy.",
  status:
    "Each row is a health check: the model, iMessage, the loop, the store, backups, each extension's own check and the relay. The overall state is the worst one.",
  rules:
    "Trust rules are your standing answers for workers: before anything net new a worker checks, and gets ok or a question for you. Teach them in chat or with /rules. Repos is the list of repos workers work in.",
} as const;

export type ExplainerKey = keyof typeof EXPLAINERS;
