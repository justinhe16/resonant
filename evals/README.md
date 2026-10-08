# evals

The example sets here are synthetic. Your real sets, built from your own texts, live outside the public repo in `~/.resonant/evals/` and are passed with `--set`.

- `router.example.jsonl`: `{text, intent}` pairs for the router (ticket 1F).
- `approvals.example.jsonl`: replies to approval requests covering yes/no/ambiguous/sarcasm/unrelated/pay, with the expected decision and which path (`parser` or `classifier`) should decide (Phase 2).
