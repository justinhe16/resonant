# example extension

A minimal extension showing the `resonant.yaml` shape: one scheduled job, a read tool, a write tool, notifications, and a health check. Phase 3 adds `resonant ext add <path>`, which validates the manifest, shows a diff of its jobs, tools, effects and secrets, and asks for owner approval.

```sh
./cli.py time --tz America/Los_Angeles   # {"now": "...", "tz": "America/Los_Angeles"}
```
