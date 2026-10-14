# example extension

A minimal extension showing the `resonant.yaml` shape: a description and router examples, one scheduled read job, a read tool, a write tool, notifications, and a health check. It is stdlib only, and `write_note` appends to `notes.txt` in `$RESONANT_EXT_DATA`.

The `morning-ping` job (weekdays 08:00) emits one `ping` event per due time, which Resonant texts as `[example] ping`. The first run after install is a silent baseline, so the first text comes on the second run.

## Reports (the pulse)

**This is a demo and it is noisy:** the `incident` job runs every 5 minutes and the fake `pnl` changes nearly every hour, so it adds an `[example] PnL ...` line to hourly pulses and the briefing. Run `resonant ext remove example` after trying it.

The manifest's `report: {max_urgency: urgent}` lets the example's jobs call `report()` from `emit.py` (a copy of the SDK helper, next to `cli.py`; tests check the copy stays identical). Resonant keeps one item per key and tells you again only when its `state` changes.

- `pnl` (hourly at :55, L0) reports `example/pnl/today` at normal urgency, e.g. `[example] PnL -$1.8k today`. The title has the exact figure, but `state` is only the figure rounded to $500, so it counts as news only when the bucket moves. It shows in the next hourly pulse and the 08:00 briefing's Updates section.
- `incident` demos the urgent path. Only job runs persist events (a tool's events are dropped), so the design is a job plus a tool that writes its input. `/example incident failing` (the `incident` tool, L3: acts, then reports) writes `incident` in the data dir. The `incident` job (every 5 minutes, or `/ext run example/incident` to not wait) reads it and reports `example/incident/demo` as urgent, which is texted at once as `[example] 🔴 Demo service failing: checkout 5xx`. Running the job again with the same state says nothing more. `/example incident resolved` makes the next run report `clear=True`, texted as `[example] ✓ Demo service recovered`, once. The file is never deleted and the job reports it every run; repeats are silent. `resolved` without an open `failing` does nothing and replies `no incident open`, so a stray resolve never texts.

A job's first run after install is a silent baseline, so run the job once (`/ext run example/incident`) before the first `failing`.

Install it on the Mini with `resonant ext add repo:example`, which shows the review screen and asks you to type `example`. Remove it with `resonant ext remove example`.

```sh
./cli.py time --tz America/Los_Angeles   # {"now": "...", "tz": "America/Los_Angeles"}
```
