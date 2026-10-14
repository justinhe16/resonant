# example extension

A minimal extension showing the `resonant.yaml` shape: a description and router examples, one scheduled read job, a read tool, a write tool, notifications, and a health check. It is stdlib only, and `write_note` appends to `notes.txt` in `$RESONANT_EXT_DATA`.

The `morning-ping` job (weekdays 08:00) emits one `ping` event per due time, which Resonant texts as `[example] ping`. The first run after install is a silent baseline, so the first text comes on the second run.

Install it on the Mini with `resonant ext add repo:example`, which shows the review screen and asks you to type `example`. Remove it with `resonant ext remove example`.

```sh
./cli.py time --tz America/Los_Angeles   # {"now": "...", "tz": "America/Los_Angeles"}
```
