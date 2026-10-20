# Follow-ups: your to-dos

Only the things that need you, the owner, at the Mini or on your phone. Planned code work is in Linear (Resonant project, epic [PER-257](https://linear.app/doriapp/issue/PER-257)). Known limits are in [`limits.md`](limits.md). Claude never enters a secret, and no secret goes in a chat or a text.

## Now

- [ ] **The github extension's token can't merge.** A merge by text failed on 2026-10-20 with "the token can't do this: it needs Contents: write and Pull requests: write". Give the fine-grained token those two permissions on the repos you want merged by text (or make a new one and `resonant secret set` it yourself). Workers merge with the GitHub App token, so they're unaffected.
- [ ] **`resonant ext update github`.** It brings in `recent_prs` (BR-9) and stops the all-zero `[github] 0 open PRs…` line (BF-1). Wait for the prompt before typing the name.
- [ ] **Phone tests:** [PER-252](https://linear.app/doriapp/issue/PER-252). After a Claude answer, "remind me at 5" runs without a yes (PER-245). The Claude brain (follow-up, two texts back to back, restart, `/kill`), a Linear batch, "dont ask for perms for linear tickets", and the worker tests from go-live step 14 (spawn, a question and your reply, `yes always`, a merge rule, a scratch run, cancel, `/kill`).
- [ ] **Mycelium by text:** [PER-253](https://linear.app/doriapp/issue/PER-253). Set it up from scratch by texting Resonant. You put the Plaid keys in its `.env.local` yourself, then run `resonant ext add path:…` for its extension.

## Ongoing

- **Branch protection as repos are added.** On GitHub, protect each repo you care about: require a PR and CI on the base branch, and keep the app out of the bypass list. Say "no CI edits in <repo>" where Actions secrets matter.
- **Review the drafted eval rows** (PER-248). Once Claude has run `resonant eval draft --from-history` with your OK, open `~/.resonant/evals/task.jsonl`; for each row fix `expect`, add the `facts` a right answer must contain, read the `note` (what can't be replayed) and set `"review": false`. Then `resonant eval task --set owner --repeat 3`. Rows still marked `review: true` are listed, not graded. How it drafts: [`evals/README.md`](../evals/README.md#drafting-from-your-texts).

## Optional

- **`backup.extra_dir`:** a second daily backup copy on another disk or in iCloud Drive. Use a folder of its own, because old copies there are pruned.
- **`brain.claude_session.remote_control: true`:** shows a Remote Control link for the Claude brain on `/status`.
- **A daily Claude cap:** off by default (`brain.ask_claude.daily_cap: 0`). The runaway guard is 60 messages an hour.
- **PRs to other people's public repos:** a classic token with only `public_repo`, then `resonant secret set runner/github_public_token`, then a first fork run with Claude watching.
- **`brain.taint_hops`:** how many turns stay careful after Resonant reads outside text (the web, a file, a log, a Claude answer). Default 3. Notes, reminders and facts never ask because of it; other changes do.
- **Voice notes:** install whisper.cpp and tell Claude the path (`brain.whisper_path`).
- **An external uptime check** on https://resonant-relay-jhe.fly.dev/healthz (it returns 503 when the checker stalls). A Fly outage is otherwise silent.
- **The ntfy alert channel:** the default is a public `ntfy.sh` topic, and the long random topic is the password. If missed pushes or that bother you, move to a protected topic (`NTFY_TOKEN`), your own ntfy server (`NTFY_URL`) or a webhook (`WEBHOOK_URL`) into SMS or email. No code change.

## Done

The github and Linear extensions are installed. The Claude login works, and the GitHub App and Resonant's own token are set. Daily backups run at 03:30. The old-runner leftovers are gone. The relay is live (PER-251, 2026-10-20): remote dashboard at https://resonant-relay-jhe.fly.dev/ and ntfy alerts if the Mini goes quiet.
